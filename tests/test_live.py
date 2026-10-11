"""Opt-in tests that hit the real APIs.

They verify the two things a mock cannot: that each provider still accepts our query shape,
and that our field names still match what it returns. Sources change; this is the alarm.

Run with:  uv run pytest -m live
Skipped by default so CI and offline work stay green.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from academic_research_mcp import config, dedupe, http
from academic_research_mcp.providers import (
    arxiv,
    crossref,
    doaj,
    europe_pmc,
    openalex,
    semantic_scholar,
    unpaywall,
)

pytestmark = pytest.mark.live

QUERY = "retrieval augmented generation"

# Providers whose reliability scoring keeps them in auto-search, so they must stay healthy.
RELIABLE = [
    ("openalex", openalex.OpenAlex()),
    ("arxiv", arxiv.Arxiv()),
    ("crossref", crossref.Crossref()),
    ("europe-pmc", europe_pmc.EuropePmc()),
    ("doaj", doaj.Doaj()),
]


@pytest.mark.parametrize(("name", "provider"), RELIABLE, ids=[n for n, _ in RELIABLE])
async def test_provider_still_returns_usable_records(name, provider):
    papers = await provider.search(QUERY, 3, None)
    assert papers, f"{name} returned nothing for a query that should match"
    for paper in papers:
        assert paper.title and paper.title != "Untitled", f"{name} returned an untitled record"
        assert paper.source, f"{name} did not set its source label"


@pytest.mark.parametrize(("name", "provider"), RELIABLE, ids=[n for n, _ in RELIABLE])
async def test_provider_field_names_have_not_drifted(name, provider):
    """A renamed upstream field shows up as records where everything parsed empty.

    Not every record carries authors or a year (book chapters, corrections), so the check
    is that the provider can still produce at least one fully-populated record.
    """
    papers = await provider.search(QUERY, 5, None)
    assert papers, f"{name} returned nothing for a query that should match"
    rich = [p for p in papers if p.authors and (p.url or p.doi)]
    assert rich, (
        f"{name}: none of {len(papers)} records had both authors and a link — "
        f"the field names probably changed. Sample: {papers[0].to_dict()}"
    )
    assert any(p.year for p in papers), f"{name}: not one record had a parsable year"


@pytest.mark.parametrize(("name", "provider"), RELIABLE, ids=[n for n, _ in RELIABLE])
async def test_provider_labels_match_their_metadata(name, provider):
    """Paper.source must equal meta.label, because merge precedence keys on it."""
    papers = await provider.search(QUERY, 1, None)
    assert papers[0].source == provider.meta.label


async def test_openalex_lookup_by_doi_round_trips():
    papers = await openalex.OpenAlex().search("attention is all you need", 1, None)
    assert papers and papers[0].doi
    same = await openalex.OpenAlex().lookup(papers[0].doi)
    assert same is not None
    assert dedupe.normalise_title(same.title) == dedupe.normalise_title(papers[0].title)


async def test_arxiv_lookup_by_id_round_trips():
    provider = arxiv.Arxiv()
    found = await provider.search("graph rag", 1, None)
    assert found and found[0].extra.get("arxiv_id")
    same = await provider.lookup(found[0].extra["arxiv_id"])
    assert same is not None
    assert same.title == found[0].title


async def test_arxiv_browse_returns_recent_submissions():
    papers = await arxiv.Arxiv().browse("cs.AI", 3)
    assert papers, "the category feed should never be empty"
    assert all(p.year for p in papers)


async def test_openalex_citations_returns_citers():
    """Resolve through a work id, then check the graph.

    The transformer paper is the regression case: OpenAlex mints it its own PID
    ("10.65215/2q58a426") rather than a Crossref DOI, and a `cited_by:doi:...` filter for
    that PID matches nothing. Resolving to a work id first is what makes this return the
    ~27k citers instead of an empty list.
    """
    provider = openalex.OpenAlex()
    seed = await provider.search("attention is all you need", 1, None)
    assert seed
    work_id = await provider._work_id_for(seed[0].doi or seed[0].source_id)
    assert work_id and work_id.startswith("W"), f"could not resolve a work id, got {work_id!r}"
    citers = await provider.citations(seed[0].doi or work_id, "cited-by", 3)
    assert citers, "the transformer paper has tens of thousands of citers"
    assert all(c.title for c in citers)


async def test_openalex_similar_returns_related_works():
    provider = openalex.OpenAlex()
    seed = await provider.search("graph retrieval augmented generation", 1, None)
    assert seed
    related = await provider.similar(seed[0], 3)
    assert related, "OpenAlex related_works should return neighbours for a normal paper"


async def test_europe_pmc_similar_returns_related_articles():
    provider = europe_pmc.EuropePmc()
    seed = await provider.lookup("PMID:33301246")  # the Pfizer vaccine trial
    assert seed
    related = await provider.similar(seed, 3)
    assert related, "Europe PMC citation neighbours should exist for a heavily cited trial"


async def test_unpaywall_finds_an_open_copy():
    provider = unpaywall.Unpaywall()
    if not http.mailto():
        pytest.skip("Unpaywall needs RESEARCH_MCP_MAILTO")
    result = await provider.fulltext_link("10.1038/s41586-023-06221-2")
    assert result.get("ok") is True, result.get("reason")
    assert result.get("best")


async def test_unpaywall_explains_a_missing_mailto(monkeypatch):
    monkeypatch.delenv("RESEARCH_MCP_MAILTO", raising=False)
    config.reload_config()
    result = await unpaywall.Unpaywall().fulltext_link("10.1/x")
    assert result["ok"] is False
    assert "RESEARCH_MCP_MAILTO" in str(result["reason"])


async def test_europe_pmc_fulltext_returns_sections():
    provider = europe_pmc.EuropePmc()
    result = await provider.fulltext_text("PMC7745181", 8000)
    assert result.get("ok") is True, result.get("reason")
    assert result.get("text"), "an open-access article should yield at least one section"


async def test_semantic_scholar_works_or_says_it_is_rate_limited():
    """Keyless S2 bursts into 429; a clear error is acceptable, a crash or empty parse is not."""
    try:
        papers = await semantic_scholar.SemanticScholar().search(QUERY, 3, None)
    except http.SourceError as exc:
        assert "rate limited" in str(exc) or "429" in str(exc), f"unexpected S2 failure: {exc}"
        return
    assert papers, "S2 answered but parsed to nothing — the field names probably changed"
    assert papers[0].title and papers[0].title != "Untitled"


async def test_arxiv_download_returns_a_real_pdf(tmp_path, monkeypatch):
    """The download path, end to end: resolve, fetch, verify the magic bytes."""
    monkeypatch.setenv("RESEARCH_MCP_STORAGE", str(tmp_path))
    config.reload_config()

    provider = arxiv.Arxiv()
    seed = await provider.lookup("2408.08921")
    assert seed and seed.pdf_url
    result = await provider.download(seed, "arxiv-live-test")
    assert result.get("ok") is True, result.get("reason")
    written = Path(result["path"])
    assert written.exists()
    assert written.read_bytes()[:4] == b"%PDF"
    assert result["bytes"] > 10_000, "a real paper PDF is not a few bytes"


async def test_find_similar_says_which_kind_of_signal_it_used():
    """Native relatedness and a keyword fallback must never be presented alike.

    Both outcomes are acceptable; silently passing a keyword match off as computed relatedness
    is not.
    """
    from academic_research_mcp import similar as similar_module
    from academic_research_mcp.providers import openalex as oa_module

    provider = oa_module.OpenAlex()
    seed = await provider.search("graph retrieval augmented generation", 1, None)
    assert seed
    found, _, method = await similar_module.find_similar([seed[0]], 3)
    assert method in {"native", "keywords", "none"}, method
    if method == "native":
        assert found, "a native signal must come with papers"
    if method == "keywords":
        assert found, "the fallback should find keyword matches for a normal title"
