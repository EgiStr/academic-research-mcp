"""Unit tests for the pure logic: models, dedupe, formatting, and each provider's parser.

Network calls are mocked throughout: these run offline and in CI. Provider parsers are
tested here rather than through the registry, because a parser is pure input→Paper and that
is where a renamed upstream field shows up first.
"""

from __future__ import annotations

import pytest

from academic_research_mcp import dedupe, formatting
from academic_research_mcp.models import Paper, truncate, year_str
from academic_research_mcp.providers import arxiv, crossref, doaj, europe_pmc, openalex, semantic_scholar

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
    assert openalex._abstract({"second": [1], "first": [0], "third": [2]}) == "first second third"


def test_openalex_abstract_handles_missing_index():
    assert openalex._abstract(None) is None
    assert openalex._abstract({}) is None


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
    paper = openalex.to_paper(work)
    assert paper.doi == "10.1/abc"
    assert paper.citations == 12
    assert paper.authors == ["A One", "B Two"]
    assert paper.abstract == "hello world"
    assert paper.is_open_access is True
    assert paper.source == "OpenAlex"


def test_openalex_paper_survives_a_work_missing_optional_blocks():
    paper = openalex.to_paper({"display_name": "Bare"})
    assert paper.title == "Bare"
    assert paper.authors == []
    assert paper.citations is None
    assert paper.abstract is None
    assert paper.extra == {}


def test_openalex_work_id_for_normalises_every_doi_form():
    assert openalex.work_id_for("10.1/x") == "doi:10.1/x"
    assert openalex.work_id_for("https://doi.org/10.1/x") == "doi:10.1/x"
    assert openalex.work_id_for("doi:10.1/x") == "doi:10.1/x"


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


def test_parse_atom_reads_every_field():
    papers = arxiv.parse_atom(ARXIV_XML)
    assert len(papers) == 1
    paper = papers[0]
    assert paper.title == "From Local to Global Retrieval"
    assert paper.year == 2024
    assert paper.authors == ["Darren Edge", "Ha Trinh"]
    assert paper.doi == "10.1234/example"
    assert paper.venue == "Some Journal"
    assert paper.pdf_url == "https://arxiv.org/pdf/2401.12345v1"
    assert paper.extra["arxiv_id"] == "2401.12345v1"
    assert paper.is_open_access is True


def test_parse_atom_handles_an_empty_feed():
    assert arxiv.parse_atom("<feed></feed>") == []


def test_unescape_covers_the_entities_arxiv_emits():
    assert arxiv.unescape("a &amp; b &lt;c&gt; &quot;d&quot; &#39;e&#39;") == "a & b <c> \"d\" 'e'"


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("2401.12345", "2401.12345"),
        ("arXiv:2401.12345", "2401.12345"),
        ("2401.12345v2", "2401.12345v2"),
        ("http://arxiv.org/abs/2401.12345v1", "2401.12345v1"),
        ("https://arxiv.org/pdf/2401.12345", "2401.12345"),
    ],
)
def test_extract_arxiv_id_preserves_the_version_suffix(identifier, expected):
    """The version matters: v1 and v2 are different revisions, so it is not stripped."""
    assert arxiv.extract_arxiv_id(identifier) == expected


def test_extract_arxiv_id_returns_none_for_a_doi():
    assert arxiv.extract_arxiv_id("10.1145/3539618") is None


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
    paper = crossref.to_paper(item)
    assert paper.year == 2023
    assert paper.authors == ["Ada Lovelace", "Org"]
    assert paper.abstract == "An abstract."
    assert paper.pdf_url == "http://x/pdf"
    assert paper.citations == 7
    assert paper.source == "Crossref"


def test_crossref_paper_tolerates_a_record_with_no_authors():
    """Book chapters routinely have none; that is not a parse failure."""
    paper = crossref.to_paper({"title": ["Chapter"], "DOI": "10.1/c"})
    assert paper.authors == []
    assert paper.title == "Chapter"


def test_crossref_date_falls_back_through_the_date_fields():
    assert crossref._date({"issued": {"date-parts": [[1999]]}}) == 1999
    assert crossref._date({}) is None


def test_crossref_select_list_excludes_unsupported_fields():
    """Crossref rejects an unknown `select` with HTTP 400 — `language` broke live searches."""
    assert "language" not in crossref._SELECT.split(",")
    assert "DOI" in crossref._SELECT and "container-title" in crossref._SELECT


def test_clean_doi_strips_every_prefix_form():
    for raw in ("https://doi.org/10.1/x", "http://dx.doi.org/10.1/x", "doi:10.1/x", " 10.1/x "):
        assert crossref.clean_doi(raw) == "10.1/x"


# --------------------------------------------------------------------------- Europe PMC

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
    paper = europe_pmc.to_paper(item)
    assert paper.title == "Trial results"
    assert paper.authors == ["A One", "B Two"]
    assert paper.citations == 4
    assert paper.is_open_access is True
    assert "<" not in (paper.abstract or "")


def test_europe_pmc_paper_handles_a_closed_article():
    paper = europe_pmc.to_paper({"title": "Closed", "id": "1", "source": "MED"})
    assert paper.is_open_access is False
    assert paper.pdf_url is None


# --------------------------------------------------------------------------- Semantic Scholar

def test_semantic_scholar_paper_maps_external_ids_and_open_pdf():
    payload = {
        "title": "S2 Work",
        "year": 2023,
        "authors": [{"name": "A"}],
        "venue": "NeurIPS",
        "citationCount": 5,
        "externalIds": {"DOI": "10.1/s2", "ArXiv": "2401.1", "CorpusId": 42},
        "url": "https://s2/x",
        "openAccessPdf": {"url": "http://x.pdf"},
        "fieldsOfStudy": ["Computer Science"],
    }
    paper = semantic_scholar.to_paper(payload)
    assert paper.doi == "10.1/s2"
    assert paper.is_open_access is True
    assert paper.pdf_url == "http://x.pdf"
    assert paper.extra["CorpusId"] == 42
    assert paper.source == "Semantic Scholar"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10.1/x", "DOI:10.1/x"),
        ("DOI:10.1/x", "DOI:10.1/x"),
        ("2401.12345", "ARXIV:2401.12345"),
        ("CorpusId:42", "CorpusId:42"),
        ("abc123", "abc123"),
    ],
)
def test_semantic_scholar_paper_id_shapes_the_identifier(raw, expected):
    assert semantic_scholar._paper_id(raw) == expected


# --------------------------------------------------------------------------- DOAJ

def test_doaj_paper_is_always_open_access_with_a_fulltext_link():
    item = {
        "id": "doaj-1",
        "bibjson": {
            "title": "Open Work",
            "year": "2020",
            "author": [{"name": "C Three"}],
            "identifier": [{"type": "doi", "id": "10.1/doaj"}],
            "link": [{"type": "fulltext", "url": "http://x/full"}],
            "journal": {"title": "OA Journal"},
            "abstract": "<p>Body</p>",
            "keywords": ["k"],
        },
    }
    paper = doaj.to_paper(item)
    assert paper.is_open_access is True
    assert paper.url == "http://x/full"
    assert paper.doi == "10.1/doaj"
    assert paper.abstract == "Body", "markup stripped and whitespace collapsed"


def test_doaj_paper_tolerates_a_string_publisher_and_title():
    """DOAJ sends journal.publisher as either a string or {"name": ...} depending on the
    record; a dict-only assumption crashed on live data."""
    string_pub = {"bibjson": {"title": "T", "journal": {"title": "J", "publisher": "Some Publisher"}}}
    assert doaj.to_paper(string_pub).extra["publisher"] == "Some Publisher"

    nested = {"bibjson": {"title": "T", "journal": {"title": "J", "publisher": {"name": "Nested Pub"}}}}
    assert doaj.to_paper(nested).extra["publisher"] == "Nested Pub"

    missing = {"bibjson": {"title": "T", "journal": {"title": "J"}}}
    assert "publisher" not in doaj.to_paper(missing).to_dict()


def test_doaj_paper_ignores_a_non_string_language():
    item = {"bibjson": {"title": "T", "journal": {"title": "J", "language": ["en"]}}}
    assert doaj.to_paper(item).language is None


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
    assert "et al." in formatting.format_paper(1, _p("T", authors=["A", "B", "C", "D", "E", "F"]))


def test_format_paper_shows_the_also_in_sources():
    assert "also indexed by: Crossref" in formatting.format_paper(1, _p("T", extra={"also_in": ["Crossref"]}))
