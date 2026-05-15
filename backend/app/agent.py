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
    CONTEXT_RESOLUTION_PROMPT,
    CYPHER_CORRECTION_PROMPT,
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


class AgentState(TypedDict):
    question: str
    resolved_question: str
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


def _normalize_cypher_syntax(cypher_query: str) -> str:
    """Corrige patrones comunes que el LLM genera con sintaxis Cypher invalida."""

    normalized = _replace_where_relationship_pattern(cypher_query)
    if normalized != cypher_query:
        logger.info("Cypher normalizado para evitar patron de relacion dentro de WHERE")
    return normalized


def _replace_where_relationship_pattern(cypher_query: str) -> str:
    """Convierte AND p-[:REL]->(Label {prop: value}) en EXISTS { MATCH ... }."""

    pattern = re.compile(
        r"(?P<connector>\b(?:AND|OR)\s+)"
        r"(?P<source>\w+)\s*-\s*\[:(?P<relationship>[A-Z_Ñ]+)\]\s*->\s*"
        r"\(\s*(?:(?P<target_var>\w+)\s*:\s*)?"
        r"(?P<label>`[^`]+`|[A-Za-zÁÉÍÓÚÜÑáéíóúüñ_][\wÁÉÍÓÚÜÑáéíóúüñ]*)"
        r"\s*\{\s*(?P<property>[A-Za-zÁÉÍÓÚÜÑáéíóúüñ_][\wÁÉÍÓÚÜÑáéíóúüñ]*)"
        r"\s*:\s*(?P<value>[^}]+?)\s*\}\s*\)",
        flags=re.IGNORECASE,
    )

    def replacement(match: re.Match[str]) -> str:
        connector = match.group("connector")
        source = match.group("source")
        relationship = match.group("relationship")
        label = match.group("label")
        property_name = match.group("property")
        value = match.group("value").strip()
        return (
            f"{connector}EXISTS {{ MATCH ({source})-[:{relationship}]->"
            f"(:{label} {{{property_name}: {value}}}) }}"
        )

    return pattern.sub(replacement, cypher_query)


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


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Extrae un objeto JSON desde texto del LLM."""

    cleaned = _strip_code_fences(text)
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return None

    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _resolve_question_with_memory(question: str, history: str) -> str:
    """Reescribe la pregunta usando memoria activa sin reglas por palabras fijas."""

    if history.strip() == "Sin historial previo.":
        return question

    prompt = CONTEXT_RESOLUTION_PROMPT.format(history=history, question=question)
    try:
        response = _invoke_llm(prompt)
    except Exception as exc:
        logger.warning(
            "No se pudo resolver contexto conversacional error_type=%s error=%s",
            type(exc).__name__,
            summarize_exception(exc),
        )
        return question

    parsed = _extract_json_object(response)
    if not parsed:
        logger.warning("Resolucion contextual no devolvio JSON valido response_preview=%s", response[:180])
        return question

    standalone_question = parsed.get("standalone_question")
    if not isinstance(standalone_question, str) or not standalone_question.strip():
        return question

    uses_context = bool(parsed.get("uses_context"))
    referenced_entities = parsed.get("referenced_entities")
    logger.info(
        "Pregunta resuelta con memoria uses_context=%s referenced_entities=%s",
        uses_context,
        referenced_entities if isinstance(referenced_entities, list) else [],
    )
    return standalone_question.strip()


def _generate_cypher(state: AgentState) -> AgentState:
    """Primer nodo LangGraph: genera Cypher o detecta pregunta fuera de dominio."""

    logger.info("LangGraph node=generate_cypher question_length=%s", len(state["question"]))
    resolved_question = _resolve_question_with_memory(state["question"], state["history"])
    prompt = CYPHER_GENERATION_PROMPT.format(
        schema=GRAPH_SCHEMA,
        history=state["history"],
        question=resolved_question,
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
            "resolved_question": resolved_question,
        }

    cypher = _prepare_cypher(cypher)
    logger.info("Cypher listo preview=%s", cypher[:180].replace("\n", " "))
    return {
        **state,
        "resolved_question": resolved_question,
        "cypher_query": cypher,
        "out_of_domain": False,
    }


def _prepare_cypher(cypher_query: str) -> str:
    """Aplica normalizaciones locales antes de ejecutar o reintentar un Cypher."""

    return _ensure_filter_values_returned(_normalize_cypher_syntax(cypher_query))


def _execute_cypher(state: AgentState) -> AgentState:
    """Segundo nodo LangGraph: ejecuta la consulta de lectura en Neo4j AuraDB."""

    if state["out_of_domain"] or not state["cypher_query"]:
        return state

    logger.info("LangGraph node=execute_cypher")
    try:
        results = neo4j_client.execute_read_query(state["cypher_query"])
    except Exception as exc:
        if not _should_attempt_cypher_correction(exc):
            raise

        corrected_query = _correct_cypher_after_error(state, exc)
        if not corrected_query or corrected_query == state["cypher_query"]:
            raise

        logger.info("Reintentando Cypher corregido preview=%s", corrected_query[:180].replace("\n", " "))
        results = neo4j_client.execute_read_query(corrected_query)
        logger.info("Neo4j devolvio rows=%s tras correccion", len(results))
        return {**state, "cypher_query": corrected_query, "results": results}

    logger.info("Neo4j devolvio rows=%s", len(results))
    return {**state, "results": results}


def _should_attempt_cypher_correction(exc: Exception) -> bool:
    """Decide si vale la pena pedir correccion por error de sintaxis Cypher."""

    error_type = type(exc).__name__.lower()
    error_code = str(getattr(exc, "code", "")).lower()
    error_text = summarize_exception(exc).lower()
    return (
        "syntax" in error_type
        or "syntax" in error_code
        or "statement.syntaxerror" in error_text
        or "invalid input" in error_text
    )


def _correct_cypher_after_error(state: AgentState, exc: Exception) -> str | None:
    """Pide al LLM corregir un Cypher que Neo4j rechazo por sintaxis."""

    if not state["cypher_query"]:
        return None

    prompt = CYPHER_CORRECTION_PROMPT.format(
        schema=GRAPH_SCHEMA,
        question=state["resolved_question"],
        cypher_query=state["cypher_query"],
        error=summarize_exception(exc),
    )
    corrected = _strip_code_fences(_invoke_llm(prompt))
    if corrected.strip() == "OUT_OF_DOMAIN":
        return None

    corrected = _prepare_cypher(corrected)
    logger.info("Cypher corregido raw_length=%s", len(corrected))
    return corrected


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

    if state["out_of_domain"] or state["results"] or not state["cypher_query"]:
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

    corrected_query = _prepare_cypher(corrected_query)
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
        resolved_question=state["resolved_question"],
        cypher_query=state["cypher_query"],
        results=json.dumps(state["results"], ensure_ascii=False, default=str),
    )
    answer = _invoke_llm(prompt)
    logger.info("Respuesta natural generada answer_length=%s", len(answer))
    return {**state, "answer": answer}


def _route_after_cypher(state: AgentState) -> str:
    """Decide si el flujo termina por fuera de dominio o continua hacia Neo4j."""

    return "finish" if state["out_of_domain"] else "execute_cypher"


def _route_after_execute(state: AgentState) -> str:
    """Ejecuta fuzzy match solo cuando Neo4j no encontro filas."""

    return "generate_answer" if state["results"] else "fuzzy_match"


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
    graph.add_conditional_edges(
        "execute_cypher",
        _route_after_execute,
        {"generate_answer": "generate_answer", "fuzzy_match": "fuzzy_match"},
    )
    graph.add_edge("fuzzy_match", "generate_answer")
    graph.add_edge("generate_answer", END)
    return graph.compile()


agent_graph = build_graph()


def run_agent(question: str, history: str) -> dict[str, Any]:
    """Ejecuta el agente con la pregunta actual y el historial del chat."""

    logger.info("run_agent start question_length=%s history_length=%s", len(question), len(history))
    initial_state: AgentState = {
        "question": question,
        "resolved_question": question,
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
