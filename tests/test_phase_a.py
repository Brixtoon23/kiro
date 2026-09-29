"""Phase A tests: seed validation -> clean seed + no_encontrados (offline).

These exercise the REAL ``phase_a.main`` code path with a fake, offline
``PoliteClient`` injected via monkeypatch and drive validation with the
checked-in fixtures. No live network. The assertions target the behaviour the
reglamento mandates and would FAIL if the phase logic were reverted:

* the 3 named-inexistent references are discarded to ``no_encontrados.json``
  (never fabricated into the clean seed);
* accessible entries are copied with the EXACT seed field shape;
* a deterministic SUIN "no resultados" fixture drives ``ok=False``.
"""

from __future__ import annotations

import json
from pathlib import Path

from corpus_ingesta import phase_a
from corpus_ingesta.http_client import FetchError, FetchResult
from corpus_ingesta.seed_io import load_seed

FIXTURES = Path(__file__).parent / "fixtures"
SEED = Path(__file__).resolve().parents[1] / "seed_targets.json"


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
        if url not in self.mapping:
            raise FetchError(url, "no fixture registered")
        return FetchResult(content=self.mapping[url], from_cache=False, final_url=url)


def _install_fake(monkeypatch, client: FakeClient) -> None:
    monkeypatch.setattr(phase_a, "PoliteClient", lambda *a, **k: client)


def _run(monkeypatch, tmp_path, seed_path: Path, client: FakeClient):
    _install_fake(monkeypatch, client)
    out_clean = tmp_path / "clean.json"
    out_missing = tmp_path / "missing.json"
    rc = phase_a.main(
        [
            "--seed",
            str(seed_path),
            "--out-clean",
            str(out_clean),
            "--out-missing",
            str(out_missing),
        ]
    )
    assert rc == 0
    clean = json.loads(out_clean.read_text(encoding="utf-8"))["documentos"]
    missing = json.loads(out_missing.read_text(encoding="utf-8"))["descartados"]
    return clean, missing


def _fixture_mapping_for_real_seed() -> dict[str, bytes]:
    """Map the 6 verbatim seed URLs to a reachable fixture body.

    The 3 inexistent SUIN references are intentionally left unmapped so the
    adapter's deterministic not-found handling discards them.
    """
    data = json.loads(SEED.read_text(encoding="utf-8"))
    consolidated = (FIXTURES / "secretariasenado_sample.html").read_bytes()
    no_result = (FIXTURES / "suin_no_resultados.html").read_bytes()
    mapping: dict[str, bytes] = {}
    for doc in data["documentos"]:
        url = doc["donde_buscar"]
        if "suin-juriscol" in url:
            mapping[url] = no_result
        else:
            mapping[url] = consolidated
    return mapping


def test_phase_a_discards_three_inexistent_refs_and_never_fabricates(
    tmp_path, monkeypatch
) -> None:
    mapping = _fixture_mapping_for_real_seed()
    client = FakeClient(mapping)
    clean, missing = _run(monkeypatch, tmp_path, SEED, client)

    clean_normas = {c["norma"] for c in clean}
    missing_normas = {m["norma"] for m in missing}

    # The 3 named-but-inexistent references must be discarded, never fabricated.
    inexistent = {"Ley 11500 de 2007", "Ley 1150 de 2005", "Ley 116 de 2006"}
    assert inexistent.issubset(missing_normas)
    assert inexistent.isdisjoint(clean_normas)

    # No fabrication: every clean norma existed in the input seed.
    seed_normas = {
        d["norma"]
        for d in json.loads(SEED.read_text(encoding="utf-8"))["documentos"]
    }
    assert clean_normas.issubset(seed_normas)

    # Each discard carries a deterministic motivo + a fecha_consulta.
    for m in missing:
        assert m["motivo"]
        assert m["fecha_consulta"]


def test_phase_a_clean_entries_preserve_exact_seed_shape(tmp_path, monkeypatch) -> None:
    good = "http://www.secretariasenado.gov.co/senado/basedoc/x.html"
    client = FakeClient({good: (FIXTURES / "secretariasenado_sample.html").read_bytes()})
    seed = tmp_path / "seed.json"
    seed.write_text(
        json.dumps(
            {
                "documentos": [
                    {
                        "norma": "Codigo de prueba",
                        "canonico": ["codigo", None, None],
                        "items_del_banco": 3,
                        "areas": ["Derecho civil"],
                        "donde_buscar": good,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    clean, missing = _run(monkeypatch, tmp_path, seed, client)
    assert len(clean) == 1
    assert missing == []
    assert set(clean[0].keys()) == {
        "norma",
        "canonico",
        "items_del_banco",
        "areas",
        "donde_buscar",
    }
    assert clean[0]["areas"] == ["Derecho civil"]
    assert clean[0]["items_del_banco"] == 3


def test_phase_a_suin_no_resultados_fixture_drives_discard(tmp_path, monkeypatch) -> None:
    """A SUIN 'no resultados' body deterministically yields ok=False."""
    suin = "https://www.suin-juriscol.gov.co/legislacion/?tipo=ley&numero=9999&anio=2020"
    client = FakeClient({suin: (FIXTURES / "suin_no_resultados.html").read_bytes()})
    seed = tmp_path / "seed.json"
    seed.write_text(
        json.dumps(
            {
                "documentos": [
                    {
                        "norma": "Ley inexistente",
                        "canonico": ["ley", "9999", "2020"],
                        "items_del_banco": 0,
                        "areas": [],
                        "donde_buscar": suin,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    clean, missing = _run(monkeypatch, tmp_path, seed, client)
    assert clean == []
    assert len(missing) == 1
    assert missing[0]["norma"] == "Ley inexistente"
    assert "no_result_marker" in missing[0]["motivo"]
    # The fixture WAS fetched (real code path), proving deterministic detection.
    assert suin in client.calls


def test_phase_a_resolves_search_url_into_clean_seed(tmp_path, monkeypatch) -> None:
    """Phase A follows a ?q= buscador result and stores the DIRECT document URL
    (not the search URL) in seed_targets_clean.json."""
    search = (
        "https://www.suin-juriscol.gov.co/legislacion/?q=Ley%2080%20de%201993"
    )
    body = (FIXTURES / "suin_resultados_match.html").read_bytes()
    client = FakeClient({search: body})
    seed = tmp_path / "seed.json"
    seed.write_text(
        json.dumps(
            {
                "documentos": [
                    {
                        "norma": "Ley 80 de 1993",
                        "canonico": ["ley", "80", "1993"],
                        "items_del_banco": 12,
                        "areas": ["Derecho administrativo"],
                        "donde_buscar": search,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    clean, missing = _run(monkeypatch, tmp_path, seed, client)
    assert missing == []
    assert len(clean) == 1
    # The clean seed carries the RESOLVED direct document URL, not the ?q= page.
    assert clean[0]["donde_buscar"] == (
        "https://www.suin-juriscol.gov.co/viewDocument.asp?ruta=Leyes/1600000"
    )
    # Field shape is preserved exactly and no other field was mutated.
    assert set(clean[0].keys()) == {
        "norma",
        "canonico",
        "items_del_banco",
        "areas",
        "donde_buscar",
    }
    assert clean[0]["items_del_banco"] == 12


def test_phase_a_search_no_match_goes_to_no_encontrados(tmp_path, monkeypatch) -> None:
    """A ?q= search whose results do not match the norma is discarded, never
    fabricated into the clean seed."""
    search = (
        "https://www.suin-juriscol.gov.co/legislacion/?q=Ley%2080%20de%201993"
    )
    body = (FIXTURES / "suin_resultados_ambiguo.html").read_bytes()
    client = FakeClient({search: body})
    seed = tmp_path / "seed.json"
    seed.write_text(
        json.dumps(
            {
                "documentos": [
                    {
                        "norma": "Ley 80 de 1993",
                        "canonico": ["ley", "80", "1993"],
                        "items_del_banco": 12,
                        "areas": [],
                        "donde_buscar": search,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    clean, missing = _run(monkeypatch, tmp_path, seed, client)
    assert clean == []
    assert len(missing) == 1
    assert "match ambiguo" in missing[0]["motivo"]


def test_phase_a_dry_run_uses_no_network(tmp_path, monkeypatch) -> None:
    """--dry-run wires adapters + routes without ever calling the client."""
    client = FakeClient({})  # any get() would raise
    _install_fake(monkeypatch, client)
    rc = phase_a.main(["--seed", str(SEED), "--dry-run"])
    assert rc == 0
    assert client.calls == []


def test_phase_a_load_seed_reads_all_documentos() -> None:
    """The shipped seed carries the full 186-entry corpus."""
    targets = load_seed(str(SEED))
    assert len(targets) == 186
    assert targets[0].norma == "Constitucion"
    normas = {t.norma for t in targets}
    # The named-inexistent references are present so Phase A can discard them.
    assert {"Ley 11500 de 2007", "Ley 1150 de 2005", "Ley 116 de 2006"} <= normas
