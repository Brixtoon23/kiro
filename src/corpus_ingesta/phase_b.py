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
from .lexis_client import LexisCollector, existing_docx, is_lexis_tipo
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


def build_lexis_collector(delay: float) -> LexisCollector:
    """Build a :class:`LexisCollector` backed by the real Playwright browser.

    Imported lazily so that a run over a seed WITHOUT any LEXIS-covered norm (or
    the offline test-suite, which injects a fake collector) never has to import
    Playwright or download Chromium. Requires a one-time::

        uv run playwright install chromium
    """
    from .lexis_playwright import PlaywrightLexisBrowser

    browser = PlaywrightLexisBrowser(headless=True)
    browser.start()
    return LexisCollector(browser=browser, base_delay=delay)


def _fetch_lexis(
    target: SeedTarget,
    adapter,  # noqa: ANN001 - the LEXIS adapter (fuente slug carrier)
    raw_root: Path,
    collector_factory,  # noqa: ANN001 - callable(delay) -> LexisCollector | None
    delay: float,
    state: dict,
) -> None:
    """Capture a LEXIS norm's ``.docx`` via the headless collector (raw-first).

    Reuses an on-disk ``raw.docx`` when present (no re-download). On a clean
    "no match" or a browser failure the entry is recorded as not found and the
    run CONTINUES. Mutates the counters in ``state``.
    """
    doc_id = compute_doc_id(adapter.fuente, target)
    dest_dir = raw_root / doc_id.replace(":", "__")
    meta_path = dest_dir / "meta.json"

    cached = existing_docx(dest_dir)
    if cached is not None:
        # RAW-FIRST cache hit: do not re-drive the browser.
        state["descargados"] += 1
        state["cache_hits"] += 1
        print(f"  + {doc_id} -> {dest_dir / 'raw.docx'} (cache)")
        return

    collector = state.get("lexis_collector")
    if collector is None:
        collector = collector_factory(delay)
        if collector is None:
            print(f"  ! LEXIS no disponible (sin navegador): {target.norma}")
            state["fallidos"] += 1
            return
        state["lexis_collector"] = collector

    result = collector.collect(tipo=target.tipo, numero=target.numero, anio=target.anio)
    if not result.ok:
        print(f"  ! no_encontrado LEXIS: {target.norma}: {result.reason}")
        state["fallidos"] += 1
        return

    dest_dir.mkdir(parents=True, exist_ok=True)
    raw_path = dest_dir / "raw.docx"
    raw_path.write_bytes(result.docx)
    meta = {
        "doc_id": doc_id,
        "norma": target.norma,
        "canonico": list(target.canonico),
        "areas": list(target.areas),
        "fuente": adapter.fuente,
        "url": result.detail_url,
        "final_url": result.detail_url,
        "fecha_consulta": _today(),
        "content_type": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        "raw_file": raw_path.name,
        "from_cache": result.from_cache,
        "source": "lexis_minjusticia",
        "lexis_doc_id": result.lexis_doc_id,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state["descargados"] += 1
    print(f"  + {doc_id} -> {raw_path} (lexis)")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``corpus-fase-b`` console script."""
    args = _build_parser().parse_args(list(argv) if argv is not None else sys.argv[1:])

    targets = load_seed(args.clean_seed)
    if args.limit is not None:
        targets = targets[: args.limit]
    adapters = default_adapters()
    raw_root = Path(args.raw_dir)
    client = PoliteClient(base_delay=args.delay, cache_dir=str(raw_root / "cache"))
    lexis_adapter = next((a for a in adapters if a.fuente == "lexis_minjusticia"), None)

    # Counters live in a dict so the LEXIS helper can mutate them in place. The
    # lexis_collector is created lazily on the first LEXIS-covered norm.
    # ``build_lexis_collector`` is looked up at call time so tests can
    # monkeypatch it with an offline fake (no Playwright, no Chromium).
    collector_factory = build_lexis_collector
    state: dict = {
        "descargados": 0,
        "fallidos": 0,
        "cache_hits": 0,
        "sin_adapter": 0,
        "lexis": 0,
        "lexis_collector": None,
    }

    for target in targets:
        # LEXIS-covered norms (leyes, decretos, codigos, estatutos, constitucion,
        # acto legislativo, acuerdo) are captured via the headless browser using
        # the canonico [tipo, numero, anio] -- NOT the legacy donde_buscar URL.
        if lexis_adapter is not None and is_lexis_tipo(target.tipo):
            state["lexis"] += 1
            _fetch_lexis(target, lexis_adapter, raw_root, collector_factory, args.delay, state)
            continue

        adapter = route(target, adapters)
        if adapter is None:
            print(f"  ! sin adapter: {target.norma} ({target.donde_buscar})")
            state["sin_adapter"] += 1
            continue

        doc_id = compute_doc_id(adapter.fuente, target)
        was_cached = client.cached(target.donde_buscar)
        try:
            # RAW-FIRST: fetch bytes, then persist before anything else.
            result = client.get(target.donde_buscar)
        except FetchError as exc:
            # Never crash the run: log and continue.
            print(f"  ! fallo de descarga: {target.norma}: {exc.message}")
            state["fallidos"] += 1
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

        state["descargados"] += 1
        if result.from_cache or was_cached:
            state["cache_hits"] += 1
        origen = "cache" if result.from_cache else "red"
        print(f"  + {doc_id} -> {raw_path} ({origen})")

    # Release the headless browser if one was started.
    collector = state.get("lexis_collector")
    if collector is not None:
        try:
            collector.browser.close()
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass

    print("\n=== Resumen Fase B ===")
    print(f"descargados: {state['descargados']}")
    print(f"fallidos:    {state['fallidos']}")
    print(f"cache hits:  {state['cache_hits']}")
    print(f"lexis:       {state['lexis']}")
    if state["sin_adapter"]:
        print(f"sin adapter: {state['sin_adapter']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
