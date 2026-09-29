"""Best-effort adapter for lexis.minjusticia.gov.co/buscador.

The Lexis buscador serves a compact HTML page (~10KB) that is mostly a search
UI shell. When a norm's consolidated text is present we segment at ARTICULO
boundaries like the primary sources; otherwise we save the raw page and record
``status='no_encontrado'`` with a documented note. Never fabricate content.

Fully deterministic (BeautifulSoup/lxml/regex). No LLM.
"""

from __future__ import annotations

import datetime as _dt

from bs4 import BeautifulSoup

from ..models import ArticleFragment, DocumentRecord, IngestStatus, SeedTarget
from .base import SourceAdapter, slugify


def _today() -> str:
    return _dt.date.today().isoformat()


class LexisMinjusticiaAdapter(SourceAdapter):
    """Best-effort adapter for the MinJusticia Lexis buscador."""

    fuente = "lexis_minjusticia"

    def matches(self, url: str) -> bool:
        return "lexis.minjusticia.gov.co" in url.lower()

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
            organo_emisor="Ministerio de Justicia y del Derecho (Lexis)",
            areas=list(target.areas),
        )

        fragments = self.build_fragments(doc_id, text)
        if not fragments:
            doc.status = IngestStatus.NO_ENCONTRADO.value
            doc.error_detail = (
                "Lexis buscador shell contained no consolidated article text; "
                "raw page saved for later processing."
            )
            return doc, []

        doc.status = IngestStatus.PARSEADO.value
        return doc, fragments
