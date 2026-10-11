"""Similarity search: find papers like a given one.

Two kinds of signal are available, and they are not the same thing:

- **Native relatedness** — OpenAlex `related_works`, Semantic Scholar `recommendations`,
  Europe PMC citation neighbours. These are computed by the source from shared references
  and concepts, so they find real neighbours that a keyword search would miss.
- **Keyword fallback** — search on the paper's own title and top keywords. Used only when
  no provider offers a native signal for this paper, and always labelled as such, because
  "papers with similar words" is a much weaker claim than "papers this source relates".

The result says which kind it is, so the model can hedge appropriately.
"""

from __future__ import annotations

from typing import Any

from . import dedupe
from .models import Paper
from .providers import Capability, with_capability
from .search import search_across


def similar_providers(preferred: list[str] | None = None) -> list[Any]:
    """Providers that can do native similarity, best first, optionally restricted."""
    providers = with_capability(Capability.SIMILAR)
    if preferred:
        wanted = set(preferred)
        chosen = [p for p in providers if p.meta.name in wanted]
        return chosen or providers
    return providers


async def find_similar(papers: list[Paper], limit: int) -> tuple[list[Paper], list[str], str]:
    """Return (papers, provider failures, method).

    `method` is "native" when a source computed the relation, "keywords" when nothing could
    and we fell back to matching words, "none" when even that found nothing.

    The caller is told which it got, because the two are not equally good evidence and a model
    should not present them as if they were.
    """
    errors: list[str] = []
    collected: list[Paper] = []
    unanswered = False

    for provider in similar_providers():
        try:
            found = await provider.similar(papers[0], limit)
        except Exception as exc:
            errors.append(f"{provider.meta.label}: {exc}")
            unanswered = True
            continue
        if found:
            collected.extend(found)
            break  # the highest-reliability provider that answered wins; no need to pile on
        unanswered = True

    if collected:
        return dedupe.dedupe(collected)[:limit], errors, "native"

    # No native signal: fall back to the paper's own words, clearly labelled.
    query = _keyword_query(papers[0])
    if not query:
        return [], errors, "none"
    found, fallback_errors, _ = await search_across(query, None, limit, None)
    errors.extend(fallback_errors)
    if not found and unanswered:
        return [], errors, "none"
    return found, errors, "keywords"


def _keyword_query(paper: Paper) -> str:
    """A query built from the paper's title, minus the parts that appear in every paper."""
    from .dedupe import normalise_title

    stop = {
        "a", "an", "the", "of", "for", "on", "in", "to", "and", "or", "with", "via", "using",
        "towards", "toward", "study", "survey", "analysis", "review", "approach", "based",
    }
    words = [w for w in normalise_title(paper.title).split() if w not in stop and len(w) > 2]
    query = " ".join(words[:12])
    # Concept keywords sharpen the query when the title alone is generic.
    if len(words) < 6 and paper.keywords:
        query = f"{query} {' '.join(paper.keywords[:3])}".strip()
    return query
