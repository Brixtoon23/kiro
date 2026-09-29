"""Playwright-backed :class:`LexisBrowser` implementation.

This is the PRODUCTION browser driver for the LEXIS collector. It is imported
lazily (only when a real run needs it) so the deterministic test-suite -- which
uses a fake browser -- never has to import Playwright or download Chromium.

Headless is mandatory (the sandbox/user machine has no display server) and the
browser is launched with ``--no-sandbox`` because the surrounding environment
already provides isolation (nesting a Chromium sandbox inside it fails).

Requires the Chromium browser binary, installed once with::

    uv run playwright install chromium

The CSS/selector strings below encode the SPA's stable structure (apply the
"tipo de norma" filter, read result rows, open ``viewDocument/{id}``, click the
Word-download control). They are centralised here so that if the portal markup
shifts, only this file changes. Everything remains deterministic: no LLM.
"""

from __future__ import annotations

from typing import Any

from .lexis_client import LEXIS_DETAIL_BASE, LexisRow

# Selector contract for the LEXIS SPA. Kept in one place for easy maintenance
# when the portal markup evolves. These mirror the "always click the same
# parts" flow the user verified in their browser.
SEL_TYPE_FILTER = "select[name='tipoNorma'], #tipoNorma"
SEL_RESULT_ROW = ".resultado-item, table.resultados tbody tr, .list-group-item"
SEL_ROW_TITLE = ".titulo, td.titulo, .list-group-item-heading"
SEL_ROW_TIPO = ".tipo, td.tipo"
SEL_DOWNLOAD_WORD = "a[href*='word'], button.descargar-word, a.descargar-word"

DEFAULT_NAV_TIMEOUT_MS = 30_000


class PlaywrightLexisBrowser:
    """Drive the LEXIS SPA with a headless Chromium via Playwright.

    Usage::

        with PlaywrightLexisBrowser() as browser:
            collector = LexisCollector(browser=browser)
            result = collector.collect(tipo="ley", numero="80", anio="1993")
    """

    def __init__(self, *, headless: bool = True, nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS):
        self._headless = headless
        self._nav_timeout_ms = nav_timeout_ms
        # Typed as Any because Playwright objects are only imported lazily; this
        # keeps mypy happy without importing playwright at module load time.
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Launch headless Chromium (``--no-sandbox``) and open a page."""
        from playwright.sync_api import sync_playwright  # lazy import

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self._headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        self._context = self._browser.new_context(accept_downloads=True)
        self._page = self._context.new_page()
        self._page.set_default_timeout(self._nav_timeout_ms)

    def __enter__(self) -> PlaywrightLexisBrowser:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        for obj in (self._context, self._browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = self._browser = self._context = self._page = None

    # -- LexisBrowser protocol ------------------------------------------

    def open_search(self, url: str) -> None:
        assert self._page is not None, "browser not started"
        self._page.goto(url, wait_until="networkidle")

    def apply_type_filter(self, filter_label: str) -> None:
        assert self._page is not None
        # The buscador exposes the tipo as a <select>; choosing by visible label
        # is the deterministic "click the same part" action the user described.
        self._page.select_option(SEL_TYPE_FILTER, label=filter_label)
        self._page.wait_for_load_state("networkidle")

    def result_rows(self) -> list[LexisRow]:
        assert self._page is not None
        rows: list[LexisRow] = []
        for handle in self._page.query_selector_all(SEL_RESULT_ROW):
            title_el = handle.query_selector(SEL_ROW_TITLE)
            titulo = (title_el.inner_text() if title_el else handle.inner_text()).strip()
            tipo_el = handle.query_selector(SEL_ROW_TIPO)
            tipo = tipo_el.inner_text().strip() if tipo_el else ""
            # The row carries the LEXIS document id either as a data attribute
            # or embedded in a viewDocument link.
            doc_id = handle.get_attribute("data-doc-id") or ""
            if not doc_id:
                link = handle.query_selector("a[href*='viewDocument/']")
                if link:
                    href = link.get_attribute("href") or ""
                    doc_id = href.rstrip("/").rsplit("/", 1)[-1]
            if doc_id:
                rows.append(LexisRow(doc_id=doc_id, titulo=titulo, tipo=tipo))
        return rows

    def open_detail(self, doc_id: str) -> str:
        assert self._page is not None
        url = f"{LEXIS_DETAIL_BASE}{doc_id}"
        self._page.goto(url, wait_until="networkidle")
        return self._page.url

    def download_docx(self) -> bytes:
        assert self._page is not None
        with self._page.expect_download() as dl_info:
            self._page.click(SEL_DOWNLOAD_WORD)
        download = dl_info.value
        path = download.path()
        if path is None:
            return b""
        return path.read_bytes()
