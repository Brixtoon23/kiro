"""Phase B: clean seed -> RAW download.

Reads ``seed_targets_clean.json`` (produced by Phase A) and politely downloads
each entry's raw payload (HTML/PDF) to ``data/raw/<doc_id>/`` using the polite
HTTP client. RAW-FIRST: the original bytes are saved to disk before any other
processing; NO parsing happens here. The :class:`PoliteClient` enforces >=1s
spacing between requests, progressive backoff on failure, and a sha256-keyed
on-disk cache so already-downloaded URLs are not re-fetched.

A :class:`FetchError` for one entry is logged and the run CONTINUES with the
next entry (host unreachable never aborts the batch). Deterministic, no LLM.

Run as a single command::

    uv run corpus-fase-b
    uv run python -m corpus_ingesta.phase_b
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .adapters import default_adapters
from .adapters.base import slugify
from .http_client import FetchError, PoliteClient
from .models import SeedTarget
from .seed_io import load_seed, route


def _today() -> str:
    return _dt.date.today().isoformat()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corpus-fase-b",
        description=(
            "Fase B: descarga educada del RAW (HTML/PDF) por doc_id a partir de la "
            "semilla limpia (determinista, sin LLM, sin parseo)."
        ),
    )
    parser.add_argument(
        "--clean-seed", default="seed_targets_clean.json", help="Semilla limpia de entrada."
    )
    parser.add_argument("--raw-dir", default="data/raw", help="Directorio raiz del RAW.")
    parser.add_argument("--delay", type=float, default=1.0, help="Segundos entre solicitudes.")
    parser.add_argument(
        "--limit", type=int, default=None, help="Procesar solo las primeras N entradas."
    )
    return parser


def compute_doc_id(fuente: str, target: SeedTarget) -> str:
    """Deterministic ``<fuente>:<slug(norma)>`` id (same convention as adapters)."""
    return f"{fuente}:{slugify(target.norma)}"


def _extension_for(content_type: str, content: bytes) -> str:
    """Pick ``pdf`` or ``html`` deterministically from headers/magic bytes."""
    ct = (content_type or "").lower()
    if "pdf" in ct or content[:5] == b"%PDF-":
        return "pdf"
    return "html"


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``corpus-fase-b`` console script."""
    args = _build_parser().parse_args(list(argv) if argv is not None else sys.argv[1:])

    targets = load_seed(args.clean_seed)
    if args.limit is not None:
        targets = targets[: args.limit]
    adapters = default_adapters()
    raw_root = Path(args.raw_dir)
    client = PoliteClient(base_delay=args.delay, cache_dir=str(raw_root / "cache"))

    descargados = 0
    fallidos = 0
    cache_hits = 0
    sin_adapter = 0

    for target in targets:
        adapter = route(target, adapters)
        if adapter is None:
            print(f"  ! sin adapter: {target.norma} ({target.donde_buscar})")
            sin_adapter += 1
            continue

        doc_id = compute_doc_id(adapter.fuente, target)
        was_cached = client.cached(target.donde_buscar)
        try:
            # RAW-FIRST: fetch bytes, then persist before anything else.
            result = client.get(target.donde_buscar)
        except FetchError as exc:
            # Never crash the run: log and continue.
            print(f"  ! fallo de descarga: {target.norma}: {exc.message}")
            fallidos += 1
            continue

        dest_dir = raw_root / doc_id.replace(":", "__")
        meta_path = dest_dir / "meta.json"
        # On a cache hit, preserve the ORIGINAL consultation date recorded when
        # the bytes were actually fetched, so re-runs do not drift the recorded
        # provenance date away from the true download. Falls back to today only
        # if no prior meta.json exists (e.g. cache seeded out-of-band).
        fecha_consulta = _today()
        if result.from_cache and meta_path.exists():
            try:
                prev = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                prev = {}
            if prev.get("fecha_consulta"):
                fecha_consulta = prev["fecha_consulta"]

        dest_dir.mkdir(parents=True, exist_ok=True)
        # Content-Type is not exposed by FetchResult; sniff from bytes.
        ext = _extension_for("", result.content)
        raw_path = dest_dir / f"raw.{ext}"
        raw_path.write_bytes(result.content)

        meta = {
            "doc_id": doc_id,
            "norma": target.norma,
            "canonico": list(target.canonico),
            "areas": list(target.areas),
            "fuente": adapter.fuente,
            "url": target.donde_buscar,
            "final_url": result.final_url,
            "fecha_consulta": fecha_consulta,
            "content_type": "application/pdf" if ext == "pdf" else "text/html",
            "raw_file": raw_path.name,
            "from_cache": result.from_cache,
        }
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        descargados += 1
        if result.from_cache or was_cached:
            cache_hits += 1
        origen = "cache" if result.from_cache else "red"
        print(f"  + {doc_id} -> {raw_path} ({origen})")

    print("\n=== Resumen Fase B ===")
    print(f"descargados: {descargados}")
    print(f"fallidos:    {fallidos}")
    print(f"cache hits:  {cache_hits}")
    if sin_adapter:
        print(f"sin adapter: {sin_adapter}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
