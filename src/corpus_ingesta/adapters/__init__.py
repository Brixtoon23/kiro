"""Source adapters (one per official gov.co portal).

The pipeline routes each seed target to the FIRST adapter whose ``matches(url)``
returns True. Order matters only for overlapping domains (none currently
overlap). All adapters share the :class:`~.base.SourceAdapter` interface so new
official gov.co portals can be added without touching the pipeline.
"""

from __future__ import annotations

from .base import RawDoc, SourceAdapter, ValidationResult
from .corteconstitucional import CorteConstitucionalAdapter
from .lexis_minjusticia import LexisMinjusticiaAdapter
from .samai_consejoestado import SamaiConsejoEstadoAdapter
from .secretariasenado import SecretariaSenadoAdapter
from .suin_juriscol import SuinJuriscolAdapter


def default_adapters() -> list[SourceAdapter]:
    """Return the ordered list of adapters used by the pipeline."""
    return [
        SecretariaSenadoAdapter(),
        SuinJuriscolAdapter(),
        CorteConstitucionalAdapter(),
        LexisMinjusticiaAdapter(),
        SamaiConsejoEstadoAdapter(),
    ]


__all__ = [
    "RawDoc",
    "SourceAdapter",
    "ValidationResult",
    "SecretariaSenadoAdapter",
    "SuinJuriscolAdapter",
    "CorteConstitucionalAdapter",
    "LexisMinjusticiaAdapter",
    "SamaiConsejoEstadoAdapter",
    "default_adapters",
]
