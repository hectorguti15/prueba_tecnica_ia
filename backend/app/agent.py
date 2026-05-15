import json
import logging
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, TypedDict

from groq import Groq
from langgraph.graph import END, StateGraph

from app.config import settings
from app.logging_config import summarize_exception
from app.neo4j_client import neo4j_client
from app.prompts import (
    ANSWER_PROMPT,
    CYPHER_GENERATION_PROMPT,
    GRAPH_SCHEMA,
    OUT_OF_DOMAIN_RESPONSE,
)

logger = logging.getLogger(__name__)

FUZZY_MATCH_THRESHOLD = 0.90
FUZZY_CANDIDATE_LIMIT = 1000

TEXT_FILTER_PROPERTIES: dict[str, tuple[str, str]] = {
    "nombre_autor": ("Autor", "nombre_autor"),
    "titulo": ("`Publicación`", "titulo"),
    "nombre_institucion": ("`Institución`", "nombre_institucion"),
    "pais_institucion": ("`País`", "pais_institucion"),
    "area_ia": ("AreaIA", "area_ia"),
    "palabra_clave": ("PalabraClave", "palabra_clave"),
    "venue": ("Venue", "venue"),
    "tipo_venue": ("Venue", "tipo_venue"),
}

TEXT_FILTER_ALIASES: dict[str, tuple[str, str]] = {
    "nombre_autor": ("autor", "autores_coincidentes"),
    "titulo": ("titulo_coincidente", "titulos_coincidentes"),
    "nombre_institucion": ("institucion", "instituciones_coincidentes"),
    "pais_institucion": ("pais", "paises_coincidentes"),
    "area_ia": ("area", "areas_coincidentes"),
    "palabra_clave": ("palabra_clave", "palabras_clave_coincidentes"),
    "venue": ("venue", "venues_coincidentes"),
    "tipo_venue": ("tipo_venue", "tipos_venue_coincidentes"),
}

FOLLOW_UP_HINTS = (
    "esa",
    "ese",
    "esas",
    "esos",
    "esta",
    "este",
    "estos",
    "estas",
    "dicha",
    "dicho",
    "anterior",
    "anteriores",
    "mencionada",
    "mencionado",
    "de ellas",
    "de ellos",
    "cuáles son",
    "cuales son",
    "qué año",
    "que año",
    "cuándo",
    "cuando",
)


class AgentState(TypedDict):
    question: str
    history: str
    cypher_query: str | None
    results: list[dict[str, Any]]
    answer: str
    out_of_domain: bool
    fuzzy_audit: list[dict[str, Any]]


def _invoke_llm(prompt: str) -> str:
    """Ejecuta el proveedor LLM configurado y devuelve texto plano."""

    """Llama Groq directamente como proveedor alternativo configurable."""

    if not settings.groq_api_key:
        raise ValueError("GROQ_API_KEY no esta configurada.")

    client = Groq(api_key=settings.groq_api_key)
    try:
        response = client.chat.completions.create(
            model=settings.groq_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
    except Exception as exc:
        logger.error(
            "Groq fallo model=%s error_type=%s error=%s",
            settings.groq_model,
            type(exc).__name__,
            summarize_exception(exc),
        )
        raise
    content = response.choices[0].message.content
    if not content:
        raise ValueError("Groq no devolvio contenido de texto.")
    logger.info("Groq respondio text_length=%s", len(content))
    return content.strip()




def _strip_code_fences(text: str) -> str:
    """Limpia bloques markdown para quedarse solo con el Cypher generado."""

    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:cypher)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    return cleaned


def _has_aggregation(return_body: str) -> bool:
    """Detecta si el RETURN usa funciones de agregacion."""

    return bool(re.search(r"\b(count|sum|avg|min|max|collect)\s*\(", return_body, flags=re.IGNORECASE))


def _ensure_filter_values_returned(cypher_query: str) -> str:
    """Agrega al RETURN los valores reales usados en filtros textuales."""

    filters = _extract_text_filters(cypher_query)
    if not filters:
        return cypher_query

    return_match = re.search(r"\bRETURN\b", cypher_query, flags=re.IGNORECASE)
    if not return_match:
        return cypher_query

    before_return = cypher_query[: return_match.start()]
    after_return = cypher_query[return_match.end() :]
    tail_match = re.search(r"\b(ORDER\s+BY|SKIP|LIMIT)\b", after_return, flags=re.IGNORECASE)
    if tail_match:
        return_body = after_return[: tail_match.start()].strip()
        tail = " " + after_return[tail_match.start() :].strip()
    else:
        return_body = after_return.strip()
        tail = ""

    if not return_body:
        return cypher_query

    aggregate_return = _has_aggregation(return_body)
    additions: list[str] = []
    normalized_return = return_body.lower()
    used_aliases = {
        alias.lower()
        for alias in re.findall(r"\bAS\s+(\w+)\b", return_body, flags=re.IGNORECASE)
    }

    for text_filter in filters:
        variable = text_filter["variable"]
        property_name = text_filter["property"]
        expression = f"{variable}.{property_name}"
        singular_alias, aggregate_alias = TEXT_FILTER_ALIASES[property_name]
        alias = aggregate_alias if aggregate_return else singular_alias

        if expression.lower() in normalized_return or alias.lower() in used_aliases:
            continue

        if aggregate_return:
            additions.append(f"collect(DISTINCT {expression}) AS {alias}")
        else:
            additions.append(f"{expression} AS {alias}")
        used_aliases.add(alias.lower())

    if not additions:
        return cypher_query

    logger.info("Cypher enriquecido con valores reales de filtros additions=%s", additions)
    return f"{before_return}RETURN {return_body}, {', '.join(additions)}{tail}"


def _normalize_text(value: str) -> str:
    """Normaliza texto para comparar coincidencias aproximadas sin acentos ni mayusculas."""

    without_accents = "".join(
        char for char in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(char)
    )
    return re.sub(r"\s+", " ", without_accents.casefold()).strip()


def _similarity(left: str, right: str) -> float:
    """Calcula similitud entre dos textos normalizados."""

    return SequenceMatcher(None, _normalize_text(left), _normalize_text(right)).ratio()


def _is_follow_up_question(question: str) -> bool:
    """Detecta preguntas cortas o referenciales que dependen del turno anterior."""

    normalized = _normalize_text(question)
    if any(_normalize_text(hint) in normalized for hint in FOLLOW_UP_HINTS):
        return True

    words = re.findall(r"\w+", normalized)
    return len(words) <= 5 and any(word in normalized for word in ("ano", "citas", "autor", "venue"))


def _extract_last_publication_id(history: str) -> str | None:
    """Obtiene el ultimo id_publicacion mencionado en respuestas o Cypher previos."""

    matches = re.findall(r"\bPUB\d+\b", history, flags=re.IGNORECASE)
    return matches[-1].upper() if matches else None


def _extract_last_cypher(history: str) -> str | None:
    """Extrae el ultimo Cypher registrado en el historial formateado."""

    matches = re.findall(r"Cypher usado por el asistente:\s*(.+)", history)
    return matches[-1].strip() if matches else None


def _extract_requested_year(question: str) -> int | None:
    """Obtiene un año de cuatro digitos desde la pregunta del usuario."""

    match = re.search(r"\b(19\d{2}|20\d{2})\b", question)
    return int(match.group(1)) if match else None


def _extract_publication_variable(cypher_query: str) -> str | None:
    """Detecta la variable usada para nodos `Publicación` en un Cypher previo."""

    match = re.search(r"\((?P<variable>\w+)\s*:\s*`Publicaci[^`]*n`", cypher_query, flags=re.IGNORECASE)
    return match.group("variable") if match else None


def _build_year_filter_from_previous_cypher(question: str, history: str) -> str | None:
    """Reutiliza el Cypher anterior y agrega un filtro por año para seguimientos."""

    year = _extract_requested_year(question)
    if year is None or not _is_follow_up_question(question):
        return None

    last_cypher = _extract_last_cypher(history)
    if not last_cypher:
        return None

    publication_variable = _extract_publication_variable(last_cypher)
    if not publication_variable:
        return None

    prefix = re.split(r"\bRETURN\b", last_cypher, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    if not prefix.lower().startswith("match "):
        return None

    logger.info(
        "Seguimiento resuelto agregando filtro de año year=%s publication_variable=%s",
        year,
        publication_variable,
    )
    return (
        f"{prefix} "
        f"MATCH ({publication_variable})-[:PUBLICADA_EN_AÑO]->(anio:`Año`) "
        f"WHERE anio.año = {year} "
        f"RETURN {publication_variable}.id_publicacion AS id_publicacion, "
        f"{publication_variable}.titulo AS titulo, "
        f"{publication_variable}.numero_citas AS numero_citas, "
        "anio.año AS año_publicacion "
        "LIMIT 20"
    )


def _build_contextual_question(question: str, history: str) -> str:
    """Agrega contexto conversacional explicito para que el LLM no pierda referencias."""

    if not _is_follow_up_question(question) or history == "Sin historial previo.":
        return question

    context_lines = [
        question,
        "",
        "Esta es una pregunta de seguimiento. Usa el contexto activo de la conversacion anterior.",
    ]
    publication_id = _extract_last_publication_id(history)
    if publication_id:
        context_lines.append(f"Publicacion previa identificada: id_publicacion={publication_id}.")

    last_cypher = _extract_last_cypher(history)
    if last_cypher:
        context_lines.append(f"Cypher previo relevante: {last_cypher}")

    context_lines.append("No la clasifiques como fuera de dominio si se refiere a publicaciones, autores, años, citas, venues, areas, paises o palabras clave previas.")
    contextual_question = "\n".join(context_lines)
    logger.info("Pregunta de seguimiento contextualizada publication_id=%s has_last_cypher=%s", publication_id, bool(last_cypher))
    return contextual_question


def _resolve_follow_up_cypher(question: str, history: str) -> str | None:
    """Resuelve seguimientos obvios sin depender del LLM."""

    if not _is_follow_up_question(question):
        return None

    year_filter_cypher = _build_year_filter_from_previous_cypher(question, history)
    if year_filter_cypher:
        return year_filter_cypher

    publication_id = _extract_last_publication_id(history)
    if not publication_id:
        return None

    normalized = _normalize_text(question)
    if "ano" in normalized or "cuando" in normalized or "fecha" in normalized:
        logger.info("Seguimiento resuelto por id_publicacion para año publication_id=%s", publication_id)
        return (
            "MATCH (p:`Publicación` {id_publicacion: \""
            f"{publication_id}"
            "\"})-[:PUBLICADA_EN_AÑO]->(anio:`Año`) "
            "RETURN p.id_publicacion AS id_publicacion, p.titulo AS titulo, anio.año AS año_publicacion "
            "LIMIT 20"
        )

    return None


def _generate_cypher(state: AgentState) -> AgentState:
    """Primer nodo LangGraph: genera Cypher o detecta pregunta fuera de dominio."""

    logger.info("LangGraph node=generate_cypher question_length=%s", len(state["question"]))
    resolved_cypher = _resolve_follow_up_cypher(state["question"], state["history"])
    if resolved_cypher:
        logger.info("Cypher generado por resolucion deterministica de seguimiento")
        return {**state, "cypher_query": _ensure_filter_values_returned(resolved_cypher), "out_of_domain": False}

    contextual_question = _build_contextual_question(state["question"], state["history"])
    prompt = CYPHER_GENERATION_PROMPT.format(
        schema=GRAPH_SCHEMA,
        history=state["history"],
        question=contextual_question,
    )
    cypher = _strip_code_fences(_invoke_llm(prompt))
    logger.info("Cypher generado raw_length=%s", len(cypher))

    if cypher.strip() == "OUT_OF_DOMAIN":
        logger.info("Pregunta fuera de dominio detectada por el agente")
        return {
            **state,
            "cypher_query": None,
            "results": [],
            "answer": OUT_OF_DOMAIN_RESPONSE,
            "out_of_domain": True,
        }

    cypher = _ensure_filter_values_returned(cypher)
    logger.info("Cypher listo preview=%s", cypher[:180].replace("\n", " "))
    return {**state, "cypher_query": cypher, "out_of_domain": False}


def _execute_cypher(state: AgentState) -> AgentState:
    """Segundo nodo LangGraph: ejecuta la consulta de lectura en Neo4j AuraDB."""

    if state["out_of_domain"] or not state["cypher_query"]:
        return state

    logger.info("LangGraph node=execute_cypher")
    results = neo4j_client.execute_read_query(state["cypher_query"])
    logger.info("Neo4j devolvio rows=%s", len(results))
    return {**state, "results": results}


def _extract_text_filters(cypher_query: str) -> list[dict[str, str]]:
    """Extrae filtros toLower(var.prop) CONTAINS toLower("texto") del Cypher."""

    pattern = re.compile(
        r"toLower\(\s*(?P<variable>\w+)\.(?P<property>\w+)\s*\)\s+"
        r"CONTAINS\s+toLower\(\s*[\"'](?P<term>[^\"']+)[\"']\s*\)",
        flags=re.IGNORECASE,
    )
    filters: list[dict[str, str]] = []
    for match in pattern.finditer(cypher_query):
        property_name = match.group("property")
        if property_name not in TEXT_FILTER_PROPERTIES:
            continue
        filters.append(
            {
                "variable": match.group("variable"),
                "property": property_name,
                "term": match.group("term"),
            }
        )
    return filters


def _find_best_fuzzy_value(property_name: str, term: str) -> dict[str, Any] | None:
    """Busca el valor existente mas parecido para una propiedad textual del grafo."""

    label, graph_property = TEXT_FILTER_PROPERTIES[property_name]
    candidate_query = (
        f"MATCH (n:{label}) "
        f"WHERE n.{graph_property} IS NOT NULL "
        f"RETURN DISTINCT n.{graph_property} AS value "
        f"LIMIT {FUZZY_CANDIDATE_LIMIT}"
    )
    candidates = neo4j_client.execute_read_query(candidate_query)

    best_value: str | None = None
    best_score = 0.0
    for candidate in candidates:
        value = candidate.get("value")
        if not isinstance(value, str) or not value.strip():
            continue
        score = _similarity(term, value)
        if score > best_score:
            best_score = score
            best_value = value

    if best_value is None or best_score < FUZZY_MATCH_THRESHOLD:
        logger.info(
            "Fuzzy sin coincidencia property=%s term=%r best_score=%.3f threshold=%.2f",
            property_name,
            term,
            best_score,
            FUZZY_MATCH_THRESHOLD,
        )
        return None

    if _normalize_text(best_value) == _normalize_text(term):
        logger.info("Fuzzy omitido: coincidencia exacta property=%s term=%r", property_name, term)
        return None

    audit = {
        "property": property_name,
        "input": term,
        "match": best_value,
        "score": round(best_score, 4),
    }
    logger.info(
        "Fuzzy candidato property=%s input=%r match=%r score=%.4f",
        property_name,
        term,
        best_value,
        best_score,
    )
    return audit


def _replace_filter_term(cypher_query: str, property_name: str, original: str, replacement: str) -> str:
    """Reemplaza el literal textual de un filtro toLower para una propiedad concreta."""

    escaped_original = re.escape(original)
    pattern = re.compile(
        rf"(toLower\(\s*\w+\.{re.escape(property_name)}\s*\)\s+"
        rf"CONTAINS\s+toLower\(\s*)([\"']){escaped_original}\2(\s*\))",
        flags=re.IGNORECASE,
    )
    safe_replacement = replacement.replace("\\", "\\\\").replace('"', '\\"')
    return pattern.sub(rf'\1"{safe_replacement}"\3', cypher_query, count=1)


def _apply_fuzzy_match(state: AgentState) -> AgentState:
    """Canoniza filtros textuales y reintenta consultas con coincidencias >= 90%."""

    if state["out_of_domain"] or not state["cypher_query"]:
        return state

    logger.info("LangGraph node=fuzzy_match")
    filters = _extract_text_filters(state["cypher_query"])
    if not filters:
        logger.info("Fuzzy omitido: no hay filtros textuales detectados")
        return state

    corrected_query = state["cypher_query"]
    fuzzy_audit = list(state.get("fuzzy_audit", []))
    for text_filter in filters:
        match = _find_best_fuzzy_value(text_filter["property"], text_filter["term"])
        if not match:
            continue
        corrected_query = _replace_filter_term(
            corrected_query,
            text_filter["property"],
            text_filter["term"],
            match["match"],
        )
        fuzzy_audit.append(match)

    if corrected_query == state["cypher_query"]:
        return {**state, "fuzzy_audit": fuzzy_audit}

    corrected_query = _ensure_filter_values_returned(corrected_query)
    fuzzy_results = neo4j_client.execute_read_query(corrected_query)
    logger.info("Fuzzy reintento rows=%s audit_count=%s", len(fuzzy_results), len(fuzzy_audit))
    if not fuzzy_results:
        return state

    for audit_entry in fuzzy_audit:
        logger.info(
            "Fuzzy match aplicado property=%s input=%r match=%r score=%s",
            audit_entry["property"],
            audit_entry["input"],
            audit_entry["match"],
            audit_entry["score"],
        )

    return {
        **state,
        "cypher_query": corrected_query,
        "results": fuzzy_results,
        "fuzzy_audit": fuzzy_audit,
    }


def _generate_answer(state: AgentState) -> AgentState:
    """Tercer nodo LangGraph: redacta la respuesta usando solo resultados de Neo4j."""

    if state["out_of_domain"]:
        return state

    logger.info("LangGraph node=generate_answer results_count=%s", len(state["results"]))
    prompt = ANSWER_PROMPT.format(
        question=state["question"],
        cypher_query=state["cypher_query"],
        results=json.dumps(state["results"], ensure_ascii=False, default=str),
    )
    answer = _invoke_llm(prompt)
    logger.info("Respuesta natural generada answer_length=%s", len(answer))
    return {**state, "answer": answer}


def _route_after_cypher(state: AgentState) -> str:
    """Decide si el flujo termina por fuera de dominio o continua hacia Neo4j."""

    return "finish" if state["out_of_domain"] else "execute_cypher"


def build_graph():
    """Compila el flujo LangGraph usado por el endpoint de mensajes."""

    graph = StateGraph(AgentState)
    graph.add_node("generate_cypher", _generate_cypher)
    graph.add_node("execute_cypher", _execute_cypher)
    graph.add_node("fuzzy_match", _apply_fuzzy_match)
    graph.add_node("generate_answer", _generate_answer)

    graph.set_entry_point("generate_cypher")
    graph.add_conditional_edges(
        "generate_cypher",
        _route_after_cypher,
        {"execute_cypher": "execute_cypher", "finish": END},
    )
    graph.add_edge("execute_cypher", "fuzzy_match")
    graph.add_edge("fuzzy_match", "generate_answer")
    graph.add_edge("generate_answer", END)
    return graph.compile()


agent_graph = build_graph()


def run_agent(question: str, history: str) -> dict[str, Any]:
    """Ejecuta el agente con la pregunta actual y el historial del chat."""

    logger.info("run_agent start question_length=%s history_length=%s", len(question), len(history))
    initial_state: AgentState = {
        "question": question,
        "history": history,
        "cypher_query": None,
        "results": [],
        "answer": "",
        "out_of_domain": False,
        "fuzzy_audit": [],
    }
    final_state = agent_graph.invoke(initial_state)
    logger.info(
        "run_agent end out_of_domain=%s has_answer=%s has_cypher=%s",
        final_state["out_of_domain"],
        bool(final_state["answer"]),
        bool(final_state["cypher_query"]),
    )
    return {
        "answer": final_state["answer"],
        "cypher_query": final_state["cypher_query"],
        "results": final_state["results"],
        "out_of_domain": final_state["out_of_domain"],
    }
