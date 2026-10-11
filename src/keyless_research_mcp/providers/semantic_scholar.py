"""Semantic Scholar — https://api.semanticscholar.org/api-docs

Strong on AI/CS, with citation and influential-citation counts plus an abstract per paper.

Reliability is scored low on purpose. Keyless requests are throttled hard (frequent HTTP
429), so it is opt-in rather than part of every multi-source search: name it explicitly
with `source="semantic-scholar"` when you want it and can tolerate a retry.
"""

from __future__ import annotations

import re
from typing import Any

from .. import http
from ..models import Paper, truncate, year_str
from .base import Capability, Provider, ProviderMeta
from .registry import register

API = "https://api.semanticscholar.org/graph/v1/paper"

_FIELDS = (
    "title,year,authors,venue,citationCount,influentialCitationCount,externalIds,url,"
    "abstract,openAccessPdf,publicationTypes,fieldsOfStudy,referenceCount"
)


def to_paper(paper: dict[str, Any]) -> Paper:
    external = paper.get("externalIds") or {}
    pdf = paper.get("openAccessPdf") or {}
    extra = {k: v for k, v in external.items() if k in ("ArXiv", "PubMed", "CorpusId") and v}
    return Paper(
        title=(paper.get("title") or "Untitled").strip(),
        source="Semantic Scholar",
        authors=[a["name"] for a in paper.get("authors", []) if a.get("name")][:20],
        year=year_str(paper.get("year")),
        venue=paper.get("venue") or None,
        citations=paper.get("citationCount"),
        doi=external.get("DOI"),
        url=paper.get("url"),
        pdf_url=pdf.get("url"),
        is_open_access=bool(pdf),
        abstract=truncate(paper.get("abstract"), 2000),
        keywords=(paper.get("fieldsOfStudy") or [])[:10],
        publication_type=(paper.get("publicationTypes") or [None])[0],
        source_id=paper.get("paperId"),
        extra=extra,
    )


def _paper_id(identifier: str) -> str:
    """Shape an identifier into the form the S2 graph API expects.

    The arXiv version suffix is dropped: S2 indexes `ARXIV:2408.08921`, and
    `ARXIV:2408.08921v2` is a 404. Versions are what arXiv serves, so this comes up on
    every arxiv-sourced record.
    """
    raw = identifier.strip()
    if raw.lower().startswith("corpusid:"):
        return raw
    if raw.lower().startswith("doi:"):
        return f"DOI:{raw[4:]}"
    if raw.lower().startswith("arxiv:"):
        # The backslash stays out of the f-string: escapes inside one need Python 3.12, and
        # this project supports 3.10.
        bare_arxiv = re.sub(r"v\d+$", "", raw[6:])
        return f"ARXIV:{bare_arxiv}"
    if raw.startswith("10."):
        return f"DOI:{raw}"
    bare = re.fullmatch(r"(\d{4}\.\d{4,5})(?:v\d+)?", raw)
    if bare:
        return f"ARXIV:{bare.group(1)}"
    return raw


@register
class SemanticScholar(Provider):
    meta = ProviderMeta(
        name="semantic-scholar",
        label="Semantic Scholar",
        homepage="https://api.semanticscholar.org/api-docs",
        summary="AI/CS strength, citation counts. Keyless requests are often rate-limited (429).",
        capabilities=frozenset({Capability.SEARCH, Capability.LOOKUP, Capability.SIMILAR}),
        # Low on purpose: see the module docstring. This keeps it out of auto-search.
        reliability=40,
        auto_search=False,
        min_interval=1.2,
        aliases=("s2", "semanticscholar", "semantic_scholar"),
    )

    async def search(self, query: str, limit: int, from_year: int | None) -> list[Paper]:
        params: dict[str, Any] = {"query": query, "limit": limit, "fields": _FIELDS}
        if from_year:
            params["year"] = f"{from_year}-"
        payload = await http.request_json(f"{API}/search", params, source=self.meta.label)
        return [to_paper(p) for p in payload.get("data", [])]

    async def lookup(self, identifier: str) -> Paper | None:
        payload = await http.request_json(
            f"{API}/{_paper_id(identifier)}",
            {"fields": _FIELDS},
            source=self.meta.label,
        )
        return to_paper(payload) if payload else None

    async def similar(self, paper: Paper, limit: int) -> list[Paper]:
        """Semantic Scholar's recommendations for a paper — a real embedding-based
        similarity signal, and the best 'more like this' available without a key."""
        anchor = paper.source_id if (paper.extra or {}).get("CorpusId") is None else f"CorpusId:{(paper.extra or {})['CorpusId']}"
        if not paper.source_id:
            found = await self.lookup(paper.doi) if paper.doi else None
            anchor = found.source_id if found else None
        if not anchor:
            return []
        payload = await http.request_json(
            f"{API}/{anchor}/recommendations",
            {"limit": limit, "fields": _FIELDS},
            source=self.meta.label,
        )
        return [to_paper(p) for p in (payload.get("recommendedPapers") or [])]
