# Agente IA para Consultar un Grafo Neo4j de Publicaciones Cientificas

Aplicacion full stack para consultar en lenguaje natural un grafo de publicaciones academicas cargado en Neo4j AuraDB. El frontend React ofrece una interfaz de chat, el backend FastAPI ejecuta un flujo LangGraph que genera Cypher de solo lectura, consulta Neo4j y responde en espanol, y PostgreSQL guarda conversaciones, mensajes y memoria conversacional.

![Modelo de Grafo](./Grafo.png)

## Tabla de Contenidos

- [Arquitectura](#arquitectura)
- [Requisitos Previos](#requisitos-previos)
- [Setup](#setup)
- [Variables de Entorno](#variables-de-entorno)
- [Modelo de Grafo](#modelo-de-grafo)
- [Carga de Datos en Neo4j Aura](#carga-de-datos-en-neo4j-aura)
- [Ejecucion del Proyecto](#ejecucion-del-proyecto)
- [Ejemplos de Queries Cypher](#ejemplos-de-queries-cypher)
- [Funcionamiento del Agente](#funcionamiento-del-agente)
- [Limitaciones](#limitaciones)

## Arquitectura

```text
React + Vite
    |
    | HTTP / Axios
    v
FastAPI
    |
    +-- PostgreSQL local en Docker
    |     - chats
    |     - messages
    |     - chat_memory_summaries
    |
    +-- LangGraph
    |     1. Resuelve referencias con memoria conversacional
    |     2. Genera Cypher de lectura
    |     3. Ejecuta Neo4j
    |     4. Aplica fuzzy match si no hay resultados
    |     5. Genera respuesta final en espanol
    |
    +-- Neo4j AuraDB
          - grafo de publicaciones cientificas
```

En desarrollo local, Docker se usa solamente para PostgreSQL. El backend y el frontend se ejecutan aparte en la maquina local.

## Requisitos Previos

Instala estas herramientas antes de comenzar:

- Git.
- Docker Desktop.
- Python 3.11 o superior.
- Node.js 20 o superior.
- Una cuenta de Neo4j Aura.
- Una API key de Groq para el LLM.

Verifica instalaciones:

```bash
git --version
docker version
python --version
node --version
npm --version
```

## Setup

### 1. Clonar el repositorio

```bash
git clone <URL_DEL_REPOSITORIO>
cd proyecto-agente-neo4j
```

### 2. Levantar PostgreSQL con Docker

Desde la raiz del proyecto:

```bash
docker compose up -d
```

PostgreSQL queda disponible en:

```text
localhost:5432
```

Este servicio guarda el historial del chat y la memoria conversacional. Neo4j no corre en Docker porque se usa Neo4j AuraDB.

Para verificar:

```bash
docker compose ps
```

Para detener la base local:

```bash
docker compose down
```

### 3. Crear y activar el entorno virtual del backend

```bash
cd backend
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Si PowerShell bloquea scripts, puedes ejecutar Python directamente:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Linux/macOS:

```bash
source .venv/bin/activate
```

Instala dependencias:

```bash
pip install -r requirements.txt
```

### 4. Configurar variables del backend

Copia el ejemplo:

```bash
cp .env.example .env
```

En Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Edita `backend/.env` con tus valores reales. No subas este archivo al repositorio.

### 5. Instalar dependencias del frontend

En otra terminal, desde la raiz:

```bash
cd frontend
npm install
```

Copia el ejemplo:

```bash
cp .env.example .env
```

En Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

## Variables de Entorno

### Backend: `backend/.env`

| Variable | Descripcion |
| --- | --- |
| `NEO4J_URI` | URI de conexion de Neo4j AuraDB. Suele tener formato `neo4j+s://<id>.databases.neo4j.io`. |
| `NEO4J_USERNAME` | Usuario de Neo4j AuraDB. En Aura suele ser `neo4j` o el usuario asignado por la instancia. |
| `NEO4J_PASSWORD` | Password de la instancia Neo4j AuraDB. No debe versionarse. |
| `NEO4J_DATABASE` | Nombre de la base de datos Neo4j. En Aura Free normalmente es `neo4j`. |
| `LLM_PROVIDER` | Proveedor LLM usado por el backend. En esta version se usa `groq`. |
| `GROQ_API_KEY` | API key de Groq para generar Cypher y respuestas. |
| `GROQ_MODEL` | Modelo de Groq. Ejemplo: `llama-3.3-70b-versatile`. |
| `DATABASE_URL` | URL de PostgreSQL para SQLAlchemy. Debe coincidir con `docker-compose.yml`. |
| `FRONTEND_ORIGIN` | Origen permitido por CORS para el frontend Vite. Por defecto `http://localhost:5173`. |

Ejemplo sin credenciales reales:

```env
NEO4J_URI=neo4j+s://<AURA_ID>.databases.neo4j.io
NEO4J_USERNAME=<NEO4J_USER>
NEO4J_PASSWORD=<NEO4J_PASSWORD>
NEO4J_DATABASE=neo4j

LLM_PROVIDER=groq
GROQ_API_KEY=<GROQ_API_KEY>
GROQ_MODEL=llama-3.3-70b-versatile

DATABASE_URL=postgresql://postgres:postgres@localhost:5432/agente_neo4j
FRONTEND_ORIGIN=http://localhost:5173
```

### Frontend: `frontend/.env`

| Variable | Descripcion |
| --- | --- |
| `VITE_API_URL` | URL base del backend FastAPI. |

Ejemplo:

```env
VITE_API_URL=http://localhost:8000
```

### Docker Compose: `.env` opcional en la raiz

`docker-compose.yml` permite sobreescribir la configuracion de PostgreSQL con un `.env` en la raiz:

```env
POSTGRES_VERSION=16
POSTGRES_DB=agente_neo4j
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_PORT=5432
```

## Modelo de Grafo

El nodo central del modelo es `Publicación`. Desde ese nodo se navega hacia autores, areas, palabras clave, venue y anio de publicacion. Los autores se conectan con instituciones, y estas con paises.

### Nodos

| Label | Propiedades | Justificacion |
| --- | --- | --- |
| `Publicación` | `id_publicacion`, `titulo`, `numero_citas` | Entidad central del dominio. Permite consultar titulos, identificadores y relevancia mediante citas. |
| `Autor` | `nombre_autor` | Permite buscar produccion cientifica por investigador y analizar colaboraciones. |
| `Institución` | `nombre_institucion` | Representa afiliaciones academicas o de investigacion. |
| `País` | `pais_institucion` | Permite analisis geografico de produccion cientifica. |
| `AreaIA` | `area_ia` | Agrupa publicaciones por area tematica de inteligencia artificial. |
| `PalabraClave` | `palabra_clave` | Facilita busquedas por temas especificos o terminos tecnicos. |
| `Venue` | `venue`, `tipo_venue` | Permite analizar donde se publican los trabajos: conferencia, revista u otro tipo. |
| `Año` | `año` | Permite filtrar y agrupar publicaciones temporalmente. |

### Relaciones

| Relacion | Direccion | Justificacion |
| --- | --- | --- |
| `ESCRIBIO` | `(Autor)-[:ESCRIBIO]->(Publicación)` | Conecta autores con sus publicaciones. |
| `AFILIADO_A` | `(Autor)-[:AFILIADO_A]->(Institución)` | Permite consultar afiliaciones de autores. |
| `UBICADA_EN` | `(Institución)-[:UBICADA_EN]->(País)` | Habilita analisis por pais. |
| `PERTENECE_A` | `(Publicación)-[:PERTENECE_A]->(AreaIA)` | Clasifica publicaciones por area de IA. |
| `TIENE_PALABRA_CLAVE` | `(Publicación)-[:TIENE_PALABRA_CLAVE]->(PalabraClave)` | Permite recuperar publicaciones por terminos tematicos. |
| `PUBLICADA_EN` | `(Publicación)-[:PUBLICADA_EN]->(Venue)` | Conecta publicaciones con su venue. |
| `PUBLICADA_EN_AÑO` | `(Publicación)-[:PUBLICADA_EN_AÑO]->(Año)` | Permite filtrar por anio y construir tendencias temporales. |

Este diseno permite referencias cruzadas. Por ejemplo:

- publicaciones de un autor en un anio especifico;
- publicaciones de un area por pais;
- venues mas frecuentes por palabra clave;
- autores e instituciones asociados a publicaciones con muchas citas.

## Carga de Datos en Neo4j Aura

El repositorio incluye el archivo:

```text
publicaciones_ia.csv
```

Columnas esperadas:

```text
id_publicacion,titulo,numero_citas,nombre_autor,orden_autor,nombre_institucion,pais,area_ia,palabra_clave,venue,tipo_venue,anio
```

### 1. Crear una instancia Neo4j Aura Free

1. Entra a [Neo4j Aura](https://neo4j.com/cloud/platform/aura-graph-database/).
2. Crea una cuenta o inicia sesion.
3. Crea una instancia Aura Free.
4. Guarda de forma segura:
   - URI de conexion;
   - usuario;
   - password;
   - nombre de base de datos.
5. Abre Neo4j Browser desde Aura y confirma que puedes conectarte.

### 2. Crear restricciones recomendadas

En Neo4j Browser, ejecuta:

```cypher
CREATE CONSTRAINT publicacion_id IF NOT EXISTS
FOR (p:`Publicación`)
REQUIRE p.id_publicacion IS UNIQUE;

CREATE CONSTRAINT autor_nombre IF NOT EXISTS
FOR (a:Autor)
REQUIRE a.nombre_autor IS UNIQUE;

CREATE CONSTRAINT institucion_nombre IF NOT EXISTS
FOR (i:`Institución`)
REQUIRE i.nombre_institucion IS UNIQUE;

CREATE CONSTRAINT pais_nombre IF NOT EXISTS
FOR (pais:`País`)
REQUIRE pais.pais_institucion IS UNIQUE;

CREATE CONSTRAINT area_nombre IF NOT EXISTS
FOR (ar:AreaIA)
REQUIRE ar.area_ia IS UNIQUE;

CREATE CONSTRAINT palabra_clave_nombre IF NOT EXISTS
FOR (pc:PalabraClave)
REQUIRE pc.palabra_clave IS UNIQUE;

CREATE CONSTRAINT venue_nombre_tipo IF NOT EXISTS
FOR (v:Venue)
REQUIRE (v.venue, v.tipo_venue) IS UNIQUE;

CREATE CONSTRAINT anio_valor IF NOT EXISTS
FOR (a:`Año`)
REQUIRE a.año IS UNIQUE;
```

### 3. Cargar datos con Neo4j Data Importer

Esta es la opcion mas simple para Aura:

1. Abre [Neo4j Data Importer](https://data-importer.neo4j.io/).
2. Conectalo a tu instancia Aura.
3. Sube `publicaciones_ia.csv`.
4. Crea nodos a partir del mismo CSV:
   - `Publicación`: `id_publicacion`, `titulo`, `numero_citas`.
   - `Autor`: `nombre_autor`.
   - `Institución`: `nombre_institucion`.
   - `País`: mapear columna `pais` a propiedad `pais_institucion`.
   - `AreaIA`: `area_ia`.
   - `PalabraClave`: `palabra_clave`.
   - `Venue`: `venue`, `tipo_venue`.
   - `Año`: mapear columna `anio` a propiedad `año`.
5. Crea las relaciones:
   - `(Autor)-[:ESCRIBIO]->(Publicación)`.
   - `(Autor)-[:AFILIADO_A]->(Institución)`.
   - `(Institución)-[:UBICADA_EN]->(País)`.
   - `(Publicación)-[:PERTENECE_A]->(AreaIA)`.
   - `(Publicación)-[:TIENE_PALABRA_CLAVE]->(PalabraClave)`.
   - `(Publicación)-[:PUBLICADA_EN]->(Venue)`.
   - `(Publicación)-[:PUBLICADA_EN_AÑO]->(Año)`.
6. Ejecuta el import.
7. Valida con:

```cypher
MATCH (p:`Publicación`)
RETURN count(p) AS total_publicaciones;
```

## Ejecucion del Proyecto

### 1. Ejecutar backend

Desde `backend`:

```bash
uvicorn app.main:app --reload
```

O en Windows sin activar el entorno:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Backend:

```text
http://localhost:8000
```

Health checks:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/health/neo4j
```

### 2. Ejecutar frontend

Desde `frontend`:

```bash
npm run dev
```

En Windows PowerShell, si `npm` queda bloqueado por politicas de ejecucion:

```powershell
npm.cmd run dev
```

Frontend:

```text
http://localhost:5173
```

### 3. Flujo esperado

1. Abre el frontend.
2. Crea un chat o usa uno existente.
3. Pregunta en lenguaje natural.
4. El backend guarda el mensaje, resuelve contexto, genera Cypher, consulta Neo4j y responde.

Ejemplos de preguntas:

- `Muestrame publicaciones sobre NLP en 2024`
- `Quiero saber todas las publicaciones de Sophie Laurent`
- `Que año fue?`
- `Cuantas citas tiene?`
- `Cuales son los venues de publicaciones sobre computer vision?`

## Ejemplos de Queries Cypher

### Total de publicaciones

```cypher
MATCH (p:`Publicación`)
RETURN count(p) AS total_publicaciones;
```

### Buscar publicaciones por autor

```cypher
MATCH (a:Autor)-[:ESCRIBIO]->(p:`Publicación`)
WHERE toLower(a.nombre_autor) CONTAINS toLower("Sophie Laurent")
RETURN a.nombre_autor AS autor,
       p.id_publicacion AS id_publicacion,
       p.titulo AS titulo,
       p.numero_citas AS numero_citas
LIMIT 20;
```

### Publicaciones por area y anio

```cypher
MATCH (p:`Publicación`)-[:PERTENECE_A]->(ar:AreaIA)
MATCH (p)-[:PUBLICADA_EN_AÑO]->(anio:`Año`)
WHERE toLower(ar.area_ia) CONTAINS toLower("NLP")
  AND anio.año = 2024
RETURN p.id_publicacion AS id_publicacion,
       p.titulo AS titulo,
       ar.area_ia AS area,
       anio.año AS año_publicacion
LIMIT 20;
```

### Top publicaciones mas citadas

```cypher
MATCH (p:`Publicación`)
RETURN p.id_publicacion AS id_publicacion,
       p.titulo AS titulo,
       p.numero_citas AS numero_citas
ORDER BY p.numero_citas DESC
LIMIT 10;
```

### Publicaciones por pais

```cypher
MATCH (pais:`País`)<-[:UBICADA_EN]-(i:`Institución`)<-[:AFILIADO_A]-(a:Autor)-[:ESCRIBIO]->(p:`Publicación`)
RETURN pais.pais_institucion AS pais,
       count(DISTINCT p) AS total_publicaciones
ORDER BY total_publicaciones DESC
LIMIT 20;
```

### Venues por tipo

```cypher
MATCH (p:`Publicación`)-[:PUBLICADA_EN]->(v:Venue)
WHERE toLower(v.tipo_venue) CONTAINS toLower("conference")
RETURN v.venue AS venue,
       v.tipo_venue AS tipo_venue,
       count(p) AS total_publicaciones
ORDER BY total_publicaciones DESC
LIMIT 20;
```

## Funcionamiento del Agente

El backend usa LangGraph con este flujo:

```text
generate_cypher
   |
   v
execute_cypher
   |
   v
route_after_execute
   |-- si hay resultados --> generate_answer
   |
   |-- si no hay resultados --> fuzzy_match --> generate_answer
```

### Memoria conversacional

El endpoint `POST /chats/{chat_id}/messages` construye una memoria activa antes de llamar al agente:

- ventana deslizante con los ultimos turnos completos;
- resumenes por bloques antiguos guardados en PostgreSQL;
- seleccion de resumenes relevantes por recencia y terminos;
- truncado inteligente para conservar lo mas probable de ser referenciado.

Esto permite preguntas de seguimiento como:

```text
Usuario: Muestrame publicaciones de Sophie Laurent
Usuario: Que año fue?
Usuario: Cuantas citas tiene?
```

### Generacion y validacion de Cypher

El agente:

- genera solo consultas de lectura;
- exige labels y propiedades exactas;
- evita operaciones peligrosas (`CREATE`, `MERGE`, `DELETE`, `SET`, `DROP`, `LOAD CSV`, `CALL`, APOC);
- normaliza algunos errores comunes de Cypher;
- reintenta una correccion si Neo4j devuelve error de sintaxis.

### Fuzzy match

El nodo `fuzzy_match` se ejecuta solo cuando Neo4j no encontro resultados. Compara terminos textuales contra valores existentes del grafo y, si encuentra una coincidencia con similitud mayor o igual a 90%, reintenta la consulta con el valor real.

Ejemplo:

```text
Sophie Lauren -> Sophie Laurent
```

## Limitaciones

### Que quedo fuera y por que

- **Resumenes/abstracts completos de publicaciones**: no se incluyeron para mantener el grafo liviano y porque el CSV no provee ese texto.
- **DOI, URL, ISBN o identificadores externos**: utiles para integracion bibliografica, pero no estan en el dataset base.
- **Coautoria como relacion directa Autor-Autor**: puede derivarse navegando por publicaciones compartidas; se evita duplicar informacion.
- **Orden de autor como propiedad de `ESCRIBIO`**: el CSV contiene `orden_autor`, pero el esquema operativo actual no lo usa en el prompt del agente para mantener las consultas simples. Se puede agregar en una extension.
- **Metadatos personales sensibles de autores**: no se incluyen por privacidad y porque no son necesarios para consultas academicas basicas.
- **Embeddings o busqueda semantica vectorial**: el proyecto usa Cypher y fuzzy match textual; embeddings quedan como mejora futura.
- **Autenticacion de usuarios**: el foco es la prueba tecnica del agente y el grafo, no control de acceso.
- **Backend y frontend en Docker**: Docker se usa solo para PostgreSQL para simplificar desarrollo local.

### Consideraciones operativas

- El LLM puede generar Cypher incorrecto; por eso el backend tiene validacion read-only, normalizacion y reintento de correccion.
- El fuzzy match solo se activa si la consulta inicial no devuelve filas.
- Las credenciales reales deben vivir en `.env`, nunca en Git.

## Endpoints Principales

```text
GET  /health
GET  /health/neo4j
POST /chats
GET  /chats
GET  /chats/{chat_id}
POST /chats/{chat_id}/messages
```

## Estructura del Repositorio

```text
proyecto-agente-neo4j/
├── backend/
│   ├── app/
│   │   ├── agent.py
│   │   ├── config.py
│   │   ├── database.py
│   │   ├── main.py
│   │   ├── models.py
│   │   ├── neo4j_client.py
│   │   ├── prompts.py
│   │   └── routes/
│   ├── .env.example
│   └── requirements.txt
├── frontend/
│   ├── src/
│   ├── .env.example
│   └── package.json
├── docker-compose.yml
├── Grafo.png
├── publicaciones_ia.csv
└── README.md
```

## Comandos Rapidos

Terminal 1:

```bash
docker compose up -d
```

Terminal 2:

```bash
cd backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Terminal 3:

```bash
cd frontend
npm.cmd run dev
```

Abrir:

```text
http://localhost:5173
```
