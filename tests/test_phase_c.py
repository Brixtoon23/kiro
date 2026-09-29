"""Phase C tests: raw -> SQLite (+ vector-ready) + manifest/CORPUS.md (offline).

The tests build a ``data/raw/<doc_id>/`` directory from checked-in fixtures
(the same layout Phase B produces) and run the REAL ``phase_c.main``. They
assert the reglamento guarantees that would FAIL if phase logic were reverted:

* article-level fragments persisted with MONOTONIC, NON-OVERLAPPING offsets
  that slice back to the article text within the cleaned document;
* ``vector_id``/``embedding_ref`` reserved NULL for a future vector DB;
* ``corpus_manifest.json`` objects carry EXACTLY the 6 mandated keys;
* ``CORPUS.md`` has Inventario / Criterio / Metodo;
* a ``no_encontrado`` raw case yields an ``ingest_log`` row and ZERO
  fabricated document/article rows.
"""

from __future__ import annotations

import json
from pathlib import Path

from corpus_ingesta import phase_c
from corpus_ingesta.adapters.base import clean_html_to_text
from corpus_ingesta.db import connect
from corpus_ingesta.manifest import MANIFEST_FIELDS

FIXTURES = Path(__file__).parent / "fixtures"


def _make_raw_doc(
    raw_root: Path,
    doc_id: str,
    *,
    fuente: str,
    url: str,
    norma: str,
    fixture: str,
    areas: list[str] | None = None,
) -> Path:
    """Materialise a data/raw/<doc_id>/ dir exactly like Phase B would."""
    body = (FIXTURES / fixture).read_bytes()
    ext = "pdf" if fixture.endswith(".pdf") else "html"
    doc_dir = raw_root / doc_id.replace(":", "__")
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / f"raw.{ext}").write_bytes(body)
    meta = {
        "doc_id": doc_id,
        "norma": norma,
        "canonico": ["codigo", None, None],
        "areas": areas or ["Derecho civil"],
        "fuente": fuente,
        "url": url,
        "final_url": url,
        "fecha_consulta": "2026-09-29",
        "content_type": "application/pdf" if ext == "pdf" else "text/html",
        "raw_file": f"raw.{ext}",
        "from_cache": False,
    }
    (doc_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    return doc_dir


def _run_phase_c(tmp_path, raw_root: Path):
    db = tmp_path / "corpus.sqlite"
    manifest = tmp_path / "corpus_manifest.json"
    corpus_md = tmp_path / "CORPUS.md"
    rc = phase_c.main(
        [
            "--raw-dir",
            str(raw_root),
            "--db",
            str(db),
            "--manifest",
            str(manifest),
            "--corpus-md",
            str(corpus_md),
        ]
    )
    assert rc == 0
    return db, manifest, corpus_md


def test_phase_c_article_offsets_slice_back_to_text(tmp_path) -> None:
    raw_root = tmp_path / "raw"
    url = "http://www.secretariasenado.gov.co/senado/basedoc/ley-5678.html"
    _make_raw_doc(
        raw_root,
        "secretariasenado:codigo-multiarticulo",
        fuente="secretariasenado",
        url=url,
        norma="Codigo multiarticulo",
        fixture="secretariasenado_multiarticulo.html",
    )
    db, manifest, corpus_md = _run_phase_c(tmp_path, raw_root)

    # Recompute the canonical cleaned text the parser segments against.
    cleaned = clean_html_to_text(
        (FIXTURES / "secretariasenado_multiarticulo.html").read_bytes()
    )

    conn = connect(str(db))
    try:
        rows = list(
            conn.execute(
                "SELECT articulo_numero, texto, offset_start, offset_end, "
                "vector_id, embedding_ref FROM articles ORDER BY offset_start"
            )
        )
        # The multiarticulo fixture has 5 article headings (incl. '2 bis').
        assert len(rows) == 5
        prev_end = -1
        for r in rows:
            # Monotonic, non-overlapping offsets.
            assert r["offset_start"] < r["offset_end"]
            assert r["offset_start"] >= prev_end
            prev_end = r["offset_end"]
            # Offsets slice back to EXACTLY the stored article text.
            assert cleaned[r["offset_start"] : r["offset_end"]].strip() == r["texto"]
            # Reserved vector columns stay NULL during ingestion.
            assert r["vector_id"] is None
            assert r["embedding_ref"] is None
        numeros = [r["articulo_numero"] for r in rows]
        assert "2 bis" in numeros
    finally:
        conn.close()

    # Manifest: EXACTLY the six mandated keys.
    objs = json.loads(manifest.read_text(encoding="utf-8"))
    assert isinstance(objs, list) and objs
    for obj in objs:
        assert set(obj.keys()) == set(MANIFEST_FIELDS)

    md = corpus_md.read_text(encoding="utf-8")
    assert "## Inventario" in md
    assert "## Criterio de seleccion" in md
    assert "## Metodo" in md


def test_phase_c_no_encontrado_yields_log_and_zero_fabrication(tmp_path) -> None:
    raw_root = tmp_path / "raw"
    # A SUIN 'no resultados' body: parser must return no_encontrado, no rows.
    url = "https://www.suin-juriscol.gov.co/legislacion/ley-9999"
    _make_raw_doc(
        raw_root,
        "suin_juriscol:ley-inexistente",
        fuente="suin_juriscol",
        url=url,
        norma="Ley inexistente",
        fixture="suin_no_resultados.html",
        areas=[],
    )
    db, _manifest, _md = _run_phase_c(tmp_path, raw_root)

    conn = connect(str(db))
    try:
        ne = conn.execute(
            "SELECT COUNT(*) FROM ingest_log WHERE status='no_encontrado'"
        ).fetchone()[0]
        assert ne == 1
        # No fabricated document or article rows.
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 0
    finally:
        conn.close()


def test_phase_c_mixed_corpus_isolates_no_encontrado(tmp_path) -> None:
    """A reachable norm + a no_encontrado norm: only the reachable one persists."""
    raw_root = tmp_path / "raw"
    _make_raw_doc(
        raw_root,
        "secretariasenado:codigo-de-prueba",
        fuente="secretariasenado",
        url="http://www.secretariasenado.gov.co/senado/basedoc/x.html",
        norma="Codigo de prueba",
        fixture="secretariasenado_sample.html",
    )
    _make_raw_doc(
        raw_root,
        "suin_juriscol:ley-inexistente",
        fuente="suin_juriscol",
        url="https://www.suin-juriscol.gov.co/legislacion/ley-9999",
        norma="Ley inexistente",
        fixture="suin_no_resultados.html",
        areas=[],
    )
    db, manifest, _md = _run_phase_c(tmp_path, raw_root)

    conn = connect(str(db))
    try:
        docs = [r["doc_id"] for r in conn.execute("SELECT doc_id FROM documents")]
        assert docs == ["secretariasenado:codigo-de-prueba"]
        n_articles = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        assert n_articles >= 3  # the sample fixture has 3 articles
        ne = conn.execute(
            "SELECT COUNT(*) FROM ingest_log WHERE status='no_encontrado'"
        ).fetchone()[0]
        assert ne == 1
    finally:
        conn.close()

    # Manifest only reflects the persisted (reachable) document.
    objs = json.loads(manifest.read_text(encoding="utf-8"))
    assert len(objs) == 1
    assert objs[0]["doc_id"] == "secretariasenado:codigo-de-prueba"
