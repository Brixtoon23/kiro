"""Domain models for the ingestion pipeline.

These dataclasses mirror the seed file and the SQLite schema. They carry no
behaviour beyond light normalisation so the pipeline stays deterministic and
easy to unit test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class IngestStatus(StrEnum):
    """Lifecycle status for a document as it moves through the pipeline.

    A ``StrEnum`` so the value serialises directly to SQLite/JSON.
    """

    DESCARGADO = "descargado"
    PARSEADO = "parseado"
    NO_ENCONTRADO = "no_encontrado"
    ERROR = "error"


@dataclass
class SeedTarget:
    """One entry of ``seed_targets.json``.

    ``canonico`` is the ``[tipo, numero, anio]`` triple from the seed. Any of
    the three positions may be ``None`` (e.g. the Constitution has no number or
    year), which is why they are stored as separate optional fields too.
    """

    norma: str
    canonico: list[Any]
    items_del_banco: int
    areas: list[str]
    donde_buscar: str

    @property
    def tipo(self) -> str | None:
        return self.canonico[0] if len(self.canonico) > 0 else None

    @property
    def numero(self) -> str | None:
        v = self.canonico[1] if len(self.canonico) > 1 else None
        return str(v) if v is not None else None

    @property
    def anio(self) -> str | None:
        v = self.canonico[2] if len(self.canonico) > 2 else None
        return str(v) if v is not None else None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SeedTarget:
        canonico = list(raw.get("canonico") or [])
        # Normalise the triple to always have exactly 3 positions.
        while len(canonico) < 3:
            canonico.append(None)
        return cls(
            norma=str(raw["norma"]),
            canonico=canonico[:3],
            items_del_banco=int(raw.get("items_del_banco", 0)),
            areas=list(raw.get("areas") or []),
            donde_buscar=str(raw["donde_buscar"]),
        )


@dataclass
class DocumentRecord:
    """Metadata row for a single norm/document (mirrors the ``documents`` table)."""

    doc_id: str
    norma: str
    tipo: str | None = None
    numero: str | None = None
    anio: str | None = None
    titulo: str | None = None
    fuente: str | None = None
    url: str | None = None
    fecha_consulta: str | None = None
    organo_emisor: str | None = None
    vigencia: str | None = None
    areas: list[str] = field(default_factory=list)
    status: str = IngestStatus.NO_ENCONTRADO.value
    raw_path: str | None = None
    error_detail: str | None = None


@dataclass
class ArticleFragment:
    """One article-level fragment of a document (mirrors the ``articles`` table).

    ``offset_start``/``offset_end`` are character offsets into the parsed
    document text, giving full traceability from fragment back to norma +
    articulo. ``vector_id``/``embedding_ref`` are reserved for a future vector
    DB and stay ``None`` during the ingestion phase.
    """

    article_id: str
    doc_id: str
    articulo_numero: str | None
    encabezado: str | None
    texto: str
    offset_start: int
    offset_end: int
    vector_id: str | None = None
    embedding_ref: str | None = None
