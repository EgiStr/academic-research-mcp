"""academic-research-mcp — a keyless MCP server for academic literature search.

Every source this server talks to is free and requires no API key, so the server can be
handed to anyone (a colleague, CI, a public demo) with no credential setup. Each tool
returns prose rather than raw JSON: the calling model reads it and cites from it.

Tools
  search_papers     multi-source search, deduplicated
  get_paper         the full record for one identifier
  get_citations     who cites a paper, and what it cites
  find_fulltext     where to read the paper for free
  read_fulltext     section text of an open-access article
  list_sources      what is available, and how to query it
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from . import dedupe, formatting, fulltext, http, sources
from .models import Paper

# Server name/instructions are what the host shows the user when deciding to trust this.
mcp = MCPServer(
    "academic-research",
    instructions=(
        "Search and read academic literature without any API key. "
        "Start with search_papers (source=\"auto\" searches OpenAlex plus other keyless sources and merges duplicates), "
        "then get_paper for full metadata, find_fulltext for a free copy, or read_fulltext for open-access body text. "
        "Every result carries a DOI or a stable id: cite it rather than paraphrasing without attribution."
    ),
)

# Which sources "auto" fans out to. These four cover most disciplines and are the ones
# that stay reliable without a key. Semantic Scholar is deliberately excluded: keyless
# requests to it are frequently 429, which would add noise to every auto search — name it
# explicitly (source="semantic-scholar") when you want it.
AUTO_SOURCES = (sources.OPENALEX, sources.CROSSREF, sources.ARXIV, sources.EUROPE_PMC)

SourceArg = Literal["auto", "openalex", "arxiv", "semantic-scholar", "crossref", "europe-pmc", "doaj"]


def _max_results(value: int | None, default: int = 10, ceiling: int = 50) -> int:
    if not value:
        return default
    return max(1, min(int(value), ceiling))


async def _search_sources(
    query: str,
    chosen: list[str],
    limit: int,
    from_year: int | None,
) -> tuple[list[Paper], list[str], int]:
    """Fan out over sources, collecting failures instead of aborting the whole search."""
    # Each source is asked for a few more than requested, since dedupe will shrink the list.
    per_source = limit if len(chosen) == 1 else max(4, (limit * 2) // len(chosen) + 2)

    results = await asyncio.gather(
        *(sources.search_one(name, query, per_source, from_year) for name in chosen),
        return_exceptions=True,
    )

    papers: list[Paper] = []
    failures: list[str] = []
    for name, result in zip(chosen, results):
        if isinstance(result, BaseException):
            failures.append(f"{name}: {result}")
            continue
        papers.extend(result)

    # Preferred order, so the best source wins the merge and leads the output.
    papers.sort(key=lambda p: sources.DISPLAY_RANK.get(p.source, 99))

    merged = dedupe.dedupe(papers)
    omitted = max(0, len(merged) - limit)
    return merged[:limit], failures, omitted


@mcp.tool()
async def search_papers(
    query: str,
    source: SourceArg = "auto",
    max_results: int = 10,
    from_year: int | None = None,
    open_access_only: bool = False,
) -> str:
    """Search scholarly papers. No API key needed.

    Args:
        query: Keywords, a title, or an author. e.g. "graph retrieval augmented generation".
        source: "auto" (default) searches several sources and merges duplicates. Or name one:
            openalex, arxiv, semantic-scholar, crossref, europe-pmc, doaj.
        max_results: How many papers to return (1-50, default 10).
        from_year: Only papers published in this year or later, e.g. 2022.
        open_access_only: Keep only papers with a free-to-read copy.

    Returns one numbered entry per paper with title, authors, year, venue, citation count,
    DOI, links and a short abstract. Cite the DOI or URL of anything you use.
    """
    query = (query or "").strip()
    if not query:
        return "Provide a search query."

    limit = _max_results(max_results)
    if source == "auto":
        chosen = list(AUTO_SOURCES)
    elif source in sources.SEARCHERS:
        chosen = [source]
    else:
        return f"Unknown source {source!r}. Call list_sources to see the options."

    papers, failures, omitted = await _search_sources(query, chosen, limit, from_year)

    if open_access_only:
        papers = [p for p in papers if p.is_open_access or p.pdf_url]
        if not papers:
            return "No open-access papers matched. Retry with open_access_only=false."

    body = formatting.format_results(papers, abstract_chars=320, omitted=omitted)
    header = f"Query: {query!r} · sources: {', '.join(chosen)}"
    if failures:
        header += "\nSome sources failed (results from the rest are still shown):\n  " + "\n  ".join(failures)
    return f"{header}\n\n{body}"


@mcp.tool()
async def get_paper(identifier: str, source: SourceArg = "auto") -> str:
    """Fetch the full record for one paper by DOI, arXiv id, PMID, PMCID, or OpenAlex id.

    Args:
        identifier: e.g. "10.1145/3539618.3592000", "2401.12345", "PMID:38012345",
            "PMC10650234", or "W4386918332".
        source: Which source to ask first. "auto" picks one from the identifier's shape.

    Use this after a search, when you need the complete author list, abstract, or DOI.
    """
    identifier = (identifier or "").strip()
    if not identifier:
        return "Provide a DOI, arXiv id, PMID, PMCID, or OpenAlex id."

    candidates = _guess_lookup_order(identifier) if source == "auto" else [source]
    errors: list[str] = []

    for name in candidates:
        lookup = sources.LOOKUPS.get(name)
        if lookup is None:
            continue
        try:
            paper = await lookup(identifier)
        except http.SourceError as exc:
            errors.append(str(exc))
            continue
        if paper:
            return f"Found via {name}:\n\n{formatting.format_paper_detail(paper)}"

    detail = f"Nothing found for {identifier!r}. Tried: {', '.join(candidates)}."
    if errors:
        detail += "\nErrors:\n  " + "\n  ".join(errors)
    detail += "\nIf this is an arXiv preprint try its id without a URL; for a DOI include the 10. prefix."
    return detail


def _guess_lookup_order(identifier: str) -> list[str]:
    """Route by identifier shape before falling back to a broad sweep."""
    raw = identifier.strip().lower()
    if raw.startswith("pmid:") or raw.startswith("pmcid:") or raw.startswith("pmc"):
        return [sources.EUROPE_PMC, sources.OPENALEX]
    if raw.startswith("w") and raw[1:].isdigit():
        return [sources.OPENALEX, sources.CROSSREF]
    if raw.startswith("arxiv:") or "/abs/" in raw or "/pdf/" in raw:
        return [sources.ARXIV, sources.SEMANTIC_SCHOLAR]
    if raw.startswith("10.") or "doi.org/" in raw:
        return [sources.OPENALEX, sources.CROSSREF, sources.SEMANTIC_SCHOLAR, sources.EUROPE_PMC]
    # A bare token: arXiv ids look like 2401.12345, everything else goes to the broad sources.
    head = raw.split("/")[0]
    if "." in head and head.replace(".", "").isdigit() and len(head) < 20:
        return [sources.ARXIV, sources.SEMANTIC_SCHOLAR]
    return [sources.OPENALEX, sources.SEMANTIC_SCHOLAR, sources.CROSSREF]


@mcp.tool()
async def get_citations(
    doi: str,
    direction: Literal["cites", "cited-by"] = "cited-by",
    max_results: int = 15,
) -> str:
    """List the papers a work cites, or the papers that cite it.

    Args:
        doi: The DOI of the paper, e.g. "10.1145/3539618.3592000".
        direction: "cited-by" (default) returns papers published after it that cite it —
            the way to find newer work building on it. "cites" returns its reference list.
        max_results: 1-50, default 15.

    Backed by OpenAlex. Coverage depends on the paper being indexed with references.
    """
    clean = (doi or "").strip().removeprefix("https://doi.org/").removeprefix("doi:")
    if not clean.startswith("10."):
        return f"{doi!r} does not look like a DOI (expected something like 10.1145/...)."

    limit = _max_results(max_results, default=15)
    work_id = f"doi:{clean}"
    params: dict[str, Any] = {"per-page": limit, "select": "id,display_name,publication_year,doi,cited_by_count,authorships,primary_location,abstract_inverted_index"}
    if http.MAILTO:
        params["mailto"] = http.MAILTO

    field = "cited_by" if direction == "cited-by" else "cites"
    url = f"https://api.openalex.org/works?filter={field}:{work_id}"
    if direction == "cited-by":
        url += ",is_retracted:false"

    try:
        payload = await http.request_json(url, params, source="OpenAlex")
    except http.SourceError as exc:
        return f"Could not fetch citation data: {exc}"

    results = payload.get("results", [])
    if not results:
        hint = "It may be too new, or not indexed with references." if direction == "cites" else "It may be too recent to have been cited yet."
        return f"No {direction.replace('-', ' ')} records found for {clean}. {hint}"

    papers = [sources._openalex_paper(w) for w in results]
    label = "Papers citing" if direction == "cited-by" else "References of"
    return f"{label} {clean} (showing {len(papers)}):\n\n{formatting.format_results(papers, abstract_chars=0)}"


@mcp.tool()
async def find_fulltext(doi: str) -> str:
    """Find a free, legal, full-text copy of a paywalled paper by DOI.

    Args:
        doi: e.g. "10.1038/s41586-023-06221-2".

    Uses Unpaywall, which indexes publisher and repository copies. Needs a contact email:
    set RESEARCH_MCP_MAILTO in the server environment (it is free — Unpaywall only asks
    who is calling). Returns the best open-access link plus other locations, or a clear
    reason when no free copy exists.
    """
    try:
        result = await fulltext.unpaywall_lookup((doi or "").strip())
    except http.SourceError as exc:
        return f"Unpaywall lookup failed: {exc}"

    if not result.get("ok"):
        return f"No free copy found.\n{result.get('reason')}"

    lines = [
        f"Open access: {result.get('title')}",
        f"Journal: {result.get('journal') or 'unknown'} · year {result.get('year') or 'n.d.'} · status {result.get('oa_status')}",
        "",
    ]
    best = result.get("best") or {}
    if isinstance(best, dict) and (best.get("pdf_url") or best.get("url")):
        lines.append("Best copy:")
        if best.get("pdf_url"):
            lines.append(f"  pdf:  {best['pdf_url']}")
        if best.get("url") and best.get("url") != best.get("pdf_url"):
            lines.append(f"  page: {best['url']}")
        lines.append(f"  version: {best.get('version') or '?'} · licence: {best.get('license') or '?'} · host: {best.get('host_type') or '?'}")
        lines.append("")

    others = [loc for loc in (result.get("locations") or []) if isinstance(loc, dict)]
    if others:
        lines.append("Other locations:")
        for loc in others:
            target = loc.get("pdf_url") or loc.get("url")
            lines.append(f"  - {target}  ({loc.get('host_type') or '?'}/{loc.get('version') or '?'})")

    lines.append("")
    lines.append("Always check the licence before redistributing; reading is what this link is for.")
    return "\n".join(lines)


@mcp.tool()
async def read_fulltext(identifier: str, max_chars: int = 20000) -> str:
    """Read the body text of an open-access article (abstract, sections).

    Args:
        identifier: A PMCID such as "PMC10650234", or a PMID such as "38012345".
        max_chars: Cap on returned characters (1000-60000, default 20000).

    Only works for articles Europe PMC holds in open access; otherwise you get a clear
    reason. Use it to quote or verify methods and results instead of guessing from an
    abstract. Respect the article's licence when reusing text.
    """
    limit = max(1000, min(int(max_chars or 20000), 60000))
    try:
        result = await fulltext.europe_pmc_fulltext((identifier or "").strip())
    except http.SourceError as exc:
        return f"Could not read full text: {exc}"

    if not result.get("ok"):
        return str(result.get("reason"))

    sections = result.get("text") or {}
    if not isinstance(sections, dict) or not sections:
        return f"{result.get('pmcid')}: no machine-readable sections available."

    chunks: list[str] = [f"Full text: {result.get('pmcid')} (open access)"]
    used = 0
    for heading, text in sections.items():
        if used >= limit:
            chunks.append("\n…(truncated — raise max_chars for more)")
            break
        allowance = min(len(text), limit - used)
        chunks.append(f"\n## {heading}\n{text[:allowance]}")
        used += allowance
    return "\n".join(chunks)


@mcp.tool()
async def list_sources() -> str:
    """List the literature sources this server can search, and which need no API key.

    Call this when a search returns nothing useful, to pick a source better suited to the
    field (e.g. europe-pmc for medicine, arxiv for preprints, doaj for open-access only).
    """
    notes = [(name, sources.SOURCE_NOTES[name]) for name in sources.SOURCE_NAMES]
    body = formatting.format_source_list(notes)

    status = [
        "",
        f"Default multi-source set: {', '.join(AUTO_SOURCES)}",
        f"DOI lookup supported by: {', '.join(sources.DOI_SOURCES)}",
        "",
    ]
    if http.MAILTO:
        status.append(f"Contact address in use: {http.MAILTO} (OpenAlex/Crossref polite pool, Unpaywall).")
    else:
        status.append(
            "No contact address set. Searches still work; find_fulltext needs one — "
            "set RESEARCH_MCP_MAILTO in the server environment."
        )
    status.append("No API keys are required by any source, and none are read.")
    return body + "\n".join(status)


def main() -> None:
    """Entry point. Transport defaults to stdio, which is what MCP hosts spawn."""
    transport = os.environ.get("RESEARCH_MCP_TRANSPORT", "stdio").strip().lower()
    if transport in ("http", "streamable-http"):
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
