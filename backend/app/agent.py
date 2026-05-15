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
    SEMANTIC_FALLBACK_PROMPT,
)

logger = logging.getLogger(__name__)

FUZZY_MATCH_THRESHOLD = 0.90
FUZZY_CANDIDATE_LIMIT = 1000
SEMANTIC_FALLBACK_PROPERTIES = {"area_ia", "palabra_clave", "tipo_venue"}

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

RELATIONSHIP_DIRECTIONS: dict[str, tuple[str, str]] = {
    "escribio": ("autor", "publicacion"),
    "afiliado_a": ("autor", "institucion"),
    "ubicada_en": ("institucion", "pais"),
    "pertenece_a": ("publicacion", "areaia"),
    "tiene_palabra_clave": ("publicacion", "palabraclave"),
    "publicada_en": ("publicacion", "venue"),
    "publicada_en_ano": ("publicacion", "ano"),
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
    semantic_audit: list[dict[str, Any]]


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
    normalized_text_filters = _normalize_text_equality_filters(normalized)
    if normalized_text_filters != normalized:
        logger.info("Cypher normalizado para convertir igualdad textual en filtro case-insensitive")
    normalized = normalized_text_filters
    normalized_counts = _normalize_publication_count_expressions(normalized)
    if normalized_counts != normalized:
        logger.info("Cypher normalizado para contar publicaciones distintas")
    normalized = normalized_counts
    normalized_relationships = _normalize_relationship_directions(normalized)
    if normalized_relationships != normalized:
        logger.info("Cypher normalizado para respetar direcciones reales de relaciones")
    normalized = normalized_relationships
    normalized_paths = _normalize_schema_path_patterns(normalized)
    if normalized_paths != normalized:
        logger.info("Cypher normalizado para corregir rutas semanticas del esquema")
    normalized = normalized_paths
    return normalized


def _choose_variable(cypher_query: str, preferred: str, fallback: str) -> str:
    """Elige una variable Cypher que no choque con las ya presentes."""

    used_variables = set(re.findall(r"\(\s*(\w+)\s*(?::|\)|\{)", cypher_query))
    if preferred not in used_variables:
        return preferred

    candidate = fallback
    index = 2
    while candidate in used_variables:
        candidate = f"{fallback}{index}"
        index += 1
    return candidate


def _canonical_graph_name(value: str) -> str:
    """Normaliza labels y relaciones para compararlos con el esquema real."""

    return _normalize_text(value.strip().strip("`")).replace(" ", "")


def _normalize_relationship_directions(cypher_query: str) -> str:
    """Reordena relaciones simples cuando el LLM invierte la direccion del esquema."""

    label_pattern = r"`[^`]+`|[A-Za-zÁÉÍÓÚÜÑáéíóúüñ_][\wÁÉÍÓÚÜÑáéíóúüñ]*"
    node_pattern = (
        r"\(\s*(?:\w+\s*)?:\s*"
        rf"(?P<label>{label_pattern})"
        r"(?:\s*\{[^{}]*\})?\s*\)"
    )
    relationship_pattern = re.compile(
        rf"(?<![\]-])"
        rf"(?P<left>{node_pattern.replace('(?P<label>', '(?P<left_label>')})"
        r"\s*(?P<left_arrow><-|-)\s*"
        r"\[:(?P<relationship>[A-Z_Ñ]+)\]\s*"
        r"(?P<right_arrow>->|-)\s*"
        rf"(?P<right>{node_pattern.replace('(?P<label>', '(?P<right_label>')})"
        r"(?!\s*[-<])",
        flags=re.IGNORECASE,
    )

    def replacement(match: re.Match[str]) -> str:
        relationship = match.group("relationship")
        expected_direction = RELATIONSHIP_DIRECTIONS.get(_canonical_graph_name(relationship))
        if not expected_direction:
            return match.group(0)

        source_label, target_label = expected_direction
        left_label = _canonical_graph_name(match.group("left_label"))
        right_label = _canonical_graph_name(match.group("right_label"))

        if left_label == source_label and right_label == target_label:
            source_node = match.group("left")
            target_node = match.group("right")
        elif right_label == source_label and left_label == target_label:
            source_node = match.group("right")
            target_node = match.group("left")
        else:
            return match.group(0)

        normalized = f"{source_node}-[:{relationship}]->{target_node}"
        if normalized != match.group(0):
            logger.info(
                "Relacion normalizada relationship=%s from=%s to=%s",
                relationship,
                match.group(0),
                normalized,
            )
        return normalized

    return relationship_pattern.sub(replacement, cypher_query)


def _normalize_schema_path_patterns(cypher_query: str) -> str:
    """Corrige rutas frecuentes que son sintacticas pero no existen en el grafo."""

    normalized = _normalize_venue_country_year_detour_path(cypher_query)
    normalized = _normalize_publication_country_year_detour_path(normalized)
    normalized = _normalize_reversed_publication_country_path(normalized)
    normalized = _normalize_reversed_venue_country_path(normalized)
    normalized = _normalize_publication_country_path(normalized)
    return normalized


def _normalize_venue_country_year_detour_path(cypher_query: str) -> str:
    """Elimina rodeos por año que separan el venue de la publicacion afiliada a un pais."""

    label_pattern = r"`[^`]+`|[^\s:(){}\[\],-]+"
    year_relationship = r"PUBLICADA_EN_A(?:Ñ|N)O"
    variable_replacements: list[tuple[str, str]] = []

    def node_pattern(prefix: str) -> str:
        return (
            rf"(?P<{prefix}_node>\(\s*(?P<{prefix}_var>\w+)\s*:\s*"
            rf"(?P<{prefix}_label>{label_pattern})"
            r"(?:\s*\{[^{}]*\})?\s*\))"
        )

    loose_node_pattern = (
        r"\(\s*(?:\w+\s*)?(?::\s*"
        rf"{label_pattern}"
        r")?(?:\s*\{[^{}]*\})?\s*\)"
    )

    detour_pattern = re.compile(
        r"(?P<match>\bMATCH\s+)?"
        + node_pattern("venue")
        + r"\s*-\s*\[:PUBLICADA_EN\]\s*->\s*"
        + node_pattern("publication")
        + rf"\s*-\s*\[:{year_relationship}\]\s*->\s*"
        + loose_node_pattern
        + rf"\s*<-\s*\[:{year_relationship}\]\s*-\s*"
        + node_pattern("publication2")
        + r"\s*-\s*\[:ESCRIBIO\]\s*->\s*"
        + node_pattern("author")
        + r"\s*-\s*\[:AFILIADO_A\]\s*->\s*"
        + node_pattern("institution")
        + r"\s*-\s*\[:UBICADA_EN\]\s*->\s*"
        + node_pattern("country"),
        flags=re.IGNORECASE,
    )

    def replacement(match: re.Match[str]) -> str:
        labels = {
            "venue": _canonical_graph_name(match.group("venue_label")),
            "publication": _canonical_graph_name(match.group("publication_label")),
            "publication2": _canonical_graph_name(match.group("publication2_label")),
            "author": _canonical_graph_name(match.group("author_label")),
            "institution": _canonical_graph_name(match.group("institution_label")),
            "country": _canonical_graph_name(match.group("country_label")),
        }
        if labels != {
            "venue": "venue",
            "publication": "publicacion",
            "publication2": "publicacion",
            "author": "autor",
            "institution": "institucion",
            "country": "pais",
        }:
            return match.group(0)

        publication_var = match.group("publication_var")
        publication2_var = match.group("publication2_var")
        author_var = match.group("author_var")
        if publication2_var != publication_var:
            variable_replacements.append((publication2_var, publication_var))

        corrected = (
            f"{match.group('match') or ''}"
            f"{match.group('publication_node')}-[:PUBLICADA_EN]->{match.group('venue_node')}, "
            f"{match.group('author_node')}-[:ESCRIBIO]->({publication_var}), "
            f"({author_var})-[:AFILIADO_A]->{match.group('institution_node')}"
            f"-[:UBICADA_EN]->{match.group('country_node')}"
        )
        logger.info(
            "Ruta venue-pais con rodeo por año normalizada from=%s to=%s",
            match.group(0),
            corrected,
        )
        return corrected

    normalized = detour_pattern.sub(replacement, cypher_query)
    for old_variable, new_variable in variable_replacements:
        normalized = re.sub(rf"\b{re.escape(old_variable)}\.", f"{new_variable}.", normalized)
    return normalized


def _normalize_publication_country_year_detour_path(cypher_query: str) -> str:
    """Elimina rodeos por año que invierten Autor -> Publicacion al filtrar por pais."""

    label_pattern = r"`[^`]+`|[^\s:(){}\[\],-]+"
    year_relationship = r"PUBLICADA_EN_A(?:Ñ|N)O"
    variable_replacements: list[tuple[str, str]] = []

    def node_pattern(prefix: str) -> str:
        return (
            rf"(?P<{prefix}_node>\(\s*(?P<{prefix}_var>\w+)\s*:\s*"
            rf"(?P<{prefix}_label>{label_pattern})"
            r"(?:\s*\{[^{}]*\})?\s*\))"
        )

    loose_node_pattern = (
        r"\(\s*(?:\w+\s*)?(?::\s*"
        rf"{label_pattern}"
        r")?(?:\s*\{[^{}]*\})?\s*\)"
    )

    detour_pattern = re.compile(
        r"(?P<match>\bMATCH\s+)?"
        + node_pattern("publication")
        + rf"\s*-\s*\[:{year_relationship}\]\s*->\s*"
        + loose_node_pattern
        + rf"\s*<-\s*\[:{year_relationship}\]\s*-\s*"
        + node_pattern("publication2")
        + r"\s*-\s*\[:ESCRIBIO\]\s*->\s*"
        + node_pattern("author")
        + r"\s*-\s*\[:AFILIADO_A\]\s*->\s*"
        + node_pattern("institution")
        + r"\s*-\s*\[:UBICADA_EN\]\s*->\s*"
        + node_pattern("country"),
        flags=re.IGNORECASE,
    )

    def replacement(match: re.Match[str]) -> str:
        labels = {
            "publication": _canonical_graph_name(match.group("publication_label")),
            "publication2": _canonical_graph_name(match.group("publication2_label")),
            "author": _canonical_graph_name(match.group("author_label")),
            "institution": _canonical_graph_name(match.group("institution_label")),
            "country": _canonical_graph_name(match.group("country_label")),
        }
        if labels != {
            "publication": "publicacion",
            "publication2": "publicacion",
            "author": "autor",
            "institution": "institucion",
            "country": "pais",
        }:
            return match.group(0)

        publication_var = match.group("publication_var")
        publication2_var = match.group("publication2_var")
        author_var = match.group("author_var")
        if publication2_var != publication_var:
            variable_replacements.append((publication2_var, publication_var))

        corrected = (
            f"{match.group('match') or ''}"
            f"{match.group('author_node')}-[:ESCRIBIO]->{match.group('publication_node')}, "
            f"({author_var})-[:AFILIADO_A]->{match.group('institution_node')}"
            f"-[:UBICADA_EN]->{match.group('country_node')}"
        )
        logger.info(
            "Ruta publicacion-pais con rodeo por año normalizada from=%s to=%s",
            match.group(0),
            corrected,
        )
        return corrected

    normalized = detour_pattern.sub(replacement, cypher_query)
    for old_variable, new_variable in variable_replacements:
        normalized = re.sub(rf"\b{re.escape(old_variable)}\.", f"{new_variable}.", normalized)
    return normalized


def _normalize_reversed_publication_country_path(cypher_query: str) -> str:
    """Corrige Publicacion -> Autor -> Institucion -> Pais."""

    label_pattern = r"`[^`]+`|[^\s:(){}\[\],-]+"

    def node_pattern(prefix: str) -> str:
        return (
            rf"(?P<{prefix}_node>\(\s*(?P<{prefix}_var>\w+)\s*:\s*"
            rf"(?P<{prefix}_label>{label_pattern})"
            r"(?:\s*\{[^{}]*\})?\s*\))"
        )

    wrong_publication_country_path_pattern = re.compile(
        r"(?P<match>\bMATCH\s+)?"
        + node_pattern("publication")
        + r"\s*-\s*\[:ESCRIBIO\]\s*->\s*"
        + node_pattern("author")
        + r"\s*-\s*\[:AFILIADO_A\]\s*->\s*"
        + node_pattern("institution")
        + r"\s*-\s*\[:UBICADA_EN\]\s*->\s*"
        + node_pattern("country"),
        flags=re.IGNORECASE,
    )

    def replacement(match: re.Match[str]) -> str:
        labels = {
            "publication": _canonical_graph_name(match.group("publication_label")),
            "author": _canonical_graph_name(match.group("author_label")),
            "institution": _canonical_graph_name(match.group("institution_label")),
            "country": _canonical_graph_name(match.group("country_label")),
        }
        if labels != {
            "publication": "publicacion",
            "author": "autor",
            "institution": "institucion",
            "country": "pais",
        }:
            return match.group(0)

        author_var = match.group("author_var")
        corrected = (
            f"{match.group('match') or ''}"
            f"{match.group('author_node')}-[:ESCRIBIO]->{match.group('publication_node')}, "
            f"({author_var})-[:AFILIADO_A]->{match.group('institution_node')}"
            f"-[:UBICADA_EN]->{match.group('country_node')}"
        )
        logger.info(
            "Ruta publicacion-pais normalizada from=%s to=%s",
            match.group(0),
            corrected,
        )
        return corrected

    return wrong_publication_country_path_pattern.sub(replacement, cypher_query)


def _normalize_reversed_venue_country_path(cypher_query: str) -> str:
    """Corrige Venue -> Publicacion <- Autor -> Institucion -> Pais."""

    label_pattern = r"`[^`]+`|[^\s:(){}\[\],-]+"

    def node_pattern(prefix: str) -> str:
        return (
            rf"(?P<{prefix}_node>\(\s*(?P<{prefix}_var>\w+)\s*:\s*"
            rf"(?P<{prefix}_label>{label_pattern})"
            r"(?:\s*\{[^{}]*\})?\s*\))"
        )

    wrong_venue_country_path_pattern = re.compile(
        r"(?P<match>\bMATCH\s+)?"
        + node_pattern("venue")
        + r"\s*-\s*\[:PUBLICADA_EN\]\s*->\s*"
        + node_pattern("publication")
        + r"\s*<-\s*\[:ESCRIBIO\]\s*-\s*"
        + node_pattern("author")
        + r"\s*-\s*\[:AFILIADO_A\]\s*->\s*"
        + node_pattern("institution")
        + r"\s*-\s*\[:UBICADA_EN\]\s*->\s*"
        + node_pattern("country"),
        flags=re.IGNORECASE,
    )

    def replacement(match: re.Match[str]) -> str:
        labels = {
            "venue": _canonical_graph_name(match.group("venue_label")),
            "publication": _canonical_graph_name(match.group("publication_label")),
            "author": _canonical_graph_name(match.group("author_label")),
            "institution": _canonical_graph_name(match.group("institution_label")),
            "country": _canonical_graph_name(match.group("country_label")),
        }
        if labels != {
            "venue": "venue",
            "publication": "publicacion",
            "author": "autor",
            "institution": "institucion",
            "country": "pais",
        }:
            return match.group(0)

        publication_var = match.group("publication_var")
        author_var = match.group("author_var")
        corrected = (
            f"{match.group('match') or ''}"
            f"{match.group('publication_node')}-[:PUBLICADA_EN]->{match.group('venue_node')}, "
            f"{match.group('author_node')}-[:ESCRIBIO]->({publication_var}), "
            f"({author_var})-[:AFILIADO_A]->{match.group('institution_node')}"
            f"-[:UBICADA_EN]->{match.group('country_node')}"
        )
        logger.info(
            "Ruta venue-pais normalizada from=%s to=%s",
            match.group(0),
            corrected,
        )
        return corrected

    return wrong_venue_country_path_pattern.sub(replacement, cypher_query)


def _normalize_publication_country_path(cypher_query: str) -> str:
    """Corrige Publicacion -> Venue -> Institucion -> Pais hacia la ruta via Autor."""

    label_pattern = r"`[^`]+`|[A-Za-zÁÉÍÓÚÜÑáéíóúüñ_][\wÁÉÍÓÚÜÑáéíóúüñ]*"

    def node_pattern(prefix: str) -> str:
        return (
            rf"(?P<{prefix}_node>\(\s*(?P<{prefix}_var>\w+)\s*:\s*"
            rf"(?P<{prefix}_label>{label_pattern})"
            r"(?:\s*\{[^{}]*\})?\s*\))"
        )

    wrong_country_path_pattern = re.compile(
        r"(?P<match>\bMATCH\s+)?"
        + node_pattern("publication")
        + r"\s*-\s*\[:PUBLICADA_EN\]\s*->\s*"
        + node_pattern("venue")
        + r"\s*-\s*\[:AFILIADO_A\]\s*->\s*"
        + node_pattern("institution")
        + r"\s*-\s*\[:UBICADA_EN\]\s*->\s*"
        + node_pattern("country"),
        flags=re.IGNORECASE,
    )

    def replacement(match: re.Match[str]) -> str:
        labels = {
            "publication": _canonical_graph_name(match.group("publication_label")),
            "venue": _canonical_graph_name(match.group("venue_label")),
            "institution": _canonical_graph_name(match.group("institution_label")),
            "country": _canonical_graph_name(match.group("country_label")),
        }
        if labels != {
            "publication": "publicacion",
            "venue": "venue",
            "institution": "institucion",
            "country": "pais",
        }:
            return match.group(0)

        author_var = _choose_variable(cypher_query, preferred="a", fallback="autor_pais")
        publication_var = match.group("publication_var")
        corrected = (
            f"{match.group('match') or ''}"
            f"{match.group('publication_node')}-[:PUBLICADA_EN]->{match.group('venue_node')}, "
            f"({author_var}:Autor)-[:ESCRIBIO]->({publication_var}), "
            f"({author_var})-[:AFILIADO_A]->{match.group('institution_node')}"
            f"-[:UBICADA_EN]->{match.group('country_node')}"
        )
        logger.info(
            "Ruta de pais normalizada from=%s to=%s",
            match.group(0),
            corrected,
        )
        return corrected

    return wrong_country_path_pattern.sub(replacement, cypher_query)


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


def _normalize_text_equality_filters(cypher_query: str) -> str:
    """Convierte var.prop = "texto" a filtros textuales robustos para el grafo."""

    pattern = re.compile(
        r"(?<![\w.])(?P<variable>\w+)\.(?P<property>\w+)\s*=\s*"
        r"(?P<quote>[\"'])(?P<term>[^\"']+)(?P=quote)",
        flags=re.IGNORECASE,
    )

    def replacement(match: re.Match[str]) -> str:
        property_name = match.group("property")
        if property_name not in TEXT_FILTER_PROPERTIES:
            return match.group(0)

        variable = match.group("variable")
        term = _escape_cypher_string(match.group("term"))
        return f'toLower({variable}.{property_name}) CONTAINS toLower("{term}")'

    return pattern.sub(replacement, cypher_query)


def _normalize_publication_count_expressions(cypher_query: str) -> str:
    """Evita sobrecontar publicaciones cuando el MATCH multiplica filas por autores."""

    pattern = re.compile(
        r"COUNT\s*\(\s*(?!DISTINCT\s+)(?P<expression>\w+\.id_publicacion)\s*\)",
        flags=re.IGNORECASE,
    )

    return pattern.sub(lambda match: f"COUNT(DISTINCT {match.group('expression')})", cypher_query)


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


def _extract_latest_assistant_publication_ids(history: str) -> list[str]:
    """Obtiene IDs de publicaciones de la respuesta reciente mas util para seguimientos."""

    assistant_blocks = re.findall(
        r"(?ms)^Asistente:\s*(.*?)(?=^Cypher usado por el asistente:|^Usuario:|^Asistente:|\Z)",
        history,
    )
    for block in reversed(assistant_blocks):
        publication_ids = re.findall(r"\bPUB\d+\b", block, flags=re.IGNORECASE)
        if publication_ids:
            seen: set[str] = set()
            ordered_ids: list[str] = []
            for publication_id in publication_ids:
                normalized_id = publication_id.upper()
                if normalized_id in seen:
                    continue
                seen.add(normalized_id)
                ordered_ids.append(normalized_id)
            return ordered_ids
    return []


def _followup_ordinal_index(question: str) -> int | None:
    """Detecta referencias ordinales simples usadas en preguntas de seguimiento."""

    normalized_question = _normalize_text(question)
    ordinal_patterns = [
        (0, r"\b(primera|primero|primer|1|1ra|1ro)\b"),
        (1, r"\b(segunda|segundo|2|2da|2do)\b"),
        (2, r"\b(tercera|tercero|tercer|3|3ra|3ro)\b"),
    ]
    for index, pattern in ordinal_patterns:
        if re.search(pattern, normalized_question):
            return index

    if re.search(r"\b(ultima|ultimo)\b", normalized_question):
        return -1

    return None


def _resolve_publication_id_followup(question: str, history: str) -> str | None:
    """Resuelve 'esta primera/segunda/ultima' hacia un id_publicacion reciente."""

    ordinal_index = _followup_ordinal_index(question)
    if ordinal_index is None:
        return None

    publication_ids = _extract_latest_assistant_publication_ids(history)
    if not publication_ids:
        return None

    try:
        return publication_ids[ordinal_index]
    except IndexError:
        return None


def _needs_context_resolution(question: str) -> bool:
    """Decide si una pregunta contiene una referencia conversacional real."""

    normalized_question = _normalize_text(question)
    reference_patterns = [
        r"\b(este|esta|estos|estas|ese|esa|esos|esas|aquel|aquella)\b",
        r"\b(anterior|anteriores|previa|previas|previo|previos)\b",
        r"\b(primera|primero|primer|segunda|segundo|tercera|tercero|tercer|ultima|ultimo)\b",
        r"\b(su|sus|misma|mismo|mismas|mismos)\b",
        r"\b(de estas|de estos|de esas|de esos|de la lista|de los anteriores|de las anteriores)\b",
        r"^\s*(y|tambien|ademas)\b",
    ]
    return any(re.search(pattern, normalized_question) for pattern in reference_patterns)


def _build_deterministic_followup_cypher(question: str, history: str) -> tuple[str, str] | None:
    """Genera Cypher directo para seguimientos inequívocos sobre publicaciones listadas."""

    publication_id = _resolve_publication_id_followup(question, history)
    if not publication_id:
        return None

    normalized_question = _normalize_text(question)
    asks_for_authors = (
        "autor" in normalized_question
        or "autores" in normalized_question
        or "quienes" in normalized_question
        or "quien" in normalized_question
    )
    if not asks_for_authors:
        return None

    resolved_question = f"¿Quiénes son los autores de la publicación {publication_id}?"
    cypher_query = (
        f'MATCH (a:Autor)-[:ESCRIBIO]->(p:`Publicación` {{id_publicacion: "{publication_id}"}}) '
        "RETURN a.nombre_autor AS autor, "
        "p.id_publicacion AS id_publicacion, "
        "p.titulo AS titulo "
        "LIMIT 20"
    )
    return resolved_question, cypher_query


def _resolve_question_with_memory(question: str, history: str) -> str:
    """Reescribe la pregunta usando memoria activa sin reglas por palabras fijas."""

    if history.strip() == "Sin historial previo.":
        return question

    publication_id = _resolve_publication_id_followup(question, history)
    if publication_id:
        logger.info("Pregunta resuelta deterministicamente publication_id=%s", publication_id)
        return f"{question.strip()} Referencia resuelta: publicación {publication_id}."

    if not _needs_context_resolution(question):
        logger.info("Resolucion contextual omitida: pregunta autonoma")
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
    deterministic_followup = _build_deterministic_followup_cypher(state["question"], state["history"])
    if deterministic_followup:
        resolved_question, cypher = deterministic_followup
        cypher = _prepare_cypher(cypher)
        logger.info("Cypher generado por seguimiento deterministico preview=%s", cypher[:180].replace("\n", " "))
        return {
            **state,
            "resolved_question": resolved_question,
            "cypher_query": cypher,
            "out_of_domain": False,
        }

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


def _is_inside_exists_block(cypher_query: str, index: int) -> bool:
    """Detecta filtros dentro de EXISTS para no tratar variables locales como globales."""

    for match in re.finditer(r"\bEXISTS\s*\{", cypher_query, flags=re.IGNORECASE):
        block_start = match.end() - 1
        if index <= block_start:
            continue

        depth = 0
        for position in range(block_start, len(cypher_query)):
            char = cypher_query[position]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    if index < position:
                        return True
                    break
    return False


def _extract_text_filters(cypher_query: str) -> list[dict[str, str]]:
    """Extrae filtros toLower(var.prop) CONTAINS toLower("texto") del Cypher."""

    pattern = re.compile(
        r"toLower\(\s*(?P<variable>\w+)\.(?P<property>\w+)\s*\)\s+"
        r"CONTAINS\s+toLower\(\s*[\"'](?P<term>[^\"']+)[\"']\s*\)",
        flags=re.IGNORECASE,
    )
    filters: list[dict[str, str]] = []
    for match in pattern.finditer(cypher_query):
        if _is_inside_exists_block(cypher_query, match.start()):
            continue
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

    candidates = _get_distinct_property_values(property_name)

    best_value: str | None = None
    best_score = 0.0
    for value in candidates:
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

    if best_value.strip().casefold() == term.strip().casefold():
        logger.info("Fuzzy omitido: coincidencia exacta case-insensitive property=%s term=%r", property_name, term)
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


def _get_distinct_property_values(property_name: str) -> list[str]:
    """Obtiene valores textuales reales del grafo para una propiedad permitida."""

    label, graph_property = TEXT_FILTER_PROPERTIES[property_name]
    candidate_query = (
        f"MATCH (n:{label}) "
        f"WHERE n.{graph_property} IS NOT NULL "
        f"RETURN DISTINCT n.{graph_property} AS value "
        f"LIMIT {FUZZY_CANDIDATE_LIMIT}"
    )
    candidates = neo4j_client.execute_read_query(candidate_query)

    values: list[str] = []
    for candidate in candidates:
        value = candidate.get("value")
        if not isinstance(value, str) or not value.strip():
            continue
        values.append(value.strip())
    return values


def _select_semantic_values(
    property_name: str,
    term: str,
    question: str,
    candidates: list[str],
) -> dict[str, Any] | None:
    """Pide al LLM elegir solo categorias existentes semanticamente relacionadas."""

    if property_name not in SEMANTIC_FALLBACK_PROPERTIES or not candidates:
        return None

    prompt = SEMANTIC_FALLBACK_PROMPT.format(
        property=property_name,
        term=term,
        question=question,
        candidates=json.dumps(candidates, ensure_ascii=False),
    )
    try:
        response = _invoke_llm(prompt)
    except Exception as exc:
        logger.warning(
            "Fallback semantico omitido por error LLM property=%s term=%r error_type=%s error=%s",
            property_name,
            term,
            type(exc).__name__,
            summarize_exception(exc),
        )
        return None

    parsed = _extract_json_object(response)
    if not parsed or not parsed.get("can_retry"):
        logger.info("Fallback semantico sin candidato property=%s term=%r", property_name, term)
        return None

    requested_values = parsed.get("values")
    if not isinstance(requested_values, list):
        return None

    candidate_by_normalized = {_normalize_text(candidate): candidate for candidate in candidates}
    selected_values: list[str] = []
    for requested_value in requested_values:
        if not isinstance(requested_value, str):
            continue
        candidate = candidate_by_normalized.get(_normalize_text(requested_value))
        if candidate and candidate not in selected_values:
            selected_values.append(candidate)

    if not selected_values:
        logger.info(
            "Fallback semantico rechazo valores fuera del grafo property=%s term=%r values=%s",
            property_name,
            term,
            requested_values,
        )
        return None

    audit = {
        "property": property_name,
        "input": term,
        "values": selected_values,
        "reason": parsed.get("reason", ""),
    }
    logger.info(
        "Fallback semantico candidato property=%s input=%r values=%s",
        property_name,
        term,
        selected_values,
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


def _escape_cypher_string(value: str) -> str:
    """Escapa un literal de texto para usarlo dentro de comillas dobles en Cypher."""

    return value.replace("\\", "\\\\").replace('"', '\\"')


def _replace_filter_with_semantic_values(
    cypher_query: str,
    property_name: str,
    original: str,
    values: list[str],
) -> str:
    """Reemplaza un filtro textual por uno o varios valores semanticos existentes."""

    if not values:
        return cypher_query

    escaped_original = re.escape(original)
    pattern = re.compile(
        rf"toLower\(\s*(?P<variable>\w+)\.{re.escape(property_name)}\s*\)\s+"
        rf"CONTAINS\s+toLower\(\s*([\"']){escaped_original}\2\s*\)",
        flags=re.IGNORECASE,
    )

    def replacement(match: re.Match[str]) -> str:
        variable = match.group("variable")
        semantic_conditions = [
            f'toLower({variable}.{property_name}) CONTAINS toLower("{_escape_cypher_string(value)}")'
            for value in values
        ]
        if len(semantic_conditions) == 1:
            return semantic_conditions[0]
        return "(" + " OR ".join(semantic_conditions) + ")"

    return pattern.sub(replacement, cypher_query, count=1)


def _find_publication_variable(cypher_query: str) -> str | None:
    """Encuentra una variable con label Publicacion en el query."""

    node_pattern = re.compile(
        r"\(\s*(?P<variable>\w+)\s*:\s*(?P<label>`[^`]+`|[^\s:(){}\[\],-]+)",
        flags=re.IGNORECASE,
    )
    for match in node_pattern.finditer(cypher_query):
        if _canonical_graph_name(match.group("label")) == "publicacion":
            return match.group("variable")
    return None


def _replace_topic_filter_with_publication_text(
    cypher_query: str,
    property_name: str,
    original: str,
) -> str:
    """Amplia un filtro tematico exacto para buscar tambien en titulos."""

    if property_name not in {"area_ia", "palabra_clave"}:
        return cypher_query

    publication_variable = _find_publication_variable(cypher_query)
    if not publication_variable:
        return cypher_query

    escaped_original = re.escape(original)
    pattern = re.compile(
        rf"toLower\(\s*(?P<variable>\w+)\.{re.escape(property_name)}\s*\)\s+"
        rf"CONTAINS\s+toLower\(\s*([\"']){escaped_original}\2\s*\)",
        flags=re.IGNORECASE,
    )

    def replacement(match: re.Match[str]) -> str:
        variable = match.group("variable")
        term = _escape_cypher_string(original)
        conditions = [
            f'toLower({variable}.{property_name}) CONTAINS toLower("{term}")',
            f'toLower({publication_variable}.titulo) CONTAINS toLower("{term}")',
        ]
        if property_name != "area_ia":
            conditions.append(
                f'EXISTS {{ MATCH ({publication_variable})-[:PERTENECE_A]->(topic_area:AreaIA) '
                f'WHERE toLower(topic_area.area_ia) CONTAINS toLower("{term}") }}'
            )
        if property_name != "palabra_clave":
            conditions.append(
                f'EXISTS {{ MATCH ({publication_variable})-[:TIENE_PALABRA_CLAVE]->'
                f'(topic_keyword:PalabraClave) '
                f'WHERE toLower(topic_keyword.palabra_clave) CONTAINS toLower("{term}") }}'
            )
        return "(" + " OR ".join(conditions) + ")"

    return pattern.sub(replacement, cypher_query, count=1)


def _try_exact_topic_text_fallback(
    state: AgentState,
    filters: list[dict[str, str]],
) -> AgentState | None:
    """Reintenta temas amplios como texto real de publicacion antes del fallback semantico."""

    corrected_query = state["cypher_query"]
    if not corrected_query:
        return None

    for text_filter in filters:
        corrected_query = _replace_topic_filter_with_publication_text(
            corrected_query,
            text_filter["property"],
            text_filter["term"],
        )

    if corrected_query == state["cypher_query"]:
        return None

    corrected_query = _prepare_cypher(corrected_query)
    exact_topic_results = neo4j_client.execute_read_query(corrected_query)
    logger.info("Fallback tematico exacto rows=%s", len(exact_topic_results))
    if not exact_topic_results:
        return None

    return {
        **state,
        "cypher_query": corrected_query,
        "results": exact_topic_results,
    }


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


def _apply_semantic_fallback(state: AgentState) -> AgentState:
    """Reintenta filtros categoricos usando interpretacion semantica controlada."""

    if state["out_of_domain"] or state["results"] or not state["cypher_query"]:
        return state

    logger.info("LangGraph node=semantic_fallback")
    filters = [
        text_filter
        for text_filter in _extract_text_filters(state["cypher_query"])
        if text_filter["property"] in SEMANTIC_FALLBACK_PROPERTIES
    ]
    if not filters:
        logger.info("Fallback semantico omitido: no hay filtros categoricos permitidos")
        return state

    exact_topic_state = _try_exact_topic_text_fallback(state, filters)
    if exact_topic_state:
        return exact_topic_state

    corrected_query = state["cypher_query"]
    semantic_audit = list(state.get("semantic_audit", []))
    for text_filter in filters:
        candidates = _get_distinct_property_values(text_filter["property"])
        match = _select_semantic_values(
            property_name=text_filter["property"],
            term=text_filter["term"],
            question=state["resolved_question"],
            candidates=candidates,
        )
        if not match:
            continue

        corrected_query = _replace_filter_with_semantic_values(
            corrected_query,
            text_filter["property"],
            text_filter["term"],
            match["values"],
        )
        semantic_audit.append(match)

    if corrected_query == state["cypher_query"]:
        return {**state, "semantic_audit": semantic_audit}

    corrected_query = _prepare_cypher(corrected_query)
    semantic_results = neo4j_client.execute_read_query(corrected_query)
    logger.info(
        "Fallback semantico reintento rows=%s audit_count=%s",
        len(semantic_results),
        len(semantic_audit),
    )

    return {
        **state,
        "cypher_query": corrected_query,
        "results": semantic_results,
        "semantic_audit": semantic_audit,
    }


def _semantic_interpretation_prefix(semantic_audit: list[dict[str, Any]]) -> str:
    """Construye una aclaracion breve sobre filtros reinterpretados."""

    if not semantic_audit:
        return ""

    notes: list[str] = []
    for entry in semantic_audit:
        values = entry.get("values", [])
        if not isinstance(values, list) or not values:
            continue
        quoted_values = ", ".join(f'"{value}"' for value in values if isinstance(value, str))
        if not quoted_values:
            continue
        notes.append(
            f'No encontre coincidencias exactas para "{entry.get("input", "")}", '
            f"asi que interprete la busqueda como {quoted_values}."
        )

    return " ".join(notes)


def _generate_deterministic_answer(state: AgentState) -> str:
    """Respuesta de respaldo cuando Neo4j respondio pero el LLM no pudo redactar."""

    results = state["results"]
    if not results:
        return "No se encontraron coincidencias."

    if all(isinstance(row.get("autor"), str) for row in results):
        authors: list[str] = []
        for row in results:
            author = row.get("autor")
            if isinstance(author, str) and author not in authors:
                authors.append(author)

        first_row = results[0]
        title = first_row.get("titulo") or first_row.get("titulo_coincidente") or first_row.get("publicacion")
        publication_id = first_row.get("id_publicacion")
        if isinstance(title, str) and title.strip():
            header = f'Autores encontrados para "{title}"'
        elif isinstance(publication_id, str) and publication_id.strip():
            header = f"Autores encontrados para {publication_id}"
        else:
            header = "Autores encontrados"
        return header + ":\n" + "\n".join(f"- {author}" for author in authors)

    lines: list[str] = ["Resultados encontrados:"]
    for index, row in enumerate(results[:10], start=1):
        values = [
            f"{key}: {value}"
            for key, value in row.items()
            if value is not None and value != []
        ]
        lines.append(f"{index}. " + ", ".join(values))
    return "\n".join(lines)


def _generate_answer(state: AgentState) -> AgentState:
    """Tercer nodo LangGraph: redacta la respuesta usando solo resultados de Neo4j."""

    if state["out_of_domain"]:
        return state

    logger.info("LangGraph node=generate_answer results_count=%s", len(state["results"]))
    if not state["results"]:
        answer = "No se encontraron coincidencias."
        logger.info("Respuesta deterministica sin resultados answer_length=%s", len(answer))
        return {**state, "answer": answer}

    prompt = ANSWER_PROMPT.format(
        question=state["question"],
        resolved_question=state["resolved_question"],
        cypher_query=state["cypher_query"],
        results=json.dumps(state["results"], ensure_ascii=False, default=str),
    )
    try:
        answer = _invoke_llm(prompt)
    except Exception as exc:
        logger.warning(
            "Respuesta LLM omitida; usando respaldo deterministico error_type=%s error=%s",
            type(exc).__name__,
            summarize_exception(exc),
        )
        answer = _generate_deterministic_answer(state)

    if state["results"]:
        semantic_prefix = _semantic_interpretation_prefix(state.get("semantic_audit", []))
        if semantic_prefix:
            answer = f"{semantic_prefix}\n\n{answer}"
    logger.info("Respuesta natural generada answer_length=%s", len(answer))
    return {**state, "answer": answer}


def _route_after_cypher(state: AgentState) -> str:
    """Decide si el flujo termina por fuera de dominio o continua hacia Neo4j."""

    return "finish" if state["out_of_domain"] else "execute_cypher"


def _route_after_execute(state: AgentState) -> str:
    """Ejecuta fuzzy match solo cuando Neo4j no encontro filas."""

    return "generate_answer" if state["results"] else "fuzzy_match"


def _route_after_fuzzy(state: AgentState) -> str:
    """Ejecuta fallback semantico solo si fuzzy tampoco encontro filas."""

    return "generate_answer" if state["results"] else "semantic_fallback"


def build_graph():
    """Compila el flujo LangGraph usado por el endpoint de mensajes."""

    graph = StateGraph(AgentState)
    graph.add_node("generate_cypher", _generate_cypher)
    graph.add_node("execute_cypher", _execute_cypher)
    graph.add_node("fuzzy_match", _apply_fuzzy_match)
    graph.add_node("semantic_fallback", _apply_semantic_fallback)
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
    graph.add_conditional_edges(
        "fuzzy_match",
        _route_after_fuzzy,
        {"generate_answer": "generate_answer", "semantic_fallback": "semantic_fallback"},
    )
    graph.add_edge("semantic_fallback", "generate_answer")
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
        "semantic_audit": [],
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
