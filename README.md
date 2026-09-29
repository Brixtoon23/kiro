# corpus-ingesta

Pipeline **determinista** de ingesta de corpus legal para un sistema **RAG de
derecho colombiano** (competencia Hackathon 2026 Uniandes, *"¿Puede un modelo
pequeno responder derecho colombiano?"*). Esta es la **fase de ingesta**: leer
una semilla de normas oficiales `.gov.co`, descargarlas en crudo (raw),
segmentarlas a nivel de articulo y persistirlas en un almacen SQLite listo para
conectar una base vectorial mas adelante.

**No se invoca ningun LLM en tiempo de ejecucion** (requisito del reglamento).
Toda la extraccion es determinista (BeautifulSoup/lxml/regex/reglas). Basta con
correr tres scripts, uno por fase, que leen la semilla y producen el corpus sin
intervencion humana ni de un modelo.

## Que es (y que NO es)

- **Es** la fase de ingesta/extraccion: scraping educado de portales oficiales
  `gov.co`, persistencia raw-first, segmentacion a nivel de articulo y
  almacenamiento en SQLite con trazabilidad completa (fragmento -> norma +
  articulo).
- **No es** todavia la fase de embeddings ni de recuperacion (RAG). Esas fases
  se listan en [Proximas fases](#proximas-fases).
- **No** contiene el banco de preguntas ni las respuestas esperadas de la
  competencia (prohibido por el reglamento). El pipeline nunca las indexa.

## Requisitos

- **Python 3.12** (fijado en `.python-version` y `requires-python`). El
  `python3` del sistema (p. ej. 3.9) **no** debe usarse.
- [`uv`](https://docs.astral.sh/uv/) para gestionar el entorno y las
  dependencias.

## Instalacion (un solo comando)

No hay que instalar nada a mano: un unico comando de arranque prepara el
entorno con el proyecto (editable) y las herramientas de desarrollo/test.

```bash
cd /root/kiro/hackeaton
uv sync --extra dev
```

> `uv sync` a secas instala solo las dependencias de ejecucion. Para poder
> correr las pruebas y el linter usa `uv sync --extra dev` (trae `pytest`,
> `ruff`, `black` y `mypy` dentro del entorno del proyecto). Si hace falta,
> fija la version con `uv python pin 3.12`.

## Las tres fases (un comando por fase)

El pipeline son **tres scripts independientes**, ejecutables uno por uno. Cada
fase expone un console script (`corpus-fase-a/-b/-c`) y su equivalente
`python -m`.

### Fase A — validar la semilla -> semilla limpia

```bash
uv run corpus-fase-a
# equivalente:
uv run python -m corpus_ingesta.phase_a
# comprobacion sin red (carga semilla + cablea adapters, no descarga nada):
uv run python -m corpus_ingesta.phase_a --dry-run
```

Lee `seed_targets.json`, valida cada entrada contra su fuente oficial `.gov.co`
de forma determinista y produce:

- `seed_targets_clean.json` — **solo** las entradas realmente accesibles,
  conservando la forma EXACTA de la semilla (`norma`, `canonico`,
  `items_del_banco`, `areas`, `donde_buscar`).
- `no_encontrados.json` — los descartes, cada uno con `norma`, `canonico`,
  `donde_buscar`, `motivo`, `status_code` y `fecha_consulta`.

Cuando `donde_buscar` es una pagina de busqueda `?q=` (secretariasenado,
suin-juriscol, corteconstitucional), la Fase A **resuelve el buscador**: hace la
peticion educada, parsea los resultados con BeautifulSoup/lxml y **sigue el
primer resultado que coincide con la norma** (por el canonico `[tipo, numero,
anio]` y/o la etiqueta) para obtener la URL directa del documento consolidado,
que es la que queda en `seed_targets_clean.json`. Todo determinista, **sin LLM**.

Nunca fabrica resultados: si el buscador no devuelve un resultado que coincida
con la norma, la entrada se registra honestamente en `no_encontrados.json` con
el motivo (`sin resultados en buscador` / `match ambiguo ...`).

### Fase B — semilla limpia -> descarga raw

```bash
uv run corpus-fase-b
# equivalente:
uv run python -m corpus_ingesta.phase_b
```

Lee `seed_targets_clean.json` y descarga el payload crudo (HTML/PDF) de cada
entrada a `data/raw/<doc_id>/` (`raw.html`|`raw.pdf` + `meta.json`).
**Raw-first**: primero se guardan los bytes originales, no se parsea nada aqui.
El cliente educado respeta >= 1s entre solicitudes, backoff progresivo y un
cache en disco por hash de URL, de modo que **una segunda corrida no vuelve a
descargar** lo ya bajado. Un `FetchError` en una entrada se registra y la
corrida **continua** con la siguiente (un host inalcanzable nunca aborta el
lote).

### Fase C — raw -> SQLite + manifiesto

```bash
uv run corpus-fase-c
# equivalente:
uv run python -m corpus_ingesta.phase_c
```

Recorre `data/raw/<doc_id>/`, parsea cada documento a nivel de articulo y
persiste:

- `data/corpus.sqlite` — metadatos + fragmentos con offsets (columnas
  `vector_id`/`embedding_ref` reservadas en NULL).
- `corpus_manifest.json` — lista de documentos, cada objeto con EXACTAMENTE
  seis claves: `doc_id`, `titulo`, `fuente`, `url`, `fecha_consulta`, `areas`.
- `CORPUS.md` — inventario legible con secciones **Inventario**, **Criterio de
  seleccion** y **Metodo**.

Una norma ausente de su fuente produce una fila `no_encontrado` en `ingest_log`
y **cero** filas de documento/articulo (sin fabricacion).

## Arquitectura y mapa de directorios

```
src/corpus_ingesta/
  phase_a.py         # Fase A: validacion -> semilla limpia
  phase_b.py         # Fase B: descarga raw-first por doc_id
  phase_c.py         # Fase C: raw -> SQLite + manifiesto/CORPUS.md
  seed_io.py         # carga de semilla + ruteo compartido por las fases
  http_client.py     # PoliteClient (spacing, backoff, cache sha256, FetchError)
  db.py              # esquema SQLite + DAO (documents/articles/ingest_log)
  models.py          # SeedTarget / DocumentRecord / ArticleFragment / IngestStatus
  manifest.py        # corpus_manifest.json (6 claves) + CORPUS.md
  adapters/          # un adaptador por portal oficial .gov.co
    base.py          # SourceAdapter (ABC) + helpers deterministas compartidos
    secretariasenado.py
    suin_juriscol.py
    corteconstitucional.py
    lexis_minjusticia.py
    samai_consejoestado.py
tests/               # suite pytest + tests/fixtures/ (HTML/PDF de muestra)
seed_targets.json    # ENTRADA (verbatim); artefactos generados en la raiz al correr
```

### Patron source-adapter (como agregar un portal `.gov.co`)

El ruteo elige el **primer** adapter cuyo `matches(url)` es `True`; todos
comparten la interfaz `SourceAdapter`, asi que agregar un portal oficial no
toca la logica de las fases. Para sumar uno nuevo:

1. Crea `src/corpus_ingesta/adapters/<nuevo>.py` con una subclase de
   `SourceAdapter`; fija `fuente = "<slug>"`.
2. Implementa `matches(url)` (dominio del portal), `parse(raw_bytes, target)`
   (usa `clean_html_to_text` + `split_articles`/`build_fragments` de `base`) y,
   si el portal es de busqueda, sobrescribe `validate(target, client)` o
   `not_found_markers` para la deteccion determinista de "sin resultados".
3. Registralo en `adapters/__init__.py::default_adapters()`.
4. Agrega un fixture en `tests/fixtures/` y un test de parseo.

Todo debe ser determinista: nada de LLM, solo BeautifulSoup/lxml/regex/reglas.

## Esquema SQLite (fuente de verdad de metadatos, listo para vectores)

SQLite es la fuente de verdad de metadatos y offsets a nivel de articulo.

- **`documents`** — metadatos por norma: `doc_id` (PK), `norma`, `tipo`,
  `numero`, `anio`, `titulo`, `fuente`, `url`, `fecha_consulta`,
  `organo_emisor`, `vigencia`, `areas_json`, `status`, `raw_path`,
  `error_detail`.
- **`articles`** — un fragmento por articulo: `article_id` (PK), `doc_id` (FK),
  `articulo_numero`, `encabezado`, `texto`, `offset_start`, `offset_end`, y las
  columnas reservadas **`vector_id`** y **`embedding_ref`**.
- **`ingest_log`** — eventos (`no_encontrado`, `error`, `parseado`) para
  auditoria.

Los `offset_start`/`offset_end` son posiciones de caracter en el texto limpio,
de modo que cada fragmento se puede recortar de vuelta a su articulo exacto
(trazabilidad total).

> **Fila centinela (`offset_start`/`offset_end` = `-1`)**: el fragmento sintetico
> `…:metadata-sentencias` de la Corte (un listado de sentencias, no un tramo del
> texto fuente) se guarda con offsets `-1/-1` para senalar que **no es
> recortable**, en vez de fabricar offsets. Una fase posterior de embeddings que
> asuma que toda fila de `articles` recorta a un tramo de documento debe excluir
> las filas con `offset_start < 0`.

### Como enlazara la base vectorial

`vector_id` y `embedding_ref` quedan en **NULL** durante la ingesta. Una fase
posterior de embeddings poblara estas columnas: `embedding_ref` apuntara al
modelo/coleccion usado y `vector_id` al identificador del vector en la base
vectorial (p. ej. FAISS/Chroma/pgvector). Como el esquema ya las reserva, la
integracion no requiere migracion: cada fila de `articles` en SQLite se vincula
1:1 con su vector, manteniendo la trazabilidad norma + articulo hasta el indice
vectorial.

## Restricciones del reglamento honradas

- **Sin LLM en ejecucion**: los tres scripts son 100% deterministas
  (BeautifulSoup/lxml/regex/reglas); ningun modelo abierto o cerrado participa
  en la extraccion.
- **Sin banco de preguntas**: el corpus no contiene ni indexa el banco de
  preguntas ni las respuestas esperadas de la competencia.
- **Trazabilidad a nivel de articulo**: cada fragmento mapea a `norma` +
  `articulo` mediante offsets de caracter.
- **Nunca fabricar**: una norma inexistente/ inaccesible se registra como
  `no_encontrado` (en `no_encontrados.json` y/o `ingest_log`), jamas se inventa.
- **Manifiesto exacto**: cada objeto de `corpus_manifest.json` tiene
  EXACTAMENTE las seis claves mandatadas.
- **Scraper educado**: >= 1s entre solicitudes, backoff progresivo y cache para
  no re-descargar.

## Matriz de soporte por fuente

| Fuente | Dominio | Soporte | Notas |
| --- | --- | --- | --- |
| Secretaria del Senado | `secretariasenado.gov.co` | **Primario (HTML)** | Parser completo de `ARTICULO N`; la Fase A **resuelve las URLs de busqueda `?q=`** siguiendo el primer resultado que coincide con la norma hasta el `.html` consolidado. |
| SUIN-Juriscol | `suin-juriscol.gov.co` | **Primario (HTML)** | Parser completo; **resolucion determinista del buscador `?q=`** por `[tipo, numero, anio]`; deteccion de "no resultados". |
| Corte Constitucional | `corteconstitucional.gov.co` | Best-effort | Relatoria/jurisprudencia; resolucion del buscador `?q=` por identificador de sentencia. **Limitacion**: el buscador es una SPA Angular que carga resultados via JSON asincrono, por lo que la pagina de aterrizaje puede no exponer enlaces parseables; en ese caso la entrada se marca `no_encontrado` (nunca se fabrica). |
| MinJusticia / Lexis | `lexis.minjusticia.gov.co` | Best-effort | Buscador; parseo aproximado. |
| Consejo de Estado / SAMAI | `samai.consejodeestado.gov.co` | **Opcional (no bloqueante)** | Portal `.aspx`; **limitacion documentada**: se omite sin abortar el resto. |

> El Consejo de Estado (SAMAI, `.aspx`) es una **limitacion opcional y
> documentada**: la Fase A la marca como omision no bloqueante y el resto del
> pipeline continua normalmente.

## La semilla (`seed_targets.json`) y su completitud

`seed_targets.json` contiene `{"seed": 20260830, "documentos": [...]}`.

El archivo trae el corpus completo de **186 entradas** (Constitucion, codigos,
leyes, decretos, acuerdos y jurisprudencia) con sus campos `norma`, `canonico`
`[tipo, numero, anio]`, `items_del_banco`, `areas` y `donde_buscar` (URL
`gov.co` oficial). Incluye deliberadamente **3 referencias nombradas pero
inexistentes** (`Ley 11500 de 2007`, `Ley 1150 de 2005`, `Ley 116 de 2006`):
como el buscador no devuelve un resultado que coincida con esas normas, la
Fase A las registra en `no_encontrados.json` y **nunca fabrica** una URL. El
pipeline procesa **todas** las entradas presentes en `documentos`, sin importar
cuantas sean; para ampliarlo, pega mas normas verbatim tal cual aparecen en la
fuente oficial (no se inventan referencias legales).

> **Resolucion automatica del buscador (Fase A)**: casi todos los
> `donde_buscar` de la semilla son paginas de busqueda `?q=`, p.ej.
> `.../senado/basedoc/?q=Codigo%20general%20proceso`,
> `.../legislacion/?q=Ley%2080%20de%201993` o
> `.../relatoria/?q=Sentencia%20C-355%20de%202006`. La Fase A, de forma
> **determinista y sin ningun LLM**, hace la peticion educada al buscador
> (1s, backoff, cache), parsea la pagina de resultados con BeautifulSoup/lxml y
> **sigue el primer resultado que coincide con la norma** (por el canonico
> `[tipo, numero, anio]` y/o la etiqueta `norma`) para obtener la URL directa
> del documento consolidado (`.html`/`.pdf`). Esa URL resuelta es la que queda
> en `seed_targets_clean.json`. Si el buscador no devuelve resultados o ninguno
> coincide, la entrada se marca `no_encontrado` con el motivo
> (`sin resultados en buscador` / `match ambiguo ...`) y **jamas se inventa una
> URL**. Un `.html` directo (como la Constitucion) se conserva tal cual.

## Verificacion

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest -q
```

Los tests corren completamente **offline**: usan clientes HTTP falsos y
fixtures HTML/PDF versionados en `tests/fixtures/`, y ejercitan las rutas reales
de las tres fases (validacion, descarga raw-first con reuso de cache y
resiliencia ante fallos, y parseo raw -> SQLite con offsets de articulo).

## Proximas fases

Esta ingesta deja el terreno listo para las siguientes fases del sistema RAG:

1. **Embeddings**: calcular vectores por fragmento de articulo (determinista,
   sin usar el banco de preguntas) y poblar `vector_id`/`embedding_ref`.
2. **Indice vectorial**: poblar una base vectorial (FAISS/Chroma/pgvector)
   enlazada al SQLite via `vector_id`, conservando la trazabilidad norma +
   articulo.
3. **Orquestacion RAG**: recuperacion + generacion con **LangChain** y
   **LangGraph** sobre el corpus, con un modelo pequeno como exige la
   competencia. (El reglamento prohibe el LLM solo en la extraccion; la fase de
   respuesta si usa el modelo.)
