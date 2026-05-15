# Backend

Backend FastAPI para consultar un grafo Neo4j AuraDB mediante un agente LangGraph y persistir chats en PostgreSQL.

## Setup

1. Crear entorno virtual:

```bash
python -m venv .venv
source .venv/bin/activate
```

En Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Instalar dependencias:

```bash
pip install -r requirements.txt
```

3. Crear `.env` desde `.env.example` y completar credenciales.

Variables pendientes:

- `NEO4J_PASSWORD`: password real de AuraDB.
- `GEMINI_API_KEY` si `LLM_PROVIDER=gemini`.
- `DATABASE_URL`: debe coincidir con PostgreSQL levantado por Docker Compose.

4. Levantar API:

```bash
uvicorn app.main:app --reload
```

La API quedara disponible en `http://localhost:8000`.
