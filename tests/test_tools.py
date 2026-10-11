"""Tool-level tests with the network stubbed out.

These drive the actual MCP tool functions, so they catch wiring mistakes: an unknown source
name, a crash on empty results, an argument that never reaches the query, or a tool that
forgets to consult the capability registry.
"""

from __future__ import annotations

import pytest

from academic_research_mcp import config as academic_config
from academic_research_mcp import http as academic_http
from academic_research_mcp import server
from academic_research_mcp.models import Paper
from academic_research_mcp.providers import Capability, Provider, ProviderMeta
from academic_research_mcp.providers import registry as registry_module


class FakeProvider(Provider):
    """A provider whose every capability is scripted, so tools can be driven deterministically."""

    meta: ProviderMeta

    def __init__(
        self,
        *,
        search_results=None,
        lookup_result=None,
        citations=None,
        link=None,
        text=None,
        download=None,
        similar=None,
        boom=False,
    ):
        self._search_results = search_results if search_results is not None else []
        self._lookup_result = lookup_result
        self._citations = citations if citations is not None else []
        self._link = link
        self._text = text
        self._download = download
        self._similar = similar if similar is not None else []
        self._boom = boom
        self.calls: list[tuple] = []

    def _maybe_boom(self):
        if self._boom:
            raise academic_http.SourceError(f"{self.meta.label}: simulated outage")

    async def search(self, query, limit, from_year):
        self.calls.append(("search", query, limit, from_year))
        self._maybe_boom()
        return self._search_results[:limit]

    async def lookup(self, identifier):
        self.calls.append(("lookup", identifier))
        self._maybe_boom()
        return self._lookup_result

    async def citations(self, doi, direction, limit):
        self.calls.append(("citations", doi, direction, limit))
        self._maybe_boom()
        return self._citations[:limit]

    async def fulltext_link(self, doi):
        self.calls.append(("fulltext_link", doi))
        self._maybe_boom()
        return self._link if self._link is not None else {"ok": False, "reason": "no link"}

    async def fulltext_text(self, identifier, max_chars):
        self.calls.append(("fulltext_text", identifier, max_chars))
        self._maybe_boom()
        return self._text if self._text is not None else {"ok": False, "reason": "no text"}

    async def download(self, paper, destination):
        self.calls.append(("download", paper.title, destination))
        self._maybe_boom()
        return self._download if self._download is not None else {"ok": False, "reason": "no file"}

    async def similar(self, paper, limit):
        self.calls.append(("similar", paper.title, limit))
        self._maybe_boom()
        return self._similar[:limit]


def make(name, label, caps, **kwargs):
    """Build a scripted provider with the given capabilities and meta overrides."""
    meta_kwargs = {k: kwargs.pop(k) for k in ("reliability", "auto_search", "deprecated", "aliases", "requires_mailto") if k in kwargs}
    provider = FakeProvider(**kwargs)
    provider.meta = ProviderMeta(
        name=name,
        label=label,
        homepage="https://example.org",
        summary=f"{label} summary",
        capabilities=frozenset(caps),
        **meta_kwargs,
    )
    provider.validate()
    return provider


@pytest.fixture(autouse=True)
def clean_registry(request):
    """Isolate each test's registry, then let a test ask for the shipped providers back.

    `rediscover` is applied inside this fixture rather than as a separate one so the
    ordering is explicit: clear first, repopulate second. Two independent fixtures would
    leave the result up to fixture teardown order.
    """
    saved = dict(registry_module._REGISTRY)
    registry_module._reset_for_tests()
    if "rediscover" in request.fixturenames:
        request.getfixturevalue("rediscover")()
    yield
    registry_module._reset_for_tests()
    registry_module._REGISTRY.update(saved)


@pytest.fixture()
def two_searchers():
    """Two search providers, one reliable and one opt-in, plus a lookup provider."""
    good = make(
        "good",
        "Good",
        [Capability.SEARCH, Capability.LOOKUP],
        reliability=90,
        search_results=[Paper(title="Good Paper", source="Good", doi="10.1/good", citations=10, year=2024)],
        lookup_result=Paper(title="Looked Up", source="Good", doi="10.1/x"),
    )
    optin = make(
        "optin",
        "OptIn",
        [Capability.SEARCH],
        reliability=50,  # below the auto-search threshold, so it stays opt-in
        search_results=[Paper(title="OptIn Paper", source="OptIn", doi="10.1/optin")],
    )
    registry_module._register_for_tests(good)
    registry_module._register_for_tests(optin)
    return good, optin


# --------------------------------------------------------------------------- search_papers

async def test_search_auto_fans_out_over_auto_providers(two_searchers):
    good, optin = two_searchers
    out = await server.search_papers(query="rag")
    assert "Good Paper" in out
    assert good.calls, "the auto provider must be queried"
    assert not optin.calls, "a low-reliability provider must stay out of auto-search"


async def test_search_naming_a_source_queries_only_that_one(two_searchers):
    good, optin = two_searchers
    out = await server.search_papers(query="rag", source="optin")
    assert "OptIn Paper" in out
    assert not good.calls
    assert [c[0] for c in optin.calls] == ["search"]


async def test_search_rejects_an_unknown_source(two_searchers):
    assert "unknown source" in await server.search_papers(query="rag", source="nope")


async def test_search_requires_a_query(two_searchers):
    assert "Provide a search query" in await server.search_papers(query="  ")


async def test_search_passes_from_year_through(two_searchers):
    good, _ = two_searchers
    await server.search_papers(query="rag", source="good", from_year=2021)
    assert good.calls[0][3] == 2021


async def test_search_survives_one_provider_failing():
    ok = make("ok", "Ok", [Capability.SEARCH], reliability=90, search_results=[Paper(title="Good Paper", source="Ok", doi="10.1/ok")])
    bad = make("bad", "Bad", [Capability.SEARCH], reliability=80, boom=True)
    registry_module._register_for_tests(ok)
    registry_module._register_for_tests(bad)
    out = await server.search_papers(query="rag")
    assert "Good Paper" in out, "a failing provider must not empty the whole result"
    assert "simulated outage" in out


async def test_search_reports_when_nothing_matches():
    empty = make("empty", "Empty", [Capability.SEARCH], reliability=90)
    registry_module._register_for_tests(empty)
    assert "No papers matched" in await server.search_papers(query="zzzz", source="empty")


async def test_open_access_filter_keeps_only_free_papers():
    mixed = make(
        "mixed",
        "Mixed",
        [Capability.SEARCH],
        reliability=90,
        search_results=[
            Paper(title="Paywalled", source="Mixed", doi="10.1/closed"),
            Paper(title="Free", source="Mixed", doi="10.1/free", is_open_access=True),
        ],
    )
    registry_module._register_for_tests(mixed)
    out = await server.search_papers(query="x", source="mixed", open_access_only=True)
    assert "Free" in out
    assert "Paywalled" not in out


async def test_max_results_is_clamped_not_rejected(two_searchers):
    good, _ = two_searchers
    await server.search_papers(query="rag", source="good", max_results=10_000)
    assert good.calls[0][2] <= 50


# --------------------------------------------------------------------------- get_paper

async def test_get_paper_falls_through_to_the_next_provider():
    miss = make("miss", "Miss", [Capability.LOOKUP], reliability=95, lookup_result=None)
    hit = make("hit", "Hit", [Capability.LOOKUP], reliability=60, lookup_result=Paper(title="Found It", source="Hit", doi="10.1/x"))
    registry_module._register_for_tests(miss)
    registry_module._register_for_tests(hit)
    out = await server.get_paper("10.1/x")
    assert "Found It" in out


async def test_get_paper_explains_itself_when_everything_misses():
    miss = make("miss", "Miss", [Capability.LOOKUP], reliability=95, lookup_result=None)
    registry_module._register_for_tests(miss)
    out = await server.get_paper("10.1/nope")
    assert "Nothing found" in out


async def test_get_paper_needs_an_identifier():
    assert "Provide a DOI" in await server.get_paper("  ")


@pytest.mark.parametrize(
    ("identifier", "expected_first"),
    [
        ("PMID:38012345", "europe-pmc"),
        ("PMC10650234", "europe-pmc"),
        ("10.1145/123", "openalex"),
        ("2401.12345", "arxiv"),
        ("W4386918332", "openalex"),
    ],
)
def test_lookup_order_routes_by_identifier_shape(identifier, expected_first, rediscover):
    """Shape routing avoids four wasted round trips for an obvious identifier."""
    order = [p.meta.name for p in server._lookup_order(identifier)]
    assert order, f"no lookup-capable provider for {identifier}"
    assert order[0] == expected_first, order


def test_lookup_order_appends_every_lookup_capable_provider():
    """A newly added provider must never end up unreachable just because it is not in the
    hand-written routing table."""
    extra = make("newcomer", "Newcomer", [Capability.LOOKUP], reliability=70)
    registry_module._register_for_tests(extra)
    names = [p.meta.name for p in server._lookup_order("10.1/x")]
    assert "newcomer" in names


# --------------------------------------------------------------------------- get_citations

async def test_get_citations_rejects_a_non_doi():
    assert "does not look like a DOI" in await server.get_citations("not-a-doi")


async def test_get_citations_uses_a_graph_capable_provider():
    graph = make(
        "graph",
        "Graph",
        [Capability.CITATIONS],
        reliability=90,
        citations=[Paper(title="Citing Work", source="Graph", doi="10.1/c")],
    )
    registry_module._register_for_tests(graph)
    out = await server.get_citations("10.1/x", max_results=5)
    assert "Citing Work" in out
    assert "Papers citing" in out
    assert graph.calls[0][2] == "cited-by"


async def test_get_citations_explains_when_no_provider_can_do_it():
    only_search = make("search-only", "SearchOnly", [Capability.SEARCH], reliability=90)
    registry_module._register_for_tests(only_search)
    assert "No enabled source can serve a citation graph" in await server.get_citations("10.1/x")


# --------------------------------------------------------------------------- find_similar

async def test_find_similar_prefers_native_relatedness_and_says_so():
    graph = make(
        "graph",
        "Graph",
        [Capability.SIMILAR],
        reliability=90,
        similar=[Paper(title="Related Paper", source="Graph", doi="10.1/r")],
    )
    lookup = make("lookup", "Lookup", [Capability.LOOKUP], reliability=95, lookup_result=Paper(title="Seed", source="Lookup", doi="10.1/x"))
    registry_module._register_for_tests(graph)
    registry_module._register_for_tests(lookup)

    out = await server.find_similar(identifier="10.1/x")
    assert "Related Paper" in out
    assert "Relatedness computed by the source" in out


async def test_find_similar_falls_back_to_keywords_and_labels_it_weaker():
    """The distinction matters: 'papers this source relates' is a stronger claim than
    'papers with similar words', so the output must not blur them."""
    seed = Paper(title="Graph RAG for QA", source="Lookup", doi="10.1/x")
    lookup = make("lookup", "Lookup", [Capability.LOOKUP], reliability=95, lookup_result=seed)
    searcher = make(
        "searcher",
        "Searcher",
        [Capability.SEARCH],
        reliability=90,
        search_results=[Paper(title="Keyword Match", source="Searcher", doi="10.1/k")],
    )
    registry_module._register_for_tests(lookup)
    registry_module._register_for_tests(searcher)

    out = await server.find_similar(identifier="10.1/x")
    assert "Keyword Match" in out
    assert "weaker signal" in out


async def test_find_similar_requires_a_seed():
    assert "Provide either an identifier or a title" in await server.find_similar()


async def test_find_similar_reports_an_unresolvable_seed():
    miss = make("miss", "Miss", [Capability.LOOKUP], reliability=95, lookup_result=None)
    registry_module._register_for_tests(miss)
    assert "Could not resolve the seed" in await server.find_similar(identifier="10.1/nope")


# --------------------------------------------------------------------------- find_fulltext / read_fulltext

async def test_find_fulltext_renders_a_resolver_result():
    resolver = make(
        "resolver",
        "Resolver",
        [Capability.FULLTEXT_LINK],
        reliability=90,
        link={
            "ok": True,
            "title": "Free Paper",
            "journal": "OA Journal",
            "year": 2023,
            "oa_status": "gold",
            "best": {"pdf_url": "http://x/free.pdf", "version": "publishedVersion", "license": "cc-by"},
            "locations": [],
        },
    )
    registry_module._register_for_tests(resolver)
    out = await server.find_fulltext("10.1/x")
    assert "http://x/free.pdf" in out
    assert "Free Paper" in out
    assert "download_pdf" in out, "it should point at the follow-up tool"


async def test_find_fulltext_surfaces_a_missing_mailto():
    resolver = make(
        "resolver",
        "Resolver",
        [Capability.FULLTEXT_LINK],
        reliability=90,
        link={"ok": False, "reason": "Set RESEARCH_MCP_MAILTO in the server environment."},
    )
    registry_module._register_for_tests(resolver)
    assert "RESEARCH_MCP_MAILTO" in await server.find_fulltext("10.1/x")


async def test_read_fulltext_renders_sections():
    reader = make(
        "reader",
        "Reader",
        [Capability.FULLTEXT_TEXT],
        reliability=90,
        text={"ok": True, "pmcid": "PMC1", "text": {"abstract": "Abstract text.", "methods": "Method text."}},
    )
    registry_module._register_for_tests(reader)
    out = await server.read_fulltext("PMC1")
    assert "## abstract" in out
    assert "Method text." in out


async def test_read_fulltext_clamps_max_chars():
    reader = make(
        "reader",
        "Reader",
        [Capability.FULLTEXT_TEXT],
        reliability=90,
        text={"ok": True, "pmcid": "PMC1", "text": {"body": "x" * 100_000}},
    )
    registry_module._register_for_tests(reader)
    out = await server.read_fulltext("PMC1", max_chars=10_000)
    assert len(out) < 12_000


async def test_read_fulltext_handles_a_closed_article():
    reader = make(
        "reader",
        "Reader",
        [Capability.FULLTEXT_TEXT],
        reliability=90,
        text={"ok": False, "reason": "no open-access full text"},
    )
    registry_module._register_for_tests(reader)
    assert "no open-access full text" in await server.read_fulltext("123")


# --------------------------------------------------------------------------- download_pdf

async def test_download_pdf_uses_the_records_own_link_first(monkeypatch):
    paper = Paper(title="A Paper", source="Good", authors=["Ada Lovelace"], year=2024, doi="10.1/x", pdf_url="http://x/paper.pdf")
    lookup = make("good", "Good", [Capability.LOOKUP], reliability=90, lookup_result=paper)
    registry_module._register_for_tests(lookup)

    async def fake_fetch(url, stem, source=""):
        return {"ok": True, "path": f"/tmp/{stem}.pdf", "bytes": 2048, "url": url, "source": source}

    monkeypatch.setattr(server.download, "fetch_pdf", fake_fetch)
    out = await server.download_pdf(identifier="10.1/x")
    assert "Saved" in out
    assert "/tmp/Lovelace-2024-A-Paper.pdf" in out
    assert "http://x/paper.pdf" in out


async def test_download_pdf_accepts_a_bare_url(monkeypatch):
    async def fake_fetch(url, stem, source=""):
        return {"ok": True, "path": f"/tmp/{stem}.pdf", "bytes": 10, "url": url, "source": source}

    monkeypatch.setattr(server.download, "fetch_pdf", fake_fetch)
    out = await server.download_pdf(url="http://x/direct.pdf")
    assert "Saved" in out


async def test_download_pdf_reports_every_failed_attempt(monkeypatch):
    paper = Paper(title="A Paper", source="Good", doi="10.1/x", pdf_url="http://x/bad.pdf")
    lookup = make("good", "Good", [Capability.LOOKUP], reliability=90, lookup_result=paper)
    registry_module._register_for_tests(lookup)

    async def fake_fetch(url, stem, source=""):
        return {"ok": False, "reason": "not a PDF"}

    monkeypatch.setattr(server.download, "fetch_pdf", fake_fetch)
    out = await server.download_pdf(identifier="10.1/x")
    assert "Could not download" in out
    assert "not a PDF" in out


async def test_download_pdf_asks_for_a_target_when_given_nothing():
    assert "Provide a paper identifier" in await server.download_pdf()


async def test_download_pdf_tries_a_resolver_when_the_record_has_no_link(monkeypatch):
    paper = Paper(title="A Paper", source="Good", doi="10.1/x")  # no pdf_url
    lookup = make("good", "Good", [Capability.LOOKUP], reliability=90, lookup_result=paper)
    resolver = make(
        "resolver",
        "Resolver",
        [Capability.FULLTEXT_LINK],
        reliability=90,
        link={"ok": True, "best": {"pdf_url": "http://resolved/x.pdf"}},
    )
    registry_module._register_for_tests(lookup)
    registry_module._register_for_tests(resolver)

    seen = {}

    async def fake_fetch(url, stem, source=""):
        seen["url"] = url
        return {"ok": True, "path": "/tmp/x.pdf", "bytes": 5, "url": url, "source": source}

    monkeypatch.setattr(server.download, "fetch_pdf", fake_fetch)
    out = await server.download_pdf(identifier="10.1/x")
    assert "Saved" in out
    assert seen["url"] == "http://resolved/x.pdf"


# --------------------------------------------------------------------------- list_sources

async def test_list_sources_separates_auto_from_opt_in():
    auto = make("auto-one", "AutoOne", [Capability.SEARCH], reliability=90)
    optin = make("optin", "OptIn", [Capability.SEARCH], reliability=10)
    registry_module._register_for_tests(auto)
    registry_module._register_for_tests(optin)
    out = await server.list_sources()
    assert "auto-one" in out
    assert "not searched by default" in out
    assert "optin" in out


async def test_list_sources_shows_deprecation_reasons(monkeypatch):
    monkeypatch.setenv("RESEARCH_MCP_MAILTO", "")
    academic_config.reload_config()
    gone = make("gone", "Gone", [Capability.SEARCH], reliability=90, deprecated="free tier withdrawn")
    registry_module._register_for_tests(gone)
    out = await server.list_sources()
    assert "free tier withdrawn" in out


async def test_list_sources_warns_about_a_missing_mailto(monkeypatch):
    monkeypatch.setenv("RESEARCH_MCP_MAILTO", "")
    academic_config.reload_config()
    registry_module._register_for_tests(make("p", "P", [Capability.SEARCH], reliability=90))
    assert "RESEARCH_MCP_MAILTO" in await server.list_sources()


async def test_list_sources_mentions_mailto_once_configured(monkeypatch):
    monkeypatch.setenv("RESEARCH_MCP_MAILTO", "researcher@example.org")
    academic_config.reload_config()
    registry_module._register_for_tests(make("p", "P", [Capability.SEARCH], reliability=90))
    assert "researcher@example.org" in await server.list_sources()


async def test_list_sources_handles_an_empty_registry():
    assert "No providers are enabled" in await server.list_sources()


# --------------------------------------------------------------------------- search_across

async def test_search_across_dedupes_the_same_paper_from_two_providers():
    a = make("a", "A", [Capability.SEARCH], reliability=90, search_results=[Paper(title="One Paper", source="A", doi="10.1/same")])
    b = make("b", "B", [Capability.SEARCH], reliability=80, search_results=[Paper(title="One Paper", source="B", doi="10.1/same")])
    registry_module._register_for_tests(a)
    registry_module._register_for_tests(b)
    papers, _, _ = await server.search_across("x", None, 10, None)
    assert len(papers) == 1


async def test_search_across_reports_omitted_count():
    many = make(
        "many",
        "Many",
        [Capability.SEARCH],
        reliability=90,
        search_results=[Paper(title=f"Paper {i}", source="Many", doi=f"10.1/{i}") for i in range(10)],
    )
    registry_module._register_for_tests(many)
    papers, _, omitted = await server.search_across("x", ["many"], 3, None)
    assert len(papers) == 3
    assert omitted == 0, "the provider only returned 3 records, so nothing was cut"


async def test_search_across_reports_an_unknown_source():
    registry_module._register_for_tests(make("p", "P", [Capability.SEARCH], reliability=90))
    _, errors, _ = await server.search_across("x", ["ghost"], 5, None)
    assert any("ghost" in e for e in errors)


async def test_search_across_excludes_non_search_providers():
    registry_module._register_for_tests(make("resolver", "Resolver", [Capability.FULLTEXT_LINK], reliability=99))
    papers, errors, _ = await server.search_across("x", None, 5, None)
    assert papers == []
    assert errors, "it should say why nothing was searched"
