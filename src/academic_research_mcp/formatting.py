"""Render Paper records into text an LLM can read and cite.

Output is deliberately plain text, not JSON: the model copies identifiers from it into
citations, and a compact numbered block is far cheaper to read than nested objects.
"""

from __future__ import annotations

from typing import Any

from . import download, http
from .models import Paper


def _authors(paper: Paper, limit: int = 4) -> str:
    if not paper.authors:
        return "unknown authors"
    if len(paper.authors) <= limit:
        return ", ".join(paper.authors)
    return f"{', '.join(paper.authors[:limit])} et al."


def _identifiers(paper: Paper) -> str:
    parts = []
    if paper.doi:
        parts.append(f"doi:{paper.doi}")
    if paper.source_id and not (paper.doi and paper.source_id.endswith(paper.doi)):
        parts.append(f"id:{paper.source_id}")
    extras = paper.extra or {}
    for key in ("arxiv_id", "pmid", "pmcid"):
        if extras.get(key):
            parts.append(f"{key}:{extras[key]}")
    return " · ".join(parts)


def format_paper(index: int, paper: Paper, *, abstract_chars: int = 0) -> str:
    """One numbered entry. `abstract_chars=0` omits the abstract (list view)."""
    header = f"[{index}] {paper.title}"
    meta_bits = [str(paper.year) if paper.year else "n.d.", _authors(paper)]
    if paper.venue:
        meta_bits.append(paper.venue)
    lines = [header, "    " + " · ".join(meta_bits)]

    stats = [f"source {paper.source}"]
    if paper.citations is not None:
        stats.append(f"{paper.citations} citations")
    if paper.is_open_access:
        stats.append("open access")
    if paper.publication_type:
        stats.append(paper.publication_type)
    lines.append("    " + " · ".join(stats))

    if paper.url:
        lines.append(f"    url: {paper.url}")
    if paper.pdf_url:
        lines.append(f"    pdf: {paper.pdf_url}")
    ids = _identifiers(paper)
    if ids:
        lines.append(f"    {ids}")

    if abstract_chars and paper.abstract:
        text = paper.abstract if len(paper.abstract) <= abstract_chars else paper.abstract[:abstract_chars].rstrip() + "…"
        lines.append(f"    abstract: {text}")

    also = (paper.extra or {}).get("also_in")
    if also:
        lines.append(f"    also indexed by: {', '.join(also)}")

    return "\n".join(lines)


def format_results(papers: list[Paper], *, abstract_chars: int = 300, omitted: int = 0) -> str:
    if not papers:
        return "No papers matched. Try broader keywords, drop from_year, or add another source."
    blocks = [format_paper(i, p, abstract_chars=abstract_chars) for i, p in enumerate(papers, 1)]
    tail = f"\n\n({omitted} further result(s) not shown — raise max_results to see them.)" if omitted > 0 else ""
    return "\n\n".join(blocks) + tail


def format_paper_detail(paper: Paper) -> str:
    return format_paper(1, paper, abstract_chars=4000).replace("[1] ", "", 1)


def format_source_list(sources: list[tuple[str, str]]) -> str:
    lines = ["Available sources (all keyless):", ""]
    for name, note in sources:
        lines.append(f"  {name:<18} {note}")
    lines.append("")
    lines.append("Use source=\"auto\" (default) to fan out across several and merge duplicates.")
    return "\n".join(lines)


def format_sources(providers: list[Any]) -> str:
    """Render the live provider registry: what exists, what it does, whether it works."""
    from .providers import Capability

    if not providers:
        return "No providers are enabled. Add a module under providers/ or enable one."

    lines = ["Searched by default (source=\"auto\"):", ""]
    auto = [p for p in providers if p.meta.included_in_auto()]
    opt_in = [p for p in providers if not p.meta.included_in_auto() and p.meta.deprecated is None]
    deprecated = [p for p in providers if p.meta.deprecated]

    def describe(provider: Any) -> str:
        caps = ", ".join(sorted(c.value for c in provider.meta.capabilities if c is not Capability.SEARCH))
        tail = f"  [also: {caps}]" if caps else ""
        return f"  {provider.meta.name:<18} {provider.meta.summary}{tail}"

    for provider in auto:
        lines.append(describe(provider))

    if opt_in:
        lines.append("")
        lines.append("Available but not searched by default (name them explicitly):")
        for provider in opt_in:
            lines.append(describe(provider))

    if deprecated:
        lines.append("")
        lines.append("Disabled:")
        for provider in deprecated:
            lines.append(f"  {provider.meta.name:<18} {provider.meta.deprecated}")

    lines.append("")
    if http.mailto():
        lines.append(f"Contact address in use: {http.mailto()} (polite pools, Unpaywall).")
    else:
        lines.append(
            "No contact address set. Searches still work; find_fulltext/download_pdf need one — "
            "set RESEARCH_MCP_MAILTO in the server environment."
        )
    lines.append("No API keys are required by any source, and none are read.")

    storage = download.storage_dir()
    lines.append(f"Downloads are saved to: {storage}")
    return "\n".join(lines)
