"""Phase A: seed validation -> clean seed.

Reads ``seed_targets.json``, validates each entry against its official
``.gov.co`` source deterministically (HTTP GET + rule-based markers, NO LLM),
and writes two files:

* ``seed_targets_clean.json`` -- ONLY the entries that are genuinely reachable,
  preserving the EXACT seed field shape (``norma``, ``canonico``,
  ``items_del_banco``, ``areas``, ``donde_buscar``).
* ``no_encontrados.json`` -- the discarded entries, each recorded with
  ``{norma, canonico, donde_buscar, motivo, status_code, fecha_consulta}``.

Never fabricates a result. The Consejo de Estado (SAMAI) source is treated as a
documented OPTIONAL, non-blocking skip.

Run as a single command::

    uv run corpus-fase-a
    uv run python -m corpus_ingesta.phase_a --dry-run
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from .adapters import default_adapters
from .http_client import PoliteClient
from .models import SeedTarget
from .seed_io import load_seed, route


def _today() -> str:
    return _dt.date.today().isoformat()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corpus-fase-a",
        description=(
            "Fase A: valida la semilla contra fuentes oficiales .gov.co y produce "
            "una semilla limpia (determinista, sin LLM)."
        ),
    )
    parser.add_argument("--seed", default="seed_targets.json", help="Semilla de entrada.")
    parser.add_argument(
        "--out-clean", default="seed_targets_clean.json", help="Semilla limpia de salida."
    )
    parser.add_argument(
        "--out-missing", default="no_encontrados.json", help="Descartes de salida."
    )
    parser.add_argument("--delay", type=float, default=1.0, help="Segundos entre solicitudes.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Carga la semilla y cablea adapters SIN hacer ninguna llamada de red.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Procesar solo las primeras N entradas."
    )
    return parser


def _clean_entry(target: SeedTarget, *, resolved_url: str = "") -> dict[str, object]:
    """Serialize a target back to the EXACT seed field shape.

    When Phase A resolved a ``?q=`` search page to a direct document URL,
    ``resolved_url`` replaces ``donde_buscar`` so Phase B downloads the
    consolidated ``.html``/``.pdf`` document instead of the results listing. For
    entries that were already direct links ``resolved_url`` is empty and the
    original ``donde_buscar`` is preserved verbatim.
    """
    return {
        "norma": target.norma,
        "canonico": list(target.canonico),
        "items_del_banco": target.items_del_banco,
        "areas": list(target.areas),
        "donde_buscar": resolved_url or target.donde_buscar,
    }


def _missing_entry(
    target: SeedTarget, *, motivo: str, status_code: int | None
) -> dict[str, object]:
    return {
        "norma": target.norma,
        "canonico": list(target.canonico),
        "donde_buscar": target.donde_buscar,
        "motivo": motivo,
        "status_code": status_code,
        "fecha_consulta": _today(),
    }


def _write_json(path: str, payload: object) -> None:
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _dry_run(args: argparse.Namespace) -> int:
    targets = load_seed(args.seed)
    adapters = default_adapters()
    print(f"[dry-run] semilla: {args.seed} ({len(targets)} entradas)")
    print(f"[dry-run] adapters cableados: {', '.join(a.fuente for a in adapters)}")
    print("[dry-run] ruteo (sin red):")
    routed = 0
    for t in targets:
        adapter = route(t, adapters)
        if adapter is None:
            print(f"  - {t.norma}: SIN ADAPTER para {t.donde_buscar}")
        else:
            routed += 1
            print(f"  - {t.norma}: -> {adapter.fuente}")
    print(f"[dry-run] {routed}/{len(targets)} entradas ruteadas. No se uso la red.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``corpus-fase-a`` console script."""
    args = _build_parser().parse_args(list(argv) if argv is not None else sys.argv[1:])

    if args.dry_run:
        return _dry_run(args)

    targets = load_seed(args.seed)
    if args.limit is not None:
        targets = targets[: args.limit]
    adapters = default_adapters()
    client = PoliteClient(base_delay=args.delay)

    clean: list[dict[str, object]] = []
    missing: list[dict[str, object]] = []
    per_fuente: dict[str, dict[str, int]] = defaultdict(
        lambda: {"accesibles": 0, "descartadas": 0, "opcional": 0}
    )

    for target in targets:
        adapter = route(target, adapters)
        if adapter is None:
            missing.append(
                _missing_entry(
                    target, motivo="sin_adapter: ninguna fuente soportada", status_code=None
                )
            )
            per_fuente["(sin-adapter)"]["descartadas"] += 1
            continue

        fuente = adapter.fuente
        result = adapter.validate(target, client)
        if result.optional:
            # Documented optional/non-blocking skip (Consejo de Estado / SAMAI).
            missing.append(
                _missing_entry(
                    target, motivo=result.reason, status_code=result.status_code
                )
            )
            per_fuente[fuente]["opcional"] += 1
            continue
        if result.ok:
            clean.append(_clean_entry(target, resolved_url=result.resolved_url))
            per_fuente[fuente]["accesibles"] += 1
        else:
            missing.append(
                _missing_entry(
                    target, motivo=result.reason, status_code=result.status_code
                )
            )
            per_fuente[fuente]["descartadas"] += 1

    _write_json(args.out_clean, {"documentos": clean})
    _write_json(args.out_missing, {"descartados": missing})

    print(f"Semilla limpia escrita en {args.out_clean} ({len(clean)} accesibles)")
    print(f"Descartes escritos en {args.out_missing} ({len(missing)} entradas)")
    print("\n=== Resumen por fuente ===")
    header = f"{'fuente':<24} {'accesibles':>10} {'descartadas':>11} {'opcional':>9}"
    print(header)
    print("-" * len(header))
    for fuente in sorted(per_fuente):
        c = per_fuente[fuente]
        print(f"{fuente:<24} {c['accesibles']:>10} {c['descartadas']:>11} {c['opcional']:>9}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
