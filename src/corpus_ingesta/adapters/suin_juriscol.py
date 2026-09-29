"""Adapter for www.suin-juriscol.gov.co/legislacion (leyes y decretos).

SUIN-Juriscol publishes the consolidated text of leyes/decretos as HTML with
``ARTICULO N`` headings, analogous to the Secretaria del Senado base. We reuse
the shared cleaner + article segmenter. When the requested norm is not present
on the fetched page (empty body / "no se encontraron resultados"), we record
``status='no_encontrado'`` and return NO article rows -- never fabricated.

Fully deterministic (BeautifulSoup/lxml/regex). No LLM.
"""

from __future__ import annotations

import datetime as _dt

from bs4 import BeautifulSoup

from ..http_client import FetchError, FetchResult, PoliteClient
from ..models import ArticleFragment, DocumentRecord, IngestStatus, SeedTarget
from .base import SourceAdapter, ValidationResult, clean_html_to_text, slugify

# Phrases that signal the norm is absent from the SUIN response.
_NOT_FOUND_MARKERS = (
    "no se encontraron resultados",
    "no se encontro",
    "no se encontró",
    "sin resultados",
    "0 resultados",
)


def _today() -> str:
    return _dt.date.today().isoformat()


class SuinJuriscolAdapter(SourceAdapter):
    """Parse leyes/decretos published on SUIN-Juriscol."""

    fuente = "suin_juriscol"

    def matches(self, url: str) -> bool:
        return "suin-juriscol.gov.co" in url.lower()

    #: SUIN-specific not-found markers (mirrors the parse-side detection).
    not_found_markers = _NOT_FOUND_MARKERS

    def validate(self, target: SeedTarget, client: PoliteClient) -> ValidationResult:
        """Validate/resolve a SUIN-Juriscol legislacion entry (deterministic).

        * ``/legislacion/?q=...`` SEARCH URL: resolve it against the buscador
          via :meth:`resolve_search` -- GET the results page, parse the anchors
          and follow the FIRST result whose title/link matches the norma by its
          canonico ``[tipo, numero, anio]`` (e.g. "Ley 80 de 1993"). On a hit
          returns ``ok=True`` with ``resolved_url`` = the direct document link;
          on the deterministic "no resultados" marker or no match returns
          ``ok=False`` with a clear reason. Never fabricates.
        * A direct legislacion document link is checked over HTTP and only the
          "no results" marker discards it.
        """
        if self.is_search_url(target.donde_buscar):
            return self.resolve_search(target, client)
        try:
            result: FetchResult = client.get(target.donde_buscar)
        except FetchError as exc:
            return ValidationResult(
                ok=False, reason=f"fetch_failed: {exc.message}", final_url=target.donde_buscar
            )
        text = clean_html_to_text(result.content).lower()
        if self._looks_absent(text):
            return ValidationResult(
                ok=False,
                reason="no_result_marker: SUIN-Juriscol no devolvio resultados para la norma",
                final_url=result.final_url,
                status_code=200,
            )
        return ValidationResult(
            ok=True, reason="reachable", final_url=result.final_url, status_code=200
        )

    def _looks_absent(self, text: str) -> bool:
        lowered = text.lower()
        return any(marker in lowered for marker in _NOT_FOUND_MARKERS)

    def parse(
        self, raw_bytes: bytes, target: SeedTarget
    ) -> tuple[DocumentRecord, list[ArticleFragment]]:
        doc_id = f"{self.fuente}:{slugify(target.norma)}"
        soup = BeautifulSoup(raw_bytes, "lxml")
        titulo = (
            soup.title.get_text(strip=True)
            if soup.title and soup.title.get_text(strip=True)
            else target.norma
        )
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
            organo_emisor="Republica de Colombia (SUIN-Juriscol)",
            areas=list(target.areas),
        )

        if self._looks_absent(text):
            doc.status = IngestStatus.NO_ENCONTRADO.value
            doc.error_detail = "SUIN-Juriscol returned no results for this norm."
            return doc, []

        fragments = self.build_fragments(doc_id, text)
        if not fragments:
            doc.status = IngestStatus.NO_ENCONTRADO.value
            doc.error_detail = "No article headings (ARTICULO N) found in the page."
            return doc, []

        doc.status = IngestStatus.PARSEADO.value
        return doc, fragments
