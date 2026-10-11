"""Full-text availability, keyless.

Unpaywall finds a free copy of a paywalled DOI; Europe PMC knows whether a life-sciences
paper has an open full text. Both are free and need no key — only a contact address,
which is why RESEARCH_MCP_MAILTO exists.
"""

from __future__ import annotations

import re

from . import http
from .models import Paper

try:  # Python 3.12 stdlib; keeps the dependency list honest.
    from defusedxml import ElementTree as ET  # type: ignore
except ImportError:  # pragma: no cover - optional hardening
    from xml.etree import ElementTree as ET  # type: ignore

UNPAYWALL = "https://api.unpaywall.org/v2/{doi}"


def _clean_doi(doi: str) -> str:
    return doi.strip().lower().removeprefix("https://doi.org/").removeprefix("http://doi.org/").removeprefix("doi:")


async def unpaywall_lookup(doi: str) -> dict[str, object]:
    """Return open-access locations for a DOI, or a dict explaining why none were found."""
    clean = _clean_doi(doi)
    if not clean or not clean.startswith("10."):
        return {"ok": False, "reason": f"{doi!r} does not look like a DOI."}

    if not http.MAILTO:
        # Unpaywall requires an email; without one we say so instead of failing opaquely.
        return {
            "ok": False,
            "reason": "Unpaywall needs a contact email. Set RESEARCH_MCP_MAILTO (e.g. your address) and restart the server.",
        }

    try:
        payload = await http.request_json(
            UNPAYWALL.format(doi=clean),
            {"email": http.MAILTO},
            source="Unpaywall",
        )
    except http.SourceError as exc:
        if "404" in str(exc):
            return {"ok": False, "reason": f"No Unpaywall record for {clean} — the DOI may be unknown or very recent."}
        raise

    best = payload.get("best_oa_location") or {}
    locations = []
    for loc in payload.get("oa_locations", []) or []:
        locations.append(
            {
                "url": loc.get("url") or loc.get("url_for_pdf"),
                "pdf_url": loc.get("url_for_pdf"),
                "host_type": loc.get("host_type"),
                "version": loc.get("version"),
                "license": loc.get("license"),
                "repository": (loc.get("repository_institution") or None),
            }
        )

    return {
        "ok": bool(payload.get("is_oa")),
        "is_oa": payload.get("is_oa"),
        "oa_status": payload.get("oa_status"),
        "title": payload.get("title"),
        "journal": payload.get("journal_name"),
        "publisher": payload.get("publisher"),
        "year": payload.get("year"),
        "best": {
            "url": best.get("url") or best.get("url_for_pdf"),
            "pdf_url": best.get("url_for_pdf"),
            "host_type": best.get("host_type"),
            "version": best.get("version"),
            "license": best.get("license"),
        }
        if best
        else None,
        "locations": [loc for loc in locations if loc.get("url") or loc.get("pdf_url")][:5],
        "reason": None if payload.get("is_oa") else "No open-access copy registered for this DOI.",
    }


async def europe_pmc_fulltext(identifier: str) -> dict[str, object]:
    """Section text for an open-access Europe PMC article (PMCID or 'PMC1234567')."""
    raw = identifier.strip()
    if raw.lower().startswith("pmcid:"):
        pmcid = raw.split(":", 1)[1]
    elif raw.upper().startswith("PMC"):
        pmcid = raw
    else:
        # A bare number is assumed to be a PMID; resolve it to a PMCID first.
        found = await http.request_json(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            {"query": f"EXT_ID:{raw}", "format": "json", "pageSize": 1},
            source="Europe PMC",
        )
        results = ((found.get("resultList") or {}).get("result") or [])
        if not results or not results[0].get("pmcid"):
            return {"ok": False, "reason": f"PMID {raw} has no open-access full text in Europe PMC."}
        pmcid = results[0]["pmcid"]

    xml = await http.request_text(
        f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML",
        source="Europe PMC",
    )
    return {"ok": True, "pmcid": pmcid, "text": _xml_sections(xml)}


def _xml_sections(xml: str) -> dict[str, str]:
    """Extract the prose sections, dropping references and markup noise."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {"body": re.sub(r"<[^>]+>", " ", xml)[:20000]}

    sections: dict[str, str] = {}
    for element in root.iter():
        tag = element.tag.split("}")[-1]
        if tag not in ("abstract", "body", "sec", "title"):
            continue
        text = " ".join("".join(element.itertext()).split())
        if not text or len(text) < 40:
            continue
        if tag == "sec":
            heading_el = element.find("./{*}title")
            heading = " ".join("".join(heading_el.itertext()).split()) if heading_el is not None else f"section {len(sections) + 1}"
            # Skip a section that is just the title of its parent.
            text = " ".join(text.split(heading, 1)[-1]).strip() or text
            sections.setdefault(heading, text[:8000])
        else:
            sections.setdefault(tag, text[:8000])
    return sections


def enrich(paper: Paper, oa: dict[str, object]) -> Paper:
    """Attach an Unpaywall result to a Paper record."""
    if not oa.get("ok"):
        return paper
    best = oa.get("best") or {}
    locations = oa.get("locations") or []
    paper.is_open_access = True
    paper.pdf_url = paper.pdf_url or (best.get("pdf_url") if isinstance(best, dict) else None)
    paper.url = paper.url or (best.get("url") if isinstance(best, dict) else None)
    if isinstance(locations, list) and locations:
        paper.extra = {**paper.extra, "oa_status": oa.get("oa_status"), "oa_locations": locations}
    return paper
