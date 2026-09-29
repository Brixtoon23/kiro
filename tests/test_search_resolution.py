"""Phase A search-resolution tests (deterministic, offline).

The three HTML portals (secretariasenado, suin-juriscol, corteconstitucional)
publish most seed ``donde_buscar`` fields as ``?q=`` SEARCH pages. Phase A must
GET the results page, parse it with BeautifulSoup/lxml and FOLLOW the first
result that matches the norma to obtain the DIRECT document URL, which then
lands in ``seed_targets_clean.json``. When the buscador returns no results, or
no result matches the norma, the entry is honestly discarded to
``no_encontrados.json`` -- never fabricated.

These tests exercise the REAL adapter ``validate()`` / ``resolve_search`` code
path against checked-in fixtures (one per portal) covering the three mandated
cases: match -> direct URL; no results -> no_encontrado; ambiguous ->
no_encontrado. No live network.
"""

from __future__ import annotations

from pathlib import Path

from corpus_ingesta.adapters.corteconstitucional import CorteConstitucionalAdapter
from corpus_ingesta.adapters.secretariasenado import SecretariaSenadoAdapter
from corpus_ingesta.adapters.suin_juriscol import SuinJuriscolAdapter
from corpus_ingesta.http_client import FetchResult
from corpus_ingesta.models import SeedTarget

FIXTURES = Path(__file__).parent / "fixtures"


class FakeClient:
    """Offline stand-in for PoliteClient: serves fixture bytes for one URL."""

    def __init__(self, body: bytes, *, final_url: str | None = None) -> None:
        self.body = body
        self.final_url = final_url
        self.calls: list[str] = []

    def get(self, url: str, *, use_cache: bool = True) -> FetchResult:
        self.calls.append(url)
        return FetchResult(
            content=self.body, from_cache=False, final_url=self.final_url or url
        )


def _target(norma: str, canonico: list, url: str) -> SeedTarget:
    return SeedTarget.from_dict(
        {
            "norma": norma,
            "canonico": canonico,
            "items_del_banco": 1,
            "areas": [],
            "donde_buscar": url,
        }
    )


# ---------------------------------------------------------------------------
# secretariasenado (/senado/basedoc/?q=)
# ---------------------------------------------------------------------------

SENADO_SEARCH = "http://www.secretariasenado.gov.co/senado/basedoc/?q=Codigo%20general%20proceso"


def test_secretariasenado_search_match_resolves_direct_url() -> None:
    adapter = SecretariaSenadoAdapter()
    body = (FIXTURES / "secretariasenado_resultados_match.html").read_bytes()
    client = FakeClient(body, final_url=SENADO_SEARCH)
    t = _target("Codigo general proceso", ["codigo_general_proceso", None, None], SENADO_SEARCH)
    res = adapter.validate(t, client)
    assert res.ok is True
    # The buscador WAS queried (real resolution path).
    assert SENADO_SEARCH in client.calls
    # Resolved to the direct .html document, absolutised against the portal.
    assert res.resolved_url == (
        "http://www.secretariasenado.gov.co/senado/basedoc/ley_1564_2012.html"
    )


def test_secretariasenado_search_ambiguous_is_no_encontrado() -> None:
    adapter = SecretariaSenadoAdapter()
    body = (FIXTURES / "secretariasenado_resultados_ambiguo.html").read_bytes()
    client = FakeClient(body, final_url=SENADO_SEARCH)
    t = _target("Codigo general proceso", ["codigo_general_proceso", None, None], SENADO_SEARCH)
    res = adapter.validate(t, client)
    assert res.ok is False
    assert res.resolved_url == ""
    assert "match ambiguo" in res.reason


def test_secretariasenado_search_no_results_is_no_encontrado() -> None:
    adapter = SecretariaSenadoAdapter()
    body = (FIXTURES / "suin_no_resultados.html").read_bytes()  # generic no-results body
    client = FakeClient(body, final_url=SENADO_SEARCH)
    t = _target("Codigo general proceso", ["codigo_general_proceso", None, None], SENADO_SEARCH)
    res = adapter.validate(t, client)
    assert res.ok is False
    assert "sin resultados" in res.reason


def test_secretariasenado_direct_html_link_is_not_resolved() -> None:
    """A direct ``.html?q=`` link (e.g. the Constitution) is reachable as-is and
    keeps its URL (no resolution / no fabrication)."""
    adapter = SecretariaSenadoAdapter()
    direct = (
        "http://www.secretariasenado.gov.co/senado/basedoc/"
        "constitucion_politica_1991.html?q=Constitucion"
    )
    body = (FIXTURES / "secretariasenado_multiarticulo.html").read_bytes()
    client = FakeClient(body, final_url=direct)
    t = _target("Constitucion", ["constitucion", None, None], direct)
    res = adapter.validate(t, client)
    assert res.ok is True
    assert res.resolved_url == ""  # direct link preserved by Phase A


# ---------------------------------------------------------------------------
# suin-juriscol (/legislacion/?q=)
# ---------------------------------------------------------------------------

SUIN_SEARCH = "https://www.suin-juriscol.gov.co/legislacion/?q=Ley%2080%20de%201993"


def test_suin_search_match_resolves_direct_url() -> None:
    adapter = SuinJuriscolAdapter()
    body = (FIXTURES / "suin_resultados_match.html").read_bytes()
    client = FakeClient(body, final_url=SUIN_SEARCH)
    t = _target("Ley 80 de 1993", ["ley", "80", "1993"], SUIN_SEARCH)
    res = adapter.validate(t, client)
    assert res.ok is True
    assert SUIN_SEARCH in client.calls
    assert res.resolved_url == (
        "https://www.suin-juriscol.gov.co/viewDocument.asp?ruta=Leyes/1600000"
    )


def test_suin_search_ambiguous_is_no_encontrado() -> None:
    adapter = SuinJuriscolAdapter()
    body = (FIXTURES / "suin_resultados_ambiguo.html").read_bytes()
    client = FakeClient(body, final_url=SUIN_SEARCH)
    t = _target("Ley 80 de 1993", ["ley", "80", "1993"], SUIN_SEARCH)
    res = adapter.validate(t, client)
    assert res.ok is False
    assert res.resolved_url == ""
    assert "match ambiguo" in res.reason


def test_suin_search_no_results_is_no_encontrado() -> None:
    adapter = SuinJuriscolAdapter()
    body = (FIXTURES / "suin_no_resultados.html").read_bytes()
    client = FakeClient(body, final_url=SUIN_SEARCH)
    t = _target("Ley 11500 de 2007", ["ley", "11500", "2007"], SUIN_SEARCH)
    res = adapter.validate(t, client)
    assert res.ok is False
    assert "sin resultados" in res.reason


# ---------------------------------------------------------------------------
# corteconstitucional (/relatoria/?q=)
# ---------------------------------------------------------------------------

CORTE_SEARCH = (
    "https://www.corteconstitucional.gov.co/relatoria/?q=Sentencia%20C-355%20de%202006"
)


def test_corte_search_match_resolves_direct_url() -> None:
    adapter = CorteConstitucionalAdapter()
    body = (FIXTURES / "corte_resultados_match.html").read_bytes()
    client = FakeClient(body, final_url=CORTE_SEARCH)
    t = _target("Sentencia C-355 de 2006", ["jurisprudencia", "C-355", "2006"], CORTE_SEARCH)
    res = adapter.validate(t, client)
    assert res.ok is True
    assert CORTE_SEARCH in client.calls
    assert res.resolved_url == (
        "https://www.corteconstitucional.gov.co/relatoria/2006/C-355-06.htm"
    )


def test_corte_search_ambiguous_is_no_encontrado() -> None:
    adapter = CorteConstitucionalAdapter()
    body = (FIXTURES / "corte_resultados_ambiguo.html").read_bytes()
    client = FakeClient(body, final_url=CORTE_SEARCH)
    t = _target("Sentencia C-355 de 2006", ["jurisprudencia", "C-355", "2006"], CORTE_SEARCH)
    res = adapter.validate(t, client)
    assert res.ok is False
    assert res.resolved_url == ""
    assert "match ambiguo" in res.reason


def test_corte_search_no_results_is_no_encontrado() -> None:
    adapter = CorteConstitucionalAdapter()
    body = (FIXTURES / "corte_no_resultados.html").read_bytes()
    client = FakeClient(body, final_url=CORTE_SEARCH)
    t = _target("Sentencia C-355 de 2006", ["jurisprudencia", "C-355", "2006"], CORTE_SEARCH)
    res = adapter.validate(t, client)
    assert res.ok is False
    assert "sin resultados" in res.reason


def test_corte_matches_jurisprudence_by_slash_year_form() -> None:
    """The canonico matcher also recognises the "C-355/06" identifier form."""
    adapter = CorteConstitucionalAdapter()
    body = (
        b"<html><body><ul>"
        b'<li><a href="/relatoria/2006/C-355-06.htm">C-355/06 aborto</a></li>'
        b"</ul></body></html>"
    )
    client = FakeClient(body, final_url=CORTE_SEARCH)
    t = _target("Sentencia C-355 de 2006", ["jurisprudencia", "C-355", "2006"], CORTE_SEARCH)
    res = adapter.validate(t, client)
    assert res.ok is True
    assert res.resolved_url.endswith("/relatoria/2006/C-355-06.htm")
