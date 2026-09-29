"""SQLite persistence: schema + small DAO layer.

The SQLite database is the source of truth for document metadata and
article-level offsets. It is intentionally designed so a future vector DB can
plug in without a migration: ``articles`` carries reserved, NULLable
``vector_id`` and ``embedding_ref`` columns that stay empty during the
ingestion phase.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence

from .models import ArticleFragment, DocumentRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id         TEXT PRIMARY KEY,
    norma          TEXT NOT NULL,
    tipo           TEXT,
    numero         TEXT,
    anio           TEXT,
    titulo         TEXT,
    fuente         TEXT,
    url            TEXT,
    fecha_consulta TEXT,
    organo_emisor  TEXT,
    vigencia       TEXT,
    areas_json     TEXT,
    status         TEXT NOT NULL,
    raw_path       TEXT,
    error_detail   TEXT
);

CREATE TABLE IF NOT EXISTS articles (
    article_id      TEXT PRIMARY KEY,
    doc_id          TEXT NOT NULL REFERENCES documents(doc_id),
    articulo_numero TEXT,
    encabezado      TEXT,
    texto           TEXT NOT NULL,
    offset_start    INTEGER NOT NULL,
    offset_end      INTEGER NOT NULL,
    -- Reserved for the future vector DB; left NULL during ingestion.
    vector_id       TEXT,
    embedding_ref   TEXT
);

CREATE TABLE IF NOT EXISTS ingest_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id  TEXT,
    url     TEXT,
    status  TEXT NOT NULL,
    message TEXT,
    ts      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_articles_doc_id ON articles(doc_id);
"""


def connect(path: str) -> sqlite3.Connection:
    """Open a connection with foreign keys enabled and row access by name."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: str) -> sqlite3.Connection:
    """Create the schema (idempotent) and return an open connection."""
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def upsert_document(conn: sqlite3.Connection, doc: DocumentRecord) -> None:
    """Insert or replace a single document metadata row."""
    conn.execute(
        """
        INSERT INTO documents (
            doc_id, norma, tipo, numero, anio, titulo, fuente, url,
            fecha_consulta, organo_emisor, vigencia, areas_json, status,
            raw_path, error_detail
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(doc_id) DO UPDATE SET
            norma=excluded.norma,
            tipo=excluded.tipo,
            numero=excluded.numero,
            anio=excluded.anio,
            titulo=excluded.titulo,
            fuente=excluded.fuente,
            url=excluded.url,
            fecha_consulta=excluded.fecha_consulta,
            organo_emisor=excluded.organo_emisor,
            vigencia=excluded.vigencia,
            areas_json=excluded.areas_json,
            status=excluded.status,
            raw_path=excluded.raw_path,
            error_detail=excluded.error_detail
        """,
        (
            doc.doc_id,
            doc.norma,
            doc.tipo,
            doc.numero,
            doc.anio,
            doc.titulo,
            doc.fuente,
            doc.url,
            doc.fecha_consulta,
            doc.organo_emisor,
            doc.vigencia,
            json.dumps(doc.areas, ensure_ascii=False),
            doc.status,
            doc.raw_path,
            doc.error_detail,
        ),
    )
    conn.commit()


def insert_articles(conn: sqlite3.Connection, articles: Iterable[ArticleFragment]) -> int:
    """Insert (or replace) article fragments. Returns the number written."""
    rows: Sequence[tuple[object, ...]] = [
        (
            a.article_id,
            a.doc_id,
            a.articulo_numero,
            a.encabezado,
            a.texto,
            a.offset_start,
            a.offset_end,
            a.vector_id,
            a.embedding_ref,
        )
        for a in articles
    ]
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT OR REPLACE INTO articles (
            article_id, doc_id, articulo_numero, encabezado, texto,
            offset_start, offset_end, vector_id, embedding_ref
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def log_event(
    conn: sqlite3.Connection,
    status: str,
    *,
    doc_id: str | None = None,
    url: str | None = None,
    message: str | None = None,
) -> None:
    """Record a pipeline event (e.g. ``no_encontrado`` / ``error``) in ingest_log."""
    conn.execute(
        "INSERT INTO ingest_log (doc_id, url, status, message) VALUES (?,?,?,?)",
        (doc_id, url, status, message),
    )
    conn.commit()
