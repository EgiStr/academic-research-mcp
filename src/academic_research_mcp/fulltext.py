"""Article body text, keyless.

Europe PMC is the only provider here that returns machine-readable sections, so this module
holds the XML handling that `EuropePmc.fulltext_text` calls. It lives apart from the
provider so the XML logic can be tested without a network round trip.
"""

from __future__ import annotations

import re
from typing import Any

from . import http

API = "https://www.ebi.ac.uk/europepmc/webservices/rest"


def _sections(xml: str) -> dict[str, str]:
    """Extract prose sections, dropping references and markup noise."""
    from xml.etree import ElementTree as ET

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {"body": re.sub(r"<[^>]+>", " ", xml)[:20000]}

    sections: dict[str, str] = {}
    for element in root.iter():
        tag = element.tag.split("}")[-1]
        if tag not in ("abstract", "body", "sec"):
            continue
        text = " ".join("".join(element.itertext()).split())
        if not text or len(text) < 40:
            continue
        if tag == "sec":
            heading_el = element.find("./{*}title")
            heading = " ".join("".join(heading_el.itertext()).split()) if heading_el is not None else f"section {len(sections) + 1}"
            remainder = " ".join(text.split(heading, 1)[-1]).strip() or text
            sections.setdefault(heading, remainder[:8000])
        else:
            sections.setdefault(tag, text[:8000])
    return sections


async def europe_pmc_fulltext(identifier: str, max_chars: int = 20000) -> dict[str, Any]:
    """Section text for an open-access Europe PMC article (PMCID or PMID)."""
    raw = identifier.strip()
    if raw.lower().startswith("pmcid:"):
        pmcid = raw.split(":", 1)[1]
    elif raw.upper().startswith("PMC"):
        pmcid = raw
    else:
        # A bare number is assumed to be a PMID; resolve it to a PMCID first.
        payload = await http.request_json(
            f"{API}/search",
            {"query": f"EXT_ID:{raw}", "format": "json", "pageSize": 1, "resultType": "core"},
            source="Europe PMC",
        )
        results = (payload.get("resultList") or {}).get("result") or []
        if not results or not results[0].get("pmcid"):
            return {"ok": False, "reason": f"PMID {raw} has no open-access full text in Europe PMC."}
        pmcid = results[0]["pmcid"]

    xml = await http.request_text(f"{API}/{pmcid}/fullTextXML", source="Europe PMC")
    return {"ok": True, "pmcid": pmcid, "text": _sections(xml)}
