"""Manifest generation: corpus_manifest.json + CORPUS.md.

``corpus_manifest.json`` is a LIST where each object has EXACTLY these keys
(reglamento requirement): ``doc_id``, ``titulo``, ``fuente``, ``url``,
``fecha_consulta``, ``areas`` -- no more, no fewer.

``CORPUS.md`` is a human-readable inventory: a table of documents with status,
plus the selection *criterio* and the *metodo* (deterministic scraping, no LLM,
article-level segmentation, full traceability).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# The exact, ordered set of manifest keys mandated by the reglamento.
MANIFEST_FIELDS = ("doc_id", "titulo", "fuente", "url", "fecha_consulta", "areas")


def _load_documents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT doc_id, titulo, fuente, url, fecha_consulta, areas_json, "
            "status, norma FROM documents ORDER BY doc_id"
        )
    )


def build_manifest(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Return the manifest as a list of dicts with EXACTLY the 6 required keys."""
    manifest: list[dict[str, object]] = []
    for row in _load_documents(conn):
        areas = json.loads(row["areas_json"]) if row["areas_json"] else []
        manifest.append(
            {
                "doc_id": row["doc_id"],
                "titulo": row["titulo"],
                "fuente": row["fuente"],
                "url": row["url"],
                "fecha_consulta": row["fecha_consulta"],
                "areas": areas,
            }
        )
    return manifest


def write_manifest_json(conn: sqlite3.Connection, path: str) -> list[dict[str, object]]:
    """Write ``corpus_manifest.json`` and return the manifest list."""
    manifest = build_manifest(conn)
    Path(path).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def _md_escape(value: object) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def write_corpus_md(conn: sqlite3.Connection, path: str) -> None:
    """Write ``CORPUS.md`` with an inventory table + criterio + metodo sections."""
    rows = _load_documents(conn)
    art_counts = {
        r["doc_id"]: r["n"]
        for r in conn.execute(
            "SELECT doc_id, COUNT(*) AS n FROM articles GROUP BY doc_id"
        )
    }

    lines: list[str] = []
    lines.append("# Corpus de derecho colombiano (fase de ingesta)")
    lines.append("")
    lines.append(
        "Inventario generado automaticamente por el pipeline determinista "
        "`corpus-ingesta`. No interviene ningun LLM en la construccion del corpus."
    )
    lines.append("")
    lines.append("## Inventario")
    lines.append("")
    lines.append("| doc_id | norma | fuente | estado | articulos | url |")
    lines.append("| --- | --- | --- | --- | ---: | --- |")
    for r in rows:
        n = art_counts.get(r["doc_id"], 0)
        lines.append(
            "| {doc_id} | {norma} | {fuente} | {status} | {n} | {url} |".format(
                doc_id=_md_escape(r["doc_id"]),
                norma=_md_escape(r["norma"]),
                fuente=_md_escape(r["fuente"]),
                status=_md_escape(r["status"]),
                n=n,
                url=_md_escape(r["url"]),
            )
        )
    if not rows:
        lines.append("| (vacio) | | | | 0 | |")
    lines.append("")

    lines.append("## Criterio de seleccion")
    lines.append("")
    lines.append(
        "- Solo fuentes oficiales y publicas del dominio `gov.co` "
        "(Secretaria del Senado, SUIN-Juriscol, Corte Constitucional, "
        "MinJusticia/Lexis, Consejo de Estado/SAMAI)."
    )
    lines.append(
        "- Las normas y referencias provienen de la semilla (`seed_targets.json`); "
        "no se inventan referencias legales."
    )
    lines.append(
        "- No se incluye el banco de preguntas ni respuestas esperadas de la "
        "competencia."
    )
    lines.append("")

    lines.append("## Metodo")
    lines.append("")
    lines.append(
        "- **Determinista, sin LLM**: la extraccion usa unicamente un navegador "
        "headless (Playwright, clics fijos) para LEXIS, python-docx para el "
        "texto Word, y BeautifulSoup/lxml + expresiones regulares para HTML/PDF."
    )
    lines.append(
        "- **Raw-first**: se guardan los bytes originales (HTML/PDF/DOCX) en "
        "`data/raw/<doc_id>/` antes de parsear; el cache en disco evita "
        "re-descargas (incluido el `.docx` de LEXIS ya bajado)."
    )
    lines.append(
        "- **Scraper educado**: 1 segundo entre solicitudes y backoff progresivo "
        "ante fallos."
    )
    lines.append(
        "- **Segmentacion a nivel de articulo**: se divide en los limites "
        "`ARTICULO N`, calculando `offset_start`/`offset_end` sobre el texto "
        "limpio para trazabilidad completa (fragmento -> norma + articulo)."
    )
    lines.append(
        "- **Trazabilidad y no fabricacion**: cada fragmento referencia su norma "
        "y articulo; cuando una norma no esta en la fuente se registra "
        "`status='no_encontrado'` y no se generan filas de documento/articulo."
    )
    lines.append(
        "- **Preparado para vectores**: la tabla `articles` reserva columnas "
        "`vector_id`/`embedding_ref` para integrar una base vectorial mas "
        "adelante sin migracion."
    )
    lines.append("")

    Path(path).write_text("\n".join(lines), encoding="utf-8")


def write_all(conn: sqlite3.Connection, *, manifest_path: str, corpus_md_path: str) -> None:
    """Write both manifest artifacts."""
    write_manifest_json(conn, manifest_path)
    write_corpus_md(conn, corpus_md_path)
