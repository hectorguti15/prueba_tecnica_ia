from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent
# Carga backend/.env de forma explicita y con override=True para que las keys
# del proyecto prevalezcan sobre variables globales antiguas de Windows.
load_dotenv(BASE_DIR / ".env", override=True)


class Settings(BaseSettings):
    """Centraliza la configuracion leida desde backend/.env.

    Pydantic Settings mapea variables como NEO4J_URI o DATABASE_URL a
    atributos Python reutilizables en toda la aplicacion.
    """

    # Credenciales y base Neo4j AuraDB. Deben completarse en backend/.env.
    neo4j_uri: str
    neo4j_username: str
    neo4j_password: str
    neo4j_database: str = "neo4j"

    # URL de PostgreSQL local. El host es localhost porque la API corre fuera de Docker.
    database_url: str = "postgresql://postgres:postgres@localhost:5432/agente_neo4j"

    # Proveedor LLM configurable. Solo se usa la API key del proveedor seleccionado.
    llm_provider: str = "groq"
    groq_api_key: str | None = None
    groq_model: str = "llama-3.3-70b-versatile"

    # Origen permitido para CORS del frontend Vite.
    frontend_origin: str = "http://localhost:5173"

    # Indica a Pydantic que tambien lea backend/.env y que ignore variables extras.
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")


# Instancia unica importada por el resto del backend.
settings = Settings()
