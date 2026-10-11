"""papersearch-mcp — a keyless MCP server for academic literature.

Architecture: two layers, which is the shape that survives upstream churn.

  Layer 1 (this file)      Unified tools. Each asks the registry for providers that
                           declare the capability it needs. No provider is named here.
  Layer 2 (providers/)     One module per source, each declaring what it can do.

The consequence is that adding a source means adding a file, and removing one means
deleting it (or setting `enabled=False` when you want to keep the code). Nothing in this
file, and nothing in the tools, refers to a source by name — so neither needs editing.

Tools
  search_papers     multi-source search, deduplicated
  get_paper         the full record for one identifier
  find_similar      papers related to a given one
  get_citations     who cites a paper, and what it cites
  find_fulltext     where to read the paper for free
  read_fulltext     section text of an open-access article
  download_pdf      save a paper's PDF to local storage
  list_sources      what is available, and what each source can do
"""

from __future__ import annotations

import os
import re
from typing import Literal

from mcp.server.mcpserver import MCPServer

from . import download, formatting, similar
from .models import Paper
from .providers import Capability, Provider, resolve_many, with_capability
from .search import search_across

mcp = MCPServer(
    "papersearch",
    instructions=(
        "Search and read academic literature without any API key. "
        "Start with search_papers (source=\"auto\" searches several sources and merges duplicates), "
        "then get_paper for full metadata, find_similar for related work, find_fulltext for a free copy, "
        "download_pdf to save a PDF, or read_fulltext for open-access body text. "
        "Every result carries a DOI or a stable id: cite it rather than paraphrasing without attribution."
    ),
)

SourceArg = str  # a provider slug, alias, or "auto"; validated against the registry at call time


def _clamp(value: int | None, default: int, ceiling: int) -> int:
    if not value:
        return default
    return max(1, min(int(value), ceiling))


# ---------------------------------------------------------------------------- layer 1 core


async def _load_paper(identifier: str, source: str) -> tuple[Paper | None, str, list[str]]:
    """Resolve an identifier to one Paper, trying providers in a sensible order."""
    if source and source != "auto":
        providers, missing = resolve_many([source])
        if missing:
            return None, "", [f"unknown source {source!r}"]
        candidates = [p for p in providers if p.has(Capability.LOOKUP)]
    else:
        candidates = _lookup_order(identifier)
        missing = []

    errors: list[str] = []
    for provider in candidates:
        try:
            paper = await provider.lookup(identifier)
        except Exception as exc:
            errors.append(f"{provider.meta.label}: {exc}")
            continue
        if paper:
            return paper, provider.meta.label, errors
    return None, "", errors + [f"unknown source {name!r}" for name in missing]


def _lookup_order(identifier: str) -> list[Provider]:
    """Route by identifier shape, then by everything that can look up at all.

    Shape routing avoids four wasted round trips for an obvious PMID, and keeps the common
    DOI path fast.
    """
    raw = identifier.strip().lower()
    if raw.startswith(("pmid:", "pmcid:", "pmc")):
        order = ["europe-pmc", "openalex"]
    elif raw.startswith("w") and raw[1:].isdigit():
        order = ["openalex", "crossref"]
    elif raw.startswith("arxiv:") or "/abs/" in raw or "/pdf/" in raw or _bare_arxiv_id(raw):
        order = ["arxiv", "semantic-scholar"]
    elif raw.startswith("10.") or "doi.org/" in raw:
        order = ["openalex", "crossref", "semantic-scholar", "europe-pmc"]
    else:
        order = ["openalex", "semantic-scholar", "crossref"]

    chosen, _ = resolve_many(order)
    lookups = [p for p in chosen if p.has(Capability.LOOKUP)]
    # Append anything else that can look up, so a new provider is never unreachable.
    for provider in with_capability(Capability.LOOKUP):
        if provider not in lookups:
            lookups.append(provider)
    return lookups


def _bare_arxiv_id(raw: str) -> bool:
    """True only for arXiv's own id shape, e.g. 2401.12345 or 2401.12345v2.

    The digits before the dot are a YYMM stamp, so the pattern is anchored to four digits.
    A looser check misroutes DOIs: "10.1145/123" would otherwise look like an arXiv id and
    send a DOI lookup to arXiv.
    """
    return re.fullmatch(r"(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?", raw) is not None


# ---------------------------------------------------------------------------- tools


@mcp.tool()
async def search_papers(
    query: str,
    source: str = "auto",
    max_results: int = 10,
    from_year: int | None = None,
    open_access_only: bool = False,
) -> str:
    """Search scholarly papers. No API key needed.

    Args:
        query: Keywords, a title, or an author. e.g. "graph retrieval augmented generation".
        source: "auto" (default) searches several sources and merges duplicates. Or name one:
            openalex, arxiv, crossref, europe-pmc, doaj, semantic-scholar. Call list_sources
            for the current set — it depends on which providers are enabled.
        max_results: How many papers to return (1-50, default 10).
        from_year: Only papers published in this year or later, e.g. 2022.
        open_access_only: Keep only papers with a free-to-read copy.

    Returns one numbered entry per paper with title, authors, year, venue, citation count,
    DOI, links and a short abstract. Cite the DOI or URL of anything you use.
    """
    query = (query or "").strip()
    if not query:
        return "Provide a search query."

    limit = _clamp(max_results, 10, 50)
    sources = None if source in ("auto", "", None) else [source]

    papers, errors, omitted = await search_across(query, sources, limit, from_year)

    if open_access_only:
        papers = [p for p in papers if p.is_open_access or p.pdf_url]
        if not papers:
            return "No open-access papers matched. Retry with open_access_only=false."

    label = "auto" if sources is None else source
    header = f"Query: {query!r} · sources: {label}"
    if errors:
        header += "\nSome sources failed (results from the rest are still shown):\n  " + "\n  ".join(errors)
    return f"{header}\n\n{formatting.format_results(papers, abstract_chars=320, omitted=omitted)}"


@mcp.tool()
async def get_paper(identifier: str, source: str = "auto") -> str:
    """Fetch the full record for one paper by DOI, arXiv id, PMID, PMCID, or OpenAlex id.

    Args:
        identifier: e.g. "10.1145/3539618.3592000", "2401.12345", "PMID:38012345",
            "PMC10650234", or "W4386918332".
        source: Which source to ask. "auto" (default) picks from the identifier's shape.

    Use this after a search, when you need the complete author list, abstract, or DOI.
    """
    identifier = (identifier or "").strip()
    if not identifier:
        return "Provide a DOI, arXiv id, PMID, PMCID, or OpenAlex id."

    paper, via, errors = await _load_paper(identifier, source)
    if paper:
        note = f"Found via {via}:\n\n" if via else ""
        return note + formatting.format_paper_detail(paper)

    detail = f"Nothing found for {identifier!r}."
    if errors:
        detail += "\nTried:\n  " + "\n  ".join(errors)
    detail += "\nIf this is an arXiv preprint try its id without a URL; for a DOI include the 10. prefix."
    return detail


@mcp.tool()
async def find_similar(
    identifier: str = "",
    title: str = "",
    source: str = "auto",
    max_results: int = 10,
) -> str:
    """Find papers related to a given paper — "more like this".

    Args:
        identifier: DOI, arXiv id, PMID or OpenAlex id of the seed paper.
        title: Alternative to identifier — a paper title to use as the seed.
        source: Which similarity source to prefer. "auto" uses the best available.
        max_results: 1-50, default 10.

    Prefers each source's own computed relatedness (OpenAlex related works, Semantic
    Scholar recommendations, Europe PMC citation neighbours) and says so. Falls back to a
    keyword search on the seed's title when no source can compute a relation, and labels
    that result as weaker. Use it to map a field, find a paper's descendants, or locate
    the seminal work behind a result.
    """
    limit = _clamp(max_results, 10, 50)

    seed: Paper | None = None
    if (identifier or "").strip():
        seed, _, errors = await _load_paper(identifier.strip(), source)
        if seed is None:
            return f"Could not resolve the seed paper {identifier!r}." + ("\nTried:\n  " + "\n  ".join(errors) if errors else "")
    elif (title or "").strip():
        papers, _, _ = await search_across(title.strip(), None, 1, None)
        if not papers:
            return f"No paper found for the seed title {title!r}."
        seed = papers[0]
    else:
        return "Provide either an identifier or a title for the seed paper."

    found, errors, method = await similar.find_similar([seed], limit)

    header = f"Similar to: {seed.title} ({seed.year or 'n.d.'})"
    if method == "native":
        header += "\nRelatedness computed by the source (shared references/concepts), not keyword matching."
    elif method == "keywords":
        header += "\nNo source could compute relatedness for this paper; these are keyword matches on its title — a weaker signal."
    else:
        header += "\nNo related papers found."
    if errors:
        header += "\nSome sources failed:\n  " + "\n  ".join(errors)

    return f"{header}\n\n{formatting.format_results(found, abstract_chars=0)}" if found else f"{header}\n\nNone found."


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

    Uses whichever enabled source can serve a citation graph (OpenAlex). Coverage depends
    on the paper being indexed with references.
    """
    clean = (doi or "").strip().removeprefix("https://doi.org/").removeprefix("doi:")
    if not clean.startswith("10."):
        return f"{doi!r} does not look like a DOI (expected something like 10.1145/...)."

    limit = _clamp(max_results, 15, 50)
    providers = with_capability(Capability.CITATIONS)
    if not providers:
        return "No enabled source can serve a citation graph."

    errors: list[str] = []
    for provider in providers:
        try:
            papers = await provider.citations(clean, direction, limit)
        except Exception as exc:
            errors.append(f"{provider.meta.label}: {exc}")
            continue
        if papers:
            label = "Papers citing" if direction == "cited-by" else "References of"
            body = formatting.format_results(papers, abstract_chars=0)
            return f"{label} {clean} (showing {len(papers)}, via {provider.meta.label}):\n\n{body}"

    hint = "It may be too new, or not indexed with references." if direction == "cites" else "It may be too recent to have been cited yet."
    detail = f"No {direction.replace('-', ' ')} records found for {clean}. {hint}"
    if errors:
        detail += "\nSome sources failed:\n  " + "\n  ".join(errors)
    return detail


@mcp.tool()
async def find_fulltext(doi: str) -> str:
    """Find a free, legal, full-text copy of a paywalled paper by DOI.

    Args:
        doi: e.g. "10.1038/s41586-023-06221-2".

    Asks every enabled open-access resolver (Unpaywall) for a publisher- or
    repository-hosted copy. Needs a contact email: set PAPERSEARCH_MCP_MAILTO in the server
    environment (it is free — Unpaywall only asks who is calling). Returns the best link
    plus other locations, or a clear reason when no free copy exists.
    """
    providers = with_capability(Capability.FULLTEXT_LINK)
    if not providers:
        return "No enabled source can resolve open-access copies."

    errors: list[str] = []
    for provider in providers:
        try:
            result = await provider.fulltext_link((doi or "").strip())
        except Exception as exc:
            errors.append(f"{provider.meta.label}: {exc}")
            continue

        if not result.get("ok"):
            return f"No free copy found.\n{result.get('reason')}"

        lines = [
            f"Open access: {result.get('title')}",
            f"Journal: {result.get('journal') or 'unknown'} · year {result.get('year') or 'n.d.'} · status {result.get('oa_status')}",
            f"(via {provider.meta.label})",
            "",
        ]
        best = result.get("best") or {}
        if isinstance(best, dict) and (best.get("pdf_url") or best.get("url")):
            lines.append("Best copy:")
            if best.get("pdf_url"):
                lines.append(f"  pdf:  {best['pdf_url']}")
            if best.get("url") and best.get("url") != best.get("pdf_url"):
                lines.append(f"  page: {best['url']}")
            version = best.get("version") or "?"
            licence = best.get("license") or "?"
            host_type = best.get("host_type") or "?"
            lines.append(f"  version: {version} · licence: {licence} · host: {host_type}")
            lines.append("")

        others = [loc for loc in (result.get("locations") or []) if isinstance(loc, dict)]
        if others:
            lines.append("Other locations:")
            for loc in others:
                lines.append(f"  - {loc.get('pdf_url') or loc.get('url')}  ({loc.get('host_type') or '?'}/{loc.get('version') or '?'})")

        lines.append("")
        lines.append(f"To save it locally, call download_pdf with doi={doi!r}.")
        lines.append("Always check the licence before redistributing; reading is what this link is for.")
        return "\n".join(lines)

    return "No free copy found.\n" + ("\n".join(errors) if errors else "No resolver answered.")


@mcp.tool()
async def read_fulltext(identifier: str, max_chars: int = 20000) -> str:
    """Read the body text of an open-access article (abstract, sections).

    Args:
        identifier: A PMCID such as "PMC10650234", or a PMID such as "38012345".
        max_chars: Cap on returned characters (1000-60000, default 20000).

    Uses whichever enabled source exposes article text (Europe PMC). Returns a clear reason
    when a paper has no open-access text. Use it to quote or verify methods and results
    instead of guessing from an abstract, and respect the article's licence when reusing it.
    """
    limit = max(1000, min(int(max_chars or 20000), 60000))
    providers = with_capability(Capability.FULLTEXT_TEXT)
    if not providers:
        return "No enabled source can return article body text."

    errors: list[str] = []
    for provider in providers:
        try:
            result = await provider.fulltext_text((identifier or "").strip(), limit)
        except Exception as exc:
            errors.append(f"{provider.meta.label}: {exc}")
            continue

        if not result.get("ok"):
            return str(result.get("reason"))

        sections = result.get("text") or {}
        if not isinstance(sections, dict) or not sections:
            return f"{result.get('pmcid')}: no machine-readable sections available."

        chunks: list[str] = [f"Full text: {result.get('pmcid')} (open access, via {provider.meta.label})"]
        used = 0
        for heading, text in sections.items():
            if used >= limit:
                chunks.append("\n…(truncated — raise max_chars for more)")
                break
            allowance = min(len(text), limit - used)
            chunks.append(f"\n## {heading}\n{text[:allowance]}")
            used += allowance
        return "\n".join(chunks)

    return "Could not read full text.\n" + ("\n".join(errors) if errors else "No source answered.")


@mcp.tool()
async def download_pdf(
    identifier: str = "",
    doi: str = "",
    url: str = "",
    max_results: int = 3,
) -> str:
    """Save a paper's PDF to local storage and return the file path.

    Args:
        identifier: DOI, arXiv id, PMID or OpenAlex id of the paper.
        doi: Alternative to identifier — a bare DOI.
        url: An explicit PDF URL to try first, when you already have one.
        max_results: How many candidate locations to try before giving up (1-6, default 3).

    Tries, in order: the URL you passed, the record's own open-access link, then each
    enabled resolver. Files land in ~/.papersearch-mcp/papers (override with
    PAPERSEARCH_MCP_STORAGE) with a name derived from the paper, and an existing file is never
    overwritten. Only public http(s) addresses are fetched, and only real PDFs are kept.
    Check the licence before redistributing what you download.
    """
    attempts = _clamp(max_results, 3, 6)
    target_url = (url or "").strip()
    ref = (identifier or "").strip() or (doi or "").strip()
    paper: Paper | None = None
    notes: list[str] = []

    if ref:
        paper, _, errors = await _load_paper(ref, "auto")
        if paper is None:
            notes.extend(errors)
        if paper and not target_url:
            target_url = paper.pdf_url or ""

    if not target_url and not paper:
        return "Provide a paper identifier, a DOI, or a direct PDF URL." + ("\nTried:\n  " + "\n  ".join(notes) if notes else "")

    # A record with no PDF link still has a resolver, so keep going.
    candidates: list[tuple[str, str]] = []
    if (url or "").strip():
        candidates.append((url.strip(), "the URL you provided"))
    if paper and paper.pdf_url:
        candidates.append((paper.pdf_url, f"{paper.source}'s open-access link"))
    if paper and paper.doi:
        for provider in with_capability(Capability.FULLTEXT_LINK):
            resolved = await _resolve_pdf(provider, paper.doi)
            if resolved:
                candidates.append((resolved, f"{provider.meta.label}"))

    if not candidates:
        title = paper.title if paper else ref or url
        return f"No downloadable PDF found for {title}." + ("\nNotes:\n  " + "\n  ".join(notes) if notes else "")

    stem = download.safe_stem(paper) if paper else "download"
    seen: set[str] = set()
    for candidate_url, origin in candidates[:attempts]:
        if candidate_url in seen:
            continue
        seen.add(candidate_url)
        result = await download.fetch_pdf(candidate_url, stem, source=origin)
        if result.get("ok"):
            size_mb = result["bytes"] / (1024 * 1024)
            return (
                f"Saved: {result['path']}\n"
                f"{size_mb:.1f} MB from {result['url']}\n"
                f"via {origin}"
                + (f"\nPaper: {paper.title}" if paper else "")
                + "\n\nCheck the licence before redistributing this file."
            )
        notes.append(f"{origin}: {result.get('reason')}")

    return "Could not download a PDF.\nAttempts:\n  " + "\n  ".join(notes)


async def _resolve_pdf(provider: Provider, doi: str) -> str | None:
    """Ask a resolver provider for a direct PDF URL."""
    try:
        result = await provider.fulltext_link(doi)
    except Exception:
        return None
    if not result.get("ok"):
        return None
    best = result.get("best") or {}
    if isinstance(best, dict):
        if best.get("pdf_url"):
            return str(best["pdf_url"])
        if best.get("url"):
            return str(best["url"])
    for loc in result.get("locations") or []:
        if isinstance(loc, dict) and loc.get("pdf_url"):
            return str(loc["pdf_url"])
    return None


@mcp.tool()
async def list_sources() -> str:
    """List the literature sources available right now, what each can do, and what it costs.

    Call this when a search returns nothing useful, to pick a source better suited to the
    field (e.g. europe-pmc for medicine, arxiv for preprints, doaj for open-access only).
    The set depends on which providers are enabled, so read it rather than assuming.
    """
    return formatting.format_sources(with_capability(Capability.SEARCH, include_deprecated=True))


def main() -> None:
    """Entry point. Transport defaults to stdio, which is what MCP hosts spawn."""
    transport = os.environ.get("PAPERSEARCH_MCP_TRANSPORT", "stdio").strip().lower()
    if transport in ("http", "streamable-http"):
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
