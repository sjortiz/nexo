# Revisión de diseño: preguntas abiertas

Revisión hecha leyendo el código en el commit `5d61cf9` (19-oct-2026) y verificada de nuevo contra `40e8e34`:
todos los hallazgos siguen vigentes. No se ejecutó el stack completo; los comportamientos descritos salen del código
y de los tests existentes. Las referencias son `archivo:línea` en `40e8e34`.

Este archivo está en la raíz y no en `docs/` porque `tests/test_docs_okf.py` exige que todo `.md` de `docs/` siga el
formato OKF y esté en un índice.

## 1. ¿Qué hace Nexo hoy?

Una CLI con tres pasos principales:

1. `nexo ingest archivo.md`: parte un archivo de texto/Markdown en chunks, los clasifica por sensibilidad, los
   indexa, extrae tripletas, las mapea a la ontología (si hay una configurada), resuelve entidades y escribe el
   grafo léxico y de dominio en Neo4j.
2. `nexo ask "pregunta"`: el router valida la pregunta, el agente Strands investiga sobre el grafo y los chunks, y
   compliance deja la respuesta en pausa hasta la revisión humana.
3. `nexo review <thread_id>`: una persona aprueba o rechaza.

Además: `status`, `eval` y `eval-extraction`.

Lo que todavía no tiene:

| Falta | Detalle |
| --- | --- |
| API o servicio | Solo CLI. |
| Formatos de entrada | `ingest` hace `read_text()` (`cli.py:36`); no lee PDF ni Word. |
| Autenticación | `--clearance` lo elige quien pregunta (`cli.py:165`), aunque `state.py` indica que lo fija una capa de servicio autenticada. `--reviewer` es texto libre (`cli.py:173`). |
| Escala | `LocalChunkStore` (`vector_store.py:110`) es un JSON en un solo nodo (documentado en el README). |
| Borrado | No hay comando para eliminar un documento (sección 2). |

## 2. ¿Qué pasa cuando un documento se actualiza o se elimina?

**Comportamiento actual:** reemplazo parcial por documento (`source_id`), no reconstrucción del grafo.

- `ingest` (`ingest.py:132`) vuelve a extraer **todos** los chunks del documento con el LLM, aunque no hayan
  cambiado. Los ids son deterministas (`schemas.py:103`), pero no hay caché de extracción.
- `replace_source` (`graph_store.py:131`) corre en una sola transacción: `_delete_source` (`graph_store.py:346`)
  quita la procedencia del documento y borra lo que solo él sostenía (incluido el nodo `:Document`); después escribe
  el grafo léxico (`:Document`, `:Chunk`, `PART_OF`, `NEXT_CHUNK`), hace `MERGE` de las tripletas nuevas y
  recalcula los alias de las entidades afectadas.
- Los chunks viejos se borran del almacén vectorial después de escribir en Neo4j (`ingest.py:143`), así que todo
  `chunk_id` del grafo existe en el almacén.

**Problemas:**

1. **No hay borrado expuesto.** La CLI no tiene `delete`, y nada combina el borrado en Neo4j con
   `ChunkStore.delete_source`.
2. **Evidencia obsoleta en relaciones compartidas.** `evidence`, `certainty` y `valid_at` solo se escriben en
   `ON CREATE` (`graph_store.py:334`). Si A y B sostienen la misma relación y A se actualiza, la relación sobrevive
   gracias a B y el `MERGE` no la recrea: queda con la evidencia y la certeza de la versión anterior de A.
3. **Un chunk que falla en la extracción pierde sus relaciones.** `_delete_source` borra toda la procedencia del
   documento antes de escribir, y las tripletas de un chunk fallido no se reescriben. El reporte incluye
   `failed_chunk_ids`, pero el cambio en el grafo ya se aplicó.
4. **La sensibilidad solo sube.** `max_sensitivity_rank` y las etiquetas `:PII`/`:PHI` nunca bajan
   (`graph_store.py:305`), aunque se retire el documento que las causó. Falla hacia el lado seguro, pero no se
   corrige.
5. **Por defecto, la identidad del documento es el nombre del archivo** (`cli.py:28`). `--source-id` permite
   fijarla, pero es opcional: sin él, renombrar el archivo crea un documento nuevo y deja el viejo, y dos archivos
   con el mismo nombre en carpetas distintas se pisan.
6. **Revisiones en pausa con citas obsoletas.** Si un documento cambia mientras una investigación espera revisión,
   sus citas pueden apuntar a chunks borrados. Al reanudar, compliance (`graph.py:176`) no verifica que existan.
7. **Escala.** `_delete_source` recorre todas las relaciones (`graph_store.py:359`); ningún índice cubre los
   arreglos `chunk_ids`.

**Propuestas:** un comando `nexo delete <source_id>`; guardar evidencia y certeza por chunk (ver sección 3); no
borrar la procedencia de los chunks que fallaron en la extracción; recalcular la sensibilidad al retirar fuentes;
hacer `--source-id` obligatorio (o derivarlo de una ruta estable); revalidar las citas al reanudar una revisión.

## 3. ¿Cómo evitar que una relación mal extraída se propague?

**Controles actuales:**

- Esquema validado con Pydantic y temperatura 0.
- Cita literal obligatoria (`ingest.py:191`).
- Ontología opcional (`ontology.py`, `ONTOLOGY_PATH` sin valor por defecto en `config.py:100`) con sinónimos,
  inversos y tipos de origen y destino. `Ontology.map` (`ontology.py:73`) invierte la dirección cuando los tipos solo
  encajan al revés (`ontology.py:81`); lo que no encaja queda como `RELATED_TO {predicate}`.
- Resolución de entidades (`resolution.py`, por defecto `llm` en `config.py:102`): exacta, por alias y con un árbitro
  LLM; cada decisión queda en `MENTIONED_IN {surface, resolution}`.
- Al responder: el agente debe leer y citar el texto, compliance bloquea citas no leídas y hay revisión humana.

**Lo que no detectan:** la cita literal prueba que la cita existe, no que la relación esté bien interpretada.

- Dirección invertida **entre entidades del mismo tipo** (por ejemplo `Organization -> Organization`): los tipos no
  la delatan y la ontología no la corrige.
- Negación o duda perdidas, por ejemplo una cita parcial de "no adquirió" marcada como `asserted`.
- Relación equivocada dentro de los tipos permitidos, o cualquier relación si no se configuró una ontología.
- Fusiones erróneas del árbitro LLM: una fusión mal hecha une el historial de dos entidades en todos los documentos.
  Queda registrada en `MENTIONED_IN`, pero no hay forma de deshacerla sin re-ingerir.

**Por qué se propaga:**

- `judge_faithfulness` (`evaluation.py:93`) solo corre en `nexo eval`, no en el flujo.
- Compliance (`compliance.py:37`) no exige una cita por cada salto de la ruta.
- Rechazar una respuesta no marca la relación: la siguiente pregunta la vuelve a usar.
- No se puede corregir una relación ni una fusión suelta; solo se puede re-ingerir el documento, y el LLM puede
  repetir el error.

**Propuestas, por impacto:**

1. Verificar en la ingesta que la cita implica la relación, con su dirección y certeza (idealmente con otro
   modelo). Lo que no pase se guarda como `unverified` en vez de descartarse.
2. Guardar afirmaciones por chunk (nodos `:Assertion` o un mapa por `chunk_id`) en vez de propiedades fusionadas en
   la relación. También corrige el problema 2.2.
3. Exigir una ontología en producción en vez de dejarla opcional.
4. Correcciones del revisor que perduren: suprimir una relación concreta (chunk + relación) o deshacer una fusión.
   Las lecturas lo respetan y una nueva ingesta también.
5. En compliance: una cita por salto, y `judge_faithfulness` antes de la revisión humana.
6. Correr `nexo eval-extraction` en CI contra un set de referencia cuando cambien el prompt, el modelo o la
   ontología.

## 4. ¿Qué pasa si dos documentos se contradicen sobre una misma entidad?

**Comportamiento actual:** no se detectan las contradicciones; el resultado depende del caso y, a veces, del orden de
ingesta.

| Caso | Resultado |
| --- | --- |
| Misma relación con distinta certeza, fecha o evidencia | Una sola relación por `MERGE`; `chunk_ids` y `extracted_as` acumulan las fuentes; `certainty`, `evidence` y `valid_at` quedan con los valores del **primer** documento ingerido (`graph_store.py:334`). |
| Relaciones que se excluyen (p. ej. dos `REPORTS_TO` distintos) | Conviven sin ninguna marca; `RelationSpec` (`ontology.py:22`) no permite declarar relaciones de un solo valor. |
| `name`, `proposed_codes` | Quedan los del primer documento (`graph_store.py:297`). |
| `description` | Gana la primera que no esté vacía. |
| `type` distinto | Dos entidades separadas: la clave incluye el tipo (`schemas.py:163`) y la resolución trabaja por tipo. |
| Sensibilidad | Gana la más alta. |

**Por qué el agente no lo resuelve:**

- `effective_date` y `doc_type` ahora están en el nodo `:Document` (`graph_store.py:258`), alcanzable con
  `(:Chunk)-[:PART_OF]->(:Document)`, pero nada los usa para comparar fuentes.
- No hay jerarquía de fuentes por `doc_type`.
- El prompt del agente no pide comparar fuentes ni reportar contradicciones.
- Compliance no tiene un chequeo de evidencia contradictoria.

**Propuestas:**

1. Afirmaciones por chunk con evidencia y certeza propias (la misma base que en 3.2).
2. Detectar conflictos en la ingesta (certeza opuesta entre las mismas entidades; relaciones de un solo valor con
   destinos distintos, declaradas en la ontología), marcarlos en el grafo y reportarlos en `IngestionReport`.
3. Una regla configurable para decidir: por `effective_date`, por jerarquía de `doc_type`, o mostrar ambas
   (recomendado por defecto).
4. Indicar en el prompt que presente las dos posturas con sus citas y fechas.
5. Un hallazgo `conflicting_evidence` en compliance (aviso; bloqueo si es PHI).
