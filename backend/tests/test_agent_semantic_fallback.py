"""Tests for the semantic fallback in the LangGraph agent.

The external Groq, Neo4j and LangGraph packages are stubbed so these tests can
run in a lightweight local environment.

Run from backend/:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import types
import unittest


os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")
os.environ.setdefault("NEO4J_USERNAME", "neo4j")
os.environ.setdefault("NEO4J_PASSWORD", "test-password")
os.environ.setdefault("NEO4J_DATABASE", "neo4j")
os.environ.setdefault("GROQ_API_KEY", "test-groq-key")


class FakeStateGraph:
    def __init__(self, state_type):
        self.state_type = state_type

    def add_node(self, *args, **kwargs):
        return None

    def set_entry_point(self, *args, **kwargs):
        return None

    def add_conditional_edges(self, *args, **kwargs):
        return None

    def add_edge(self, *args, **kwargs):
        return None

    def compile(self):
        return types.SimpleNamespace(invoke=lambda state: state)


class FakeNeo4jClient:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def execute_read_query(self, query: str, parameters=None):
        self.queries.append(query)
        if "RETURN DISTINCT n.area_ia AS value" in query:
            return [
                {"value": "NLP"},
                {"value": "IA Generativa"},
                {"value": "Machine Learning"},
                {"value": "Robotica"},
            ]
        if "RETURN DISTINCT n.tipo_venue AS value" in query:
            return [
                {"value": "Conferencia"},
                {"value": "Revista"},
                {"value": "Workshop"},
            ]
        if "RETURN DISTINCT n.pais_institucion AS value" in query:
            return [
                {"value": "Perú"},
                {"value": "Chile"},
                {"value": "Colombia"},
            ]
        if 'toLower(p.titulo) CONTAINS toLower("Robótica")' in query:
            return [
                {
                    "titulo": "Percepción visual para robótica turismo",
                    "número_de_citas": 61,
                }
            ]
        if "IA Generativa" in query:
            return [{"titulo": "Sistema de respuesta para redes neuronales", "area": "IA Generativa"}]
        if "Revista" in query:
            return [{"titulo": "Fine-tuning de modelos generativos", "tipo_venue": "Revista"}]
        return []


def _install_import_stubs() -> None:
    groq_module = types.ModuleType("groq")
    groq_module.Groq = object
    sys.modules["groq"] = groq_module

    langgraph_module = types.ModuleType("langgraph")
    langgraph_graph_module = types.ModuleType("langgraph.graph")
    langgraph_graph_module.END = "__end__"
    langgraph_graph_module.StateGraph = FakeStateGraph
    sys.modules["langgraph"] = langgraph_module
    sys.modules["langgraph.graph"] = langgraph_graph_module

    neo4j_client_module = types.ModuleType("app.neo4j_client")
    neo4j_client_module.neo4j_client = FakeNeo4jClient()
    sys.modules["app.neo4j_client"] = neo4j_client_module


class AgentSemanticFallbackTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _install_import_stubs()
        sys.modules.pop("app.agent", None)
        cls.agent = importlib.import_module("app.agent")

    def setUp(self) -> None:
        self.fake_neo4j_client = FakeNeo4jClient()
        self.original_neo4j_client = self.agent.neo4j_client
        self.original_invoke_llm = self.agent._invoke_llm
        self.agent.neo4j_client = self.fake_neo4j_client
        self.agent._invoke_llm = lambda prompt: json.dumps(
            {
                "can_retry": True,
                "values": ["IA Generativa"],
                "reason": "Inteligencia Artificial se interpreta como una categoria existente.",
            }
        )

    def tearDown(self) -> None:
        self.agent.neo4j_client = self.original_neo4j_client
        self.agent._invoke_llm = self.original_invoke_llm

    def test_semantic_fallback_retries_allowed_category_filter(self) -> None:
        state = {
            "question": "Muestrame publicaciones sobre Inteligencia Artificial",
            "resolved_question": "Muestrame publicaciones sobre Inteligencia Artificial",
            "history": "Sin historial previo.",
            "cypher_query": (
                "MATCH (p:`Publicacion`)-[:PERTENECE_A]->(ar:AreaIA) "
                'WHERE toLower(ar.area_ia) CONTAINS toLower("Inteligencia Artificial") '
                "RETURN p.titulo AS titulo LIMIT 20"
            ),
            "results": [],
            "answer": "",
            "out_of_domain": False,
            "fuzzy_audit": [],
            "semantic_audit": [],
        }

        result = self.agent._apply_semantic_fallback(state)

        self.assertEqual(result["results"], [{"titulo": "Sistema de respuesta para redes neuronales", "area": "IA Generativa"}])
        self.assertIn('toLower(ar.area_ia) CONTAINS toLower("IA Generativa")', result["cypher_query"])
        self.assertEqual(result["semantic_audit"][0]["property"], "area_ia")
        self.assertEqual(result["semantic_audit"][0]["values"], ["IA Generativa"])

    def test_topic_fallback_searches_publication_titles_before_semantic_values(self) -> None:
        self.agent._invoke_llm = lambda prompt: self.fail("No debe llamar al LLM si el titulo coincide")
        state = {
            "question": "Que publicaciones hablan de Robotica?",
            "resolved_question": "Que publicaciones hablan de Robotica?",
            "history": "Sin historial previo.",
            "cypher_query": (
                "MATCH (p:`Publicacion`)-[:TIENE_PALABRA_CLAVE]->(pc:PalabraClave) "
                'WHERE toLower(pc.palabra_clave) CONTAINS toLower("Robótica") '
                "RETURN p.titulo AS titulo, p.numero_citas AS numero_citas LIMIT 20"
            ),
            "results": [],
            "answer": "",
            "out_of_domain": False,
            "fuzzy_audit": [],
            "semantic_audit": [],
        }

        result = self.agent._apply_semantic_fallback(state)

        self.assertEqual(
            result["results"],
            [
                {
                    "titulo": "Percepción visual para robótica turismo",
                    "número_de_citas": 61,
                }
            ],
        )
        self.assertIn('toLower(p.titulo) CONTAINS toLower("Robótica")', result["cypher_query"])
        self.assertNotIn("topic_area.area_ia AS area", result["cypher_query"])
        self.assertNotIn("topic_keyword.palabra_clave AS palabra_clave", result["cypher_query"])
        self.assertEqual(result["semantic_audit"], [])

    def test_semantic_fallback_ignores_non_category_filters(self) -> None:
        state = {
            "question": "Muestrame publicaciones de Sofia",
            "resolved_question": "Muestrame publicaciones de Sofia",
            "history": "Sin historial previo.",
            "cypher_query": (
                "MATCH (a:Autor)-[:ESCRIBIO]->(p:`Publicacion`) "
                'WHERE toLower(a.nombre_autor) CONTAINS toLower("Sofia") '
                "RETURN p.titulo AS titulo LIMIT 20"
            ),
            "results": [],
            "answer": "",
            "out_of_domain": False,
            "fuzzy_audit": [],
            "semantic_audit": [],
        }

        result = self.agent._apply_semantic_fallback(state)

        self.assertEqual(result["results"], [])
        self.assertEqual(result["semantic_audit"], [])
        self.assertEqual(self.fake_neo4j_client.queries, [])

    def test_normalize_relationship_direction_for_publication_authors(self) -> None:
        query = (
            'MATCH (p:`Publicación` {id_publicacion: "PUB099"})-[:ESCRIBIO]->(a:Autor) '
            "RETURN a.nombre_autor AS autor LIMIT 20"
        )

        normalized = self.agent._normalize_cypher_syntax(query)

        self.assertEqual(
            normalized,
            'MATCH (a:Autor)-[:ESCRIBIO]->(p:`Publicación` {id_publicacion: "PUB099"}) '
            "RETURN a.nombre_autor AS autor LIMIT 20",
        )

    def test_normalize_relationship_direction_keeps_chained_patterns_unchanged(self) -> None:
        query = (
            "MATCH (p:`Publicación`)<-[:ESCRIBIO]-(a:Autor)-[:AFILIADO_A]->(i:`Institución`) "
            "RETURN a.nombre_autor AS autor, i.nombre_institucion AS institucion LIMIT 20"
        )

        normalized = self.agent._normalize_cypher_syntax(query)

        self.assertEqual(normalized, query)


    def test_prepare_cypher_normalizes_text_equality_filters(self) -> None:
        query = (
            "MATCH (a:Autor)-[:ESCRIBIO]->(p:`Publicacion`)-[:PUBLICADA_EN]->(v:Venue) "
            'WHERE toLower(a.nombre_autor) CONTAINS toLower("Maria Quispe") '
            'AND v.tipo_venue = "revista" '
            "RETURN p.titulo AS titulo LIMIT 20"
        )

        normalized = self.agent._prepare_cypher(query)

        self.assertIn('toLower(v.tipo_venue) CONTAINS toLower("revista")', normalized)
        self.assertNotIn('v.tipo_venue = "revista"', normalized)

    def test_prepare_cypher_counts_distinct_publications(self) -> None:
        query = (
            "MATCH (p:`Publicacion`)-[:PUBLICADA_EN]->(v:Venue), "
            "(a:Autor)-[:ESCRIBIO]->(p) "
            "RETURN v.venue AS revista, COUNT(p.id_publicacion) AS cantidad_publicaciones "
            "ORDER BY COUNT(p.id_publicacion) DESC LIMIT 3"
        )

        normalized = self.agent._prepare_cypher(query)

        self.assertIn("COUNT(DISTINCT p.id_publicacion) AS cantidad_publicaciones", normalized)
        self.assertIn("ORDER BY COUNT(DISTINCT p.id_publicacion) DESC", normalized)

    def test_deterministic_followup_uses_latest_publication_list_not_old_venues(self) -> None:
        history = """
VENTANA ACTIVA RECIENTE:
Usuario: Dame top 3 revistas en el Peru
Asistente: Las top 3 revistas en Perú son:
1. Revista Iberoamericana de IA con 5 publicaciones
2. Information Sciences con 4 publicaciones
3. Nature Machine Intelligence con 2 publicaciones
Usuario: Top 3 publicaciones del año 2024
Asistente: Las top 3 publicaciones del año 2024 son:
1. "Modelos multimodales aplicados a noticias periodísticas" (PUB060) con 52 citas.
2. "Evaluación de prompts en tareas de agricultura" (PUB055) con 50 citas.
3. "Percepción visual para robótica atención al cliente" (PUB045) con 46 citas.
"""

        result = self.agent._build_deterministic_followup_cypher(
            "De esta primera quienes son los autores?",
            history,
        )

        self.assertIsNotNone(result)
        resolved_question, cypher_query = result
        self.assertIn("PUB060", resolved_question)
        self.assertIn('{id_publicacion: "PUB060"}', cypher_query)
        self.assertIn("(a:Autor)-[:ESCRIBIO]->(p:`Publicación`", cypher_query)
        self.assertNotIn("Revista Iberoamericana de IA", cypher_query)

    def test_context_resolution_skips_standalone_question_even_with_history(self) -> None:
        history = """
VENTANA ACTIVA RECIENTE:
Usuario: Dame top 3 revistas en el Peru
Asistente: Las top 3 revistas en Perú son:
1. Revista Iberoamericana de IA con 5 publicaciones
2. Information Sciences con 4 publicaciones
3. Nature Machine Intelligence con 2 publicaciones
"""
        self.agent._invoke_llm = lambda prompt: self.fail("No debe llamar al LLM para preguntas autonomas")

        resolved = self.agent._resolve_question_with_memory(
            "Top 3 publicaciones del año 2024",
            history,
        )

        self.assertEqual(resolved, "Top 3 publicaciones del año 2024")

    def test_generate_answer_falls_back_when_llm_fails_after_results(self) -> None:
        self.agent._invoke_llm = lambda prompt: (_ for _ in ()).throw(RuntimeError("rate limit"))
        state = {
            "question": "De esta primera quienes son los autores?",
            "resolved_question": "¿Quiénes son los autores de la publicación PUB060?",
            "history": "Historial con PUB060.",
            "cypher_query": (
                'MATCH (a:Autor)-[:ESCRIBIO]->(p:`Publicación` {id_publicacion: "PUB060"}) '
                "RETURN a.nombre_autor AS autor, p.id_publicacion AS id_publicacion, p.titulo AS titulo LIMIT 20"
            ),
            "results": [
                {
                    "autor": "Ana Torres",
                    "id_publicacion": "PUB060",
                    "titulo": "Modelos multimodales aplicados a noticias periodísticas",
                },
                {
                    "autor": "Luis Perez",
                    "id_publicacion": "PUB060",
                    "titulo": "Modelos multimodales aplicados a noticias periodísticas",
                },
            ],
            "answer": "",
            "out_of_domain": False,
            "fuzzy_audit": [],
            "semantic_audit": [],
        }

        result = self.agent._generate_answer(state)

        self.assertIn("Modelos multimodales aplicados a noticias periodísticas", result["answer"])
        self.assertIn("Ana Torres", result["answer"])
        self.assertIn("Luis Perez", result["answer"])

    def test_prepare_cypher_corrects_invalid_country_path_through_venue(self) -> None:
        query = (
            "MATCH (p:`Publicación`)-[:PUBLICADA_EN]->(v:Venue)-[:AFILIADO_A]->"
            "(i:`Institución`)-[:UBICADA_EN]->(pais:`País`) "
            'WHERE toLower(pais.pais_institucion) CONTAINS toLower("Perú") '
            "RETURN p.id_publicacion AS id_publicacion, p.titulo AS titulo LIMIT 20"
        )

        normalized = self.agent._prepare_cypher(query)

        self.assertIn("(a:Autor)-[:ESCRIBIO]->(p)", normalized)
        self.assertIn("(a)-[:AFILIADO_A]->(i:`Institución`)-[:UBICADA_EN]->(pais:`País`)", normalized)
        self.assertNotIn("(v:Venue)-[:AFILIADO_A]->", normalized)

    def test_prepare_cypher_corrects_reversed_publication_country_path(self) -> None:
        query = (
            "MATCH (p:`Publicación`)-[:ESCRIBIO]->(a:Autor)-[:AFILIADO_A]->"
            "(i:`Institución`)-[:UBICADA_EN]->(pais:`País`) "
            'WHERE toLower(pais.pais_institucion) CONTAINS toLower("peru") '
            "RETURN p.titulo AS titulo, p.numero_citas AS numero_citas "
            "ORDER BY p.numero_citas DESC LIMIT 3"
        )

        normalized = self.agent._prepare_cypher(query)

        self.assertIn(
            "MATCH (a:Autor)-[:ESCRIBIO]->(p:`Publicación`), "
            "(a)-[:AFILIADO_A]->(i:`Institución`)-[:UBICADA_EN]->(pais:`País`)",
            normalized,
        )
        self.assertNotIn("(p:`Publicación`)-[:ESCRIBIO]->(a:Autor)", normalized)

    def test_prepare_cypher_removes_publication_year_detour_for_country_path(self) -> None:
        query = (
            "MATCH (p:`Publicación`)-[:PUBLICADA_EN_AÑO]->(:`Año`)"
            "<-[:PUBLICADA_EN_AÑO]-(pub:`Publicación`)-[:ESCRIBIO]->(a:Autor)"
            "-[:AFILIADO_A]->(i:`Institución`)-[:UBICADA_EN]->(pais:`País`) "
            'WHERE toLower(pais.pais_institucion) CONTAINS toLower("peru") '
            "RETURN p.titulo AS titulo, pub.numero_citas AS numero_citas "
            "ORDER BY pub.numero_citas DESC LIMIT 3"
        )

        normalized = self.agent._prepare_cypher(query)

        self.assertIn(
            "MATCH (a:Autor)-[:ESCRIBIO]->(p:`Publicación`), "
            "(a)-[:AFILIADO_A]->(i:`Institución`)-[:UBICADA_EN]->(pais:`País`)",
            normalized,
        )
        self.assertIn("p.numero_citas AS numero_citas", normalized)
        self.assertNotIn("PUBLICADA_EN_AÑO", normalized)
        self.assertNotIn("pub.", normalized)

    def test_prepare_cypher_corrects_reversed_venue_country_path(self) -> None:
        query = (
            "MATCH (v:Venue)-[:PUBLICADA_EN]->(p:`Publicacion`)<-[:ESCRIBIO]-(a:Autor)"
            "-[:AFILIADO_A]->(i:`Institucion`)-[:UBICADA_EN]->(pais:`Pais`) "
            'WHERE toLower(pais.pais_institucion) CONTAINS toLower("peru") '
            'AND toLower(v.tipo_venue) CONTAINS toLower("revista") '
            "RETURN v.venue AS revista LIMIT 3"
        )

        normalized = self.agent._prepare_cypher(query)

        self.assertIn(
            "(p:`Publicacion`)-[:PUBLICADA_EN]->(v:Venue), "
            "(a:Autor)-[:ESCRIBIO]->(p), "
            "(a)-[:AFILIADO_A]->(i:`Institucion`)-[:UBICADA_EN]->(pais:`Pais`)",
            normalized,
        )
        self.assertNotIn("(v:Venue)-[:PUBLICADA_EN]->(p:`Publicacion`)", normalized)

    def test_prepare_cypher_corrects_reversed_venue_country_path_with_accented_labels(self) -> None:
        query = (
            "MATCH (v:Venue)-[:PUBLICADA_EN]->(p:`Publicación`)<-[:ESCRIBIO]-(a:Autor)"
            "-[:AFILIADO_A]->(i:`Institución`)-[:UBICADA_EN]->(pais:`País`) "
            'WHERE toLower(pais.pais_institucion) CONTAINS toLower("Perú") '
            'AND toLower(v.tipo_venue) CONTAINS toLower("revista") '
            "RETURN v.venue AS revista LIMIT 3"
        )

        normalized = self.agent._prepare_cypher(query)

        self.assertIn(
            "(p:`Publicación`)-[:PUBLICADA_EN]->(v:Venue), "
            "(a:Autor)-[:ESCRIBIO]->(p), "
            "(a)-[:AFILIADO_A]->(i:`Institución`)-[:UBICADA_EN]->(pais:`País`)",
            normalized,
        )
        self.assertNotIn("(v:Venue)-[:PUBLICADA_EN]->(p:`Publicación`)", normalized)

    def test_prepare_cypher_removes_year_detour_between_venue_and_country(self) -> None:
        query = (
            "MATCH (v:Venue)-[:PUBLICADA_EN]->(p:`Publicación`)-[:PUBLICADA_EN_AÑO]->(:`Año`)"
            "<-[:PUBLICADA_EN_AÑO]-(p2:`Publicación`)-[:ESCRIBIO]->(a:Autor)"
            "-[:AFILIADO_A]->(i:`Institución`)-[:UBICADA_EN]->(pais:`País`) "
            'WHERE toLower(pais.pais_institucion) CONTAINS toLower("peru") '
            'AND toLower(v.tipo_venue) CONTAINS toLower("Revista") '
            "RETURN DISTINCT v.venue AS revista, COUNT(DISTINCT p2.id_publicacion) AS cantidad_publicaciones "
            "ORDER BY cantidad_publicaciones DESC LIMIT 3"
        )

        normalized = self.agent._prepare_cypher(query)

        self.assertIn(
            "(p:`Publicación`)-[:PUBLICADA_EN]->(v:Venue), "
            "(a:Autor)-[:ESCRIBIO]->(p), "
            "(a)-[:AFILIADO_A]->(i:`Institución`)-[:UBICADA_EN]->(pais:`País`)",
            normalized,
        )
        self.assertIn("COUNT(DISTINCT p.id_publicacion) AS cantidad_publicaciones", normalized)
        self.assertNotIn("PUBLICADA_EN_AÑO", normalized)
        self.assertNotIn("p2.", normalized)

    def test_fuzzy_replaces_accentless_country_with_graph_value(self) -> None:
        audit = self.agent._find_best_fuzzy_value("pais_institucion", "peru")

        self.assertIsNotNone(audit)
        self.assertEqual(audit["match"], "Perú")

    def test_semantic_fallback_maps_revista_to_existing_venue_type(self) -> None:
        self.agent._invoke_llm = lambda prompt: json.dumps(
            {
                "can_retry": True,
                "values": ["Revista"],
                "reason": "revista corresponde al tipo de venue Revista.",
            }
        )
        cypher_query = self.agent._prepare_cypher(
            "MATCH (a:Autor)-[:ESCRIBIO]->(p:`Publicacion`)-[:PUBLICADA_EN]->(v:Venue) "
            'WHERE toLower(a.nombre_autor) CONTAINS toLower("Maria Quispe") '
            'AND v.tipo_venue = "revista" '
            "RETURN p.titulo AS titulo LIMIT 20"
        )
        state = {
            "question": "Cuales de estas son revistas?",
            "resolved_question": "Cuales publicaciones de Maria Quispe son revistas?",
            "history": "Historial con publicaciones de Maria Quispe.",
            "cypher_query": cypher_query,
            "results": [],
            "answer": "",
            "out_of_domain": False,
            "fuzzy_audit": [],
            "semantic_audit": [],
        }

        result = self.agent._apply_semantic_fallback(state)

        self.assertEqual(result["results"], [{"titulo": "Fine-tuning de modelos generativos", "tipo_venue": "Revista"}])
        self.assertIn('toLower(v.tipo_venue) CONTAINS toLower("Revista")', result["cypher_query"])
        self.assertEqual(result["semantic_audit"][0]["property"], "tipo_venue")
        self.assertEqual(result["semantic_audit"][0]["values"], ["Revista"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
