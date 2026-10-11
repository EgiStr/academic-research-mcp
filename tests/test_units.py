"""Unit tests. Network calls are mocked: these run offline and in CI.

The parsing and merging logic is where the real bugs live — a source changing its field
names, or dedupe collapsing two different papers. That is what is covered here; the
end-to-end path is exercised by tests/test_live.py, which is opt-in.
"""

from __future__ import annotations

import pytest

from academic_research_mcp import dedupe, formatting, sources
from academic_research_mcp.models import Paper, truncate, year_str


# --------------------------------------------------------------------------- models

@pytest.mark.parametrize(
    ("raw", "expected"),
    [(2024, 2024), ("2024-05-01", 2024), ("2024", 2024), (None, None), ("n.d.", None), (1234, None), ("abc", None)],
)
def test_year_str_coerces_the_formats_sources_use(raw, expected):
    assert year_str(raw) == expected


def test_truncate_collapses_whitespace_and_marks_the_cut():
    assert truncate("a\n\n  b   c", 100) == "a b c"
    assert truncate("x" * 50, 10).endswith("…")
    assert truncate("", 10) is None


def test_paper_to_dict_drops_empty_fields():
    out = Paper(title="T", source="OpenAlex", authors=[], year=None, citations=0).to_dict()
    assert out == {"title": "T", "source": "OpenAlex", "citations": 0}, "0 must survive; None/[] must not"


# --------------------------------------------------------------------------- OpenAlex

def test_openalex_abstract_rebuilds_word_order():
    inverted = {"second": [1], "first": [0], "third": [2]}
    assert sources._openalex_abstract(inverted) == "first second third"


def test_openalex_abstract_handles_missing_index():
    assert sources._openalex_abstract(None) is None
    assert sources._openalex_abstract({}) is None


def test_openalex_paper_maps_fields_and_strips_the_doi_prefix():
    work = {
        "display_name": "Graph RAG",
        "publication_year": 2024,
        "cited_by_count": 12,
        "doi": "https://doi.org/10.1/abc",
        "id": "https://openalex.org/W1",
        "authorships": [{"author": {"display_name": "A One"}}, {"author": {"display_name": "B Two"}}],
        "primary_location": {"source": {"display_name": "NeurIPS"}},
        "open_access": {"is_oa": True},
        "abstract_inverted_index": {"hello": [0], "world": [1]},
        "keywords": [{"display_name": "RAG"}],
        "type": "article",
    }
    paper = sources._openalex_paper(work)
    assert paper.doi == "10.1/abc"
    assert paper.citations == 12
    assert paper.authors == ["A One", "B Two"]
    assert paper.abstract == "hello world"
    assert paper.is_open_access is True


def test_openalex_paper_survives_a_work_missing_optional_blocks():
    paper = sources._openalex_paper({"display_name": "Bare"})
    assert paper.title == "Bare"
    assert paper.authors == []
    assert paper.citations is None
    assert paper.abstract is None


def test_openalex_search_params_include_year_filter_and_mailto(monkeypatch):
    monkeypatch.setattr(sources.http, "MAILTO", "a@b.c")
    params = sources._openalex_params("rag", 5, 2022, "mailto")
    assert params["search"] == "rag"
    assert params["per-page"] == 5
    assert params["filter"] == "from_publication_date:2022-01-01"
    assert params["mailto"] == "a@b.c"


# --------------------------------------------------------------------------- arXiv

ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2401.12345v1</id>
    <title>From Local   to Global
    Retrieval</title>
    <summary>An abstract about graphs.</summary>
    <published>2024-01-22T18:00:00Z</published>
    <author><name>Darren Edge</name></author>
    <author><name>Ha Trinh</name></author>
    <arxiv:doi>10.1234/example</arxiv:doi>
    <arxiv:journal_ref>Some Journal</arxiv:journal_ref>
  </entry>
</feed>"""


def test_parse_arxiv_reads_every_field_and_unescapes():
    papers = sources._parse_arxiv(ARXIV_XML)
    assert len(papers) == 1
    paper = papers[0]
    assert paper.title == "From Local to Global Retrieval"
    assert paper.year == 2024
    assert paper.authors == ["Darren Edge", "Ha Trinh"]
    assert paper.doi == "10.1234/example"
    assert paper.venue == "Some Journal"
    assert paper.pdf_url == "https://arxiv.org/pdf/2401.12345v1"
    assert paper.extra["arxiv_id"] == "2401.12345v1"


def test_parse_arxiv_handles_a_feed_with_no_entries():
    assert sources._parse_arxiv("<feed></feed>") == []


def test_unescape_covers_the_entities_arxiv_emits():
    assert sources._unescape("a &amp; b &lt;c&gt; &quot;d&quot; &#39;e&#39;") == "a & b <c> \"d\" 'e'"


# --------------------------------------------------------------------------- Crossref

def test_crossref_paper_reads_authors_dates_and_pdf_link():
    item = {
        "title": ["A Study"],
        "DOI": "10.1/x",
        "published-print": {"date-parts": [[2023, 4]]},
        "author": [{"given": "Ada", "family": "Lovelace"}, {"name": "Org"}],
        "container-title": ["J. Things"],
        "is-referenced-by-count": 7,
        "abstract": "<jats:p>An abstract.</jats:p>",
        "type": "journal-article",
        "link": [{"URL": "http://x/pdf", "content-type": "application/pdf"}],
    }
    paper = sources._crossref_paper(item)
    assert paper.year == 2023
    assert paper.authors == ["Ada Lovelace", "Org"]
    assert paper.abstract == "An abstract."
    assert paper.pdf_url == "http://x/pdf"
    assert paper.citations == 7


def test_crossref_date_falls_back_through_the_date_fields():
    assert sources._crossref_date({"issued": {"date-parts": [[1999]]}}) == 1999
    assert sources._crossref_date({}) is None


# --------------------------------------------------------------------------- Europe PMC / DOAJ

def test_europe_pmc_paper_marks_open_access_and_strips_markup():
    item = {
        "title": "Trial results.",
        "authorString": "A One, B Two.",
        "pubYear": "2021",
        "journalTitle": "BMJ",
        "citedByCount": "4",
        "doi": "10.1/ep",
        "id": "123",
        "source": "MED",
        "isOpenAccess": "Y",
        "abstractText": "<h4>Background</h4>Text.",
    }
    paper = sources._epmc_paper(item)
    assert paper.title == "Trial results"
    assert paper.authors == ["A One", "B Two"]
    assert paper.citations == 4
    assert paper.is_open_access is True
    assert "<" not in (paper.abstract or "")


def test_doaj_paper_tolerates_a_string_publisher_and_title():
    """DOAJ sends journal.publisher as either a string or {"name": ...} depending on the
    record; a dict-only assumption crashed on live data."""
    base = {"bibjson": {"title": "T", "journal": {"title": "J", "publisher": "Some Publisher"}}}
    paper = sources._doaj_paper(base)
    assert paper.extra["publisher"] == "Some Publisher"

    nested = {"bibjson": {"title": "T", "journal": {"title": "J", "publisher": {"name": "Nested Pub"}}}}
    assert sources._doaj_paper(nested).extra["publisher"] == "Nested Pub"

    missing = {"bibjson": {"title": "T", "journal": {"title": "J"}}}
    assert "publisher" not in sources._doaj_paper(missing).to_dict()


def test_doaj_paper_ignores_a_non_string_language():
    item = {"bibjson": {"title": "T", "journal": {"title": "J", "language": ["en"]}}}
    assert sources._doaj_paper(item).language is None


def test_crossref_select_list_excludes_unsupported_fields():
    """Crossref rejects an unknown `select` with HTTP 400 — `language` broke live searches."""
    import asyncio

    seen: dict[str, Any] = {}

    async def fake(url, params=None, **kwargs):
        seen.update(params or {})
        return {"message": {"items": []}}

    original = sources.http.request_json
    sources.http.request_json = fake
    try:
        asyncio.run(sources.crossref_search("x", 3, None))
    finally:
        sources.http.request_json = original

    selected = seen["select"].split(",")
    assert "language" not in selected
    assert "DOI" in selected and "container-title" in selected


def test_doaj_paper_is_always_open_access_with_a_fulltext_link():
    item = {
        "id": "doaj-1",
        "bibjson": {
            "title": "Open Work",
            "year": "2020",
            "author": [{"name": "C Three"}],
            "identifier": [{"type": "doi", "id": "10.1/doaj"}],
            "link": [{"type": "fulltext", "url": "http://x/full"}],
            "journal": {"title": "OA Journal", "publisher": {"name": "P"}},
            "abstract": "<p>Body</p>",
            "keywords": ["k"],
        },
    }
    paper = sources._doaj_paper(item)
    assert paper.is_open_access is True
    assert paper.url == "http://x/full"
    assert paper.doi == "10.1/doaj"
    assert paper.abstract == "Body", "markup stripped and whitespace collapsed"


# --------------------------------------------------------------------------- dedupe

def _p(title="T", **kwargs):
    kwargs.setdefault("source", "OpenAlex")
    return Paper(title=title, **kwargs)


def test_dedupe_merges_on_doi_and_keeps_the_richest_fields():
    a = _p("Same Paper", doi="10.1/x", citations=5, abstract="short")
    b = _p("Same Paper", source="Crossref", doi="https://doi.org/10.1/x", citations=9, abstract="a much longer abstract")
    merged = dedupe.dedupe([a, b])
    assert len(merged) == 1
    assert merged[0].citations == 9, "highest reported citation count wins"
    assert merged[0].abstract == "a much longer abstract"
    assert merged[0].doi == "10.1/x"
    assert merged[0].extra["also_in"] == ["Crossref"]


def test_dedupe_merges_on_normalised_title_when_no_doi():
    a = _p("Deep Learning: A Review!", year=2020)
    b = _p("deep learning a review".upper(), source="Crossref", year=2020)
    assert len(dedupe.dedupe([a, b])) == 1


def test_dedupe_merges_on_title_alone_when_one_year_is_missing():
    a = _p("Attention Is All You Need", year=None)
    b = _p("Attention Is All You Need", source="Crossref", year=2017)
    merged = dedupe.dedupe([a, b])
    assert len(merged) == 1
    assert merged[0].year == 2017


def test_dedupe_keeps_genuinely_different_papers_apart():
    a = _p("Graph RAG for Question Answering", year=2024)
    b = _p("Graph RAG for Medical Question Answering", source="Crossref", year=2024)
    assert len(dedupe.dedupe([a, b])) == 2


def test_dedupe_preserves_input_order():
    papers = [_p("First Paper", year=2020), _p("Second Paper", year=2021), _p("Third Paper", year=2022)]
    assert [p.title for p in dedupe.dedupe(papers)] == ["First Paper", "Second Paper", "Third Paper"]


def test_normalise_doi_strips_every_prefix_form():
    for raw in ("https://doi.org/10.1/X", "http://dx.doi.org/10.1/X", "doi:10.1/X", "  10.1/X  "):
        assert dedupe.normalise_doi(raw) == "10.1/x"
    assert dedupe.normalise_doi(None) is None
    assert dedupe.normalise_doi("  ") is None


def test_dedupe_of_empty_input_is_empty():
    assert dedupe.dedupe([]) == []


# --------------------------------------------------------------------------- formatting

def test_format_results_reports_omitted_count():
    out = formatting.format_results([_p("A"), _p("B")], omitted=3)
    assert "3 further result(s) not shown" in out


def test_format_results_gives_recovery_advice_when_empty():
    assert "No papers matched" in formatting.format_results([])


def test_format_paper_omits_abstract_unless_requested():
    paper = _p("Title", abstract="secret", url="http://u", doi="10.1/x", citations=3)
    assert "secret" not in formatting.format_paper(1, paper)
    assert "secret" in formatting.format_paper(1, paper, abstract_chars=100)


def test_format_paper_summarises_long_author_lists():
    paper = _p("T", authors=["A", "B", "C", "D", "E", "F"])
    assert "et al." in formatting.format_paper(1, paper)


def test_format_paper_shows_the_also_in_sources():
    paper = _p("T", extra={"also_in": ["Crossref"]})
    assert "also indexed by: Crossref" in formatting.format_paper(1, paper)


def test_source_list_mentions_every_source():
    out = formatting.format_source_list([(n, sources.SOURCE_NOTES[n]) for n in sources.SOURCE_NAMES])
    for name in sources.SOURCE_NAMES:
        assert name in out


def test_display_rank_orders_sources_by_trust():
    # Keyed by the display label, which is exactly what Paper.source carries.
    assert sources.DISPLAY_RANK["OpenAlex"] == 0
    assert sources.DISPLAY_RANK["OpenAlex"] < sources.DISPLAY_RANK["Semantic Scholar"]
    assert sources.DISPLAY_RANK["Semantic Scholar"] < sources.DISPLAY_RANK["Crossref"]


def test_every_source_has_a_display_label_that_papers_and_rank_agree_on():
    """A missing label silently ranks a source last and mislabels its records, so pin it."""
    assert set(sources.DISPLAY_NAME) == set(sources.SOURCE_NAMES)
    assert set(sources.DISPLAY_RANK) == set(sources.DISPLAY_NAME.values())
    assert all(sources.DISPLAY_NAME[name] for name in sources.SOURCE_NAMES)

    # Each adapter must emit the label its own DISPLAY_NAME claims.
    for slug, label in sources.DISPLAY_NAME.items():
        expected = {"openalex": "OpenAlex", "arxiv": "arXiv", "semantic-scholar": "Semantic Scholar", "crossref": "Crossref", "europe-pmc": "Europe PMC", "doaj": "DOAJ"}
        assert label == expected[slug], f"{slug} label drifted; server.py sorts on this string"


def test_paper_sources_are_all_ranked(monkeypatch):
    """Any adapter returning an unranked label would sink to the bottom of results."""
    samples = [
        sources._openalex_paper({"display_name": "x"}),
        sources._crossref_paper({"title": ["x"]}),
        sources._epmc_paper({"title": "x"}),
        sources._doaj_paper({"bibjson": {"title": "x"}}),
        sources._s2_paper({"title": "x"}),
    ]
    for paper in samples:
        assert paper.source in sources.DISPLAY_RANK, f"{paper.source} is unranked"
