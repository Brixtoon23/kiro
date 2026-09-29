"""Deterministic LEXIS (MinJusticia) collector driven by a headless browser.

The MinJusticia LEXIS portal (``lexis.minjusticia.gov.co/buscador/Detallado/3``)
is a single-page application: both the search results and the document detail
(with its "descargar Word" control) are rendered by JavaScript, so the raw HTML
is useless for scraping. This module drives a **headless browser** through the
exact same clicks a human performs -- apply the type filter, page through the
results tabs, open ``viewDocument/{id}`` for the row that matches the norm, and
click the download control to capture the ``.docx`` -- which makes the flow
automatable AND fully deterministic (no LLM, no guessing).

Design:

* :class:`LexisBrowser` is a tiny PROTOCOL over the handful of browser actions
  the flow needs. The production implementation :class:`PlaywrightLexisBrowser`
  wraps Playwright (headless Chromium, ``--no-sandbox``). Tests inject a fake
  implementing the same protocol so the deterministic selection logic is
  validated OFFLINE, with NO network and WITHOUT downloading Chromium.
* :class:`LexisCollector` orchestrates the flow: it maps the seed ``canonico``
  ``[tipo, numero, anio]`` to a LEXIS type filter, drives the browser, selects
  the matching result deterministically by ``numero``/``anio`` (and ``tipo``),
  and returns a :class:`LexisResult` carrying the ``.docx`` bytes -- or a
  ``no_encontrado`` reason when there is no clear match. It NEVER fabricates.
* Politeness mirrors :class:`~corpus_ingesta.http_client.PoliteClient`: at least
  ``base_delay`` seconds between navigations/downloads, progressive capped
  backoff on transient failures, and a RAW-FIRST on-disk check so a norm whose
  ``.docx`` is already on disk is not re-downloaded.

NETWORK NOTE: the .gov.co portals are unreachable from the build sandbox, so the
live Playwright<->LEXIS flow is validated on the user's machine. Here it is
validated deterministically against a mocked browser (see the tests).
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# ---------------------------------------------------------------------------
# Canonico tipo -> LEXIS "tipo de norma" filter label
# ---------------------------------------------------------------------------

#: Default LEXIS buscador (Detallado) entry point. Overridable per-collector.
LEXIS_BUSCADOR_URL = "https://lexis.minjusticia.gov.co/buscador/Detallado/3"

#: Base of the detail view. The row id is appended: ``viewDocument/{id}``.
LEXIS_DETAIL_BASE = "https://lexis.minjusticia.gov.co/minjusticia/viewDocument/"

#: Map the seed canonico ``tipo`` to the exact LEXIS type-filter label. The
#: portal exposes: Constitucion, Acto Legislativo, Ley, Decreto, Decreto Unico,
#: Codigo, Estatuto, Resolucion, Circular, Directiva Presidencial /
#: Vicepresidencial, Acuerdo, Institucion Administrativa, Institucion
#: Administrativa Conjunta. Types NOT in LEXIS (e.g. ``decision_andina_486``,
#: ``jurisprudencia``) map to ``None`` so the collector records no_encontrado
#: instead of inventing a filter.
TIPO_FILTER_MAP: dict[str, str | None] = {
    "constitucion": "Constitucion",
    "acto_legislativo": "Acto Legislativo",
    "ley": "Ley",
    "decreto": "Decreto",
    "decreto_unico": "Decreto Unico",
    "acuerdo": "Acuerdo",
    "estatuto": "Estatuto",
    # Consolidated codes / statutes referenced by a symbolic canonico tipo.
    "codigo_general_proceso": "Codigo",
    "codigo_sustantivo_trabajo": "Codigo",
    "codigo_infancia": "Codigo",
    "codigo_disciplinario": "Codigo",
    "codigo_nacional_policia": "Codigo",
    "estatuto_tributario": "Estatuto",
    "estatuto_consumidor": "Estatuto",
    # Explicitly NOT covered by LEXIS.
    "decision_andina_486": None,
    "jurisprudencia": None,
}

#: The canonico tipos this collector is willing to handle. ``jurisprudencia``
#: is deliberately excluded (it goes to the Corte Constitucional flow).
LEXIS_TIPOS = frozenset(t for t, label in TIPO_FILTER_MAP.items() if label is not None)


def lexis_filter_for(tipo: str | None) -> str | None:
    """Return the LEXIS filter label for a canonico ``tipo`` (or ``None``)."""
    if not tipo:
        return None
    return TIPO_FILTER_MAP.get(tipo.lower())


def is_lexis_tipo(tipo: str | None) -> bool:
    """True when ``tipo`` is a norm type LEXIS is expected to serve."""
    if not tipo:
        return False
    return tipo.lower() in LEXIS_TIPOS


# ---------------------------------------------------------------------------
# Browser abstraction (mockable) + result rows
# ---------------------------------------------------------------------------


@dataclass
class LexisRow:
    """One result row parsed from the LEXIS buscador results.

    ``doc_id`` is the LEXIS document id used to build ``viewDocument/{id}``.
    ``titulo`` is the visible row label (e.g. "Ley 80 de 1993 ..."). ``tipo``
    is the row's declared type when the portal exposes it, else empty. Nothing
    here is fabricated: all fields come straight from the rendered results.
    """

    doc_id: str
    titulo: str
    tipo: str = ""


class LexisBrowser(Protocol):
    """Minimal browser surface the LEXIS flow drives (Playwright or a fake).

    Every method models one deterministic user action. The production
    implementation performs the real clicks; the test fake returns canned data.
    """

    def open_search(self, url: str) -> None:
        """Navigate to the Detallado buscador page."""

    def apply_type_filter(self, filter_label: str) -> None:
        """Select the given "tipo de norma" filter in the buscador."""

    def result_rows(self) -> list[LexisRow]:
        """Return the currently visible result rows (across paged tabs)."""

    def open_detail(self, doc_id: str) -> str:
        """Open ``viewDocument/{doc_id}`` and return the resulting detail URL."""

    def download_docx(self) -> bytes:
        """Click the "descargar Word" control and return the ``.docx`` bytes."""

    def close(self) -> None:
        """Release browser resources."""


@dataclass
class LexisResult:
    """Outcome of :meth:`LexisCollector.collect` for one norm.

    ``ok`` is True only when a ``.docx`` was captured for a row that
    deterministically matches the norm. ``docx`` holds those bytes (empty on
    failure). ``detail_url`` is the ``viewDocument/{id}`` URL actually opened.
    ``reason`` documents any non-match / skip. ``from_cache`` is True when the
    ``.docx`` was already on disk (raw-first, not re-downloaded).
    """

    ok: bool
    docx: bytes = b""
    detail_url: str = ""
    lexis_doc_id: str = ""
    reason: str = ""
    from_cache: bool = False


# ---------------------------------------------------------------------------
# Deterministic result matching
# ---------------------------------------------------------------------------


def _fold(value: str) -> str:
    """Lowercase + strip accents + collapse whitespace for robust comparison."""
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_str = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"\s+", " ", ascii_str).strip()


def _numero_variants(numero: str) -> set[str]:
    """Return equivalent renderings of a norm number (``046`` ~ ``46``)."""
    numero = numero.strip()
    variants = {numero}
    stripped = numero.lstrip("0")
    if stripped:
        variants.add(stripped)
    if numero.isdigit():
        variants.add(str(int(numero)))
    return {v for v in variants if v}


def select_matching_row(
    rows: list[LexisRow],
    *,
    numero: str | None,
    anio: str | None,
    filter_label: str | None,
) -> tuple[LexisRow | None, str]:
    """Pick the row that deterministically matches the norm, or explain why not.

    Matching rule (FIRST matching row wins, stable order):

    * When the norm carries ``numero`` + ``anio`` (laws, decrees, acuerdos), a
      row matches when a folded ``"<numero> de <anio>"`` (or ``"<numero> of
      <anio>"`` numeric variants like ``046``~``46``) appears in the row title.
    * When the norm has NO number/year (consolidated codes/statutes, the
      Constitution), we fall back to a type-label match: the row's declared
      ``tipo`` equals the requested ``filter_label`` (folded). This deliberately
      returns a match only when exactly one such row exists, to avoid picking an
      arbitrary code; otherwise it reports an ambiguous result.

    Returns ``(row, "")`` on a clean match, or ``(None, reason)`` when nothing
    matches or the match is ambiguous. NEVER fabricates a row.
    """
    if not rows:
        return None, "sin resultados en el buscador LEXIS"

    if numero and anio:
        variants = _numero_variants(numero)
        folded_anio = _fold(anio)
        wanted = {f"{v} de {folded_anio}" for v in variants}
        matches = [r for r in rows if any(w in _fold(r.titulo) for w in wanted)]
        if not matches:
            return (
                None,
                f"ninguna fila coincide con numero={numero} anio={anio}",
            )
        # Deterministic: first match in portal order.
        return matches[0], ""

    # No numero/anio: match by the type filter label (single unambiguous row).
    if filter_label:
        folded_label = _fold(filter_label)
        typed = [r for r in rows if _fold(r.tipo) == folded_label]
        if len(typed) == 1:
            return typed[0], ""
        if len(typed) > 1:
            return None, f"match ambiguo: {len(typed)} filas del tipo {filter_label}"
    return None, "match ambiguo: no hay numero/anio para desambiguar la fila"


# ---------------------------------------------------------------------------
# Collector (orchestration + politeness + cache)
# ---------------------------------------------------------------------------


@dataclass
class LexisCollector:
    """Drive :class:`LexisBrowser` to capture a norm's ``.docx`` from LEXIS.

    Politeness knobs mirror :class:`PoliteClient`: ``base_delay`` seconds
    between navigations, exponential ``backoff_factor`` capped at
    ``max_backoff`` on transient browser errors, and ``max_retries`` attempts.
    """

    browser: LexisBrowser
    buscador_url: str = LEXIS_BUSCADOR_URL
    base_delay: float = 1.0
    max_retries: int = 3
    backoff_factor: float = 2.0
    max_backoff: float = 60.0
    _last_action_ts: float | None = field(default=None, init=False, repr=False)

    def _respect_rate_limit(self) -> None:
        if self._last_action_ts is not None:
            elapsed = time.monotonic() - self._last_action_ts
            wait = self.base_delay - elapsed
            if wait > 0:
                time.sleep(wait)
        self._last_action_ts = time.monotonic()

    def _backoff_delay(self, attempt: int) -> float:
        return min(self.base_delay * (self.backoff_factor**attempt), self.max_backoff)

    def collect(
        self,
        *,
        tipo: str | None,
        numero: str | None,
        anio: str | None,
    ) -> LexisResult:
        """Locate the norm in LEXIS and capture its ``.docx`` (deterministic).

        Steps: map ``tipo`` -> LEXIS filter (unsupported types -> no_encontrado
        without touching the browser); open the buscador; apply the filter; read
        the result rows; select the matching row by numero/anio (or type when
        the norm has none); open its ``viewDocument/{id}``; download the
        ``.docx``. Transient browser errors are retried with progressive
        backoff; after exhausting retries the norm is reported as not found
        (never crash, never fabricate).
        """
        filter_label = lexis_filter_for(tipo)
        if filter_label is None:
            return LexisResult(
                ok=False,
                reason=(
                    f"tipo '{tipo}' no está cubierto por LEXIS "
                    "(p. ej. jurisprudencia o decision_andina_486)"
                ),
            )

        last_error = "error desconocido"
        for attempt in range(self.max_retries):
            if attempt > 0:
                time.sleep(self._backoff_delay(attempt))
            try:
                self._respect_rate_limit()
                self.browser.open_search(self.buscador_url)
                self._respect_rate_limit()
                self.browser.apply_type_filter(filter_label)
                rows = self.browser.result_rows()

                row, reason = select_matching_row(
                    rows, numero=numero, anio=anio, filter_label=filter_label
                )
                if row is None:
                    # A clean "no match" is a final, non-retryable outcome.
                    return LexisResult(ok=False, reason=reason)

                self._respect_rate_limit()
                detail_url = self.browser.open_detail(row.doc_id)
                self._respect_rate_limit()
                docx = self.browser.download_docx()
                if not docx:
                    last_error = "descarga vacía del control Word"
                    continue
                return LexisResult(
                    ok=True,
                    docx=docx,
                    detail_url=detail_url or f"{LEXIS_DETAIL_BASE}{row.doc_id}",
                    lexis_doc_id=row.doc_id,
                    reason="descargado_desde_lexis",
                )
            except Exception as exc:  # noqa: BLE001 - resilience: retry, never crash
                last_error = f"error de navegación: {exc}"
                continue

        return LexisResult(
            ok=False,
            reason=f"fallo tras {self.max_retries} intentos: {last_error}",
        )


# ---------------------------------------------------------------------------
# Raw-first cache helper (shared by Phase B)
# ---------------------------------------------------------------------------


def existing_docx(dest_dir: Path) -> bytes | None:
    """Return the bytes of an already-downloaded ``raw.docx`` if present.

    RAW-FIRST cache: if Phase B already captured the ``.docx`` for this doc_id,
    we do NOT re-drive the browser. Returns ``None`` when absent/empty.
    """
    candidate = dest_dir / "raw.docx"
    if candidate.exists() and candidate.stat().st_size > 0:
        return candidate.read_bytes()
    return None
