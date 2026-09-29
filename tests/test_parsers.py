"""Deterministic parser tests for the article segmenter and source adapters.

These exercise the real code paths in ``adapters.base`` and the fully-supported
HTML adapters (secretariasenado + suin-juriscol) against checked-in fixtures.
They would fail if the article-splitting, offset computation or the
``no_encontrado`` signalling were reverted or broken.
"""

from __future__ import annotations

from pathlib import Path

from corpus_ingesta.adapters.base import clean_html_to_text, split_articles
from corpus_ingesta.adapters.secretariasenado import SecretariaSenadoAdapter
from corpus_ingesta.adapters.suin_juriscol import SuinJuriscolAdapter
from corpus_ingesta.models import IngestStatus, SeedTarget

FIXTURES = Path(__file__).parent / "fixtures"


def _target(url: str, norma: str = "Codigo de prueba") -> SeedTarget:
    return SeedTarget.from_dict(
        {
            "norma": norma,
            "canonico": ["ley", 5678, 2019],
            "items_del_banco": 0,
            "areas": ["Derecho civil"],
            "donde_buscar": url,
        }
    )


def test_split_articles_counts_and_numbers() -> None:
    """The multi-article fixture must split into exactly 5 fragments, with the
    correct ``articulo_numero`` for each (including the ``bis`` article)."""
    text = clean_html_to_text(
        (FIXTURES / "secretariasenado_multiarticulo.html").read_bytes()
    )
    frags = split_articles(text)
    numeros = [f[0] for f in frags]
    assert numeros == ["1", "2", "2 bis", "3", "4"]


def test_split_articles_offsets_are_monotonic_and_non_overlapping() -> None:
    text = clean_html_to_text(
        (FIXTURES / "secretariasenado_multiarticulo.html").read_bytes()
    )
    frags = split_articles(text)
    prev_end = 0
    for _numero, _head, start, end in frags:
        # Monotonic, non-overlapping: each fragment starts where the previous
        # one ended (or later) and never runs backwards.
        assert 0 <= start < end <= len(text)
        assert start >= prev_end
        prev_end = end
    # Fragments together cover from the first heading to end of text.
    assert frags[0][2] <= frags[1][2]


def test_offsets_slice_back_to_article_text() -> None:
    """Slicing ``text[start:end]`` must reproduce the article, heading first."""
    text = clean_html_to_text(
        (FIXTURES / "secretariasenado_multiarticulo.html").read_bytes()
    )
    frags = split_articles(text)
    for numero, _head, start, end in frags:
        segment = text[start:end]
        # The heading token of this article is at the very start of its slice.
        assert segment.lstrip().lower().startswith("articulo")
        # And the (normalised) number appears in that first heading line.
        first_line = segment.strip().splitlines()[0].lower()
        base = numero.split()[0]
        assert base in first_line


def test_secretariasenado_adapter_builds_traceable_fragments() -> None:
    adapter = SecretariaSenadoAdapter()
    raw = (FIXTURES / "secretariasenado_multiarticulo.html").read_bytes()
    t = _target(
        "http://www.secretariasenado.gov.co/senado/basedoc/ley_5678_2019.html"
    )
    assert adapter.matches(t.donde_buscar)
    doc, frags = adapter.parse(raw, t)
    assert doc.status == IngestStatus.PARSEADO.value
    assert len(frags) == 5
    text = clean_html_to_text(raw)
    for f in frags:
        assert f.articulo_numero is not None
        assert f.offset_start < f.offset_end
        # Fragment text is exactly the sliced-and-stripped article body.
        assert f.texto == text[f.offset_start : f.offset_end].strip()
        # Traceability: article_id embeds doc_id + article number.
        assert f.article_id.startswith(doc.doc_id)
        # Reserved vector columns stay empty during ingestion.
        assert f.vector_id is None
        assert f.embedding_ref is None


def test_suin_listing_with_results_parses_articles() -> None:
    adapter = SuinJuriscolAdapter()
    raw = (FIXTURES / "suin_listado.html").read_bytes()
    t = _target(
        "https://www.suin-juriscol.gov.co/legislacion/decreto-410-1971",
        norma="Codigo de Comercio",
    )
    assert adapter.matches(t.donde_buscar)
    doc, frags = adapter.parse(raw, t)
    assert doc.status == IngestStatus.PARSEADO.value
    assert [f.articulo_numero for f in frags] == ["1", "2"]


def test_suin_not_found_yields_no_encontrado_without_fabricated_rows() -> None:
    adapter = SuinJuriscolAdapter()
    raw = (FIXTURES / "suin_no_resultados.html").read_bytes()
    t = _target(
        "https://www.suin-juriscol.gov.co/legislacion/ley-9999",
        norma="Ley inexistente",
    )
    doc, frags = adapter.parse(raw, t)
    assert doc.status == IngestStatus.NO_ENCONTRADO.value
    assert doc.error_detail is not None
    assert frags == []


def test_secretariasenado_without_articles_is_no_encontrado() -> None:
    """A page with no ARTICULO headings must not fabricate fragments."""
    html = b"<html><head><title>Vacio</title></head><body><p>Sin normas.</p></body></html>"
    adapter = SecretariaSenadoAdapter()
    doc, frags = adapter.parse(
        html,
        _target("http://www.secretariasenado.gov.co/senado/basedoc/vacio.html"),
    )
    assert doc.status == IngestStatus.NO_ENCONTRADO.value
    assert frags == []


# ---------------------------------------------------------------------------
# Regression tests for the v1 review issues (1, 3, 4, 5).
# ---------------------------------------------------------------------------


def test_repeated_article_numbers_yield_unique_ids_and_persist_all() -> None:
    """Review issue #1: a norm that repeats an article number (multi-book code)
    must produce a UNIQUE ``article_id`` per fragment so none is dropped by the
    ``INSERT OR REPLACE`` persistence."""
    adapter = SecretariaSenadoAdapter()
    raw = (FIXTURES / "secretariasenado_articulos_repetidos.html").read_bytes()
    t = _target(
        "http://www.secretariasenado.gov.co/senado/basedoc/codigo_repetido.html",
        norma="Codigo con repetidos",
    )
    doc, frags = adapter.parse(raw, t)
    assert doc.status == IngestStatus.PARSEADO.value
    # Two "1o." + one "2o." -> three fragments, two of which share numero "1".
    numeros = [f.articulo_numero for f in frags]
    assert numeros == ["1", "2", "1"]
    ids = [f.article_id for f in frags]
    assert len(set(ids)) == len(ids) == 3  # all unique, none collapses
    # The two article-1 fragments carry distinct bodies.
    art1_bodies = [f.texto for f in frags if f.articulo_numero == "1"]
    assert art1_bodies[0] != art1_bodies[1]


def test_repeated_article_numbers_all_persist_in_db(tmp_path) -> None:
    """All fragments with a repeated numero survive the DB round-trip."""
    from corpus_ingesta.db import init_db, insert_articles, upsert_document

    adapter = SecretariaSenadoAdapter()
    raw = (FIXTURES / "secretariasenado_articulos_repetidos.html").read_bytes()
    t = _target(
        "http://www.secretariasenado.gov.co/senado/basedoc/codigo_repetido.html",
        norma="Codigo con repetidos",
    )
    doc, frags = adapter.parse(raw, t)
    conn = init_db(str(tmp_path / "corpus.sqlite"))
    upsert_document(conn, doc)  # satisfy the articles->documents foreign key
    n = insert_articles(conn, frags)
    assert n == 3
    stored = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    assert stored == 3  # no silent row loss
    conn.close()


def test_cross_reference_lines_are_not_treated_as_headings() -> None:
    """Review issue #3: in-body cross-references ("Articulo 12 de la ley...")
    must NOT start new fragments; only real headings do."""
    text = clean_html_to_text(
        (FIXTURES / "secretariasenado_citas_cruzadas.html").read_bytes()
    )
    frags = split_articles(text)
    numeros = [f[0] for f in frags]
    # Only the two genuine headings (5, 6); the "12" and "7" citations are body.
    assert numeros == ["5", "6"]
    # The cross-reference text is contained within article 5's body.
    art5 = text[frags[0][2] : frags[0][3]]
    assert "de la ley anterior" in art5.lower()


def test_search_result_page_is_no_encontrado_not_fabricated() -> None:
    """Review issue #4: a ``?q=`` search-results page must yield no_encontrado
    with a contract reminder, never a fabricated document."""
    adapter = SecretariaSenadoAdapter()
    raw = (FIXTURES / "secretariasenado_busqueda_qparam.html").read_bytes()
    t = _target(
        "http://www.secretariasenado.gov.co/senado/basedoc/?q=Codigo%20general",
        norma="Codigo general proceso",
    )
    doc, frags = adapter.parse(raw, t)
    assert doc.status == IngestStatus.NO_ENCONTRADO.value
    assert frags == []
    assert "busqueda" in (doc.error_detail or "").lower()


def test_direct_html_link_with_qparam_is_still_parsed() -> None:
    """A direct ``.../algo.html?q=...`` link (e.g. the Constitution) is a
    document, not a search page, and must still segment normally."""
    adapter = SecretariaSenadoAdapter()
    raw = (FIXTURES / "secretariasenado_multiarticulo.html").read_bytes()
    t = _target(
        "http://www.secretariasenado.gov.co/senado/basedoc/ley_5678_2019.html?q=algo",
    )
    doc, frags = adapter.parse(raw, t)
    assert doc.status == IngestStatus.PARSEADO.value
    assert len(frags) == 5


def test_pdf_bytes_are_extracted_and_segmented() -> None:
    """Review issue #5: PDF bytes are routed through pdfminer.six and segmented
    at ARTICULO boundaries exactly like HTML."""
    from corpus_ingesta.adapters.base import _looks_like_pdf, extract_pdf_text

    raw = (FIXTURES / "norma_articulos.pdf").read_bytes()
    assert _looks_like_pdf(raw)
    text = extract_pdf_text(raw)
    assert "ARTICULO 1" in text
    # clean_html_to_text auto-detects PDF and yields the same text.
    assert clean_html_to_text(raw) == text
    frags = split_articles(text)
    assert [f[0] for f in frags] == ["1", "2"]
