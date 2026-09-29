"""Common source-adapter interface + shared HTML/article helpers.

Every portal adapter subclasses :class:`SourceAdapter` and implements
``matches``/``fetch``/``parse``. The base class provides the shared, fully
deterministic building blocks (no LLM anywhere):

* :func:`clean_html_to_text` -- BeautifulSoup + lxml HTML -> normalized text.
* :func:`split_articles` -- regex segmentation on ``ARTICULO/ARTICULO N``
  boundaries, computing char ``offset_start``/``offset_end`` into the cleaned
  text so every fragment is traceable back to norma + articulo.
* :meth:`save_raw` -- raw-first persistence to ``data/raw/<fuente>/<slug>``.

The unit of sense for the corpus is the *article*; segmentation is therefore
purely rule-based and independent of the source portal.
"""

from __future__ import annotations

import io
import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..http_client import FetchError, FetchResult, PoliteClient
from ..models import ArticleFragment, DocumentRecord, SeedTarget

# ---------------------------------------------------------------------------
# Raw fetch result
# ---------------------------------------------------------------------------


@dataclass
class RawDoc:
    """Outcome of :meth:`SourceAdapter.fetch`.

    ``content`` are the raw bytes as downloaded (persisted before any parsing),
    ``raw_path`` is where they were saved on disk, and ``final_url`` is the URL
    actually retrieved (after any redirects).
    """

    content: bytes
    raw_path: str
    final_url: str
    from_cache: bool = False


@dataclass
class ValidationResult:
    """Outcome of :meth:`SourceAdapter.validate` (Phase A).

    ``ok`` is True only when the norm is genuinely reachable at the official
    source. ``reason`` is a short deterministic code/phrase used to explain a
    discard (e.g. ``"http_404"``, ``"timeout"``, ``"no_result_marker"``,
    ``"optional_not_supported"``). ``final_url`` is the URL actually reached and
    ``status_code`` the HTTP status when available. ``optional`` marks a
    documented non-blocking skip (e.g. Consejo de Estado / SAMAI): Phase A must
    never treat it as an error that stops the run. ``resolved_url`` is the
    DIRECT document URL (``.html``/``.pdf``) that a search-page ``?q=`` entry
    resolved to; when set, Phase A stores it in ``seed_targets_clean.json`` in
    place of the original search URL so Phase B downloads the consolidated text
    instead of a results listing. Empty when the entry was already a direct URL.
    """

    ok: bool
    reason: str = ""
    final_url: str = ""
    status_code: int | None = None
    optional: bool = False
    resolved_url: str = ""


# ---------------------------------------------------------------------------
# Shared deterministic helpers
# ---------------------------------------------------------------------------

# Matches an article HEADING such as "ARTICULO 12.", "ARTICULO 12o.",
# "Articulo 12 bis.", "ART. 5:". Case-insensitive; also accepts ARTICULO with
# no accent. The number group keeps the raw token so a suffix like "bis"
# survives.
#
# Boundary hardening (review issue #3): a genuine heading is followed by
# heading punctuation (. - : ) or an ordinal mark) OR the end of the line. It
# must NOT match in-body cross-references such as "Articulo 12 de la ley
# anterior..." where the number is followed by a lowercase word; those
# previously produced spurious fragments and over-segmented consolidated codes.
ARTICLE_RE = re.compile(
    r"(?im)^\s*"
    r"(?:art[ii\u00ed\u00cd]culo|art\.?)\s+"
    r"(\d+(?:o|\u00ba|\u00b0)?(?:\s+bis)?)"
    r"\s*(?:[\.\-:)\u00ba\u00b0]|$)"
)


def slugify(value: str, *, max_len: int = 80) -> str:
    """Return a filesystem/URL-safe ASCII slug for ``value``.

    Deterministic: strips accents, lowercases, collapses non-alphanumerics to a
    single hyphen. Empty input yields ``"item"``.
    """
    normalized = unicodedata.normalize("NFKD", value)
    ascii_str = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_str = ascii_str.lower()
    ascii_str = re.sub(r"[^a-z0-9]+", "-", ascii_str).strip("-")
    ascii_str = re.sub(r"-{2,}", "-", ascii_str)
    if not ascii_str:
        return "item"
    return ascii_str[:max_len].strip("-") or "item"


def _looks_like_pdf(raw: bytes | str) -> bool:
    """True when ``raw`` is PDF bytes (magic header ``%PDF-``).

    Some .gov.co sources serve consolidated norms as PDF rather than HTML, so
    the cleaner detects this and routes to :func:`extract_pdf_text` (review
    issue #5: wire pdfminer.six instead of leaving it unused).
    """
    if isinstance(raw, str):
        return raw.lstrip()[:5] == "%PDF-"
    return raw[:5] == b"%PDF-"


def _normalize_text(text: str) -> str:
    """Shared deterministic whitespace normalization for HTML/PDF text."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\xa0", " ").replace("\t", " ")
    lines = [re.sub(r"[ ]{2,}", " ", ln).rstrip() for ln in text.split("\n")]
    out_lines: list[str] = []
    blank_run = 0
    for ln in lines:
        if ln.strip() == "":
            blank_run += 1
            if blank_run <= 1:
                out_lines.append("")
        else:
            blank_run = 0
            out_lines.append(ln)
    return "\n".join(out_lines).strip()


def extract_pdf_text(raw: bytes) -> str:
    """Extract plain text from PDF bytes with pdfminer.six (deterministic).

    Applies the same normalization as :func:`clean_html_to_text` so the text is
    directly compatible with :func:`split_articles` and its character offsets.
    No LLM/OCR: pdfminer reads the embedded text layer only.
    """
    from pdfminer.high_level import extract_text  # local import: optional path

    text = extract_text(io.BytesIO(raw)) or ""
    return _normalize_text(text)


def clean_html_to_text(raw: bytes | str) -> str:
    """Convert raw HTML into normalized plain text (deterministic).

    Drops ``script``/``style``/``head`` noise, extracts text with newline
    separators, then normalizes whitespace: CRLF -> LF, tabs -> space, runs of
    blank lines collapsed, trailing spaces trimmed. The resulting string is the
    canonical text against which article offsets are computed.
    """
    if _looks_like_pdf(raw):
        data = raw if isinstance(raw, bytes) else raw.encode("latin-1", "ignore")
        return extract_pdf_text(data)

    soup = BeautifulSoup(raw, "lxml")

    for tag in soup(["script", "style", "head", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    # Normalize newlines and whitespace deterministically (shared with PDF path).
    return _normalize_text(text)


def _normalize_articulo_numero(raw_token: str) -> str:
    """Normalize a captured article token into a stable ``articulo_numero``.

    Keeps the leading digits and an optional ``bis`` suffix; drops ordinal
    marks and trailing punctuation. ``"12o."`` -> ``"12"``, ``"5 bis"`` ->
    ``"5 bis"``.
    """
    token = raw_token.strip()
    m = re.match(r"(\d+)(?:\s*(bis))?", token, flags=re.IGNORECASE)
    if not m:
        return token.strip(".").strip()
    numero = m.group(1)
    if m.group(2):
        numero = f"{numero} bis"
    return numero


def split_articles(text: str) -> list[tuple[str, str, int, int]]:
    """Segment ``text`` at ARTICULO boundaries.

    Returns a list of ``(articulo_numero, encabezado, offset_start,
    offset_end)`` tuples where the offsets are character positions into ``text``
    delimiting the full article body (heading included). If no article heading
    is found the list is empty (the caller decides how to handle it).
    """
    matches = list(ARTICLE_RE.finditer(text))
    if not matches:
        return []

    fragments: list[tuple[str, str, int, int]] = []
    for idx, m in enumerate(matches):
        start = m.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        numero = _normalize_articulo_numero(m.group(1))
        heading = m.group(0).strip()
        fragments.append((numero, heading, start, end))
    return fragments


# ---------------------------------------------------------------------------
# Search-result resolution (Phase A) -- deterministic, no LLM
# ---------------------------------------------------------------------------


@dataclass
class SearchHit:
    """One candidate link parsed from a portal search-results page.

    ``href`` is the (possibly relative) link to the document, ``text`` is the
    anchor/label text and ``context`` is nearby text (row/list-item) used only
    as an extra deterministic matching signal. Nothing here is fabricated: all
    fields come straight from the results HTML.
    """

    href: str
    text: str
    context: str = ""


def parse_search_hits(raw: bytes | str) -> list[SearchHit]:
    """Extract candidate document links from a search-results page.

    Deterministic (BeautifulSoup/lxml): every ``<a href>`` becomes a
    :class:`SearchHit` carrying its anchor text and the text of its closest
    ``<li>``/``<tr>``/``<div>`` ancestor as context. Order is preserved so a
    caller can implement a stable "first matching result" policy. No network,
    no LLM.
    """
    soup = BeautifulSoup(raw, "lxml")
    hits: list[SearchHit] = []
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        text = a.get_text(" ", strip=True)
        context = ""
        ancestor = a.find_parent(["li", "tr", "article", "div"])
        if ancestor is not None:
            context = ancestor.get_text(" ", strip=True)
        hits.append(SearchHit(href=href, text=text, context=context))
    return hits


def _norm_match_text(value: str) -> str:
    """Lowercase + strip accents + collapse whitespace for robust comparison."""
    normalized = unicodedata.normalize("NFKD", value)
    ascii_str = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"\s+", " ", ascii_str).strip()


def canonico_match_tokens(target: SeedTarget) -> list[str]:
    """Deterministic phrases that identify ``target`` inside a results page.

    Built purely from the seed ``canonico`` triple ``[tipo, numero, anio]`` and
    the ``norma`` label. For "Ley 80 de 1993" this yields tokens like
    ``"ley 80 de 1993"`` and ``"80 de 1993"``; for a sentence like
    "Sentencia C-355 de 2006" it yields ``"c-355 de 2006"`` and ``"c-355/06"``.
    Empty positions (e.g. the Constitution) fall back to the ``norma`` label.
    """
    tipo = (target.tipo or "").replace("_", " ")
    numero = target.numero or ""
    anio = target.anio or ""
    tokens: list[str] = []
    if numero and anio:
        if tipo and not tipo.startswith("jurispruden"):
            tokens.append(_norm_match_text(f"{tipo} {numero} de {anio}"))
        tokens.append(_norm_match_text(f"{numero} de {anio}"))
        # Jurisprudence identifiers also appear as "C-355/06" / "C-355/2006".
        tokens.append(_norm_match_text(f"{numero}/{anio}"))
        if len(anio) == 4:
            tokens.append(_norm_match_text(f"{numero}/{anio[2:]}"))
    # Always include the full norma label as a fallback signal.
    tokens.append(_norm_match_text(target.norma))
    # De-duplicate while preserving order; drop empties.
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


#: Words dropped when comparing a norma label word-by-word against a results
#: hit: connectors that add no discriminating power ("codigo general DEL
#: proceso" must still match the seed label "codigo general proceso").
_STOPWORDS = frozenset({"de", "del", "la", "el", "los", "las", "y", "no"})


def _significant_words(phrase: str) -> list[str]:
    """Split ``phrase`` into accent-folded words minus connective stopwords."""
    return [w for w in _norm_match_text(phrase).split() if w and w not in _STOPWORDS]


def select_matching_hit(target: SeedTarget, hits: list[SearchHit]) -> SearchHit | None:
    """Return the first results hit that deterministically matches ``target``.

    Two deterministic signals, tried in order (FIRST matching hit wins, stable):

    1. Substring: any :func:`canonico_match_tokens` phrase (e.g. ``"ley 80 de
       1993"``, ``"c-355 de 2006"``, ``"c-355/06"``) appears verbatim in the
       accent-folded anchor text or its row context. This is the primary path
       for numbered norms and jurisprudence.
    2. Word-subset fallback: every significant word of the ``norma`` label is
       present in the hit (connectors like "de"/"del" ignored). This lets a
       seed label such as "Codigo general proceso" match a results anchor
       "Codigo General del Proceso" without fabricating anything.

    Returns ``None`` when nothing matches -- the caller then records
    ``no_encontrado`` and NEVER fabricates a URL.
    """
    tokens = canonico_match_tokens(target)
    norma_words = _significant_words(target.norma)
    for hit in hits:
        haystack = _norm_match_text(f"{hit.text} {hit.context}")
        if any(tok in haystack for tok in tokens):
            return hit
    # Word-subset fallback only when the label carries enough signal (>=2 words)
    # to avoid over-eager single-word matches.
    if len(norma_words) >= 2:
        for hit in hits:
            haystack_words = set(_significant_words(f"{hit.text} {hit.context}"))
            if all(w in haystack_words for w in norma_words):
                return hit
    return None


# ---------------------------------------------------------------------------
# Abstract adapter
# ---------------------------------------------------------------------------


class SourceAdapter(ABC):
    """Abstract base for one official gov.co portal.

    Subclasses set :attr:`fuente` (a short slug identifying the portal) and
    implement :meth:`matches`, :meth:`fetch` and :meth:`parse`. All shared,
    deterministic parsing utilities live on this base so adapters stay small and
    the pipeline can treat every source uniformly.
    """

    #: Short slug identifying this portal (used in ``data/raw/<fuente>/``).
    fuente: str = "generic"

    @abstractmethod
    def matches(self, url: str) -> bool:
        """Return True if this adapter handles ``url``."""

    def fetch(self, target: SeedTarget, client: PoliteClient, raw_dir: str) -> RawDoc:
        """Download ``target.donde_buscar`` and persist the raw bytes first.

        Default implementation: GET the URL through the polite client, save the
        raw bytes to ``<raw_dir>/<fuente>/<slug>.html`` (raw-first), then return
        a :class:`RawDoc`. Adapters with special fetch needs may override.
        """
        result: FetchResult = client.get(target.donde_buscar)
        raw_path = self.save_raw(result.content, target, raw_dir)
        return RawDoc(
            content=result.content,
            raw_path=raw_path,
            final_url=result.final_url,
            from_cache=result.from_cache,
        )

    def save_raw(self, content: bytes, target: SeedTarget, raw_dir: str) -> str:
        """Persist raw bytes to ``<raw_dir>/<fuente>/<slug>.html`` and return path."""
        slug = slugify(target.norma)
        dest_dir = Path(raw_dir) / self.fuente
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{slug}.html"
        dest.write_bytes(content)
        return str(dest)

    # -- Phase A: deterministic reachability check -----------------------

    #: Deterministic phrases that, when present in a fetched results page,
    #: signal the requested norm is NOT available. Subclasses override for
    #: portal-specific markers.
    not_found_markers: tuple[str, ...] = (
        "no se encontraron resultados",
        "no se encontro",
        "no se encontró",
        "sin resultados",
        "0 resultados",
    )

    def validate(self, target: SeedTarget, client: PoliteClient) -> ValidationResult:
        """Deterministically check whether ``target`` is reachable (Phase A).

        Default implementation: GET ``target.donde_buscar`` through the polite
        client. Returns ``ok=False`` on transport failure (``timeout`` /
        network error) and, for search-style portals, when the response body
        carries a deterministic "no results" marker. Never fabricates: a body
        that cannot be confirmed as a real hit is reported honestly. Adapters
        with portal-specific not-found signals override this.
        """
        try:
            result: FetchResult = client.get(target.donde_buscar)
        except FetchError as exc:
            return ValidationResult(
                ok=False,
                reason=f"fetch_failed: {exc.message}",
                final_url=target.donde_buscar,
                status_code=None,
            )
        text = clean_html_to_text(result.content).lower()
        for marker in self.not_found_markers:
            if marker in text:
                return ValidationResult(
                    ok=False,
                    reason="no_result_marker",
                    final_url=result.final_url,
                    status_code=200,
                )
        return ValidationResult(
            ok=True,
            reason="reachable",
            final_url=result.final_url,
            status_code=200,
        )

    # -- Phase A: shared deterministic search-page resolution ------------

    @staticmethod
    def is_search_url(url: str) -> bool:
        """True when ``url`` is a portal ``?q=`` search endpoint (not a direct doc).

        A direct document link that merely carries a ``?q=`` marketing param but
        whose path already ends in ``.html``/``.pdf`` (e.g. the Constitution)
        is NOT treated as a search page.
        """
        low = url.lower()
        path_before_query = low.split("?", 1)[0]
        has_q = "?q=" in low or "&q=" in low
        if not has_q:
            return False
        return not (path_before_query.endswith(".html") or path_before_query.endswith(".pdf"))

    def resolve_search(
        self, target: SeedTarget, client: PoliteClient
    ) -> ValidationResult:
        """Resolve a ``?q=`` search URL to a DIRECT document URL (Phase A).

        Deterministic pipeline (no LLM):

        1. GET the search page via the polite client (1s spacing, backoff,
           on-disk cache all provided by :class:`PoliteClient`).
        2. Parse the results anchors with :func:`parse_search_hits`.
        3. Pick the FIRST hit matching ``target`` via
           :func:`select_matching_hit` (canonico ``[tipo, numero, anio]`` +
           norma label).
        4. Return ``ok=True`` with ``resolved_url`` = the absolute document URL.

        When the page shows a deterministic "no results" marker or no hit
        matches, returns ``ok=False`` with a clear ``reason`` and NEVER
        fabricates a URL.
        """
        try:
            result: FetchResult = client.get(target.donde_buscar)
        except FetchError as exc:
            return ValidationResult(
                ok=False,
                reason=f"fetch_failed: {exc.message}",
                final_url=target.donde_buscar,
                status_code=None,
            )
        text = clean_html_to_text(result.content).lower()
        for marker in self.not_found_markers:
            if marker in text:
                return ValidationResult(
                    ok=False,
                    reason="sin resultados en buscador",
                    final_url=result.final_url,
                    status_code=200,
                )
        hits = parse_search_hits(result.content)
        hit = select_matching_hit(target, hits)
        if hit is None:
            return ValidationResult(
                ok=False,
                reason="match ambiguo: ningun resultado del buscador coincide con la norma",
                final_url=result.final_url,
                status_code=200,
            )
        resolved = urljoin(result.final_url, hit.href)
        return ValidationResult(
            ok=True,
            reason="resuelto_desde_buscador",
            final_url=result.final_url,
            status_code=200,
            resolved_url=resolved,
        )

    @abstractmethod
    def parse(
        self, raw_bytes: bytes, target: SeedTarget
    ) -> tuple[DocumentRecord, list[ArticleFragment]]:
        """Parse raw bytes into a document record + article fragments.

        Implementations MUST be deterministic (BeautifulSoup/lxml/regex only)
        and MUST NOT fabricate content. When the norm is absent from the source
        they should return a :class:`DocumentRecord` with
        ``status='no_encontrado'`` and an empty article list.
        """

    # -- shared helpers exposed to subclasses ----------------------------

    @staticmethod
    def clean_html_to_text(raw: bytes | str) -> str:
        return clean_html_to_text(raw)

    @staticmethod
    def split_articles(text: str) -> list[tuple[str, str, int, int]]:
        return split_articles(text)

    def build_fragments(self, doc_id: str, text: str) -> list[ArticleFragment]:
        """Turn cleaned ``text`` into traceable :class:`ArticleFragment` rows."""
        fragments: list[ArticleFragment] = []
        for numero, encabezado, start, end in split_articles(text):
            fragments.append(
                ArticleFragment(
                    # Fold offset_start into the id so repeated article
                    # numbers (transitory articles, multi-book codes) stay
                    # unique and are not silently dropped by INSERT OR
                    # REPLACE (review issue #1).
                    article_id=f"{doc_id}:art-{slugify(numero)}-{start}",
                    doc_id=doc_id,
                    articulo_numero=numero,
                    encabezado=encabezado,
                    texto=text[start:end].strip(),
                    offset_start=start,
                    offset_end=end,
                )
            )
        return fragments
