"""Unpaywall — https://unpaywall.org/products/api

Not a search source: this is the open-access resolver. Given a DOI it reports whether a
free, publisher-authorised copy exists and where.

It is the one provider here that *requires* a contact address rather than merely preferring
one, so `requires_mailto` is True and the tool tells the caller exactly what to set when it
is missing.
"""

from __future__ import annotations

from typing import Any

from .. import http
from ..models import Paper
from .base import Capability, Provider, ProviderMeta
from .registry import register

API = "https://api.unpaywall.org/v2"


def clean_doi(doi: str) -> str:
    return doi.strip().lower().removeprefix("https://doi.org/").removeprefix("http://doi.org/").removeprefix("doi:")


@register
class Unpaywall(Provider):
    meta = ProviderMeta(
        name="unpaywall",
        label="Unpaywall",
        homepage="https://unpaywall.org/products/api",
        summary="Finds a free legal copy of a paywalled DOI. Needs a contact email.",
        capabilities=frozenset({Capability.FULLTEXT_LINK, Capability.DOWNLOAD}),
        reliability=92,
        requires_mailto=True,
    )

    async def fulltext_link(self, doi: str) -> dict[str, Any]:
        clean = clean_doi(doi)
        if not clean.startswith("10."):
            return {"ok": False, "reason": f"{doi!r} does not look like a DOI."}
        if not http.mailto():
            return {
                "ok": False,
                "reason": (
                    "Unpaywall requires a contact email. Set RESEARCH_MCP_MAILTO "
                    "(e.g. you@example.org) in the server environment and restart."
                ),
            }

        payload = await http.request_json(f"{API}/{clean}", {"email": http.mailto()}, source=self.meta.label)

        best = payload.get("best_oa_location") or {}
        locations = [
            {
                "url": loc.get("url") or loc.get("url_for_pdf"),
                "pdf_url": loc.get("url_for_pdf"),
                "host_type": loc.get("host_type"),
                "version": loc.get("version"),
                "license": loc.get("license"),
                "repository": loc.get("repository_institution"),
            }
            for loc in payload.get("oa_locations", []) or []
        ]

        return {
            "ok": bool(payload.get("is_oa")),
            "is_oa": payload.get("is_oa"),
            "oa_status": payload.get("oa_status"),
            "title": payload.get("title"),
            "journal": payload.get("journal_name"),
            "publisher": payload.get("publisher"),
            "year": payload.get("year"),
            "best": (
                {
                    "url": best.get("url") or best.get("url_for_pdf"),
                    "pdf_url": best.get("url_for_pdf"),
                    "host_type": best.get("host_type"),
                    "version": best.get("version"),
                    "license": best.get("license"),
                }
                if best
                else None
            ),
            "locations": [loc for loc in locations if loc.get("url") or loc.get("pdf_url")][:5],
            "reason": None if payload.get("is_oa") else "No open-access copy registered for this DOI.",
        }

    async def download(self, paper: Paper, destination: str) -> dict[str, Any]:
        """Resolve a free copy of the paper's DOI, then fetch it."""
        from ..download import fetch_pdf

        if not paper.doi:
            return {"ok": False, "reason": "This record has no DOI to resolve."}
        resolved = await self.fulltext_link(paper.doi)
        if not resolved.get("ok"):
            return {"ok": False, "reason": str(resolved.get("reason"))}
        best = resolved.get("best") or {}
        if not isinstance(best, dict):
            return {"ok": False, "reason": "Unpaywall reported open access but gave no location."}
        url = best.get("pdf_url") or best.get("url")
        if not url:
            return {"ok": False, "reason": "Unpaywall reported open access but gave no PDF link."}
        return await fetch_pdf(str(url), destination, source=self.meta.label)
