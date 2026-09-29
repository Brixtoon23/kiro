"""Adapter for www.secretariasenado.gov.co/senado/basedoc (PRIMARY source).

This is the fully-supported source: the ``basedoc`` pages render each norm as
in-line HTML with ``ARTICULO N`` headings. We clean the HTML to text and
segment at article boundaries, yielding one traceable fragment per article.

Everything here is deterministic (BeautifulSoup/lxml/regex). No LLM.
"""

from __future__ import annotations

import datetime as _dt

from bs4 import BeautifulSoup

from ..http_client import FetchError, FetchResult, PoliteClient
from ..models import ArticleFragment, DocumentRecord, IngestStatus, SeedTarget
from .base import SourceAdapter, ValidationResult, clean_html_to_text, slugify


def _today() -> str:
    return _dt.date.today().isoformat()


class SecretariaSenadoAdapter(SourceAdapter):
    """Parse norms published on the Secretaria del Senado base documental."""

    fuente = "secretariasenado"

    def matches(self, url: str) -> bool:
        return "secretariasenado.gov.co" in url.lower()

    def validate(self, target: SeedTarget, client: PoliteClient) -> ValidationResult:
        """Validate/resolve a Secretaria del Senado base-documental entry.

        Two deterministic paths (no LLM):

        * ``?q=...`` SEARCH URL (not a direct ``.html``): resolve it against the
          buscador. :meth:`resolve_search` GETs the results page, parses the
          anchors and follows the FIRST result matching the norma (canonico
          triple / label). On a hit it returns ``ok=True`` with
          ``resolved_url`` = the direct ``.html`` document URL; on no/ambiguous
          results it returns ``ok=False`` with a clear reason. Never fabricated.
        * Direct ``.html`` link (even with a ``?q=`` marketing param, e.g. the
          Constitution): checked over HTTP, guarding against an unexpected
          search-results body.
        """
        url = target.donde_buscar
        if self.is_search_url(url):
            return self.resolve_search(target, client)
        try:
            result: FetchResult = client.get(url)
        except FetchError as exc:
            return ValidationResult(
                ok=False, reason=f"fetch_failed: {exc.message}", final_url=url
            )
        text = clean_html_to_text(result.content)
        if self._is_search_page(result.final_url, text):
            return ValidationResult(
                ok=False,
                reason="pagina_de_busqueda: la respuesta es una lista de resultados, "
                "no el texto consolidado",
                final_url=result.final_url,
                status_code=200,
            )
        return ValidationResult(
            ok=True, reason="reachable", final_url=result.final_url, status_code=200
        )

    def _extract_titulo(self, soup: BeautifulSoup, target: SeedTarget) -> str:
        # Prefer the document <title>, then the first <h1>/<h2>, else the norma.
        if soup.title and soup.title.get_text(strip=True):
            return soup.title.get_text(strip=True)
        for tag in ("h1", "h2"):
            el = soup.find(tag)
            if el and el.get_text(strip=True):
                return el.get_text(strip=True)
        return target.norma

    @staticmethod
    def _is_search_page(url: str, text: str) -> bool:
        """True when the fetched page is a base-documental SEARCH result, not a
        consolidated norm.

        Review issue #4: five of the six seed URLs are ``basedoc/?q=<norma>``
        search endpoints rather than direct ``.html`` document pages. Such a
        page has no ``ARTICULO N`` bodies, so we detect it and record a precise
        ``no_encontrado`` reason instead of a generic "no headings" message. A
        direct ``.../algo.html?q=...`` link (e.g. the Constitution) still points
        at a document, so only a ``?q=`` search WITHOUT a ``.html`` path counts
        as a search page. We never fabricate a document from a search page.
        """
        low = url.lower()
        path_before_query = low.split("?", 1)[0]
        has_q = "?q=" in low or "&q=" in low
        if has_q and not path_before_query.endswith(".html"):
            return True
        lowered = text.lower()
        markers = ("resultados de la busqueda", "resultados de busqueda",
                   "lista-resultados", "seleccione uno")
        return any(m in lowered for m in markers)

    def parse(
        self, raw_bytes: bytes, target: SeedTarget
    ) -> tuple[DocumentRecord, list[ArticleFragment]]:
        doc_id = f"{self.fuente}:{slugify(target.norma)}"
        soup = BeautifulSoup(raw_bytes, "lxml")
        titulo = self._extract_titulo(soup, target)
        text = self.clean_html_to_text(raw_bytes)

        doc = DocumentRecord(
            doc_id=doc_id,
            norma=target.norma,
            tipo=target.tipo,
            numero=target.numero,
            anio=target.anio,
            titulo=titulo,
            fuente=self.fuente,
            url=target.donde_buscar,
            fecha_consulta=_today(),
            organo_emisor="Congreso de la Republica de Colombia",
            areas=list(target.areas),
        )

        if self._is_search_page(target.donde_buscar, text):
            # A search-results page, not a consolidated norm. Honest outcome:
            # no_encontrado with a contract reminder (seed URLs should be direct
            # .html document links). No fabrication.
            doc.status = IngestStatus.NO_ENCONTRADO.value
            doc.error_detail = (
                "URL apunta a una pagina de busqueda (?q=...), no al texto "
                "consolidado. Use un enlace .html directo al documento en "
                "basedoc para poder segmentar articulos."
            )
            return doc, []

        fragments = self.build_fragments(doc_id, text)

        if not fragments:
            # No article headings found: do NOT fabricate. Record no_encontrado.
            doc.status = IngestStatus.NO_ENCONTRADO.value
            doc.error_detail = "No article headings (ARTICULO N) found in the page."
            return doc, []

        doc.status = IngestStatus.PARSEADO.value
        return doc, fragments
