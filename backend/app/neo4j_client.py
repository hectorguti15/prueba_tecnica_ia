from typing import Any
import logging

from neo4j import GraphDatabase

from app.config import settings

logger = logging.getLogger(__name__)


class Neo4jClient:
    """Cliente pequeno para Neo4j AuraDB usando el driver oficial de Python."""

    def __init__(self) -> None:
        """Crea el driver con credenciales leidas desde backend/.env."""

        logger.info("Creando driver Neo4j uri_configured=%s database=%s", bool(settings.neo4j_uri), settings.neo4j_database)
        self._driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, settings.neo4j_password),
        )
        self._database = settings.neo4j_database

    def close(self) -> None:
        """Cierra el driver al apagar FastAPI para liberar conexiones."""

        self._driver.close()

    def verify_connectivity(self) -> None:
        """Verifica que AuraDB acepte conexion con las credenciales actuales."""

        logger.info("Verificando conectividad Neo4j")
        self._driver.verify_connectivity()
        logger.info("Conectividad Neo4j OK")

    def execute_read_query(
        self,
        cypher_query: str,
        parameters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Ejecuta una consulta Cypher de lectura y devuelve filas serializables."""

        query = cypher_query.strip()
        if not _is_read_only_query(query):
            logger.warning("Cypher rechazado por validacion read-only preview=%s", query[:180].replace("\n", " "))
            raise ValueError("Solo se permiten consultas Cypher de lectura.")

        logger.info("Ejecutando Cypher read query preview=%s", query[:180].replace("\n", " "))
        with self._driver.session(database=self._database) as session:
            result = session.run(query, parameters or {})
            rows = [_serialize_record(record.data()) for record in result]
            logger.info("Cypher ejecutado rows=%s", len(rows))
            return rows


def _is_read_only_query(query: str) -> bool:
    """Aplica una validacion basica para evitar operaciones de escritura."""

    normalized = query.strip().lower()
    forbidden = ("create ", "merge ", "delete ", "detach ", "set ", "remove ", "drop ", "load csv")
    return normalized.startswith(("match ", "with ", "call ")) and not any(word in normalized for word in forbidden)


def _serialize_record(value: Any) -> Any:
    """Convierte valores del driver Neo4j en estructuras JSON compatibles."""

    if isinstance(value, dict):
        return {key: _serialize_record(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_serialize_record(item) for item in value]
    if hasattr(value, "items"):
        return dict(value.items())
    if hasattr(value, "iso_format"):
        return value.iso_format()
    return value


# Instancia compartida por rutas y agente.
neo4j_client = Neo4jClient()
