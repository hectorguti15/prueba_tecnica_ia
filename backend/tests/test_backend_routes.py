"""HTTP endpoint tests for the FastAPI backend.

These tests use SQLite in memory and stub the agent/Neo4j calls, so the check
does not need Groq, Neo4j AuraDB or PostgreSQL running.

Run from backend/:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import types
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")
os.environ.setdefault("NEO4J_USERNAME", "neo4j")
os.environ.setdefault("NEO4J_PASSWORD", "test-password")
os.environ.setdefault("NEO4J_DATABASE", "neo4j")
os.environ.setdefault("GROQ_API_KEY", "test-groq-key")


fake_agent = types.ModuleType("app.agent")
fake_agent.run_agent = lambda question, history: {
    "answer": "Respuesta simulada del agente.",
    "cypher_query": "MATCH (p:`Publicacion`) RETURN p LIMIT 1",
    "results": [{"titulo": "Publicacion de prueba"}],
    "out_of_domain": False,
}
sys.modules["app.agent"] = fake_agent

fake_neo4j_client_module = types.ModuleType("app.neo4j_client")


class FakeNeo4jClient:
    def verify_connectivity(self) -> None:
        return None

    def close(self) -> None:
        return None


fake_neo4j_client_module.neo4j_client = FakeNeo4jClient()
sys.modules["app.neo4j_client"] = fake_neo4j_client_module


from app import main  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.models import Message  # noqa: E402
from app.routes import messages  # noqa: E402


class BackendEndpointTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        Base.metadata.create_all(bind=self.engine)

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        main.app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(main.app)

        self._original_verify_connectivity = main.neo4j_client.verify_connectivity
        main.neo4j_client.verify_connectivity = lambda: None

    def tearDown(self) -> None:
        main.neo4j_client.verify_connectivity = self._original_verify_connectivity
        main.app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_health_endpoint_returns_ok(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_neo4j_health_endpoint_uses_connectivity_check(self) -> None:
        response = self.client.get("/health/neo4j")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "neo4j": "connected"})

    def test_chat_endpoints_create_list_and_load_chat(self) -> None:
        create_response = self.client.post("/chats", json={"title": "Chat de prueba"})

        self.assertEqual(create_response.status_code, 200)
        created_chat = create_response.json()
        self.assertIsInstance(created_chat["id"], int)
        self.assertEqual(created_chat["title"], "Chat de prueba")
        self.assertIn("created_at", created_chat)
        self.assertIn("updated_at", created_chat)

        list_response = self.client.get("/chats")
        self.assertEqual(list_response.status_code, 200)
        listed_chats = list_response.json()
        self.assertEqual(len(listed_chats), 1)
        self.assertEqual(listed_chats[0]["id"], created_chat["id"])

        detail_response = self.client.get(f"/chats/{created_chat['id']}")
        self.assertEqual(detail_response.status_code, 200)
        chat_detail = detail_response.json()
        self.assertEqual(chat_detail["id"], created_chat["id"])
        self.assertEqual(chat_detail["messages"], [])

    def test_get_chat_endpoint_returns_404_when_chat_does_not_exist(self) -> None:
        response = self.client.get("/chats/999")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Chat no encontrado"})

    def test_message_endpoint_persists_user_and_assistant_messages(self) -> None:
        create_response = self.client.post("/chats", json={"title": "Consulta NLP"})
        chat_id = create_response.json()["id"]
        calls: list[dict[str, str]] = []

        def run_agent_stub(question: str, history: str) -> dict[str, object]:
            calls.append({"question": question, "history": history})
            return {
                "answer": "Hay 2 publicaciones sobre NLP.",
                "cypher_query": (
                    "MATCH (p:`Publicacion`)-[:PERTENECE_A]->(ar:AreaIA) "
                    "RETURN p.titulo AS titulo LIMIT 20"
                ),
                "results": [{"titulo": "NLP Paper 1"}, {"titulo": "NLP Paper 2"}],
                "out_of_domain": False,
            }

        original_run_agent = messages.run_agent
        messages.run_agent = run_agent_stub
        try:
            response = self.client.post(
                f"/chats/{chat_id}/messages",
                json={"content": "Muestrame publicaciones sobre NLP"},
            )
        finally:
            messages.run_agent = original_run_agent

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["question"], "Muestrame publicaciones sobre NLP")
        self.assertEqual(calls[0]["history"], "Sin historial previo.")

        payload = response.json()
        self.assertEqual(payload["user_message"]["role"], "user")
        self.assertEqual(payload["user_message"]["content"], "Muestrame publicaciones sobre NLP")
        self.assertEqual(payload["assistant_message"]["role"], "assistant")
        self.assertEqual(payload["assistant_message"]["content"], "Hay 2 publicaciones sobre NLP.")
        self.assertIn("MATCH", payload["assistant_message"]["cypher_query"])

        chat_response = self.client.get(f"/chats/{chat_id}")
        stored_messages = chat_response.json()["messages"]
        self.assertEqual(len(stored_messages), 2)
        self.assertEqual(stored_messages[0]["role"], "user")
        self.assertEqual(stored_messages[1]["role"], "assistant")

    def test_message_endpoint_returns_404_when_chat_does_not_exist(self) -> None:
        response = self.client.post("/chats/999/messages", json={"content": "Hola"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Chat no encontrado"})

    def test_message_endpoint_rejects_empty_content(self) -> None:
        create_response = self.client.post("/chats", json={"title": "Validacion"})
        chat_id = create_response.json()["id"]

        response = self.client.post(f"/chats/{chat_id}/messages", json={"content": ""})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self._count_messages(), 0)

    def _count_messages(self) -> int:
        db = self.SessionLocal()
        try:
            return db.query(Message).count()
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
