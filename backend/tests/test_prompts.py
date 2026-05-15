"""Prompt formatting regression tests.

These tests catch unescaped Cypher/JSON braces inside prompt templates before
they can fail at runtime with KeyError.
"""

from __future__ import annotations

import unittest

from app import prompts


class PromptFormattingTestCase(unittest.TestCase):
    def test_context_resolution_prompt_formats(self) -> None:
        formatted = prompts.CONTEXT_RESOLUTION_PROMPT.format(
            history="Sin historial previo.",
            question="Que publicaciones hay sobre NLP?",
        )

        self.assertIn('"standalone_question"', formatted)

    def test_cypher_generation_prompt_formats_with_literal_cypher_maps(self) -> None:
        formatted = prompts.CYPHER_GENERATION_PROMPT.format(
            schema=prompts.GRAPH_SCHEMA,
            history="Sin historial previo.",
            question="Dame el top 3 de publicaciones relacionadas a IA generativa",
        )

        self.assertIn('{id_publicacion: "PUB001"}', formatted)
        self.assertIn('{id_publicacion: "ID_PUBLICACION"}', formatted)
        self.assertIn('"libros", "papers", "articulos"', formatted)
        self.assertIn("ordena por p.numero_citas DESC", formatted)

    def test_cypher_correction_prompt_formats_with_literal_cypher_maps(self) -> None:
        formatted = prompts.CYPHER_CORRECTION_PROMPT.format(
            schema=prompts.GRAPH_SCHEMA,
            question="Cuales son los autores de PUB099?",
            cypher_query='MATCH (p:`Publicación` {id_publicacion: "PUB099"})-[:ESCRIBIO]->(a:Autor)',
            error="Sin error de sintaxis; consulta sin resultados.",
        )

        self.assertIn('{id_publicacion: "PUB001"}', formatted)

    def test_semantic_fallback_prompt_formats_json_examples(self) -> None:
        formatted = prompts.SEMANTIC_FALLBACK_PROMPT.format(
            question="Dame publicaciones sobre Inteligencia Artificial",
            property="area_ia",
            term="Inteligencia Artificial",
            candidates='["IA Generativa", "Machine Learning"]',
        )

        self.assertIn('"can_retry": true', formatted)
        self.assertIn('"can_retry": false', formatted)

    def test_answer_prompt_formats(self) -> None:
        formatted = prompts.ANSWER_PROMPT.format(
            question="Dame el top 3",
            resolved_question="Dame el top 3",
            cypher_query="MATCH (p:`Publicación`) RETURN p LIMIT 3",
            results='[{"titulo": "Ejemplo"}]',
        )

        self.assertIn("Resultados de Neo4j", formatted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
