"""Tool-level tests with the network stubbed out.

These drive the actual MCP tool functions, so they catch wiring mistakes: a wrong source
name, a crash on empty results, an argument that never reaches the query.
"""

from __future__ import annotations

import pytest

from academic_research_mcp import http, server
from academic_research_mcp.models import Paper


@pytest.fixture()
def fake_sources(monkeypatch):
    """Replace every source adapter with a recorder returning canned papers."""
    calls: list[tuple[str, str, int, int | None]] = []

    def make(name: str, papers: list[Paper], *, boom: bool = False):
        async def adapter(query: str, limit: int, from_year: int | None) -> list[Paper]:
            calls.append((name, query, limit, from_year))
            if boom:
                raise http.SourceError(f"{name}: simulated outage")
            return papers[:limit]

        return adapter

    registry = {
        "openalex": make("openalex", [Paper(title="OA Paper", source="OpenAlex", doi="10.1/oa", citations=10, year=2024)]),
        "arxiv": make("arxiv", [Paper(title="arXiv Paper", source="arXiv", is_open_access=True, year=2024)]),
        "semantic-scholar": make("semantic-scholar", [Paper(title="S2 Paper", source="Semantic Scholar", doi="10.1/s2")]),
        "crossref": make("crossref", [Paper(title="CR Paper", source="Crossref", doi="10.1/cr")]),
        "europe-pmc": make("europe-pmc", [Paper(title="PMC Paper", source="Europe PMC", is_open_access=True)]),
        "doaj": make("doaj", [Paper(title="DOAJ Paper", source="DOAJ", is_open_access=True)]),
    }
    monkeypatch.setattr(server.sources, "SEARCHERS", registry)
    return calls


async def test_search_papers_auto_fans_out_and_dedupes(fake_sources):
    out = await server.search_papers(query="rag")
    assert "OA Paper" in out
    assert "arXiv Paper" in out
    assert "PMC Paper" in out
    named = {c[0] for c in fake_sources}
    assert named == set(server.AUTO_SOURCES), "auto must fan out over exactly AUTO_SOURCES"


async def test_semantic_scholar_is_reachable_but_not_in_auto(fake_sources):
    """It rate-limits keyless callers, so it is opt-in rather than part of every search."""
    assert "semantic-scholar" not in server.AUTO_SOURCES
    out = await server.search_papers(query="rag", source="semantic-scholar")
    assert "S2 Paper" in out


async def test_search_papers_single_source_only_calls_that_source(fake_sources):
    out = await server.search_papers(query="rag", source="arxiv")
    assert [c[0] for c in fake_sources] == ["arxiv"]
    assert "OA Paper" not in out


async def test_search_papers_rejects_an_unknown_source(fake_sources):
    out = await server.search_papers(query="rag", source="not-a-source")  # type: ignore[arg-type]
    assert "Unknown source" in out


async def test_search_papers_requires_a_query(fake_sources):
    assert "Provide a search query" in await server.search_papers(query="  ")


async def test_search_papers_passes_from_year_through(fake_sources):
    await server.search_papers(query="rag", source="openalex", from_year=2021)
    assert fake_sources[0][3] == 2021


async def test_search_papers_survives_one_source_failing(monkeypatch):
    async def ok(query, limit, from_year):
        return [Paper(title="Good Paper", source="OpenAlex", doi="10.1/ok")]

    async def boom(query, limit, from_year):
        raise http.SourceError("crossref: simulated outage")

    monkeypatch.setattr(server.sources, "SEARCHERS", {"openalex": ok, "crossref": boom})
    out = await server.search_papers(query="rag", source="auto")
    assert "Good Paper" in out, "a failing source must not empty the whole result"
    assert "crossref: simulated outage" in out


async def test_search_papers_reports_when_nothing_matches(monkeypatch):
    async def empty(query, limit, from_year):
        return []

    monkeypatch.setattr(server.sources, "SEARCHERS", {"openalex": empty})
    out = await server.search_papers(query="zzzz", source="openalex")
    assert "No papers matched" in out


async def test_open_access_filter_keeps_only_free_papers(monkeypatch):
    async def mixed(query, limit, from_year):
        return [
            Paper(title="Paywalled", source="Crossref", doi="10.1/closed"),
            Paper(title="Free", source="Crossref", doi="10.1/free", is_open_access=True),
        ]

    monkeypatch.setattr(server.sources, "SEARCHERS", {"crossref": mixed})
    out = await server.search_papers(query="x", source="crossref", open_access_only=True)
    assert "Free" in out
    assert "Paywalled" not in out


async def test_open_access_filter_explains_an_empty_result(monkeypatch):
    async def closed(query, limit, from_year):
        return [Paper(title="Paywalled", source="Crossref")]

    monkeypatch.setattr(server.sources, "SEARCHERS", {"crossref": closed})
    out = await server.search_papers(query="x", source="crossref", open_access_only=True)
    assert "No open-access papers" in out


async def test_max_results_is_clamped_not_rejected(fake_sources):
    await server.search_papers(query="rag", source="openalex", max_results=10_000)
    assert fake_sources[0][2] <= 50


# --------------------------------------------------------------------------- get_paper

async def test_get_paper_falls_through_to_the_next_source(monkeypatch):
    async def miss(_identifier):
        return None

    async def hit(_identifier):
        return Paper(title="Found It", source="Crossref", doi="10.1/x")

    monkeypatch.setattr(server.sources, "LOOKUPS", {"openalex": miss, "crossref": hit})
    out = await server.get_paper("10.1/x")
    assert "Found It" in out


async def test_get_paper_explains_itself_when_everything_misses(monkeypatch):
    async def miss(_identifier):
        return None

    monkeypatch.setattr(server.sources, "LOOKUPS", {k: miss for k in server.sources.DOI_SOURCES})
    out = await server.get_paper("10.1/nope")
    assert "Nothing found" in out


async def test_get_paper_routes_a_pmid_to_europe_pmc():
    assert server._guess_lookup_order("PMID:38012345")[0] == "europe-pmc"
    assert server._guess_lookup_order("PMC10650234")[0] == "europe-pmc"


async def test_get_paper_routes_a_doi_to_openalex_and_an_arxiv_id_to_arxiv():
    assert server._guess_lookup_order("10.1145/123")[0] == "openalex"
    assert server._guess_lookup_order("2401.12345")[0] == "arxiv"
    assert server._guess_lookup_order("W4386918332")[0] == "openalex"


async def test_get_paper_needs_an_identifier():
    assert "Provide a DOI" in await server.get_paper("  ")


# --------------------------------------------------------------------------- get_citations

async def test_get_citations_rejects_a_non_doi():
    assert "does not look like a DOI" in await server.get_citations("not-a-doi")


async def test_get_citations_renders_results(monkeypatch):
    async def fake(url, params=None, **kwargs):
        assert "cited_by:doi:10.1/x" in url
        return {"results": [{"display_name": "Citing Work", "publication_year": 2025, "cited_by_count": 1, "authorships": [{"author": {"display_name": "Z"}}]}]}

    monkeypatch.setattr(server.http, "request_json", fake)
    out = await server.get_citations("10.1/x", max_results=5)
    assert "Citing Work" in out
    assert "Papers citing" in out


async def test_get_citations_strips_a_doi_url(monkeypatch):
    seen = {}

    async def fake(url, params=None, **kwargs):
        seen["url"] = url
        return {"results": []}

    monkeypatch.setattr(server.http, "request_json", fake)
    await server.get_citations("https://doi.org/10.1/x")
    assert "doi:10.1/x" in seen["url"]


async def test_get_citations_explains_an_empty_reference_list(monkeypatch):
    async def fake(url, params=None, **kwargs):
        return {"results": []}

    monkeypatch.setattr(server.http, "request_json", fake)
    out = await server.get_citations("10.1/x", direction="cites")
    assert "No cites records" in out or "No " in out


# --------------------------------------------------------------------------- list_sources

async def test_list_sources_lists_everything_and_notes_missing_mailto(monkeypatch):
    monkeypatch.setattr(server.http, "MAILTO", "")
    out = await server.list_sources()
    for name in server.sources.SOURCE_NAMES:
        assert name in out
    assert "RESEARCH_MCP_MAILTO" in out, "the Unpaywall requirement must be visible"


async def test_list_sources_mentions_mailto_once_configured(monkeypatch):
    monkeypatch.setattr(server.http, "MAILTO", "researcher@example.org")
    out = await server.list_sources()
    assert "researcher@example.org" in out


# --------------------------------------------------------------------------- read_fulltext

async def test_read_fulltext_handles_a_closed_article(monkeypatch):
    async def closed(identifier):
        return {"ok": False, "reason": "PMID 123 has no open-access full text in Europe PMC."}

    monkeypatch.setattr(server.fulltext, "europe_pmc_fulltext", closed)
    out = await server.read_fulltext("123")
    assert "no open-access full text" in out


async def test_read_fulltext_renders_sections(monkeypatch):
    async def open_article(identifier):
        return {"ok": True, "pmcid": "PMC1", "text": {"abstract": "Abstract text.", "methods": "Method text."}}

    monkeypatch.setattr(server.fulltext, "europe_pmc_fulltext", open_article)
    out = await server.read_fulltext("PMC1")
    assert "## abstract" in out
    assert "Method text." in out


async def test_read_fulltext_clamps_max_chars(monkeypatch):
    async def open_article(identifier):
        return {"ok": True, "pmcid": "PMC1", "text": {"body": "x" * 100_000}}

    monkeypatch.setattr(server.fulltext, "europe_pmc_fulltext", open_article)
    out = await server.read_fulltext("PMC1", max_chars=10_000)
    assert len(out) < 12_000


# --------------------------------------------------------------------------- find_fulltext

async def test_find_fulltext_explains_a_missing_mailto(monkeypatch):
    monkeypatch.setattr(server.http, "MAILTO", "")
    out = await server.find_fulltext("10.1/x")
    assert "RESEARCH_MCP_MAILTO" in out


async def test_find_fulltext_rejects_a_non_doi():
    assert "does not look like a DOI" in await server.find_fulltext("nonsense")


async def test_find_fulltext_renders_an_open_access_result(monkeypatch):
    monkeypatch.setattr(server.http, "MAILTO", "a@b.c")

    async def fake(url, params=None, **kwargs):
        return {
            "is_oa": True,
            "oa_status": "gold",
            "title": "Free Paper",
            "journal_name": "OA Journal",
            "year": 2023,
            "best_oa_location": {"url_for_pdf": "http://x/free.pdf", "version": "publishedVersion", "license": "cc-by"},
            "oa_locations": [{"url_for_pdf": "http://x/free.pdf", "host_type": "publisher", "version": "publishedVersion"}],
        }

    monkeypatch.setattr(server.http, "request_json", fake)
    out = await server.find_fulltext("10.1/x")
    assert "http://x/free.pdf" in out
    assert "Free Paper" in out


async def test_find_fulltext_reports_when_no_free_copy_exists(monkeypatch):
    monkeypatch.setattr(server.http, "MAILTO", "a@b.c")

    async def fake(url, params=None, **kwargs):
        return {"is_oa": False, "best_oa_location": None, "oa_locations": []}

    monkeypatch.setattr(server.http, "request_json", fake)
    out = await server.find_fulltext("10.1/x")
    assert "No free copy found" in out
