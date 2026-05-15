from datetime import datetime, timezone
import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload

from app.agent import run_agent
from app.database import get_db
from app.logging_config import summarize_exception
from app.models import Chat, ChatMemorySummary, Message
from app.schemas import AssistantResponse, MessageCreate

router = APIRouter(prefix="/chats/{chat_id}/messages", tags=["messages"])
logger = logging.getLogger(__name__)

ACTIVE_WINDOW_TURNS = 6
SUMMARY_BLOCK_TURNS = 4
MAX_MEMORY_CHARS = 9000


@router.post("", response_model=AssistantResponse)
def send_message(chat_id: int, payload: MessageCreate, db: Session = Depends(get_db)):
    """Guarda el mensaje, ejecuta el agente y persiste la respuesta."""

    logger.info(
        "POST message recibido chat_id=%s content_length=%s",
        chat_id,
        len(payload.content),
    )
    chat = (
        db.query(Chat)
        .options(selectinload(Chat.messages))
        .filter(Chat.id == chat_id)
        .first()
    )
    if not chat:
        logger.warning("Chat no encontrado chat_id=%s", chat_id)
        raise HTTPException(status_code=404, detail="Chat no encontrado")

    user_message = Message(chat_id=chat.id, role="user", content=payload.content)
    db.add(user_message)
    chat.updated_at = datetime.now(timezone.utc)
    if chat.title == "Nuevo chat":
        chat.title = payload.content[:80]
    db.commit()
    db.refresh(user_message)
    logger.info("Mensaje de usuario guardado chat_id=%s message_id=%s", chat.id, user_message.id)

    db.refresh(chat)
    previous_messages = (
        db.query(Message)
        .filter(Message.chat_id == chat.id, Message.id != user_message.id)
        .order_by(Message.created_at.asc(), Message.id.asc())
        .all()
    )
    history = _build_memory_context(
        db=db,
        chat_id=chat.id,
        messages=previous_messages,
        current_question=payload.content,
    )
    logger.info(
        "Historial cargado chat_id=%s previous_messages=%s history_length=%s",
        chat.id,
        len(previous_messages),
        len(history),
    )

    try:
        logger.info("Ejecutando agente chat_id=%s user_message_id=%s", chat.id, user_message.id)
        agent_result = run_agent(question=payload.content, history=history)
    except Exception as exc:
        logger.error(
            "Error ejecutando el agente chat_id=%s error_type=%s error=%s",
            chat.id,
            type(exc).__name__,
            summarize_exception(exc),
        )
        raise HTTPException(
            status_code=502,
            detail=(
                "No se pudo ejecutar el agente. Verifica que la API key del proveedor LLM "
                "este activa y que Neo4j este disponible."
            ),
        ) from exc
    logger.info(
        "Agente finalizado chat_id=%s out_of_domain=%s cypher_present=%s",
        chat.id,
        agent_result["out_of_domain"],
        bool(agent_result["cypher_query"]),
    )

    assistant_message = Message(
        chat_id=chat.id,
        role="assistant",
        content=agent_result["answer"],
        cypher_query=agent_result["cypher_query"],
    )
    db.add(assistant_message)
    db.commit()
    db.refresh(assistant_message)
    db.refresh(user_message)
    logger.info(
        "Respuesta de asistente guardada chat_id=%s message_id=%s",
        chat.id,
        assistant_message.id,
    )

    return {
        "user_message": user_message,
        "assistant_message": assistant_message,
    }


def _build_memory_context(
    db: Session,
    chat_id: int,
    messages: list[Message],
    current_question: str,
) -> str:
    """Construye memoria activa con ventana reciente y resumenes persistidos."""

    if not messages:
        return "Sin historial previo."

    active_message_count = ACTIVE_WINDOW_TURNS * 2
    active_messages = messages[-active_message_count:]
    summaries = _ensure_memory_summaries(
        db=db,
        chat_id=chat_id,
        messages=messages[:-active_message_count],
    )
    selected_summaries = _select_relevant_summaries(summaries, current_question)

    lines = [
        "MEMORIA DE CONVERSACION",
        "Usa esta memoria para resolver referencias implicitas y mantener coherencia entre turnos.",
        "La ventana activa contiene los mensajes recientes completos; los resumenes condensan bloques antiguos persistidos.",
        "",
        "RESUMENES RELEVANTES DE BLOQUES ANTERIORES:",
    ]
    if selected_summaries:
        for summary in selected_summaries:
            lines.append(
                f"[Bloque {summary.block_index} | mensajes {summary.start_message_id}-{summary.end_message_id}] "
                f"{summary.summary}"
            )
    else:
        lines.append("Sin resumenes anteriores relevantes.")

    lines.extend(["", "VENTANA ACTIVA RECIENTE:"])
    lines.extend(_format_messages(active_messages))

    memory = "\n".join(lines)
    if len(memory) > MAX_MEMORY_CHARS:
        memory = _truncate_memory(memory, active_messages, selected_summaries)
    logger.info(
        "Memoria construida chat_id=%s summaries=%s active_messages=%s memory_length=%s",
        chat_id,
        len(selected_summaries),
        len(active_messages),
        len(memory),
    )
    return memory


def _ensure_memory_summaries(
    db: Session,
    chat_id: int,
    messages: list[Message],
) -> list[ChatMemorySummary]:
    """Crea o actualiza resumenes por bloques para mensajes fuera de la ventana activa."""

    block_size = SUMMARY_BLOCK_TURNS * 2
    existing = {
        summary.block_index: summary
        for summary in db.query(ChatMemorySummary)
        .filter(ChatMemorySummary.chat_id == chat_id)
        .order_by(ChatMemorySummary.block_index.asc())
        .all()
    }

    summaries: list[ChatMemorySummary] = []
    changed = False
    for block_index, start in enumerate(range(0, len(messages), block_size)):
        block = messages[start : start + block_size]
        if not block:
            continue

        start_message_id = block[0].id
        end_message_id = block[-1].id
        summary_text = _summarize_message_block(block)
        summary = existing.get(block_index)

        if summary is None:
            summary = ChatMemorySummary(
                chat_id=chat_id,
                block_index=block_index,
                start_message_id=start_message_id,
                end_message_id=end_message_id,
                summary=summary_text,
            )
            db.add(summary)
            changed = True
        elif (
            summary.start_message_id != start_message_id
            or summary.end_message_id != end_message_id
            or summary.summary != summary_text
        ):
            summary.start_message_id = start_message_id
            summary.end_message_id = end_message_id
            summary.summary = summary_text
            changed = True

        summaries.append(summary)

    if changed:
        db.commit()
        for summary in summaries:
            db.refresh(summary)

    return summaries


def _summarize_message_block(messages: list[Message]) -> str:
    """Resume un bloque preservando entidades, resultados y Cypher utiles."""

    user_questions: list[str] = []
    assistant_facts: list[str] = []
    cypher_queries: list[str] = []
    publication_ids: set[str] = set()

    for message in messages:
        publication_ids.update(re.findall(r"\bPUB\d+\b", message.content, flags=re.IGNORECASE))
        if message.cypher_query:
            publication_ids.update(re.findall(r"\bPUB\d+\b", message.cypher_query, flags=re.IGNORECASE))
            cypher_queries.append(_compact_text(message.cypher_query, 260))

        if message.role == "user":
            user_questions.append(_compact_text(message.content, 180))
        elif message.role == "assistant":
            assistant_facts.append(_compact_text(message.content, 260))

    parts: list[str] = []
    if user_questions:
        parts.append("Preguntas: " + " | ".join(user_questions[-SUMMARY_BLOCK_TURNS:]))
    if assistant_facts:
        parts.append("Resultados: " + " | ".join(assistant_facts[-SUMMARY_BLOCK_TURNS:]))
    if publication_ids:
        normalized_ids = sorted({publication_id.upper() for publication_id in publication_ids})
        parts.append("Publicaciones referenciables: " + ", ".join(normalized_ids))
    if cypher_queries:
        parts.append("Cypher utiles: " + " | ".join(cypher_queries[-2:]))

    return " ".join(parts) if parts else "Bloque sin contenido referenciable."


def _select_relevant_summaries(
    summaries: list[ChatMemorySummary],
    current_question: str,
    max_summaries: int = 6,
) -> list[ChatMemorySummary]:
    """Selecciona resumenes recientes y lexicalmente cercanos a la pregunta actual."""

    if not summaries:
        return []

    question_terms = _extract_terms(current_question)
    scored: list[tuple[int, int, ChatMemorySummary]] = []
    total = len(summaries)
    for position, summary in enumerate(summaries):
        summary_terms = _extract_terms(summary.summary)
        overlap = len(question_terms & summary_terms)
        recency = total - position
        scored.append((overlap, recency, summary))

    selected = sorted(scored, key=lambda item: (item[0], item[1]), reverse=True)[:max_summaries]
    return [summary for _, _, summary in sorted(selected, key=lambda item: item[2].block_index)]


def _extract_terms(text: str) -> set[str]:
    """Extrae terminos normalizados para relevancia sin listas fijas de palabras."""

    return {term.casefold() for term in re.findall(r"\w{4,}", text)}


def _format_messages(messages: list[Message]) -> list[str]:
    """Convierte mensajes a texto preservando Cypher ejecutado."""

    lines: list[str] = []
    for message in messages:
        role = "Usuario" if message.role == "user" else "Asistente"
        lines.append(f"{role}: {message.content}")
        if message.role == "assistant" and message.cypher_query:
            lines.append(f"Cypher usado por el asistente: {message.cypher_query}")
    return lines


def _truncate_memory(
    memory: str,
    active_messages: list[Message],
    summaries: list[ChatMemorySummary],
) -> str:
    """Recorta memoria conservando ventana activa y resumenes mas recientes."""

    active_section = "\n".join(["VENTANA ACTIVA RECIENTE:", *_format_messages(active_messages)])
    selected_summaries: list[ChatMemorySummary] = []
    for summary in reversed(summaries):
        candidate_summaries = [summary, *selected_summaries]
        summary_text = "\n".join(
            f"[Bloque {item.block_index} | mensajes {item.start_message_id}-{item.end_message_id}] {item.summary}"
            for item in candidate_summaries
        )
        candidate = "\n".join(
            [
                "MEMORIA DE CONVERSACION",
                "Resumenes anteriores seleccionados por relevancia y recencia:",
                summary_text,
                "",
                active_section,
            ]
        )
        if len(candidate) <= MAX_MEMORY_CHARS:
            selected_summaries = candidate_summaries

    summary_lines = [
        f"[Bloque {summary.block_index} | mensajes {summary.start_message_id}-{summary.end_message_id}] {summary.summary}"
        for summary in selected_summaries
    ]
    return "\n".join(
        [
            "MEMORIA DE CONVERSACION",
            "Memoria truncada: se conservaron la ventana activa y los resumenes mas relevantes.",
            "",
            "RESUMENES RELEVANTES DE BLOQUES ANTERIORES:",
            *(summary_lines or ["Sin resumenes anteriores relevantes."]),
            "",
            active_section,
        ]
    )[:MAX_MEMORY_CHARS]


def _compact_text(text: str, max_length: int) -> str:
    """Compacta texto para memoria sin perder la idea principal."""

    compacted = re.sub(r"\s+", " ", text).strip()
    if len(compacted) <= max_length:
        return compacted
    return compacted[: max_length - 3].rstrip() + "..."
