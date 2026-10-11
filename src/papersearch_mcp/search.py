"""Multi-source search: fan out over providers, merge the duplicates.

Its own module rather than a function in `server` so that `similar` can use it without
importing the server (which owns the tools and would close an import cycle).
"""

from __future__ import annotations

import asyncio

from . import dedupe
from .models import Paper
from .providers import Capability, auto_search_providers, resolve_many


async def search_across(
    query: str,
    sources: list[str] | None,
    limit: int,
    from_year: int | None,
) -> tuple[list[Paper], list[str], int]:
    """Search several providers and merge the results.

    Returns (papers, failures, omitted_count). A provider that fails contributes its error and
    nothing else: the caller still gets whatever the others returned, because one dead source
    is not a reason to report "no results".
    """
    if sources:
        providers, missing = resolve_many(sources)
        errors = [f"unknown source {name!r}" for name in missing]
    else:
        providers = auto_search_providers()
        errors = []

    providers = [p for p in providers if p.has(Capability.SEARCH)]
    if not providers:
        return [], errors or ["No search-capable provider is available."], 0

    # Ask each for more than requested, since deduplication will shrink the set.
    per_provider = limit if len(providers) == 1 else max(4, (limit * 2) // len(providers) + 2)

    results = await asyncio.gather(
        *(p.search(query, per_provider, from_year) for p in providers),
        return_exceptions=True,
    )

    papers: list[Paper] = []
    # strict=True: both lists are built from the same provider list, so a length mismatch
    # would mean a result was silently attributed to the wrong source.
    for provider, result in zip(providers, results, strict=True):
        if isinstance(result, BaseException):
            errors.append(f"{provider.meta.label}: {result}")
            continue
        papers.extend(result)

    # Highest-reliability source first, so its richer metadata leads the merged record.
    rank = {p.meta.label: -p.meta.reliability for p in providers}
    papers.sort(key=lambda paper: rank.get(paper.source, 0))
    merged = dedupe.dedupe(papers)
    return merged[:limit], errors, max(0, len(merged) - limit)
