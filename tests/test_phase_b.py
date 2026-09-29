"""Phase B tests: clean seed -> raw-first download (offline).

The tests exercise the REAL ``phase_b.main`` code path:

* raw-first persistence: raw bytes + ``meta.json`` land under
  ``data/raw/<doc_id>/`` and Phase B does NO parsing;
* cache reuse: a second run over the same URL does not re-fetch (validated
  against the REAL :class:`PoliteClient` on-disk cache with a fake session,
  so the caching behaviour itself is under test, not a stub);
* error resilience: a :class:`FetchError` on one entry is logged and the run
  CONTINUES with the next entry.
"""

from __future__ import annotations

import json
from pathlib import Path

from corpus_ingesta import phase_b
from corpus_ingesta.http_client import FetchError, FetchResult, PoliteClient

FIXTURES = Path(__file__).parent / "fixtures"


class FakeClient:
    """Offline stand-in for PoliteClient: serves fixture bytes, never networks."""

    def __init__(self, mapping: dict[str, bytes], fail: set[str] | None = None) -> None:
        self.mapping = mapping
        self.fail = fail or set()
        self.calls: list[str] = []

    def cached(self, url: str) -> bool:
        return False

    def get(self, url: str, *, use_cache: bool = True) -> FetchResult:
        self.calls.append(url)
        if url in self.fail:
            raise FetchError(url, "simulated timeout")
        return FetchResult(content=self.mapping[url], from_cache=False, final_url=url)


class _FakeResponse:
    def __init__(self, content: bytes, url: str) -> None:
        self.status_code = 200
        self.content = content
        self.url = url


class _CountingSession:
    """A fake requests session that counts network calls."""

    def __init__(self, mapping: dict[str, bytes]) -> None:
        self.mapping = mapping
        self.headers: dict[str, str] = {}
        self.calls = 0

    def get(self, url, timeout=None):  # noqa: ANN001
        self.calls += 1
        return _FakeResponse(self.mapping[url], url)


def _write_seed(path: Path, entries: list[dict]) -> None:
    path.write_text(json.dumps({"documentos": entries}, ensure_ascii=False), encoding="utf-8")


def _entry(norma: str, url: str, **kw) -> dict:
    return {
        "norma": norma,
        "canonico": kw.get("canonico", ["codigo", None, None]),
        "items_del_banco": kw.get("items_del_banco", 1),
        "areas": kw.get("areas", ["Derecho civil"]),
        "donde_buscar": url,
    }


def test_phase_b_writes_raw_first_and_meta(tmp_path, monkeypatch) -> None:
    url = "http://www.secretariasenado.gov.co/senado/basedoc/x.html"
    body = (FIXTURES / "secretariasenado_sample.html").read_bytes()
    clean = tmp_path / "clean.json"
    _write_seed(clean, [_entry("Codigo de prueba", url)])
    monkeypatch.setattr(phase_b, "PoliteClient", lambda *a, **k: FakeClient({url: body}))

    raw_dir = tmp_path / "raw"
    rc = phase_b.main(["--clean-seed", str(clean), "--raw-dir", str(raw_dir)])
    assert rc == 0

    doc_dirs = [d for d in raw_dir.iterdir() if d.is_dir() and d.name != "cache"]
    assert len(doc_dirs) == 1
    d = doc_dirs[0]

    # Raw-first: the ORIGINAL bytes are on disk, unmodified (no parsing).
    raw_files = list(d.glob("raw.*"))
    assert len(raw_files) == 1
    assert raw_files[0].read_bytes() == body
    assert raw_files[0].suffix == ".html"

    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["fuente"] == "secretariasenado"
    assert meta["url"] == url
    assert meta["raw_file"] == raw_files[0].name
    assert meta["fecha_consulta"]
    # No parsed article data leaks into Phase B output.
    assert "articles" not in meta and "fragments" not in meta


def test_phase_b_cache_reuse_no_refetch_second_run(tmp_path) -> None:
    """Second run over the same URL hits the on-disk cache, no re-fetch.

    Uses the REAL PoliteClient with a counting fake session so the caching
    logic itself is exercised, not a stub.
    """
    url = "http://www.secretariasenado.gov.co/senado/basedoc/x.html"
    body = (FIXTURES / "secretariasenado_sample.html").read_bytes()
    clean = tmp_path / "clean.json"
    _write_seed(clean, [_entry("Codigo de prueba", url)])
    raw_dir = tmp_path / "raw"

    session = _CountingSession({url: body})
    real_client = PoliteClient(base_delay=0.0, cache_dir=str(raw_dir / "cache"), session=session)
    import corpus_ingesta.phase_b as pb

    orig = pb.PoliteClient
    pb.PoliteClient = lambda *a, **k: real_client  # noqa: E731
    try:
        assert pb.main(["--clean-seed", str(clean), "--raw-dir", str(raw_dir)]) == 0
        assert session.calls == 1  # first run fetched once
        # Second run: same PoliteClient cache dir -> served from cache.
        real_client._last_request_ts = None
        assert pb.main(["--clean-seed", str(clean), "--raw-dir", str(raw_dir)]) == 0
        assert session.calls == 1  # NOT re-fetched
    finally:
        pb.PoliteClient = orig


def test_phase_b_continues_past_fetch_error(tmp_path, monkeypatch) -> None:
    good = "http://www.secretariasenado.gov.co/senado/basedoc/x.html"
    bad = "https://www.suin-juriscol.gov.co/legislacion/timeout"
    body = (FIXTURES / "secretariasenado_sample.html").read_bytes()
    clean = tmp_path / "clean.json"
    _write_seed(
        clean,
        [
            # A jurisprudencia entry stays on the HTTP path (LEXIS handles only
            # leyes/decretos/codigos/estatutos), so this exercises FetchError
            # resilience of the polite-client download flow.
            _entry("Sentencia timeout", bad, canonico=["jurisprudencia", "C-1", "2020"]),
            _entry("Codigo de prueba", good),
        ],
    )
    client = FakeClient({good: body}, fail={bad})
    monkeypatch.setattr(phase_b, "PoliteClient", lambda *a, **k: client)

    raw_dir = tmp_path / "raw"
    rc = phase_b.main(["--clean-seed", str(clean), "--raw-dir", str(raw_dir)])
    # Did not abort despite one failure; both URLs were attempted.
    assert rc == 0
    assert client.calls == [bad, good]
    doc_dirs = [d for d in raw_dir.iterdir() if d.is_dir() and d.name != "cache"]
    # Only the reachable entry produced a raw dir; the failed one produced none.
    assert len(doc_dirs) == 1
    assert (doc_dirs[0] / "meta.json").exists()


def test_phase_b_sniffs_pdf_extension(tmp_path, monkeypatch) -> None:
    url = "http://www.secretariasenado.gov.co/senado/basedoc/norma.pdf"
    body = (FIXTURES / "norma_articulos.pdf").read_bytes()
    clean = tmp_path / "clean.json"
    _write_seed(clean, [_entry("Norma PDF", url)])
    monkeypatch.setattr(phase_b, "PoliteClient", lambda *a, **k: FakeClient({url: body}))

    raw_dir = tmp_path / "raw"
    assert phase_b.main(["--clean-seed", str(clean), "--raw-dir", str(raw_dir)]) == 0
    doc_dirs = [d for d in raw_dir.iterdir() if d.is_dir() and d.name != "cache"]
    raw_files = list(doc_dirs[0].glob("raw.*"))
    assert raw_files[0].suffix == ".pdf"


def test_phase_b_cache_hit_preserves_original_fecha_consulta(tmp_path) -> None:
    """A cache-hit re-run keeps the ORIGINAL consultation date, no drift.

    Runs Phase B once against the REAL PoliteClient (fetches, stamps today),
    rewrites the stored ``fecha_consulta`` to an older date to simulate a prior
    download, then re-runs. The second run is served from the on-disk cache
    (``from_cache`` true) and MUST carry the original date forward rather than
    re-stamping today.
    """
    url = "http://www.secretariasenado.gov.co/senado/basedoc/x.html"
    body = (FIXTURES / "secretariasenado_sample.html").read_bytes()
    clean = tmp_path / "clean.json"
    _write_seed(clean, [_entry("Codigo de prueba", url)])
    raw_dir = tmp_path / "raw"

    session = _CountingSession({url: body})
    real_client = PoliteClient(base_delay=0.0, cache_dir=str(raw_dir / "cache"), session=session)
    import corpus_ingesta.phase_b as pb

    orig = pb.PoliteClient
    pb.PoliteClient = lambda *a, **k: real_client  # noqa: E731
    try:
        assert pb.main(["--clean-seed", str(clean), "--raw-dir", str(raw_dir)]) == 0
        assert session.calls == 1

        doc_dirs = [d for d in raw_dir.iterdir() if d.is_dir() and d.name != "cache"]
        assert len(doc_dirs) == 1
        meta_path = doc_dirs[0] / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

        # Simulate a norm actually downloaded on an earlier day.
        original_date = "2000-01-02"
        assert meta["fecha_consulta"] != original_date
        meta["fecha_consulta"] = original_date
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        # Second run: served from cache, must NOT re-fetch nor re-stamp the date.
        real_client._last_request_ts = None
        assert pb.main(["--clean-seed", str(clean), "--raw-dir", str(raw_dir)]) == 0
        assert session.calls == 1  # cache hit, no re-fetch

        meta_after = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta_after["from_cache"] is True
        assert meta_after["fecha_consulta"] == original_date
    finally:
        pb.PoliteClient = orig
