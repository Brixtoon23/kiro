"""Best-effort adapter for corteconstitucional.gov.co/relatoria jurisprudence.

DOCUMENTED LIMITATION: the "buscador de jurisprudencia" is an Angular SPA that
redirects to ``/relatoria/buscador-jurisprudencia`` and loads its results via
asynchronous JSON calls. Without a headless browser we cannot deterministically
extract the sentence list from the landing HTML alone.

Behaviour (never crash, never fabricate):

* We fetch and SAVE the raw landing page (raw-first) via the default fetch.
* If the response carries recognizable sentence metadata (a JSON payload with
  ``providencia``/``sentencia`` fields, or ``ARTICULO`` headings in HTML), we
  parse what we can deterministically.
* Otherwise we return ``status='no_encontrado'`` with a clear message so the
  pipeline logs the documented limitation and continues.

Fully deterministic (json/regex/BeautifulSoup). No LLM.
"""

from __future__ import annotations

import datetime as _dt
import json
import re

from ..http_client import PoliteClient
from ..models import ArticleFragment, DocumentRecord, IngestStatus, SeedTarget
from .base import SourceAdapter, ValidationResult, slugify

# A very loose detector for sentence identifiers like "C-123/24", "T-045 de 2020".
_SENTENCIA_RE = re.compile(r"\b([CTSA]U?-\d{1,4}(?:[/ ]\s*(?:de\s+)?\d{2,4})?)\b")


def _today() -> str:
    return _dt.date.today().isoformat()


class CorteConstitucionalAdapter(SourceAdapter):
    """Best-effort jurisprudence adapter (SPA limitation documented)."""

    fuente = "corteconstitucional"

    def matches(self, url: str) -> bool:
        return "corteconstitucional.gov.co" in url.lower()

    def validate(self, target: SeedTarget, client: PoliteClient) -> ValidationResult:
        """Validate/resolve a Corte Constitucional relatoria entry (deterministic).

        For a ``/relatoria/?q=Sentencia ...`` SEARCH URL we resolve against the
        buscador via :meth:`resolve_search`: GET the results page, parse the
        anchors and follow the FIRST result matching the sentence by its
        canonico ``[tipo, numero, anio]`` (e.g. "C-355 de 2006", also matched as
        "C-355/06"). On a hit returns ``ok=True`` with ``resolved_url`` = the
        direct providencia link; on no/ambiguous results returns ``ok=False``
        with a clear reason.

        DOCUMENTED LIMITATION: the relatoria buscador is an Angular SPA that
        loads results via async JSON, so a single GET of the landing page may
        expose no parseable result anchors. In that case the entry is honestly
        recorded as no_encontrado ("sin resultados en buscador") -- we never
        fabricate a providencia URL. A direct (non ``?q=``) link is checked over
        HTTP with the base reachability logic.
        """
        if self.is_search_url(target.donde_buscar):
            return self.resolve_search(target, client)
        return super().validate(target, client)

    def _try_json(self, raw_bytes: bytes) -> list[str]:
        """If the payload is JSON, pull any sentence-looking identifiers."""
        try:
            data = json.loads(raw_bytes.decode("utf-8", errors="ignore"))
        except (ValueError, UnicodeDecodeError):
            return []
        found: list[str] = []
        flat = json.dumps(data, ensure_ascii=False)
        found.extend(m.group(1) for m in _SENTENCIA_RE.finditer(flat))
        # De-duplicate while preserving order.
        seen: set[str] = set()
        return [s for s in found if not (s in seen or seen.add(s))]

    def parse(
        self, raw_bytes: bytes, target: SeedTarget
    ) -> tuple[DocumentRecord, list[ArticleFragment]]:
        doc_id = f"{self.fuente}:{slugify(target.norma)}"
        doc = DocumentRecord(
            doc_id=doc_id,
            norma=target.norma,
            tipo=target.tipo,
            numero=target.numero,
            anio=target.anio,
            titulo=target.norma,
            fuente=self.fuente,
            url=target.donde_buscar,
            fecha_consulta=_today(),
            organo_emisor="Corte Constitucional de Colombia",
            areas=list(target.areas),
        )

        sentencias = self._try_json(raw_bytes)
        text = self.clean_html_to_text(raw_bytes)
        if not sentencias:
            sentencias = [
                m.group(1) for m in _SENTENCIA_RE.finditer(text)
            ]

        # Try article-level segmentation (a saved sentence body may have them).
        fragments = self.build_fragments(doc_id, text)

        if not fragments and not sentencias:
            doc.status = IngestStatus.NO_ENCONTRADO.value
            doc.error_detail = (
                "Angular SPA buscador: results load via async JSON; no "
                "deterministic sentence text available without a browser. "
                "Raw landing page saved for later processing."
            )
            return doc, []

        doc.status = IngestStatus.PARSEADO.value
        if sentencias and not fragments:
            # Record the discovered sentence identifiers as a single METADATA
            # fragment. Review issue #6: this is NOT a span of the cleaned page
            # text (unlike real article fragments whose offsets slice back to
            # clean_html_to_text output), so we key it as ``metadata-sentencias``
            # and set sentinel offsets of -1/-1 to signal explicitly that these
            # do not index the source text. No body text is fabricated.
            listado = ", ".join(sentencias[:200])
            fragments = [
                ArticleFragment(
                    article_id=f"{doc_id}:metadata-sentencias",
                    doc_id=doc_id,
                    articulo_numero=None,
                    encabezado="Listado de sentencias detectadas (metadato)",
                    texto=listado,
                    offset_start=-1,
                    offset_end=-1,
                )
            ]
        return doc, fragments
