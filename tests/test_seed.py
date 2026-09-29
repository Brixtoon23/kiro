"""Seed file integrity + model parsing."""

from __future__ import annotations

import json
from pathlib import Path

from corpus_ingesta.models import SeedTarget

SEED_PATH = Path(__file__).resolve().parents[1] / "seed_targets.json"

# A few fully-specified norms known to be present verbatim in the full seed.
EXPECTED_VERBATIM_NORMAS = [
    "Constitucion",
    "Codigo general proceso",
    "Codigo sustantivo trabajo",
    "Estatuto tributario",
    "Decision andina 486",
    "Estatuto consumidor",
]

# Named references that do not correspond to a real norm and must therefore end
# up in no_encontrados.json once Phase A queries the buscador (never fabricated).
EXPECTED_INEXISTENT_NORMAS = [
    "Ley 11500 de 2007",
    "Ley 1150 de 2005",
    "Ley 116 de 2006",
]

# The full corpus seed as shipped.
EXPECTED_SEED_COUNT = 186


def _load() -> dict:
    return json.loads(SEED_PATH.read_text(encoding="utf-8"))


def test_seed_is_valid_json_with_expected_seed() -> None:
    data = _load()
    assert data["seed"] == 20260830


def test_seed_has_full_corpus_including_verbatim_and_inexistent_entries() -> None:
    data = _load()
    normas = [d["norma"] for d in data["documentos"]]
    assert len(normas) == EXPECTED_SEED_COUNT
    for norma in EXPECTED_VERBATIM_NORMAS + EXPECTED_INEXISTENT_NORMAS:
        assert norma in normas


def test_seed_byte_exact_fields() -> None:
    data = _load()
    docs = {d["norma"]: d for d in data["documentos"]}

    const = docs["Constitucion"]
    assert const["canonico"] == ["constitucion", None, None]
    assert const["items_del_banco"] == 90
    assert const["donde_buscar"] == (
        "http://www.secretariasenado.gov.co/senado/basedoc/"
        "constitucion_politica_1991.html?q=Constitucion"
    )

    consumidor = docs["Estatuto consumidor"]
    assert consumidor["items_del_banco"] == 13
    assert consumidor["canonico"] == ["estatuto_consumidor", None, None]


def test_seed_inexistent_references_shape() -> None:
    data = _load()
    docs = {d["norma"]: d for d in data["documentos"]}

    ref = docs["Ley 11500 de 2007"]
    assert ref["canonico"] == ["ley", "11500", "2007"]
    assert ref["donde_buscar"].startswith("https://www.suin-juriscol.gov.co/legislacion/")

    for norma in EXPECTED_INEXISTENT_NORMAS:
        entry = docs[norma]
        assert entry["canonico"][0] == "ley"
        assert "suin-juriscol.gov.co" in entry["donde_buscar"]


def test_seedtarget_from_dict_normalises_canonico() -> None:
    data = _load()
    target = SeedTarget.from_dict(data["documentos"][0])
    assert target.norma == "Constitucion"
    assert target.tipo == "constitucion"
    assert target.numero is None
    assert target.anio is None
    assert len(target.canonico) == 3
