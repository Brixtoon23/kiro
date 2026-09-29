"""LEXIS collector tests with a MOCKED headless browser (no network, no Chromium).

These validate the deterministic flow the user described -- apply the type
filter, locate the row by numero/anio, open ``viewDocument/{id}`` and download
the ``.docx`` -- WITHOUT touching the real portal or downloading a browser. The
:class:`FakeLexisBrowser` records every action so we can assert the exact clicks
happened in order.
"""

from __future__ import annotations

from pathlib import Path

from corpus_ingesta.lexis_client import (
    LEXIS_DETAIL_BASE,
    LexisCollector,
    LexisRow,
    is_lexis_tipo,
    lexis_filter_for,
    select_matching_row,
)

FIXTURES = Path(__file__).parent / "fixtures"
DOCX = (FIXTURES / "lexis_ley_articulos.docx").read_bytes()


class FakeLexisBrowser:
    """Offline stand-in for a Playwright-driven LEXIS browser.

    ``rows`` are the canned results returned after a filter is applied;
    ``docx`` the bytes the download control yields. Every action is appended to
    ``self.actions`` so tests can assert the deterministic click sequence.
    """

    def __init__(self, rows: list[LexisRow], docx: bytes = DOCX) -> None:
        self.rows = rows
        self.docx = docx
        self.actions: list[tuple[str, str]] = []
        self.applied_filter = ""

    def open_search(self, url: str) -> None:
        self.actions.append(("open_search", url))

    def apply_type_filter(self, filter_label: str) -> None:
        self.applied_filter = filter_label
        self.actions.append(("apply_type_filter", filter_label))

    def result_rows(self) -> list[LexisRow]:
        self.actions.append(("result_rows", ""))
        return self.rows

    def open_detail(self, doc_id: str) -> str:
        self.actions.append(("open_detail", doc_id))
        return f"{LEXIS_DETAIL_BASE}{doc_id}"

    def download_docx(self) -> bytes:
        self.actions.append(("download_docx", ""))
        return self.docx

    def close(self) -> None:
        self.actions.append(("close", ""))


def test_tipo_filter_mapping() -> None:
    assert lexis_filter_for("ley") == "Ley"
    assert lexis_filter_for("decreto") == "Decreto"
    assert lexis_filter_for("acuerdo") == "Acuerdo"
    assert lexis_filter_for("codigo_general_proceso") == "Codigo"
    assert lexis_filter_for("estatuto_tributario") == "Estatuto"
    assert lexis_filter_for("constitucion") == "Constitucion"
    # Not in LEXIS -> no filter (collector will report no_encontrado).
    assert lexis_filter_for("decision_andina_486") is None
    assert lexis_filter_for("jurisprudencia") is None
    assert is_lexis_tipo("ley") and not is_lexis_tipo("jurisprudencia")


def test_collect_matches_by_numero_anio_and_downloads() -> None:
    rows = [
        LexisRow(doc_id="1600001", titulo="Ley 100 de 1993 - Seguridad social"),
        LexisRow(doc_id="1600025", titulo="Ley 80 de 1993 - Estatuto de contratacion"),
    ]
    browser = FakeLexisBrowser(rows)
    collector = LexisCollector(browser=browser, base_delay=0.0)

    result = collector.collect(tipo="ley", numero="80", anio="1993")

    assert result.ok
    assert result.docx == DOCX
    assert result.lexis_doc_id == "1600025"
    assert result.detail_url == f"{LEXIS_DETAIL_BASE}1600025"
    # Deterministic click sequence: search -> filter -> rows -> detail -> download.
    kinds = [a[0] for a in browser.actions]
    assert kinds == [
        "open_search",
        "apply_type_filter",
        "result_rows",
        "open_detail",
        "download_docx",
    ]
    assert browser.applied_filter == "Ley"


def test_collect_no_match_returns_no_encontrado_without_download() -> None:
    rows = [LexisRow(doc_id="1600001", titulo="Ley 100 de 1993 - Seguridad social")]
    browser = FakeLexisBrowser(rows)
    collector = LexisCollector(browser=browser, base_delay=0.0)

    result = collector.collect(tipo="ley", numero="80", anio="1993")

    assert not result.ok
    assert result.docx == b""
    assert "numero=80" in result.reason
    # Never opened a detail or clicked download when there is no clear match.
    kinds = [a[0] for a in browser.actions]
    assert "open_detail" not in kinds
    assert "download_docx" not in kinds


def test_collect_unsupported_tipo_skips_browser() -> None:
    browser = FakeLexisBrowser([])
    collector = LexisCollector(browser=browser, base_delay=0.0)

    result = collector.collect(tipo="jurisprudencia", numero="C-355", anio="2006")

    assert not result.ok
    assert "no está cubierto por LEXIS" in result.reason
    # The browser was never driven for an unsupported type.
    assert browser.actions == []


def test_collect_numero_zero_padding_variant_matches() -> None:
    """A seed ``046`` must match a portal row rendered as ``46``."""
    rows = [LexisRow(doc_id="1700010", titulo="Decreto 46 de 2024 - algo")]
    browser = FakeLexisBrowser(rows)
    collector = LexisCollector(browser=browser, base_delay=0.0)

    result = collector.collect(tipo="decreto", numero="046", anio="2024")

    assert result.ok
    assert result.lexis_doc_id == "1700010"


def test_select_matching_row_typeless_norm_uses_single_type_row() -> None:
    """A code/statute (no numero/anio) matches only a single unambiguous row."""
    rows = [
        LexisRow(doc_id="1", titulo="Codigo General del Proceso", tipo="Codigo"),
    ]
    row, reason = select_matching_row(rows, numero=None, anio=None, filter_label="Codigo")
    assert row is not None and row.doc_id == "1"
    assert reason == ""

    # Ambiguous: two rows of the same type -> no fabricated pick.
    rows2 = [
        LexisRow(doc_id="1", titulo="Codigo A", tipo="Codigo"),
        LexisRow(doc_id="2", titulo="Codigo B", tipo="Codigo"),
    ]
    row2, reason2 = select_matching_row(rows2, numero=None, anio=None, filter_label="Codigo")
    assert row2 is None
    assert "ambiguo" in reason2


def test_collect_retries_then_succeeds_on_transient_error() -> None:
    """A transient browser error is retried with backoff, never crashes."""

    class FlakyBrowser(FakeLexisBrowser):
        def __init__(self, rows):
            super().__init__(rows)
            self._calls = 0

        def open_search(self, url: str) -> None:
            self._calls += 1
            if self._calls == 1:
                raise RuntimeError("navigation glitch")
            super().open_search(url)

    rows = [LexisRow(doc_id="1600025", titulo="Ley 80 de 1993")]
    browser = FlakyBrowser(rows)
    collector = LexisCollector(browser=browser, base_delay=0.0)

    result = collector.collect(tipo="ley", numero="80", anio="1993")
    assert result.ok
    assert result.lexis_doc_id == "1600025"
