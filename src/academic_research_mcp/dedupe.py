"""Deduplication for multi-source searches.

The same paper is routinely returned by several sources with different identifiers and
slightly different titles. Merging them keeps the model from citing one paper four times,
and lets us keep the richest field from each copy.
"""

from __future__ import annotations

import re

from .models import Paper

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")


def normalise_title(title: str) -> str:
    text = _PUNCT.sub(" ", title.lower())
    return _WS.sub(" ", text).strip()


def normalise_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    cleaned = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "http://dx.doi.org/", "doi:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    return cleaned or None


def _merge(base: Paper, other: Paper) -> Paper:
    """Fold `other` into `base`, preferring whichever copy actually has a value."""

    def prefer(current, candidate):
        if candidate is None or candidate == [] or candidate == "":
            return current
        if current is None or current == [] or current == "":
            return candidate
        return current

    # Longest author list and richest abstract win; they are rarely truncated identically.
    authors = base.authors if len(base.authors) >= len(other.authors) else other.authors
    abstract = base.abstract or other.abstract
    if base.abstract and other.abstract and len(other.abstract) > len(base.abstract):
        abstract = other.abstract

    # Citations are only comparable within a source, so take the highest reported count.
    citations = base.citations
    if other.citations is not None:
        citations = other.citations if citations is None else max(citations, other.citations)

    seen = {base.source}
    extra = dict(base.extra)
    extra["also_in"] = sorted(s for s in ([*base.extra.get("also_in", []), other.source]) if s not in seen)

    return Paper(
        title=base.title if len(base.title) >= len(other.title) else other.title,
        source=base.source,
        authors=authors,
        year=prefer(base.year, other.year),
        venue=prefer(base.venue, other.venue),
        citations=citations,
        doi=normalise_doi(base.doi) or normalise_doi(other.doi),
        url=prefer(base.url, other.url),
        pdf_url=prefer(base.pdf_url, other.pdf_url),
        is_open_access=True if base.is_open_access or other.is_open_access else base.is_open_access,
        abstract=abstract,
        keywords=(base.keywords or other.keywords)[:10],
        language=prefer(base.language, other.language),
        publication_type=prefer(base.publication_type, other.publication_type),
        source_id=prefer(base.source_id, other.source_id),
        extra={k: v for k, v in extra.items() if v},
    )


def dedupe(papers: list[Paper]) -> list[Paper]:
    """Merge records that share a DOI, or failing that a normalised title + year.

    Order is preserved, so a caller can rank sources by trust and keep that ranking.
    """
    by_doi: dict[str, int] = {}
    by_title: dict[str, int] = {}
    out: list[Paper] = []

    for paper in papers:
        doi = normalise_doi(paper.doi)
        title_key = f"{normalise_title(paper.title)}|{paper.year or ''}"
        loose_key = normalise_title(paper.title)

        index = None
        if doi and doi in by_doi:
            index = by_doi[doi]
        elif title_key in by_title:
            index = by_title[title_key]
        elif loose_key and len(loose_key) > 20 and loose_key in by_title:
            # Same title, year missing on one side — still almost certainly one paper.
            index = by_title[loose_key]

        if index is None:
            out.append(paper)
            index = len(out) - 1
        else:
            out[index] = _merge(out[index], paper)

        merged = out[index]
        merged_doi = normalise_doi(merged.doi)
        if merged_doi:
            by_doi[merged_doi] = index
        by_title[f"{normalise_title(merged.title)}|{merged.year or ''}"] = index
        by_title[normalise_title(merged.title)] = index

    return out
