"""Crossref — https://api.crossref.org

Publisher-deposited metadata: journals, books, conference papers, with the DOI as the
native key. Author lists are occasionally absent (book chapters, corrections), so treat a
missing author list as normal here rather than as a parse failure.
"""

from __future__ import annotations

import re
from typing import Any

from .. import http
from ..models import Paper, truncate
from .base import Capability, Provider, ProviderMeta
from .registry import register

API = "https://api.crossref.org/works"

# `select` is validated against a per-route allowlist: an unknown field yields HTTP 400,
# not a silently ignored one. Keep this to fields /works actually supports.
_SELECT = (
    "DOI,title,author,issued,container-title,is-referenced-by-count,URL,type,subject,"
    "abstract,link,published-print,published-online,publisher"
)


def _date(item: dict[str, Any]) -> int | None:
    from ..models import year_str

    for key in ("published-print", "published-online", "issued", "created"):
        parts = ((item.get(key) or {}).get("date-parts") or [[None]])[0]
        if parts and parts[0]:
            return year_str(parts[0])
    return None


def to_paper(item: dict[str, Any]) -> Paper:
    authors = []
    for a in item.get("author", [])[:20]:
        name = " ".join(filter(None, [a.get("given"), a.get("family")])) or a.get("name")
        if name:
            authors.append(name)
    links = item.get("link") or []
    pdf_url = next((entry.get("URL") for entry in links if entry.get("content-type") == "application/pdf"), None)
    return Paper(
        title=(item.get("title") or ["Untitled"])[0].strip(),
        source="Crossref",
        authors=authors,
        year=_date(item),
        venue=(item.get("container-title") or [None])[0],
        citations=item.get("is-referenced-by-count"),
        doi=item.get("DOI"),
        url=item.get("URL") or (f"https://doi.org/{item['DOI']}" if item.get("DOI") else None),
        pdf_url=pdf_url,
        abstract=truncate(re.sub(r"<[^>]+>", " ", item.get("abstract") or ""), 2000),
        keywords=(item.get("subject") or [])[:10],
        publication_type=item.get("type"),
        source_id=item.get("DOI"),
        extra={"publisher": item.get("publisher")} if item.get("publisher") else {},
    )


def clean_doi(doi: str) -> str:
    return doi.strip().removeprefix("https://doi.org/").removeprefix("http://dx.doi.org/").removeprefix("doi:")


@register
class Crossref(Provider):
    meta = ProviderMeta(
        name="crossref",
        label="Crossref",
        homepage="https://api.crossref.org",
        summary="Publisher metadata for journals, books and conference papers. Strong on DOIs.",
        capabilities=frozenset({Capability.SEARCH, Capability.LOOKUP}),
        reliability=85,
    )

    async def search(self, query: str, limit: int, from_year: int | None) -> list[Paper]:
        params: dict[str, Any] = {"query": query, "rows": limit, "select": _SELECT}
        if from_year:
            params["filter"] = f"from-pub-date:{from_year}-01-01"
        if http.mailto():
            params["mailto"] = http.mailto()
        payload = await http.request_json(API, params, source=self.meta.label)
        return [to_paper(i) for i in (payload.get("message") or {}).get("items", [])]

    async def lookup(self, identifier: str) -> Paper | None:
        payload = await http.request_json(
            f"{API}/{clean_doi(identifier)}",
            {"mailto": http.mailto()} if http.mailto() else None,
            source=self.meta.label,
        )
        item = payload.get("message") if payload else None
        return to_paper(item) if item else None
