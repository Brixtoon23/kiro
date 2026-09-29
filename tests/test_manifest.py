"""Manifest generation tests: exact 6-key manifest + CORPUS.md sections.

Builds the SQLite state directly (independent of the pipeline) so these tests
target ``manifest.build_manifest`` / ``write_all`` code paths precisely.
"""

from __future__ import annotations

import json
from pathlib import Path

from corpus_ingesta.db import init_db, insert_articles, log_event, upsert_document
from corpus_ingesta.manifest import (
    MANIFEST_FIELDS,
    build_manifest,
    write_all,
    write_manifest_json,
)
from corpus_ingesta.models import ArticleFragment, DocumentRecord, IngestStatus


def _populate(db_path: str) -> None:
    conn = init_db(db_path)
    parseado = DocumentRecord(
        doc_id="secretariasenado:codigo-de-prueba",
        norma="Codigo de prueba",
        tipo="ley",
        numero="1234",
        anio="2020",
        titulo="LEY 1234 DE 2020",
        fuente="secretariasenado",
        url="http://www.secretariasenado.gov.co/senado/basedoc/x.html",
        fecha_consulta="2026-01-01",
        areas=["Derecho civil"],
        status=IngestStatus.PARSEADO.value,
    )
    upsert_document(conn, parseado)
    insert_articles(
        conn,
        [
            ArticleFragment(
                article_id="secretariasenado:codigo-de-prueba:art-1",
                doc_id=parseado.doc_id,
                articulo_numero="1",
                encabezado="ARTICULO 1o.",
                texto="ARTICULO 1o. Objeto.",
                offset_start=0,
                offset_end=20,
            )
        ],
    )
    ausente = DocumentRecord(
        doc_id="suin_juriscol:ley-inexistente",
        norma="Ley inexistente",
        fuente="suin_juriscol",
        url="https://www.suin-juriscol.gov.co/legislacion/ley-9999",
        fecha_consulta="2026-01-01",
        areas=["Derecho civil"],
        status=IngestStatus.NO_ENCONTRADO.value,
        error_detail="SUIN-Juriscol returned no results for this norm.",
    )
    upsert_document(conn, ausente)
    log_event(
        conn,
        IngestStatus.NO_ENCONTRADO.value,
        doc_id=ausente.doc_id,
        url=ausente.url,
        message="no results",
    )
    conn.commit()
    conn.close()


def test_manifest_objects_have_exactly_six_keys(tmp_path) -> None:
    db = str(tmp_path / "corpus.sqlite")
    _populate(db)
    conn = init_db(db)
    manifest = build_manifest(conn)
    conn.close()

    assert len(manifest) == 2
    for obj in manifest:
        assert list(obj.keys()) == list(MANIFEST_FIELDS)
        assert set(obj.keys()) == {
            "doc_id",
            "titulo",
            "fuente",
            "url",
            "fecha_consulta",
            "areas",
        }
    areas = manifest[0]["areas"]
    assert isinstance(areas, list)


def test_write_manifest_json_roundtrips(tmp_path) -> None:
    db = str(tmp_path / "corpus.sqlite")
    _populate(db)
    conn = init_db(db)
    manifest_path = str(tmp_path / "corpus_manifest.json")
    write_manifest_json(conn, manifest_path)
    conn.close()

    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    assert isinstance(data, list) and len(data) == 2
    for obj in data:
        assert set(obj.keys()) == set(MANIFEST_FIELDS)


def test_corpus_md_has_required_sections_and_rows(tmp_path) -> None:
    db = str(tmp_path / "corpus.sqlite")
    _populate(db)
    conn = init_db(db)
    manifest_path = str(tmp_path / "corpus_manifest.json")
    corpus_md = str(tmp_path / "CORPUS.md")
    write_all(conn, manifest_path=manifest_path, corpus_md_path=corpus_md)
    conn.close()

    md = Path(corpus_md).read_text(encoding="utf-8")
    assert "## Inventario" in md
    assert "## Criterio de seleccion" in md
    assert "## Metodo" in md
    # Both documents appear in the inventory, including the no_encontrado one.
    assert "codigo-de-prueba" in md
    assert "no_encontrado" in md
    # The article count column reflects the single parsed article.
    assert "secretariasenado" in md and "suin_juriscol" in md
