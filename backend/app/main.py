from contextlib import asynccontextmanager
import logging
import time

from fastapi import Request
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.logging_config import setup_logging

setup_logging()

from app.database import init_db  # noqa: E402
from app.neo4j_client import neo4j_client
from app.routes import chats, messages

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa tablas al arrancar y cierra Neo4j al apagar la API."""

    logger.info("Iniciando API FastAPI y creando tablas si no existen")
    init_db()
    yield
    logger.info("Apagando API FastAPI y cerrando driver Neo4j")
    neo4j_client.close()


app = FastAPI(title="Agente IA Neo4j", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.frontend_origin,
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chats.router)
app.include_router(messages.router)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Registra cada request para confirmar si el frontend llega al backend."""

    started_at = time.perf_counter()
    logger.info("HTTP request start method=%s path=%s", request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
        logger.exception(
            "HTTP request exception method=%s path=%s elapsed_ms=%s",
            request.method,
            request.url.path,
            elapsed_ms,
        )
        raise

    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
    logger.info(
        "HTTP request end method=%s path=%s status=%s elapsed_ms=%s",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


@app.get("/health")
def health():
    """Healthcheck basico de FastAPI."""

    return {"status": "ok"}


@app.get("/health/neo4j")
def health_neo4j():
    """Healthcheck de conectividad contra Neo4j AuraDB."""

    neo4j_client.verify_connectivity()
    return {"status": "ok", "neo4j": "connected"}
