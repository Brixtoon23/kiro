"""Phase C tests for the LEXIS ``.docx`` path + file-type sniffing (offline).

Builds a ``data/raw/<doc_id>/raw.docx`` exactly like Phase B's LEXIS flow would,
then runs the REAL ``phase_c.main`` and asserts the Word text is extracted,
segmented at ARTICULO level with sliceable offsets, and persisted to SQLite with
the reserved vector columns NULL. Also checks the deterministic file-type sniff.
"""

from __future__ import annotations

import json
from pathlib import Path

from corpus_ingesta import phase_c
from corpus_ingesta.adapters.lexis_minjusticia import extract_docx_text
from corpus_ingesta.db import connect
from corpus_ingesta.manifest import MANIFEST_FIELDS
from corpus_ingesta.phase_c import sniff_filetype

FIXTURES = Path(__file__).parent / "fixtures"


def _make_lexis_raw(raw_root: Path, doc_id: str) -> Path:
    body = (FIXTURES / "lexis_ley_articulos.docx").read_bytes()
    doc_dir = raw_root / doc_id.replace(":", "__")
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / "raw.docx").write_bytes(body)
    meta = {
        "doc_id": doc_id,
        "norma": "Ley 80 de 1993",
        "canonico": ["ley", "80", "1993"],
        "areas": ["Derecho administrativo"],
        "fuente": "lexis_minjusticia",
        "url": "https://lexis.minjusticia.gov.co/minjusticia/viewDocument/1600025",
        "final_url": "https://lexis.minjusticia.gov.co/minjusticia/viewDocument/1600025",
        "fecha_consulta": "2026-09-29",
        "content_type": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        "raw_file": "raw.docx",
        "from_cache": False,
        "source": "lexis_minjusticia",
    }
    (doc_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return doc_dir


def test_sniff_filetype_detects_docx_pdf_html(tmp_path) -> None:
    docx = (FIXTURES / "lexis_ley_articulos.docx").read_bytes()
    pdf = (FIXTURES / "norma_articulos.pdf").read_bytes()
    html = (FIXTURES / "secretariasenado_sample.html").read_bytes()
    assert sniff_filetype(docx, Path("raw.docx")) == "docx"
    assert sniff_filetype(pdf, Path("raw.pdf")) == "pdf"
    assert sniff_filetype(html, Path("raw.html")) == "htm"


def test_phase_c_extracts_and_segments_docx(tmp_path) -> None:
    raw_root = tmp_path / "raw"
    doc_id = "lexis_minjusticia:ley-80-de-1993"
    _make_lexis_raw(raw_root, doc_id)

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

    cleaned = extract_docx_text((FIXTURES / "lexis_ley_articulos.docx").read_bytes())

    conn = connect(str(db))
    try:
        # The document persisted with the LEXIS source + detail URL.
        drow = conn.execute("SELECT doc_id, fuente, url, status FROM documents").fetchone()
        assert drow["fuente"] == "lexis_minjusticia"
        assert "viewDocument/1600025" in drow["url"]
        assert drow["status"] == "parseado"

        rows = list(
            conn.execute(
                "SELECT articulo_numero, texto, offset_start, offset_end, "
                "vector_id, embedding_ref FROM articles ORDER BY offset_start"
            )
        )
        # Three ARTICULO headings in the fixture.
        assert len(rows) == 3
        assert [r["articulo_numero"] for r in rows] == ["1", "2", "3"]
        prev_end = -1
        for r in rows:
            assert r["offset_start"] < r["offset_end"]
            assert r["offset_start"] >= prev_end
            prev_end = r["offset_end"]
            # Offsets slice back to EXACTLY the stored article text.
            assert cleaned[r["offset_start"] : r["offset_end"]].strip() == r["texto"]
            assert r["vector_id"] is None and r["embedding_ref"] is None
    finally:
        conn.close()

    objs = json.loads(manifest.read_text(encoding="utf-8"))
    assert len(objs) == 1
    assert set(objs[0].keys()) == set(MANIFEST_FIELDS)
    assert objs[0]["fuente"] == "lexis_minjusticia"
