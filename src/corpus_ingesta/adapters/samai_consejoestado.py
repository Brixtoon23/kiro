"""Best-effort adapter for samai.consejodeestado.gov.co (Consejo de Estado).

DOCUMENTED LIMITATION: SAMAI is an ASP.NET WebForms application (~450KB) that
drives its results through ``__VIEWSTATE`` / ``__EVENTVALIDATION`` postbacks.
Retrieving the actual providencia listing requires simulating the stateful
postback flow, which is not deterministically reproducible from a single GET.

Behaviour (never crash, never fabricate):

* We fetch and SAVE the raw landing page (raw-first) via the default fetch.
* If the saved page happens to contain consolidated ARTICULO text we segment
  it. Otherwise we record ``status='no_encontrado'`` with a clear message about
  the WebForms/viewstate limitation so the pipeline logs it and continues.

Fully deterministic (BeautifulSoup/lxml/regex). No LLM.
"""

from __future__ import annotations

import datetime as _dt

from bs4 import BeautifulSoup

from ..http_client import PoliteClient
from ..models import ArticleFragment, DocumentRecord, IngestStatus, SeedTarget
from .base import SourceAdapter, ValidationResult, slugify


def _today() -> str:
    return _dt.date.today().isoformat()


class SamaiConsejoEstadoAdapter(SourceAdapter):
    """Best-effort adapter for the Consejo de Estado SAMAI relatoria."""

    fuente = "samai_consejoestado"

    def matches(self, url: str) -> bool:
        return "samai.consejodeestado.gov.co" in url.lower()

    def validate(self, target: SeedTarget, client: PoliteClient) -> ValidationResult:
        """Documented OPTIONAL, non-blocking skip.

        The Consejo de Estado SAMAI portal is an ASP.NET WebForms application
        whose providencia listing needs stateful ``__VIEWSTATE`` postbacks, so a
        single deterministic GET cannot confirm a real hit. Per the reglamento
        this source is optional and must never block Phase A: we return a
        non-error, ``optional`` result. No network call is made.
        """
        return ValidationResult(
            ok=False,
            reason="optional_not_supported: Consejo de Estado (SAMAI) es opcional "
            "y no bloquea el pipeline",
            final_url=target.donde_buscar,
            status_code=None,
            optional=True,
        )

    def _is_webforms_shell(self, raw_bytes: bytes) -> bool:
        head = raw_bytes[:200000].lower()
        return b"__viewstate" in head

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
            organo_emisor="Consejo de Estado de Colombia",
            areas=list(target.areas),
        )

        fragments = self.build_fragments(doc_id, text)
        if not fragments:
            doc.status = IngestStatus.NO_ENCONTRADO.value
            if self._is_webforms_shell(raw_bytes):
                doc.error_detail = (
                    "ASP.NET WebForms shell (viewstate/postbacks): providencia "
                    "listing requires stateful postbacks, not a single GET. Raw "
                    "landing page saved for later processing."
                )
            else:
                doc.error_detail = "No consolidated article text found; raw page saved."
            return doc, []

        doc.status = IngestStatus.PARSEADO.value
        return doc, fragments
