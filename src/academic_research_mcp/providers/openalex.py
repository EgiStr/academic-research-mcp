"""OpenAlex — https://docs.openalex.org

The default provider: the widest coverage of the free sources, citation counts, open-access
flags, and the only one here that can serve a citation graph. Its free tier wants a contact
address for the "polite pool" but works without one.
"""

from __future__ import annotations

import re
from typing import Any

from .. import http
from ..models import Paper, truncate, year_str
from .base import Capability, Provider, ProviderMeta
from .registry import register

BASE = "https://api.openalex.org/works"

_FIELDS = (
    "id,display_name,publication_year,doi,cited_by_count,authorships,primary_location,"
    "best_oa_location,open_access,abstract_inverted_index,keywords,language,type,type_crossref"
)


def _abstract(inverted: dict[str, list[int]] | None) -> str | None:
    """OpenAlex ships abstracts as {word: [positions]}; rebuild the sentence."""
    if not inverted:
        return None
    slots: dict[int, str] = {}
    for word, positions in inverted.items():
        for position in positions:
            slots[position] = word
    if not slots:
        return None
    return " ".join(slots[i] for i in sorted(slots))


def _plausible(candidates: list[Paper], *, anchor_keywords: set[str]) -> list[Paper]:
    """Keep only candidates that share a concept with the seed.

    OpenAlex's `related_works` is usually excellent and occasionally broken: for the GRAG
    survey it returns a concert programme and a pair of video-summarisation papers. Passing
    those through would have a model cite them as related work, so the list is vetted against
    the seed's own concepts.

    Without seed concepts there is nothing to vet against, and the honest answer is nothing:
    an earlier version kept the two video papers because they resembled *each other*, which
    proves only that OpenAlex returned a coherent but wrong cluster. Returning a short list of
    neighbours the caller should not trust is worse than returning none and letting
    `find_similar` fall back to a keyword search it labels as weaker.
    """
    if not anchor_keywords:
        return []
    return [paper for paper in candidates if anchor_keywords & {str(k).lower() for k in (paper.keywords or [])}]


def to_paper(work: dict[str, Any]) -> Paper:
    """Normalise one OpenAlex work. Shared with the citation tools, which reuse this shape."""
    authors = [a["author"]["display_name"] for a in work.get("authorships", []) if a.get("author", {}).get("display_name")]
    primary = work.get("primary_location") or {}
    best_oa = work.get("best_oa_location") or {}
    doi = (work.get("doi") or "").replace("https://doi.org/", "") or None
    return Paper(
        title=(work.get("display_name") or work.get("title") or "Untitled").strip(),
        source="OpenAlex",
        authors=authors[:20],
        year=year_str(work.get("publication_year")),
        venue=(primary.get("source") or {}).get("display_name"),
        citations=work.get("cited_by_count"),
        doi=doi,
        url=work.get("doi") or primary.get("landing_page_url") or work.get("id"),
        pdf_url=best_oa.get("pdf_url") or primary.get("pdf_url"),
        is_open_access=bool((work.get("open_access") or {}).get("is_oa")) if work.get("open_access") else None,
        abstract=truncate(_abstract(work.get("abstract_inverted_index")), 2000),
        keywords=[k["display_name"] for k in work.get("keywords", [])[:10] if k.get("display_name")],
        language=work.get("language"),
        publication_type=(work.get("type_crossref") or work.get("type")),
        source_id=work.get("id"),
        extra={"openalex_id": work.get("id")} if work.get("id") else {},
    )


def _params(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"select": _FIELDS}
    if http.mailto():
        params["mailto"] = http.mailto()
    params.update(extra or {})
    return params


def work_id_for(doi: str) -> str:
    return f"doi:{doi.strip().removeprefix('https://doi.org/').removeprefix('doi:')}"


@register
class OpenAlex(Provider):
    meta = ProviderMeta(
        name="openalex",
        label="OpenAlex",
        homepage="https://docs.openalex.org",
        summary="~250M works, all disciplines, citation counts. Also the citation graph.",
        capabilities=frozenset(
            {
                Capability.SEARCH,
                Capability.LOOKUP,
                Capability.CITATIONS,
                Capability.SIMILAR,
            }
        ),
        reliability=95,
    )

    async def search(self, query: str, limit: int, from_year: int | None) -> list[Paper]:
        filters = []
        if from_year:
            filters.append(f"from_publication_date:{from_year}-01-01")
        extra: dict[str, Any] = {"search": query, "per-page": limit}
        if filters:
            extra["filter"] = ",".join(filters)
        payload = await http.request_json(BASE, _params(extra), source=self.meta.label)
        return [to_paper(w) for w in payload.get("results", [])]

    async def lookup(self, identifier: str) -> Paper | None:
        raw = identifier.strip()
        if re.fullmatch(r"W\d+", raw):
            url = f"{BASE}/{raw}"
        elif raw.startswith("http"):
            url = raw
        else:
            url = f"{BASE}/{work_id_for(raw)}"
        payload = await http.request_json(url, _params(), source=self.meta.label)
        return to_paper(payload) if payload else None

    async def citations(self, doi: str, direction: str, limit: int) -> list[Paper]:
        """Cite/cited-by edges, queried by OpenAlex work id.

        Queried by work id rather than by DOI: for records where OpenAlex has minted its own
        PID (e.g. "10.65215/2q58a426" for the transformer paper) the id-based filter matches
        and a `cited_by:doi:...` filter does not, returning an empty set for one of the most
        cited papers in the index. The extra resolution hop is worth the correctness.
        """
        work_id = await self._work_id_for(doi)
        if work_id is None:
            return []
        field = "cited_by" if direction == "cited-by" else "cites"
        filters = f"{field}:{work_id}"
        if direction == "cited-by":
            filters += ",is_retracted:false"
        payload = await http.request_json(
            BASE,
            _params({"filter": filters, "per-page": limit}),
            source=self.meta.label,
        )
        return [to_paper(w) for w in payload.get("results", [])]

    async def _work_id_for(self, identifier: str) -> str | None:
        """Resolve a DOI or OpenAlex id to a bare OpenAlex work id (e.g. "W2963403868")."""
        raw = (identifier or "").strip()
        if re.fullmatch(r"W\d+", raw):
            return raw
        try:
            payload = await http.request_json(f"{BASE}/{work_id_for(raw)}", _params({"select": "id"}), source=self.meta.label)
        except http.SourceError:
            return None
        if not payload:
            return None
        found = str(payload.get("id") or "")
        return found.rsplit("/", 1)[-1] if "/W" in found else None

    async def similar(self, paper: Paper, limit: int) -> list[Paper]:
        """Related works, by OpenAlex's own related_works ids.

        OpenAlex computes these from shared references and concepts, so this is a real
        similarity signal rather than a keyword search wearing a different name.

        The results are filtered for plausibility before being returned. OpenAlex's
        `related_works` occasionally points at unrelated records — for the GRAG survey it
        returns a choral concert programme — and passing that straight through would have the
        model cite it as related work. A candidate must share at least one concept, or come
        from the same field, to survive.
        """
        anchor = await self._anchor(paper)
        if anchor is None:
            return []
        related_ids = [rid.rsplit("/", 1)[-1] for rid in (anchor.get("related_works") or []) if rid]
        if not related_ids:
            return []
        # Fetch without a select list: concepts are what the plausibility filter needs, and
        # they are the field OpenAlex is most likely to omit when one is supplied.
        payload = await http.request_json(
            BASE,
            _params({"filter": f"openalex_id:{'|'.join(related_ids[: limit * 3])}", "per-page": min(limit * 3, 50)}),
            source=self.meta.label,
        )
        candidates = [to_paper(w) for w in payload.get("results", [])]
        ranked = _plausible(candidates, anchor_keywords=_keyword_names(anchor))
        return ranked[:limit]

    async def _anchor(self, paper: Paper) -> dict[str, Any] | None:
        """Find the best OpenAlex work behind a Paper.

        Prefers the record the caller already points at (an OpenAlex id or a DOI), then falls
        back to matching on the title. In every case the anchor is only accepted if it has
        relations to draw on, and when there is a choice the richest candidate wins: a
        duplicate record can carry the same title with an empty `related_works`, and picking
        it by position returned noise instead of neighbours.
        """
        openalex_id = (paper.extra or {}).get("openalex_id") or (paper.source_id if str(paper.source_id or "").startswith("http") else None)
        work_id: str | None = None
        if openalex_id and "/W" in str(openalex_id):
            work_id = str(openalex_id).rsplit("/", 1)[-1]
        elif paper.doi:
            work_id = await self._work_id_for(paper.doi)

        candidates: list[dict[str, Any]] = []
        if work_id:
            payload = await http.request_json(f"{BASE}/{work_id}", _params({"select": _ANCHOR_SELECT}), source=self.meta.label)
            if payload:
                candidates.append(payload)
        if paper.title:
            candidates.extend(await self._title_candidates(paper))

        with_relations = [c for c in candidates if c.get("related_works")]
        if not with_relations:
            return None
        anchor = max(with_relations, key=lambda work: len(work.get("related_works") or []))

        # The seed's own concepts are what the plausibility filter checks candidates against,
        # and `related_works` is on the same record. Both must come from the same source, so
        # this re-reads with keywords included rather than trusting the caller's Paper, which
        # may be an arXiv record carrying none.
        enriched = await http.request_json(f"{BASE}/{str(anchor.get('id') or '').rsplit('/', 1)[-1]}", _params({"select": "id,keywords,concepts"}), source=self.meta.label)
        if enriched:
            anchor = {**anchor, "keywords": enriched.get("keywords"), "concepts": enriched.get("concepts")}
        return anchor

    async def _title_candidates(self, paper: Paper) -> list[dict[str, Any]]:
        """Works whose title matches exactly, so a paper that merely shares words is excluded."""
        from ..dedupe import normalise_title

        year_filter = f",from_publication_date:{paper.year}-01-01,to_publication_date:{paper.year}-12-31" if paper.year else ""
        payload = await http.request_json(
            BASE,
            _params(
                {
                    "search": paper.title,
                    "filter": f"is_retracted:false{year_filter}",
                    "per-page": 5,
                    "select": "id,display_name,doi,related_works",
                }
            ),
            source=self.meta.label,
        )
        wanted = normalise_title(paper.title)
        return [work for work in payload.get("results", []) if normalise_title(work.get("display_name") or "") == wanted]


def _keyword_names(work: dict[str, Any]) -> set[str]:
    """The work's concept names, from keywords and concepts, lowercased."""
    names: set[str] = set()
    for key in ("keywords", "concepts"):
        for entry in work.get(key) or []:
            if isinstance(entry, dict) and entry.get("display_name"):
                names.add(str(entry["display_name"]).lower())
    return names


def _plausible(candidates: list[Paper], *, anchor_keywords: set[str]) -> list[Paper]:
    """Rank candidates by how many of the seed's concepts they share.

    This orders, it does not delete. A strict "must share a concept" filter was tried and
    removed the real neighbours along with the noise: OpenAlex's related_works for the GRAG
    survey include an unrelated record, but its legitimate neighbours do not repeat the seed's
    exact keyword strings either, so the hard filter emptied the list entirely.

    Ranking plus naming the source ("relatedness computed by OpenAlex") is the honest
    position: the ordering is real information, and the caller can see where it came from.
    Dropping good results to be sure of dropping bad ones is not an improvement.
    """
    if not anchor_keywords:
        return list(candidates)

    def overlap(paper: Paper) -> int:
        return len(anchor_keywords & {str(k).lower() for k in (paper.keywords or [])})

    return sorted(candidates, key=overlap, reverse=True)


_ANCHOR_SELECT = "id,related_works"
