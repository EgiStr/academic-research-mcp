"""Europe PMC — https://europepmc.org/RestfulWebService

Life sciences and biomedicine. The only provider here that can return article *body text*,
which makes it the one `read_fulltext` depends on.
"""

from __future__ import annotations

import re
from typing import Any

from .. import http
from ..models import Paper, truncate, year_str
from .base import Capability, Provider, ProviderMeta
from .registry import register

API = "https://www.ebi.ac.uk/europepmc/webservices/rest"


def to_paper(item: dict[str, Any]) -> Paper:
    authors = (item.get("authorString") or "").rstrip(".").split(", ") if item.get("authorString") else []
    abstract = item.get("abstractText")
    if abstract:
        abstract = re.sub(r"<[^>]+>", " ", abstract)
    extra = {k: item.get(k) for k in ("pmid", "pmcid") if item.get(k)}
    return Paper(
        title=(item.get("title") or "Untitled").strip().rstrip("."),
        source="Europe PMC",
        authors=[a for a in authors if a][:20],
        year=year_str(item.get("pubYear")),
        venue=item.get("journalTitle") or None,
        citations=int(item["citedByCount"]) if str(item.get("citedByCount", "")).isdigit() else None,
        doi=item.get("doi"),
        url=(
            f"https://doi.org/{item['doi']}"
            if item.get("doi")
            else (f"https://europepmc.org/article/{item.get('source', 'MED')}/{item.get('id')}" if item.get("id") else None)
        ),
        pdf_url=(
            f"https://europepmc.org/api/fulltextRepo?pprId={item['id']}&type=FILE&fileName=EMS.pdf"
            if item.get("isOpenAccess") == "Y" and item.get("id")
            else None
        ),
        is_open_access=item.get("isOpenAccess") == "Y",
        abstract=truncate(abstract, 2000),
        publication_type=item.get("pubType"),
        source_id=item.get("id"),
        extra=extra,
    )


async def search_payload(query: str, size: int, sort: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"query": query, "format": "json", "pageSize": size, "resultType": "core"}
    if sort:
        params["sort"] = sort
    payload = await http.request_json(f"{API}/search", params, source="Europe PMC")
    return ((payload.get("resultList") or {}).get("result") or [])


@register
class EuropePmc(Provider):
    meta = ProviderMeta(
        name="europe-pmc",
        label="Europe PMC",
        homepage="https://europepmc.org/RestfulWebService",
        summary="Life sciences and biomedicine. The only source here with full body text.",
        capabilities=frozenset(
            {
                Capability.SEARCH,
                Capability.LOOKUP,
                Capability.FULLTEXT_TEXT,
                Capability.SIMILAR,
            }
        ),
        reliability=88,
        aliases=("europepmc", "pmc"),
    )

    async def search(self, query: str, limit: int, from_year: int | None) -> list[Paper]:
        if from_year:
            query = f"({query}) AND (PUB_YEAR:[{from_year} TO 3000])"
        return [to_paper(i) for i in await search_payload(query, limit, sort="CITED desc")]

    async def lookup(self, identifier: str) -> Paper | None:
        raw = identifier.strip()
        lowered = raw.lower()
        if lowered.startswith("pmid:"):
            query = f"EXT_ID:{raw.split(':', 1)[1]}"
        elif lowered.startswith("pmcid:"):
            query = raw.split(":", 1)[1]
        elif lowered.startswith("doi:"):
            query = f'DOI:"{raw.split(":", 1)[1]}"'
        elif lowered.startswith("10."):
            query = f'DOI:"{raw}"'
        else:
            query = raw
        results = await search_payload(query, 1)
        return to_paper(results[0]) if results else None

    async def similar(self, paper: Paper, limit: int) -> list[Paper]:
        """Related articles via Europe PMC's own similarity index (the 'citations' graph it
        computes from shared references and MeSH terms)."""
        extra = paper.extra or {}
        pmid = extra.get("pmid")
        if not pmid:
            anchor = await self.lookup(paper.doi) if paper.doi else None
            pmid = (anchor.extra or {}).get("pmid") if anchor else None
        if not pmid:
            return []
        results = await http.request_json(
            f"{API}/MED/{pmid}/citations",
            {"format": "json", "pageSize": limit},
            source=self.meta.label,
        )
        citations = ((results.get("citationList") or {}).get("citation") or [])
        # The citations route returns stub records; re-fetch them properly by id.
        ids = [c.get("id") for c in citations if c.get("id")][:limit]
        if not ids:
            return []
        found = await search_payload(f"EXT_ID:{' OR EXT_ID:'.join(ids)}", limit)
        return [to_paper(i) for i in found]

    async def fulltext_text(self, identifier: str, max_chars: int) -> dict[str, Any]:
        from ..fulltext import europe_pmc_fulltext

        return await europe_pmc_fulltext(identifier, max_chars)
