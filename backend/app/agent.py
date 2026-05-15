import json
import logging
import re
from typing import Any, TypedDict

from google import genai
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


class AgentState(TypedDict):
    question: str
    history: str
    cypher_query: str | None
    results: list[dict[str, Any]]
    answer: str
    out_of_domain: bool


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


def _generate_cypher(state: AgentState) -> AgentState:
    """Primer nodo LangGraph: genera Cypher o detecta pregunta fuera de dominio."""

    logger.info("LangGraph node=generate_cypher question_length=%s", len(state["question"]))
    prompt = CYPHER_GENERATION_PROMPT.format(
        schema=GRAPH_SCHEMA,
        history=state["history"],
        question=state["question"],
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
    graph.add_node("generate_answer", _generate_answer)

    graph.set_entry_point("generate_cypher")
    graph.add_conditional_edges(
        "generate_cypher",
        _route_after_cypher,
        {"execute_cypher": "execute_cypher", "finish": END},
    )
    graph.add_edge("execute_cypher", "generate_answer")
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
