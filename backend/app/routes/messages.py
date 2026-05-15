from datetime import datetime, timezone
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload

from app.agent import run_agent
from app.database import get_db
from app.logging_config import summarize_exception
from app.models import Chat, Message
from app.schemas import AssistantResponse, MessageCreate

router = APIRouter(prefix="/chats/{chat_id}/messages", tags=["messages"])
logger = logging.getLogger(__name__)


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
    history = _format_history(previous_messages)
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


def _format_history(messages: list[Message], max_messages: int = 12) -> str:
    """Convierte mensajes previos en texto de contexto para LangGraph."""

    if not messages:
        return "Sin historial previo."

    recent_messages = messages[-max_messages:]
    lines: list[str] = []
    for message in recent_messages:
        role = "Usuario" if message.role == "user" else "Asistente"
        lines.append(f"{role}: {message.content}")
        if message.role == "assistant" and message.cypher_query:
            lines.append(f"Cypher usado por el asistente: {message.cypher_query}")
    return "\n".join(lines)
