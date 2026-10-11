"""Opt-in tests that hit the real APIs.

They verify the two things a mock cannot: that each source still accepts our query shape,
and that our field names still match what it returns. Sources change; this is the alarm.

Run with:  uv run pytest -m live
Skipped by default so CI and offline work stay green.
"""

from __future__ import annotations

import pytest

from academic_research_mcp import dedupe, http, sources

pytestmark = pytest.mark.live

QUERY = "retrieval augmented generation"
# Every keyless source. Semantic Scholar rate-limits keyless callers hard, so it is tested
# separately below rather than failing the whole matrix.
SEARCHABLE = [sources.OPENALEX, sources.ARXIV, sources.CROSSREF, sources.EUROPE_PMC, sources.DOAJ]


@pytest.mark.parametrize("name", SEARCHABLE)
async def test_source_still_returns_usable_records(name):
    papers = await sources.SEARCHERS[name](QUERY, 3, None)
    assert papers, f"{name} returned nothing for a query that should match"
    for paper in papers:
        assert paper.title and paper.title != "Untitled", f"{name} returned an untitled record"
        assert paper.source, f"{name} did not set a source label"


@pytest.mark.parametrize("name", SEARCHABLE)
async def test_source_field_names_have_not_drifted(name):
    """A renamed upstream field shows up as records where everything parsed empty.

    Not every record carries authors or a year (book chapters, corrections), so the check
    is that the source can still produce at least one fully-populated record.
    """
    papers = await sources.SEARCHERS[name](QUERY, 5, None)
    assert papers, f"{name} returned nothing for a query that should match"
    rich = [p for p in papers if p.authors and (p.url or p.doi)]
    assert rich, (
        f"{name}: none of {len(papers)} records had both authors and a link — "
        f"the field names probably changed. Sample: {papers[0].to_dict()}"
    )
    assert any(p.year for p in papers), f"{name}: not one record had a parsable year"


async def test_openalex_lookup_by_doi_round_trips():
    papers = await sources.openalex_search("attention is all you need", 1, None)
    assert papers and papers[0].doi
    same = await sources.openalex_get(papers[0].doi)
    assert same is not None
    assert dedupe.normalise_title(same.title) == dedupe.normalise_title(papers[0].title)


async def test_arxiv_lookup_by_id_round_trips():
    found = await sources.arxiv_search("graph rag", 1, None)
    assert found and found[0].extra.get("arxiv_id")
    same = await sources.arxiv_get(found[0].extra["arxiv_id"])
    assert same is not None
    assert same.title == found[0].title


async def test_semantic_scholar_works_or_says_it_is_rate_limited():
    """Keyless S2 bursts into 429; a clear error is acceptable, a crash or empty parse is not."""
    try:
        papers = await sources.semantic_scholar_search(QUERY, 3, None)
    except http.SourceError as exc:
        assert "rate limited" in str(exc) or "429" in str(exc), f"unexpected S2 failure: {exc}"
        return
    assert papers, "S2 answered but parsed to nothing — the field names probably changed"
    assert papers[0].title and papers[0].title != "Untitled"
