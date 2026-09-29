"""Phase C: RAW -> SQLite (+ vector-ready schema) + manifest.

Walks ``data/raw/<doc_id>/`` (produced by Phase B), loads each raw payload plus
its ``meta.json``, routes to the correct adapter by the stored ``url``/``fuente``
and parses it deterministically (BeautifulSoup/lxml/regex, NO LLM) into a
:class:`DocumentRecord` + article-level :class:`ArticleFragment` rows with
character offsets for full traceability. The rows are persisted to SQLite via
``db.py`` (the reserved ``vector_id``/``embedding_ref`` columns stay NULL for a
future vector DB). Finally it emits ``corpus_manifest.json`` (EXACTLY the six
mandated keys) and ``CORPUS.md``.

Norms absent from their source yield an ``ingest_log`` ``no_encontrado`` row and
NO fabricated document/article rows.

Run as a single command::

    uv run corpus-fase-c
    uv run python -m corpus_ingesta.phase_c
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from .adapters import default_adapters
from .adapters.base import slugify
from .db import connect, init_db, insert_articles, log_event, upsert_document
from .manifest import write_all
from .models import DocumentRecord, IngestStatus, SeedTarget
from .seed_io import route


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corpus-fase-c",
        description=(
            "Fase C: transforma el RAW a SQLite (esquema listo para vectores) y "
            "genera el manifiesto (determinista, sin LLM)."
        ),
    )
    parser.add_argument("--raw-dir", default="data/raw", help="Directorio raiz del RAW.")
    parser.add_argument("--db", default="data/corpus.sqlite", help="Ruta de la base SQLite.")
    parser.add_argument(
        "--manifest", default="corpus_manifest.json", help="Ruta del manifiesto JSON."
    )
    parser.add_argument("--corpus-md", default="CORPUS.md", help="Ruta de CORPUS.md.")
    return parser


def _target_from_meta(meta: dict[str, object]) -> SeedTarget:
    """Rebuild a :class:`SeedTarget` from a Phase B ``meta.json``."""
    return SeedTarget.from_dict(
        {
            "norma": meta.get("norma", ""),
            "canonico": meta.get("canonico") or [],
            "items_del_banco": 0,
            "areas": meta.get("areas") or [],
            "donde_buscar": meta.get("url", ""),
        }
    )


def _iter_raw_dirs(raw_root: Path) -> list[Path]:
    """Return the per-doc raw directories (those carrying a meta.json), sorted."""
    if not raw_root.exists():
        return []
    dirs = [
        d
        for d in sorted(raw_root.iterdir())
        if d.is_dir() and d.name != "cache" and (d / "meta.json").exists()
    ]
    return dirs


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``corpus-fase-c`` console script."""
    args = _build_parser().parse_args(list(argv) if argv is not None else sys.argv[1:])

    raw_root = Path(args.raw_dir)
    adapters = default_adapters()

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(args.db)

    per_status: dict[str, int] = defaultdict(int)
    processed = 0

    try:
        for doc_dir in _iter_raw_dirs(raw_root):
            meta_path = doc_dir / "meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            raw_file = meta.get("raw_file")
            raw_path = doc_dir / raw_file if raw_file else None
            if raw_path is None or not raw_path.exists():
                # Fall back to any raw.* file in the directory.
                candidates = sorted(doc_dir.glob("raw.*"))
                raw_path = candidates[0] if candidates else None
            if raw_path is None:
                log_event(
                    conn,
                    IngestStatus.ERROR.value,
                    doc_id=str(meta.get("doc_id")),
                    url=str(meta.get("url")),
                    message="raw file missing in directory",
                )
                per_status[IngestStatus.ERROR.value] += 1
                continue

            processed += 1
            target = _target_from_meta(meta)
            adapter = route(target, adapters)
            fuente = meta.get("fuente") or (adapter.fuente if adapter else "(sin-adapter)")

            if adapter is None:
                log_event(
                    conn,
                    IngestStatus.ERROR.value,
                    doc_id=str(meta.get("doc_id")),
                    url=target.donde_buscar,
                    message="no adapter matched the stored url",
                )
                per_status[IngestStatus.ERROR.value] += 1
                continue

            raw_bytes = raw_path.read_bytes()
            try:
                doc, fragments = adapter.parse(raw_bytes, target)
            except Exception as exc:  # noqa: BLE001 - resilience is required
                doc_id = f"{fuente}:{slugify(target.norma)}"
                log_event(
                    conn,
                    IngestStatus.ERROR.value,
                    doc_id=doc_id,
                    url=target.donde_buscar,
                    message=f"parse failed: {exc}",
                )
                upsert_document(
                    conn,
                    DocumentRecord(
                        doc_id=doc_id,
                        norma=target.norma,
                        tipo=target.tipo,
                        numero=target.numero,
                        anio=target.anio,
                        fuente=fuente,
                        url=target.donde_buscar,
                        fecha_consulta=str(meta.get("fecha_consulta")),
                        areas=list(target.areas),
                        status=IngestStatus.ERROR.value,
                        raw_path=str(raw_path),
                        error_detail=f"parse failed: {exc}",
                    ),
                )
                per_status[IngestStatus.ERROR.value] += 1
                continue

            # Prefer the fecha_consulta captured at download time.
            if meta.get("fecha_consulta"):
                doc.fecha_consulta = str(meta["fecha_consulta"])
            doc.raw_path = str(raw_path)

            if doc.status == IngestStatus.NO_ENCONTRADO.value:
                # Record the outcome but do NOT fabricate document/article rows.
                log_event(
                    conn,
                    IngestStatus.NO_ENCONTRADO.value,
                    doc_id=doc.doc_id,
                    url=target.donde_buscar,
                    message=doc.error_detail or "norm not found in source",
                )
                per_status[IngestStatus.NO_ENCONTRADO.value] += 1
                continue

            upsert_document(conn, doc)
            n = insert_articles(conn, fragments)
            log_event(
                conn,
                IngestStatus.PARSEADO.value,
                doc_id=doc.doc_id,
                url=target.donde_buscar,
                message=f"parsed {n} article fragment(s)",
            )
            per_status[IngestStatus.PARSEADO.value] += 1
    finally:
        conn.close()

    # Emit manifest artifacts from the populated DB.
    conn = connect(args.db)
    try:
        write_all(conn, manifest_path=args.manifest, corpus_md_path=args.corpus_md)
    finally:
        conn.close()

    print(f"Base SQLite: {args.db}")
    print(f"Manifiesto:  {args.manifest}")
    print(f"Inventario:  {args.corpus_md}")
    print("\n=== Resumen Fase C ===")
    print(f"documentos procesados: {processed}")
    for status in sorted(per_status):
        print(f"  {status}: {per_status[status]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
