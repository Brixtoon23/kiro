"""Adapter for lexis.minjusticia.gov.co (MinJusticia LEXIS).

LEXIS is the primary source for consolidated NORMS (leyes, decretos, codigos,
estatutos, constitucion, acto legislativo, acuerdo). Its buscador and detail
pages are a JavaScript SPA, so the RAW is captured with a headless browser (see
:mod:`corpus_ingesta.lexis_client` / :mod:`corpus_ingesta.lexis_playwright`)
which downloads the official ``.docx`` for each norm. This adapter therefore
parses **Word** RAW (via ``python-docx``) in Phase C -- but it degrades
gracefully: if the RAW is HTML (e.g. an older capture) or PDF it reuses the
shared cleaner. Segmentation is the same rule-based ARTICULO split used by every
source, so every fragment stays traceable to norma + articulo.

Jurisprudence is NOT handled here (it goes to the Corte Constitucional flow).

Fully deterministic (python-docx / BeautifulSoup / regex). No LLM.
"""

from __future__ import annotations

import datetime as _dt
import io

from ..lexis_client import is_lexis_tipo
from ..models import ArticleFragment, DocumentRecord, IngestStatus, SeedTarget
from .base import SourceAdapter, slugify


def _today() -> str:
    return _dt.date.today().isoformat()


def _looks_like_docx(raw: bytes) -> bool:
    """True when ``raw`` is a .docx (a ZIP container: magic ``PK\\x03\\x04``).

    A .docx is an OOXML ZIP. We additionally require the ``word/`` marker to be
    present so a random ZIP is not mistaken for a Word document.
    """
    if raw[:4] != b"PK\x03\x04":
        return False
    # Cheap containment check on the central directory bytes.
    head = raw[:8192]
    return b"word/" in raw[:200000] or b"[Content_Types].xml" in head


def extract_docx_text(raw: bytes) -> str:
    """Extract normalized plain text from ``.docx`` bytes with python-docx.

    Deterministic, no LLM/OCR: reads the Word text layer paragraph by paragraph
    and applies the SAME normalization as the HTML/PDF cleaners so the result is
    directly compatible with :func:`split_articles` and its character offsets.
    """
    from docx import Document  # lazy import: only needed on the .docx path

    from .base import _normalize_text

    document = Document(io.BytesIO(raw))
    parts: list[str] = [p.text for p in document.paragraphs]
    # Include table cell text too (consolidated norms sometimes use tables).
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text:
                    parts.append(cell.text)
    return _normalize_text("\n".join(parts))


class LexisMinjusticiaAdapter(SourceAdapter):
    """Adapter for the MinJusticia LEXIS portal (Word RAW, deterministic)."""

    fuente = "lexis_minjusticia"

    def matches(self, url: str) -> bool:
        return "lexis.minjusticia.gov.co" in url.lower()

    def handles_tipo(self, tipo: str | None) -> bool:
        """True when the norm ``tipo`` is one LEXIS is expected to serve.

        Phase B uses this (alongside the canonico) to route a seed entry to the
        headless LEXIS collector instead of a plain HTTP GET, regardless of the
        entry's legacy ``donde_buscar`` URL.
        """
        return is_lexis_tipo(tipo)

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
            organo_emisor="Ministerio de Justicia y del Derecho (LEXIS)",
            areas=list(target.areas),
        )

        # File-type aware extraction: LEXIS delivers Word; fall back to the
        # shared HTML/PDF cleaner for any non-docx capture.
        if _looks_like_docx(raw_bytes):
            text = extract_docx_text(raw_bytes)
        else:
            text = self.clean_html_to_text(raw_bytes)

        fragments = self.build_fragments(doc_id, text)
        if not fragments:
            doc.status = IngestStatus.NO_ENCONTRADO.value
            doc.error_detail = (
                "El RAW de LEXIS no contenía texto de artículos segmentable; "
                "se conserva el archivo crudo para revisión posterior."
            )
            return doc, []

        doc.status = IngestStatus.PARSEADO.value
        return doc, fragments
