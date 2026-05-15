# Modelo de Grafo

El grafo fue cargado previamente en Neo4j AuraDB Free usando Neo4j Data Importer a partir del archivo `publicaciones_ia.csv`.

## Nodos

- `Publicacion(id_publicacion, titulo, numero_citas)`
- `Autor(nombre_autor)`
- `Institucion(nombre_institucion)`
- `Pais(nombre)`
- `AreaIA(nombre)`
- `PalabraClave(nombre)`
- `Venue(nombre, tipo_venue)`
- `Anio(valor)`

## Relaciones

- `(Autor)-[:ESCRIBIO {orden_autor}]->(Publicacion)`
- `(Autor)-[:AFILIADO_A]->(Institucion)`
- `(Institucion)-[:UBICADA_EN]->(Pais)`
- `(Publicacion)-[:PERTENECE_A]->(AreaIA)`
- `(Publicacion)-[:TIENE_PALABRA_CLAVE]->(PalabraClave)`
- `(Publicacion)-[:PUBLICADA_EN]->(Venue)`
- `(Publicacion)-[:PUBLICADA_EN_ANIO]->(Anio)`

## Diagrama textual

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

## Decisiones de modelado

- `orden_autor` es propiedad de `ESCRIBIO` porque depende de la publicacion concreta.
- `numero_citas` es propiedad de `Publicacion` porque representa impacto academico.
- `tipo_venue` es propiedad de `Venue` porque clasifica el venue sin requerir un nodo propio.
- `Anio` es nodo para facilitar consultas temporales y agregaciones por periodo.
