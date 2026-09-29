"""Phase B tests for the LEXIS headless-collector integration (offline).

These exercise the REAL ``phase_b.main`` with ``build_lexis_collector``
monkeypatched to return a fake collector (no Playwright, no Chromium, no
network). They assert LEXIS-covered norms are routed to the collector by their
canonico, the ``.docx`` lands raw-first under ``data/raw/<doc_id>/`` with a
LEXIS ``meta.json``, a second run reuses the on-disk ``.docx`` (no re-download),
and a no-match norm is skipped without crashing.
"""

from __future__ import annotations

import json
from pathlib import Path

from corpus_ingesta import phase_b
from corpus_ingesta.lexis_client import LexisResult

FIXTURES = Path(__file__).parent / "fixtures"
DOCX = (FIXTURES / "lexis_ley_articulos.docx").read_bytes()


class FakeCollector:
    """Offline collector: returns canned :class:`LexisResult` per (tipo,numero)."""

    def __init__(self, results: dict[tuple, LexisResult]) -> None:
        self.results = results
        self.calls: list[tuple] = []

        class _B:
            def close(self_inner) -> None:  # noqa: N805
                pass

        self.browser = _B()

    def collect(self, *, tipo, numero, anio) -> LexisResult:  # noqa: ANN001
        self.calls.append((tipo, numero, anio))
        return self.results.get((tipo, numero, anio), LexisResult(ok=False, reason="no match"))


def _write_seed(path: Path, entries: list[dict]) -> None:
    path.write_text(json.dumps({"documentos": entries}, ensure_ascii=False), encoding="utf-8")


def _entry(norma: str, canonico: list, url: str = "https://ignored.example/legacy") -> dict:
    return {
        "norma": norma,
        "canonico": canonico,
        "items_del_banco": 1,
        "areas": ["Derecho administrativo"],
        "donde_buscar": url,
    }


def _install_fake(monkeypatch, collector) -> None:
    monkeypatch.setattr(phase_b, "build_lexis_collector", lambda delay: collector)


def test_phase_b_routes_lexis_norm_and_saves_docx(tmp_path, monkeypatch) -> None:
    clean = tmp_path / "clean.json"
    _write_seed(clean, [_entry("Ley 80 de 1993", ["ley", "80", "1993"])])
    collector = FakeCollector(
        {
            ("ley", "80", "1993"): LexisResult(
                ok=True,
                docx=DOCX,
                detail_url="https://lexis.minjusticia.gov.co/minjusticia/viewDocument/1600025",
                lexis_doc_id="1600025",
                reason="descargado_desde_lexis",
            )
        }
    )
    _install_fake(monkeypatch, collector)

    raw_dir = tmp_path / "raw"
    assert phase_b.main(["--clean-seed", str(clean), "--raw-dir", str(raw_dir)]) == 0

    # Routed by canonico (NOT the legacy donde_buscar).
    assert collector.calls == [("ley", "80", "1993")]

    doc_dirs = [d for d in raw_dir.iterdir() if d.is_dir() and d.name != "cache"]
    assert len(doc_dirs) == 1
    d = doc_dirs[0]
    raw_docx = d / "raw.docx"
    assert raw_docx.read_bytes() == DOCX  # raw-first, unmodified

    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["source"] == "lexis_minjusticia"
    assert meta["fuente"] == "lexis_minjusticia"
    assert "viewDocument/1600025" in meta["url"]
    assert meta["raw_file"] == "raw.docx"
    assert meta["lexis_doc_id"] == "1600025"


def test_phase_b_lexis_cache_reuse_no_second_download(tmp_path, monkeypatch) -> None:
    clean = tmp_path / "clean.json"
    _write_seed(clean, [_entry("Ley 80 de 1993", ["ley", "80", "1993"])])
    collector = FakeCollector(
        {
            ("ley", "80", "1993"): LexisResult(
                ok=True, docx=DOCX, lexis_doc_id="1600025", detail_url="u"
            )
        }
    )
    _install_fake(monkeypatch, collector)
    raw_dir = tmp_path / "raw"

    assert phase_b.main(["--clean-seed", str(clean), "--raw-dir", str(raw_dir)]) == 0
    assert len(collector.calls) == 1
    # Second run: raw.docx already on disk -> collector NOT invoked again.
    assert phase_b.main(["--clean-seed", str(clean), "--raw-dir", str(raw_dir)]) == 0
    assert len(collector.calls) == 1  # no re-download


def test_phase_b_lexis_no_match_is_skipped_without_crash(tmp_path, monkeypatch) -> None:
    clean = tmp_path / "clean.json"
    _write_seed(
        clean,
        [
            _entry("Ley inexistente", ["ley", "99999", "2099"]),
            _entry("Decreto 2153 de 1992", ["decreto", "2153", "1992"]),
        ],
    )
    collector = FakeCollector(
        {
            ("decreto", "2153", "1992"): LexisResult(
                ok=True, docx=DOCX, lexis_doc_id="1700001", detail_url="u"
            )
        }
    )
    _install_fake(monkeypatch, collector)
    raw_dir = tmp_path / "raw"

    assert phase_b.main(["--clean-seed", str(clean), "--raw-dir", str(raw_dir)]) == 0
    # Both LEXIS norms were attempted; only the matching one produced a raw dir.
    assert set(collector.calls) == {("ley", "99999", "2099"), ("decreto", "2153", "1992")}
    doc_dirs = [d for d in raw_dir.iterdir() if d.is_dir() and d.name != "cache"]
    assert len(doc_dirs) == 1
    assert doc_dirs[0].name.startswith("lexis_minjusticia__decreto")


def test_phase_b_non_lexis_norm_does_not_touch_collector(tmp_path, monkeypatch) -> None:
    """A jurisprudencia entry must NOT build the LEXIS collector at all."""
    clean = tmp_path / "clean.json"
    # Use a suin URL for a codigo (not a LEXIS canonico tipo like 'ley').
    _write_seed(
        clean,
        [_entry("Codigo generico", ["codigo", None, None], url="https://ignored.example/x")],
    )

    built = {"count": 0}

    def _factory(delay):
        built["count"] += 1
        raise AssertionError("collector should not be built for non-LEXIS norms")

    monkeypatch.setattr(phase_b, "build_lexis_collector", _factory)

    # 'codigo' (bare) is not a LEXIS canonico tipo, and the URL matches no
    # adapter, so it is a sin-adapter skip -- the collector is never built.
    raw_dir = tmp_path / "raw"
    assert phase_b.main(["--clean-seed", str(clean), "--raw-dir", str(raw_dir)]) == 0
    assert built["count"] == 0
