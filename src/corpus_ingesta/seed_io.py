"""Shared seed-loading and routing helpers used by all three phases.

These helpers are intentionally tiny and dependency-free (beyond the models and
adapters) so Phase A, Phase B and Phase C can each import them without pulling
in any monolithic orchestrator. Everything here is deterministic; no LLM.
"""

from __future__ import annotations

import json
from pathlib import Path

from .adapters import SourceAdapter
from .models import SeedTarget


def load_seed(seed_path: str) -> list[SeedTarget]:
    """Load and parse a seed JSON file into :class:`SeedTarget` objects.

    Reads the ``documentos`` array (the seed and clean-seed share this shape).
    """
    data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
    return [SeedTarget.from_dict(item) for item in data.get("documentos", [])]


def route(target: SeedTarget, adapters: list[SourceAdapter]) -> SourceAdapter | None:
    """Return the first adapter whose ``matches(url)`` is true, or ``None``."""
    for adapter in adapters:
        if adapter.matches(target.donde_buscar):
            return adapter
    return None
