"""Adapter parsing tests against small handcrafted fixtures (no network)."""

from __future__ import annotations

from pathlib import Path

from corpus_ingesta.adapters import default_adapters
from corpus_ingesta.adapters.base import clean_html_to_text, slugify, split_articles
from corpus_ingesta.adapters.secretariasenado import SecretariaSenadoAdapter
from corpus_ingesta.adapters.suin_juriscol import SuinJuriscolAdapter
from corpus_ingesta.models import IngestStatus, SeedTarget

FIXTURES = Path(__file__).parent / "fixtures"


def _target(url: str, norma: str = "Codigo de prueba") -> SeedTarget:
    return SeedTarget.from_dict(
        {
            "norma": norma,
            "canonico": ["ley", 1234, 2020],
            "items_del_banco": 0,
            "areas": ["Derecho civil"],
            "donde_buscar": url,
        }
    )


def test_slugify_deterministic() -> None:
    assert slugify("Codigo General del Proceso") == "codigo-general-del-proceso"
    assert slugify("Decision Andina 486") == "decision-andina-486"
    assert slugify("") == "item"


def test_split_articles_offsets_are_ordered() -> None:
    text = clean_html_to_text((FIXTURES / "secretariasenado_sample.html").read_bytes())
    frags = split_articles(text)
    assert len(frags) == 3
    numeros = [f[0] for f in frags]
    assert numeros == ["1", "2", "3"]
    for _numero, _head, start, end in frags:
        assert 0 <= start < end <= len(text)


def test_secretariasenado_parses_multiple_articles() -> None:
    adapter = SecretariaSenadoAdapter()
    raw = (FIXTURES / "secretariasenado_sample.html").read_bytes()
    t = _target("http://www.secretariasenado.gov.co/senado/basedoc/x.html")
    assert adapter.matches(t.donde_buscar)
    doc, frags = adapter.parse(raw, t)
    assert doc.status == IngestStatus.PARSEADO.value
    assert len(frags) > 1
    for f in frags:
        assert f.articulo_numero is not None
        assert f.offset_start < f.offset_end
        assert f.texto.strip()


def test_suin_no_encontrado_produces_no_articles() -> None:
    adapter = SuinJuriscolAdapter()
    raw = (FIXTURES / "suin_no_resultados.html").read_bytes()
    t = _target(
        "https://www.suin-juriscol.gov.co/legislacion/ley-9999", norma="Ley inexistente"
    )
    doc, frags = adapter.parse(raw, t)
    assert doc.status == IngestStatus.NO_ENCONTRADO.value
    assert frags == []


def test_routing_covers_all_seed_domains() -> None:
    adapters = default_adapters()
    urls = [
        "http://www.secretariasenado.gov.co/senado/basedoc/x.html",
        "https://www.suin-juriscol.gov.co/legislacion/ley-1",
        "https://www.corteconstitucional.gov.co/relatoria/buscador-jurisprudencia",
        "https://lexis.minjusticia.gov.co/buscador/Detallado/3",
        "https://samai.consejodeestado.gov.co/TitulacionRelatoria/x.aspx",
    ]
    for url in urls:
        assert any(a.matches(url) for a in adapters), url


def test_corte_metadata_fragment_uses_sentinel_offsets() -> None:
    """Review issue #6: when the Corte adapter detects sentence identifiers but
    no article bodies, the synthetic fragment is labelled metadata-only and uses
    sentinel offsets (-1/-1) rather than implying a source-text span."""
    from corpus_ingesta.adapters.corteconstitucional import CorteConstitucionalAdapter

    adapter = CorteConstitucionalAdapter()
    raw = (
        b"<html><body><p>Providencias: C-123/24, T-045 de 2020, "
        b"SU-111/19.</p></body></html>"
    )
    t = _target(
        "https://www.corteconstitucional.gov.co/relatoria/buscador-jurisprudencia",
        norma="Jurisprudencia comercial",
    )
    doc, frags = adapter.parse(raw, t)
    assert doc.status == IngestStatus.PARSEADO.value
    assert len(frags) == 1
    frag = frags[0]
    assert frag.article_id.endswith(":metadata-sentencias")
    assert frag.offset_start == -1 and frag.offset_end == -1
    assert frag.articulo_numero is None
    assert "C-123" in frag.texto
