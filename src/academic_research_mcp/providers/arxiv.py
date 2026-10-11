"""arXiv — https://info.arxiv.org/help/api

Preprints in physics, maths, CS and quantitative biology. Always open access, so it is the
most reliable provider here for PDFs; it reports no citation counts.
"""

from __future__ import annotations

import re
from typing import Any

from .. import http
from ..download import fetch_pdf
from ..models import Paper, truncate, year_str
from .base import Capability, Provider, ProviderMeta
from .registry import register

API = "https://export.arxiv.org/api/query"

_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.DOTALL)
_TAGS = {
    "title": re.compile(r"<title>(.*?)</title>", re.DOTALL),
    "summary": re.compile(r"<summary>(.*?)</summary>", re.DOTALL),
    "published": re.compile(r"<published>(.*?)</published>", re.DOTALL),
    "id": re.compile(r"<id>(.*?)</id>", re.DOTALL),
    "doi": re.compile(r"<arxiv:doi[^>]*>(.*?)</arxiv:doi>", re.DOTALL),
    "journal": re.compile(r"<arxiv:journal_ref[^>]*>(.*?)</arxiv:journal_ref>", re.DOTALL),
}


def unescape(text: str) -> str:
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&apos;", "'")
        .replace("&amp;", "&")
    )


def parse_atom(xml: str) -> list[Paper]:
    """Parse arXiv's Atom feed. A small regex parser on purpose: the feed is stable and
    this keeps a heavyweight XML dependency out of the search path."""
    papers: list[Paper] = []
    for block in _ENTRY.findall(xml):
        def tag(name: str) -> str | None:
            match = _TAGS[name].search(block)
            return unescape(" ".join(match.group(1).split())) if match else None

        title = tag("title")
        if not title:
            continue
        authors = [unescape(" ".join(a.split())) for a in re.findall(r"<name>(.*?)</name>", block, re.DOTALL)]
        entry_id = tag("id") or ""
        arxiv_id = entry_id.rsplit("/", 1)[-1] if entry_id else None
        papers.append(
            Paper(
                title=title,
                source="arXiv",
                authors=authors[:20],
                year=year_str(tag("published")),
                venue=tag("journal") or "arXiv preprint",
                doi=tag("doi"),
                url=entry_id or None,
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else None,
                is_open_access=True,
                abstract=truncate(tag("summary"), 2000),
                publication_type="preprint",
                source_id=arxiv_id,
                extra={"arxiv_id": arxiv_id} if arxiv_id else {},
            )
        )
    return papers


def extract_arxiv_id(identifier: str) -> str | None:
    """Accept '2401.12345', '2401.12345v2', 'arXiv:2401.12345', or an abs/pdf URL."""
    match = re.search(r"(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)", identifier)
    return match.group(1) if match else None


@register
class Arxiv(Provider):
    meta = ProviderMeta(
        name="arxiv",
        label="arXiv",
        homepage="https://info.arxiv.org/help/api",
        summary="Physics, maths, CS, quantitative biology preprints. Always open access.",
        capabilities=frozenset(
            {
                Capability.SEARCH,
                Capability.LOOKUP,
                Capability.DOWNLOAD,
                Capability.BROWSE,
            }
        ),
        reliability=90,
        aliases=("arxiv.org",),
    )

    async def search(self, query: str, limit: int, from_year: int | None) -> list[Paper]:
        terms = f"all:{query}"
        if from_year:
            terms = f"({terms}) AND submittedDate:[{from_year}01010000 TO 999912312359]"
        xml = await http.request_text(
            API,
            {"search_query": terms, "start": 0, "max_results": limit, "sortBy": "relevance"},
            source=self.meta.label,
        )
        return parse_atom(xml)

    async def browse(self, topic: str, limit: int) -> list[Paper]:
        """Newest submissions in a category such as cs.AI."""
        xml = await http.request_text(
            API,
            {"search_query": f"cat:{topic}", "start": 0, "max_results": limit, "sortBy": "submittedDate", "sortOrder": "descending"},
            source=self.meta.label,
        )
        return parse_atom(xml)

    async def lookup(self, identifier: str) -> Paper | None:
        arxiv_id = extract_arxiv_id(identifier)
        if not arxiv_id:
            return None
        xml = await http.request_text(API, {"id_list": arxiv_id}, source=self.meta.label)
        papers = parse_atom(xml)
        return papers[0] if papers else None

    async def download(self, paper: Paper, destination: str) -> dict[str, Any]:
        url = paper.pdf_url
        if not url and (paper.extra or {}).get("arxiv_id"):
            url = f"https://arxiv.org/pdf/{paper.extra['arxiv_id']}"
        if not url:
            return {"ok": False, "reason": "No arXiv PDF URL on this record."}
        return await fetch_pdf(url, destination, source=self.meta.label)
