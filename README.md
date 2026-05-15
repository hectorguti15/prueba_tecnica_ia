# Proyecto Agente IA Neo4j

Aplicacion full stack para consultar en lenguaje natural un grafo de publicaciones academicas en Neo4j AuraDB. El usuario conversa desde una interfaz React, el backend FastAPI persiste el historial en PostgreSQL y un flujo LangGraph genera Cypher, consulta Neo4j y redacta una respuesta breve en espanol.

## Arquitectura

```text
React + Vite
    |
    | HTTP / Axios
    v
FastAPI
    |
    +-- PostgreSQL local: chats y messages
    |
    +-- LangGraph:
          1. Recibe pregunta + historial
          2. Genera Cypher con Gemini o Groq
          3. Ejecuta consulta de lectura en Neo4j AuraDB
          4. Genera respuesta natural basada en resultados
    |
    +-- Neo4j AuraDB: grafo ya cargado con Data Importer
```

## Tecnologias

- **FastAPI**: API simple, tipada y rapida para exponer endpoints del agente.
- **LangGraph + LangChain**: flujo explicito para separar generacion de Cypher, ejecucion y respuesta.
- **Neo4j Python Driver**: conexion oficial a Neo4j AuraDB.
- **PostgreSQL + SQLAlchemy**: persistencia relacional de chats y mensajes.
- **Docker Compose**: solo para levantar PostgreSQL local.
- **React + Vite + Axios**: frontend ligero de chat.
- **Gemini o Groq**: proveedor LLM configurable por variables de entorno.

## Flujo del agente

1. El frontend envia un mensaje a `POST /chats/{chat_id}/messages`.
2. FastAPI guarda el mensaje del usuario en PostgreSQL.
3. El backend carga mensajes anteriores del mismo chat y los formatea como historial.
4. LangGraph ejecuta:
   - `generate_cypher`: genera un Cypher de lectura o marca la pregunta como fuera de dominio.
   - `execute_cypher`: ejecuta el query contra Neo4j AuraDB.
   - `generate_answer`: redacta una respuesta natural usando solo los resultados.
5. FastAPI guarda la respuesta del asistente y devuelve ambos mensajes al frontend.

## PostgreSQL

Tablas:

```text
chats
- id
- title
- created_at
- updated_at

messages
- id
- chat_id
- role
- content
- cypher_query
- created_at
```

`messages.chat_id` referencia a `chats.id`.

## Endpoints

- `GET /health`
- `GET /health/neo4j`
- `POST /chats`
- `GET /chats`
- `GET /chats/{chat_id}`
- `POST /chats/{chat_id}/messages`

## Modelo del grafo

El grafo fue cargado previamente en Neo4j AuraDB Free usando Neo4j Data Importer. Este proyecto no implementa la carga automatica del CSV.

```text
(:Pais)<-[:UBICADA_EN]-(:Institucion)<-[:AFILIADO_A]-(:Autor)
                                                        |
                                                        | [:ESCRIBIO {orden_autor}]
                                                        v
(:PalabraClave)<-[:TIENE_PALABRA_CLAVE]-(:Publicacion)-[:PERTENECE_A]->(:AreaIA)
                                            |
                                            +--[:PUBLICADA_EN]->(:Venue {tipo_venue})
                                            |
                                            +--[:PUBLICADA_EN_ANIO]->(:Anio)
```

Labels:

- `Publicacion(id_publicacion, titulo, numero_citas)`
- `Autor(nombre_autor)`
- `Institucion(nombre_institucion)`
- `Pais(nombre)`
- `AreaIA(nombre)`
- `PalabraClave(nombre)`
- `Venue(nombre, tipo_venue)`
- `Anio(valor)`

Relaciones:

- `(Autor)-[:ESCRIBIO {orden_autor}]->(Publicacion)`
- `(Autor)-[:AFILIADO_A]->(Institucion)`
- `(Institucion)-[:UBICADA_EN]->(Pais)`
- `(Publicacion)-[:PERTENECE_A]->(AreaIA)`
- `(Publicacion)-[:TIENE_PALABRA_CLAVE]->(PalabraClave)`
- `(Publicacion)-[:PUBLICADA_EN]->(Venue)`
- `(Publicacion)-[:PUBLICADA_EN_ANIO]->(Anio)`

## Setup

### 1. Levantar PostgreSQL

Desde la raiz del proyecto:

```bash
docker compose up -d
```

PostgreSQL queda disponible en `localhost:5432`.

Si en Windows aparece un error similar a:

```text
unable to get image 'postgres:16': failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine
```

revisar:

- Docker Desktop debe estar abierto y con estado `Docker Desktop is running`.
- El contexto recomendado es `desktop-linux`; se puede verificar con `docker context ls`.
- Si el contexto activo no es `desktop-linux`, ejecutar `docker context use desktop-linux`.
- Verificar el daemon con `docker version`. Debe mostrar secciones `Client` y `Server`.
- Si hay errores de permisos leyendo `C:\Users\<usuario>\.docker\config.json`, abrir la terminal con el mismo usuario que ejecuta Docker Desktop o reiniciar Docker Desktop.

Este Compose solo levanta PostgreSQL. Neo4j no corre en Docker porque el grafo ya esta cargado en Neo4j AuraDB.

Opcionalmente se puede crear un `.env` en la raiz desde `.env.example` para cambiar puerto, usuario, password o version de PostgreSQL:

```env
POSTGRES_VERSION=16
POSTGRES_DB=agente_neo4j
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_PORT=5432
```

### 2. Configurar backend

```bash
cd backend
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Linux/macOS:

```bash
source .venv/bin/activate
```

Instalar dependencias:

```bash
pip install -r requirements.txt
```

Crear `backend/.env` a partir de `backend/.env.example`:

```env
NEO4J_URI=neo4j+s://c21ad175.databases.neo4j.io
NEO4J_USERNAME=c21ad175
NEO4J_PASSWORD=your_neo4j_password
NEO4J_DATABASE=neo4j

LLM_PROVIDER=gemini
GEMINI_API_KEY=your_gemini_api_key
GROQ_API_KEY=your_groq_api_key

DATABASE_URL=postgresql://postgres:postgres@localhost:5432/agente_neo4j
FRONTEND_ORIGIN=http://localhost:5173
```

Pendientes obligatorios antes de arrancar el backend:

- `NEO4J_PASSWORD`: password real de Neo4j AuraDB.
- `GEMINI_API_KEY` si `LLM_PROVIDER=gemini`.
- `DATABASE_URL` debe coincidir con las credenciales usadas por Docker Compose.

Ejecutar API:

```bash
uvicorn app.main:app --reload
```

API: `http://localhost:8000`

### 3. Configurar frontend

En otra terminal:

```bash
cd frontend
npm install
```

Crear `frontend/.env` a partir de `frontend/.env.example`:

```env
VITE_API_URL=http://localhost:8000
```

Ejecutar frontend:

```bash
npm run dev
```

Frontend: `http://localhost:5173`

## Ejemplos de preguntas

- ¿Que publicaciones hay sobre NLP?
- ¿Y de esas cuales son del 2024?
- ¿Cuales son los autores con mas publicaciones?
- ¿Que publicaciones tienen mas citas?
- ¿Que instituciones aparecen en publicaciones sobre computer vision?
- ¿Cuantas publicaciones hay por pais?
- ¿Que venues son conferencias?

## Fuera de alcance en esta version

Esta primera version se enfoca en cumplir los requerimientos obligatorios end-to-end. Quedan fuera por ahora:

- Guardrails avanzados.
- Bloqueo completo de prompt injection.
- Validacion semantica profunda del Cypher.
- Auto-correccion con multiples reintentos.
- Summary memory.
- Autenticacion de usuarios.
- Docker para Neo4j.
- Despliegue en produccion.
- Diseno visual avanzado.
- Tests complejos.
- Sistema multiusuario.
