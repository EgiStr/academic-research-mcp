"""DOAJ — https://doaj.org/api/docs

Open-access journals only. Every record is free to read, which makes it the right choice
when the caller needs certainty that a paper can be obtained, not just found.

DOAJ nests some fields inconsistently (`journal.publisher` is sometimes a string, sometimes
`{"name": ...}`), which is handled here so callers never see the difference.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from .. import http
from ..models import Paper, truncate, year_str
from .base import Capability, Provider, ProviderMeta
from .registry import register

API = "https://doaj.org/api/search/articles"


def _name(value: Any) -> str | None:
    """DOAJ sends a name as either a bare string or {"name": ...}."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        return value.get("name") or None
    return None


def to_paper(item: dict[str, Any]) -> Paper:
    bib = item.get("bibjson") or {}
    identifiers = {i.get("type"): i.get("id") for i in bib.get("identifier", [])}
    links = bib.get("link") or []
    journal = bib.get("journal") or {}
    authors = []
    for a in bib.get("author", [])[:20]:
        name = a.get("name") or " ".join(filter(None, [a.get("given"), a.get("family")]))
        if name:
            authors.append(name)
    fulltext = next((entry.get("url") for entry in links if entry.get("type") == "fulltext"), None)
    language = journal.get("language")
    publisher = _name(journal.get("publisher"))
    return Paper(
        title=(bib.get("title") or "Untitled").strip(),
        source="DOAJ",
        authors=authors,
        year=year_str(bib.get("year")),
        venue=_name(journal.get("title")),
        citations=None,
        doi=identifiers.get("doi"),
        url=fulltext or (f"https://doi.org/{identifiers['doi']}" if identifiers.get("doi") else item.get("id")),
        pdf_url=fulltext,
        is_open_access=True,
        abstract=truncate(re.sub(r"<[^>]+>", " ", bib.get("abstract") or ""), 2000),
        keywords=[k for k in (bib.get("keywords") or [])][:10],
        language=language if isinstance(language, str) else None,
        source_id=item.get("id"),
        extra={"publisher": publisher} if publisher else {},
    )


@register
class Doaj(Provider):
    meta = ProviderMeta(
        name="doaj",
        label="DOAJ",
        homepage="https://doaj.org/api/docs",
        summary="Open-access journals only. Every record is free to read.",
        capabilities=frozenset({Capability.SEARCH}),
        reliability=80,
    )

    async def search(self, query: str, limit: int, from_year: int | None) -> list[Paper]:
        search = f"bibjson.title:({query}) OR bibjson.abstract:({query})"
        if from_year:
            search = f"({search}) AND bibjson.year:[{from_year} TO 3000]"
        payload = await http.request_json(
            f"{API}/{quote(search, safe='')}",
            {"pageSize": limit},
            source=self.meta.label,
        )
        return [to_paper(r) for r in payload.get("results", [])]
