"""SQLite schema + DAO tests."""

from __future__ import annotations

import sqlite3

from corpus_ingesta.db import init_db, insert_articles, log_event, upsert_document
from corpus_ingesta.models import ArticleFragment, DocumentRecord, IngestStatus


def test_init_db_creates_tables(tmp_path) -> None:
    db_path = str(tmp_path / "corpus.sqlite")
    conn = init_db(db_path)
    tables = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    assert "documents" in tables
    assert "articles" in tables
    assert "ingest_log" in tables


def test_articles_has_reserved_vector_columns(tmp_path) -> None:
    conn = init_db(str(tmp_path / "corpus.sqlite"))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
    assert "vector_id" in cols
    assert "embedding_ref" in cols
    assert {"offset_start", "offset_end"} <= cols


def test_upsert_and_insert_roundtrip(tmp_path) -> None:
    conn = init_db(str(tmp_path / "corpus.sqlite"))
    doc = DocumentRecord(
        doc_id="constitucion",
        norma="Constitucion",
        tipo="constitucion",
        areas=["Derecho constitucional"],
        status=IngestStatus.PARSEADO.value,
    )
    upsert_document(conn, doc)

    # Upsert again to confirm it replaces rather than duplicates.
    doc.titulo = "Constitucion Politica de 1991"
    upsert_document(conn, doc)
    rows = list(conn.execute("SELECT doc_id, titulo, areas_json FROM documents"))
    assert len(rows) == 1
    assert rows[0]["titulo"] == "Constitucion Politica de 1991"
    assert "Derecho constitucional" in rows[0]["areas_json"]

    n = insert_articles(
        conn,
        [
            ArticleFragment(
                article_id="constitucion:1",
                doc_id="constitucion",
                articulo_numero="1",
                encabezado="Articulo 1",
                texto="Colombia es un Estado social de derecho...",
                offset_start=0,
                offset_end=42,
            )
        ],
    )
    assert n == 1
    art = conn.execute("SELECT vector_id, embedding_ref FROM articles").fetchone()
    assert art["vector_id"] is None
    assert art["embedding_ref"] is None


def test_log_event(tmp_path) -> None:
    conn = init_db(str(tmp_path / "corpus.sqlite"))
    log_event(conn, IngestStatus.NO_ENCONTRADO.value, doc_id="x", url="http://e", message="nope")
    row = conn.execute("SELECT status, message FROM ingest_log").fetchone()
    assert row["status"] == "no_encontrado"
    assert row["message"] == "nope"


def test_foreign_keys_enabled(tmp_path) -> None:
    conn = init_db(str(tmp_path / "corpus.sqlite"))
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert isinstance(conn, sqlite3.Connection)
