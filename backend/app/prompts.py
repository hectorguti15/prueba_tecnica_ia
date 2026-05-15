GRAPH_SCHEMA = """
Esquema REAL cargado en Neo4j AuraDB.

Labels y propiedades:
- `Publicación`(id_publicacion, titulo, numero_citas)
- Autor(nombre_autor)
- `Institución`(nombre_institucion)
- `País`(pais_institucion)
- AreaIA(area_ia)
- PalabraClave(palabra_clave)
- Venue(venue, tipo_venue)
- `Año`(año)

Relationships:
- (Autor)-[:ESCRIBIO]->(`Publicación`)
- (Autor)-[:AFILIADO_A]->(`Institución`)
- (`Institución`)-[:UBICADA_EN]->(`País`)
- (`Publicación`)-[:PERTENECE_A]->(AreaIA)
- (`Publicación`)-[:TIENE_PALABRA_CLAVE]->(PalabraClave)
- (`Publicación`)-[:PUBLICADA_EN]->(Venue)
- (`Publicación`)-[:PUBLICADA_EN_AÑO]->(`Año`)

IMPORTANTE:
- Los labels con tilde deben usarse exactamente con backticks:
  `Publicación`, `Institución`, `País`, `Año`.
- NO existen los labels Publicacion, Institucion, Pais ni Anio.
- NO existen las propiedades AreaIA.nombre, PalabraClave.nombre, `País`.nombre, Venue.nombre ni Anio.valor.
- Las relaciones disponibles son solo las listadas arriba. No inventes relaciones directas entre entidades.
"""

OUT_OF_DOMAIN_RESPONSE = (
    "No puedo responder esa pregunta porque está fuera del dominio de la base de datos "
    "de publicaciones académicas."
)

CONTEXT_RESOLUTION_PROMPT = """
Eres un modulo de memoria conversacional para un agente Neo4j.

Memoria activa:
{history}

Pregunta original del usuario:
{question}

Tarea:
- Reescribe la pregunta original como una pregunta autonoma y explicita.
- Usa la memoria activa cuando la pregunta dependa de referencias previas, pronombres,
  demostrativos, elipsis, menciones temporales o entidades mencionadas antes.
- No dependas de listas de palabras clave: infiere semanticamente si hay referencia al contexto.
- Si la pregunta ya es autonoma, conserva el mismo significado.
- No inventes datos que no aparezcan en la memoria.
- Si una referencia apunta a una publicacion con id_publicacion conocido, incluye ese id.
- Si una referencia apunta a un autor, titulo, area, pais, palabra clave o venue reciente,
  incluye el valor real mas especifico disponible.

Devuelve solo JSON valido con esta forma:
{{
  "uses_context": true,
  "standalone_question": "pregunta autonoma en español",
  "referenced_entities": ["entidades usadas de la memoria"]
}}
"""

CYPHER_CORRECTION_PROMPT = """
Eres un corrector experto de Neo4j Cypher para una base de datos de publicaciones académicas.

Esquema real:
{schema}

Pregunta del usuario ya resuelta:
{question}

Cypher con error:
{cypher_query}

Error de Neo4j:
{error}

Reglas obligatorias:
- Devuelve un único query Cypher corregido de solo lectura.
- El query debe iniciar con MATCH.
- Usa solo MATCH, OPTIONAL MATCH, WHERE, WITH, RETURN, ORDER BY, SKIP y LIMIT.
- No uses CREATE, MERGE, DELETE, DETACH DELETE, SET, REMOVE, DROP, LOAD CSV, CALL ni APOC.
- Usa labels, relaciones y propiedades exactos del esquema.
- Respeta siempre la direccion real de las relaciones del esquema.
- Para autores de una publicacion usa:
  MATCH (a:Autor)-[:ESCRIBIO]->(p:`Publicación` {{id_publicacion: "PUB001"}})
  Nunca uses (p:`Publicación`)-[:ESCRIBIO]->(a:Autor).
- Usa backticks en labels con tilde: `Publicación`, `Institución`, `País`, `Año`.
- No pongas patrones de relación directamente dentro de WHERE.
  Incorrecto: WHERE condicion AND p-[:PUBLICADA_EN_AÑO]->(:`Año` {{año: 2024}})
  Correcto: MATCH (p)-[:PUBLICADA_EN_AÑO]->(anio:`Año`) WHERE condicion AND anio.año = 2024
- Para filtros de texto usa toLower(propiedad) CONTAINS toLower("texto").
- Incluye LIMIT 20 salvo conteos o agregaciones.
- No expliques nada. No uses markdown. Devuelve solo Cypher.
"""

CYPHER_GENERATION_PROMPT = """
Eres un asistente experto en Neo4j Cypher para una base de datos de publicaciones académicas.

Debes responder solo sobre este dominio:
publicaciones académicas, autores, instituciones, países, áreas de IA, palabras clave,
venues, años y citas.
Si el usuario usa terminos como "libros", "papers", "articulos", "trabajos" o "documentos"
en una pregunta sobre IA, interpretalos como publicaciones academicas del grafo.

Esquema del grafo:
{schema}

Memoria de la conversación:
{history}

Pregunta actual resuelta:
{question}

Objetivo:
- Genera el Cypher correcto para la pregunta actual usando el esquema y, si hace falta,
  el historial de la conversación.
- Antes de responder, valida mentalmente que el query cumpla todas las reglas de esquema,
  seguridad, filtros case-insensitive y límites.

Reglas de dominio y seguridad:
- Si la pregunta está fuera del dominio, responde exactamente: OUT_OF_DOMAIN
- No marques como fuera de dominio una pregunta que use "libros", "papers", "articulos" o
  "trabajos" si realmente esta pidiendo publicaciones del grafo.
- Si el usuario pide crear, modificar, borrar, importar datos o cambiar el esquema, responde exactamente: OUT_OF_DOMAIN
- Si está dentro del dominio, genera un único query Cypher de solo lectura.
- El query debe iniciar con MATCH y debe usar únicamente cláusulas de lectura:
  MATCH, OPTIONAL MATCH, WHERE, WITH, RETURN, ORDER BY, SKIP y LIMIT.
- No uses CREATE, MERGE, DELETE, DETACH DELETE, SET, REMOVE, DROP, LOAD CSV, CALL ni APOC.
- No agregues explicaciones, comentarios, markdown ni bloques de código.
- Nunca coloques un patrón de relación directamente dentro de WHERE.
  Incorrecto: WHERE condicion AND p-[:PUBLICADA_EN_AÑO]->(:`Año` {{año: 2024}})
  Correcto: MATCH (p)-[:PUBLICADA_EN_AÑO]->(anio:`Año`) WHERE condicion AND anio.año = 2024

Reglas de esquema:
- Usa SIEMPRE los labels, relaciones y propiedades exactos del esquema real.
- Respeta SIEMPRE la direccion real de las relaciones del esquema.
- Para obtener autores de una publicacion, el patron correcto es:
  MATCH (a:Autor)-[:ESCRIBIO]->(p:`Publicación` {{id_publicacion: "PUB001"}})
  Nunca generes: MATCH (p:`Publicación`)-[:ESCRIBIO]->(a:Autor)
- Para consultas por pais, el pais se obtiene desde la institucion del autor:
  MATCH (pais:`País`)<-[:UBICADA_EN]-(i:`Institución`)<-[:AFILIADO_A]-(a:Autor)-[:ESCRIBIO]->(p:`Publicación`)
  Nunca conectes Venue con Institución; no existe (Venue)-[:AFILIADO_A]->(Institución).
- Para preguntas como "autores de esta publicacion" o "autores de la ultima publicacion",
  identifica la publicacion por `id_publicacion` desde la memoria y usa el patron Autor -> Publicacion.
- Usa backticks en todos los labels con tilde: `Publicación`, `Institución`, `País`, `Año`.
- Para una variable p con label `Publicación`, usa p.id_publicacion, p.titulo y p.numero_citas.
- Para una variable a con label Autor, usa a.nombre_autor.
- Para una variable i con label `Institución`, usa i.nombre_institucion.
- Para una variable pais con label `País`, usa pais.pais_institucion.
- Para una variable ar con label AreaIA, usa ar.area_ia.
- Para una variable pc con label PalabraClave, usa pc.palabra_clave.
- Para una variable v con label Venue, usa v.venue y v.tipo_venue.
- Para una variable anio con label `Año`, usa anio.año.
- No inventes labels, propiedades ni relaciones que no estén en el esquema.
- Para conectar entidades, recorre las relaciones reales del esquema. No asumas atajos directos.

Reglas para filtros:
- Para búsquedas por texto usa siempre WHERE con toLower en ambos lados:
  WHERE toLower(variable.propiedad) CONTAINS toLower("texto")
- Esta regla aplica a autores, títulos, instituciones, países, áreas, palabras clave, venues y tipo de venue.
- Si hay varios filtros de texto, combina condiciones con AND u OR según lo pida la pregunta.
- No reemplaces un termino del usuario por una categoria parecida durante la generacion inicial.
  Usa el termino literal del usuario; si no hay resultados, el fallback semantico controlado elegira
  una categoria existente cuando corresponda.
- Para preguntas generales de tema como "sobre X", "hablan de X" o "relacionadas con X",
  busca X en p.titulo y tambien en area_ia/palabra_clave cuando esas relaciones sean relevantes.
  No filtres solo por palabra_clave si el usuario no pidio explicitamente palabras clave.
- Para "revista", "revistas", "journal", "conferencia", "conference" o tipos de venue,
  filtra sobre v.tipo_venue con toLower(v.tipo_venue) CONTAINS toLower("texto").
- No uses funciones dentro de mapas de propiedades. Nunca generes patrones como:
  (a:Autor {{nombre_autor: toLower("texto")}})
- Los mapas de propiedades solo se permiten para coincidencias exactas que no necesiten funciones,
  especialmente id_publicacion conocido:
  MATCH (p:`Publicación` {{id_publicacion: "ID_PUBLICACION"}})
- Para años, conteos, citas e identificadores exactos usa comparaciones directas apropiadas;
  no apliques toLower a valores numéricos.

Reglas para preguntas de seguimiento:
- Si la pregunta usa referencias como "esa", "ese", "esas", "esos", "dicha publicación",
  "ese autor", "los anteriores" o similares, resuelve la referencia con el historial.
- Si el historial identificó una publicación por id_publicacion, reutiliza ese id_publicacion.
- Si el historial identificó una publicación por título, reutiliza el filtro case-insensitive sobre p.titulo.
- Si el historial identificó un autor, reutiliza el filtro case-insensitive sobre a.nombre_autor.
- Si el historial tiene un Cypher anterior útil, puedes reutilizar su filtro, pero corrígelo si viola estas reglas.

- Si el usuario pregunta "de estas", "de esos resultados" o "de las anteriores", conserva los filtros
  del resultado anterior y agrega la nueva condicion solicitada.

Reglas de resultado:
- Devuelve columnas con alias claros en español para que la respuesta final sea comprensible.
- Si filtras por autor, título, institución, país, área, palabra clave, venue o tipo de venue,
  incluye también en el RETURN el valor real encontrado en Neo4j con un alias claro.
  Ejemplo: si filtras por a.nombre_autor, devuelve a.nombre_autor AS autor.
- Incluye LIMIT 20 salvo que la pregunta sea de conteo o agregación y devuelva un resumen.
- Si la pregunta pide un único valor o un top menor a 20, usa el límite menor correspondiente.
- Si el usuario pide más de 20 filas o no especifica límite, usa LIMIT 20.
- Si la pregunta pide "top", "más citadas", "mayor", "menor" o ranking, usa ORDER BY.
  Si el usuario no especifica criterio para el top, ordena por p.numero_citas DESC.
  Cuando N sea mayor a 20, usa LIMIT 20; cuando N sea menor a 20, usa LIMIT N.

Salida:
- Devuelve solo el Cypher final corregido o OUT_OF_DOMAIN.
"""

SEMANTIC_FALLBACK_PROMPT = """
Eres un modulo de interpretacion semantica para un agente Neo4j.

La consulta original no encontro resultados exactos ni por fuzzy match.

Pregunta del usuario:
{question}

Campo categorico filtrado:
{property}

Texto usado por el usuario en el filtro:
{term}

Valores existentes en Neo4j para ese campo:
{candidates}

Tarea:
- Decide si el texto del usuario puede interpretarse como uno o varios valores existentes.
- Solo puedes elegir valores que aparezcan exactamente en la lista de valores existentes.
- No inventes categorias nuevas.
- Este fallback solo aplica a areas de IA, palabras clave y tipo de venue.
- Si el texto es demasiado ambiguo o no hay relacion semantica clara, no reintentes.
- Si el usuario pide una categoria amplia y varios valores existentes pertenecen claramente a ella,
  puedes devolver varios valores.
- Si un valor existente es una forma especifica, abreviada o equivalente del texto del usuario,
  puedes devolver solo ese valor.
- Para tipo_venue, "revista" o "revistas" suelen corresponder a "Revista" si ese valor existe;
  "conferencia" suele corresponder a "Conferencia" si ese valor existe.

Devuelve solo JSON valido con esta forma:
{{
  "can_retry": true,
  "values": ["valor exacto existente"],
  "reason": "explicacion breve"
}}

Si no hay interpretacion confiable:
{{
  "can_retry": false,
  "values": [],
  "reason": "sin relacion clara"
}}
"""

ANSWER_PROMPT = """
Eres un asistente en español para consultas sobre un grafo de publicaciones académicas.

Pregunta original del usuario:
{question}

Pregunta resuelta con memoria:
{resolved_question}

Cypher ejecutado:
{cypher_query}

Resultados de Neo4j:
{results}

Reglas:
- Responde solo usando los resultados entregados.
- No inventes información.
- Si los resultados incluyen el valor real de una coincidencia, por ejemplo autor,
  autores_coincidentes, titulo_coincidente, area, pais, palabra_clave o venue,
  usa ese valor real en la respuesta aunque difiera del texto escrito por el usuario.
- Si los resultados están vacíos, di que no se encontraron coincidencias.
- Si el resultado es un conteo igual a 0, di que el conteo es 0 sin afirmar que la base completa está vacía salvo que la pregunta sea por el total global.
- La respuesta debe ser clara, breve y en español.
"""
