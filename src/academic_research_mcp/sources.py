"""Search across keyless scholarly sources.

Every source here is free and needs no API key. Each adapter takes a query and returns
normalised `Paper` records; nothing outside this package knows a source's wire format.
"""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

from . import http
from .models import Paper, truncate, year_str

# Sources usable without any credential. `openalex` is the default because it has the
# widest coverage and reports citation counts; the others fill specific gaps.
SourceFn = Callable[[str, int, int | None], Awaitable[list[Paper]]]
LookupFn = Callable[[str], Awaitable[Paper | None]]

OPENALEX = "openalex"
ARXIV = "arxiv"
SEMANTIC_SCHOLAR = "semantic-scholar"
CROSSREF = "crossref"
EUROPE_PMC = "europe-pmc"
DOAJ = "doaj"

SOURCE_NAMES = (OPENALEX, ARXIV, SEMANTIC_SCHOLAR, CROSSREF, EUROPE_PMC, DOAJ)

# Sources that answer a DOI lookup with a full record.
DOI_SOURCES = (OPENALEX, CROSSREF, SEMANTIC_SCHOLAR, EUROPE_PMC)


# --------------------------------------------------------------------------------------
# OpenAlex — https://docs.openalex.org
# --------------------------------------------------------------------------------------

def _openalex_params(query: str, limit: int, from_year: int | None, mailto_key: str) -> dict[str, Any]:
    params: dict[str, Any] = {"search": query, "per-page": limit}
    if from_year:
        params["filter"] = f"from_publication_date:{from_year}-01-01"
    if http.MAILTO:
        params[mailto_key] = http.MAILTO
    return params


def _openalex_abstract(inverted: dict[str, list[int]] | None) -> str | None:
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


def _openalex_paper(work: dict[str, Any]) -> Paper:
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
        abstract=truncate(_openalex_abstract(work.get("abstract_inverted_index")), 2000),
        keywords=[k["display_name"] for k in work.get("keywords", [])[:10] if k.get("display_name")],
        language=work.get("language"),
        publication_type=(work.get("type_crossref") or work.get("type")),
        source_id=work.get("id"),
        extra={"openalex_id": work.get("id")},
    )


async def openalex_search(query: str, limit: int = 10, from_year: int | None = None) -> list[Paper]:
    payload = await http.request_json(
        "https://api.openalex.org/works",
        _openalex_params(query, limit, from_year, "mailto"),
        source="OpenAlex",
    )
    return [_openalex_paper(w) for w in payload.get("results", [])]


async def openalex_get(identifier: str) -> Paper | None:
    """identifier: a DOI, an OpenAlex id, or a full work URL."""
    raw = identifier.strip()
    if re.fullmatch(r"W\d+", raw):
        url = f"https://api.openalex.org/works/{raw}"
    elif raw.startswith("http"):
        url = raw
    else:
        url = f"https://api.openalex.org/works/doi:{raw.removeprefix('https://doi.org/').removeprefix('doi:')}"
    params = {"mailto": http.MAILTO} if http.MAILTO else None
    payload = await http.request_json(url, params, source="OpenAlex")
    return _openalex_paper(payload) if payload else None


# --------------------------------------------------------------------------------------
# arXiv — https://info.arxiv.org/help/api
# --------------------------------------------------------------------------------------

_ATOM_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.DOTALL)
_ATOM_TAG = {
    "title": re.compile(r"<title>(.*?)</title>", re.DOTALL),
    "summary": re.compile(r"<summary>(.*?)</summary>", re.DOTALL),
    "published": re.compile(r"<published>(.*?)</published>", re.DOTALL),
    "id": re.compile(r"<id>(.*?)</id>", re.DOTALL),
    "doi": re.compile(r'<arxiv:doi[^>]*>(.*?)</arxiv:doi>', re.DOTALL),
    "journal": re.compile(r'<arxiv:journal_ref[^>]*>(.*?)</arxiv:journal_ref>', re.DOTALL),
}


def _unescape(text: str) -> str:
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&apos;", "'")
        .replace("&amp;", "&")
    )


def _parse_arxiv(xml: str) -> list[Paper]:
    """Deliberately a small regex parser: arXiv's Atom feed is stable and this avoids a
    heavyweight XML dependency."""
    papers: list[Paper] = []
    for block in _ATOM_ENTRY.findall(xml):
        def tag(name: str) -> str | None:
            match = _ATOM_TAG[name].search(block)
            return _unescape(" ".join(match.group(1).split())) if match else None

        title = tag("title")
        if not title:
            continue
        authors = [_unescape(" ".join(a.split())) for a in re.findall(r"<name>(.*?)</name>", block, re.DOTALL)]
        entry_id = tag("id") or ""
        arxiv_id = entry_id.rsplit("/", 1)[-1] if entry_id else None
        doi = tag("doi")
        papers.append(
            Paper(
                title=title,
                source="arXiv",
                authors=authors[:20],
                year=year_str(tag("published")),
                venue=tag("journal") or "arXiv preprint",
                doi=doi,
                url=entry_id or None,
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else None,
                is_open_access=True,
                abstract=truncate(tag("summary"), 2000),
                publication_type="preprint",
                source_id=arxiv_id,
                extra={"arxiv_id": arxiv_id},
            )
        )
    return papers


async def arxiv_search(query: str, limit: int = 10, from_year: int | None = None) -> list[Paper]:
    terms = f"all:{query}"
    if from_year:
        # arXiv's submittedDate range is coarse; YYYYMMDDHHMM form is required.
        terms = f"({terms}) AND submittedDate:[{from_year}01010000 TO 999912312359]"
    xml = await http.request_text(
        "https://export.arxiv.org/api/query",
        {"search_query": terms, "start": 0, "max_results": limit, "sortBy": "relevance"},
        source="arXiv",
    )
    return _parse_arxiv(xml)


async def arxiv_get(identifier: str) -> Paper | None:
    raw = identifier.strip()
    # Accept "2401.12345", "2401.12345v2", or an abs/pdf URL.
    match = re.search(r"(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)", raw)
    if not match:
        return None
    xml = await http.request_text("https://export.arxiv.org/api/query", {"id_list": match.group(1)}, source="arXiv")
    papers = _parse_arxiv(xml)
    return papers[0] if papers else None


# --------------------------------------------------------------------------------------
# Semantic Scholar — https://api.semanticscholar.org/api-docs
# --------------------------------------------------------------------------------------

_S2_FIELDS = "title,year,authors,venue,citationCount,influentialCitationCount,externalIds,url,abstract,openAccessPdf,publicationTypes,fieldsOfStudy"


def _s2_paper(paper: dict[str, Any]) -> Paper:
    external = paper.get("externalIds") or {}
    pdf = paper.get("openAccessPdf") or {}
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
        extra={k: v for k, v in external.items() if k in ("ArXiv", "PubMed", "CorpusId")},
    )


async def semantic_scholar_search(query: str, limit: int = 10, from_year: int | None = None) -> list[Paper]:
    params: dict[str, Any] = {"query": query, "limit": limit, "fields": _S2_FIELDS}
    if from_year:
        params["year"] = f"{from_year}-"
    payload = await http.request_json(
        "https://api.semanticscholar.org/graph/v1/paper/search",
        params,
        source="Semantic Scholar",
    )
    return [_s2_paper(p) for p in payload.get("data", [])]


async def semantic_scholar_get(identifier: str) -> Paper | None:
    raw = identifier.strip()
    if raw.lower().startswith("doi:"):
        paper_id = raw
    elif raw.lower().startswith("10."):
        paper_id = f"DOI:{raw}"
    elif re.fullmatch(r"ARXIV:.*", raw, re.IGNORECASE):
        paper_id = raw.upper()
    else:
        paper_id = raw
    paper_id = paper_id.replace("ARXIV:", "ARXIV:").replace("arxiv:", "ARXIV:")
    payload = await http.request_json(
        f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}",
        {"fields": _S2_FIELDS},
        source="Semantic Scholar",
    )
    return _s2_paper(payload) if payload else None


# --------------------------------------------------------------------------------------
# Crossref — https://api.crossref.org
# --------------------------------------------------------------------------------------

def _crossref_date(item: dict[str, Any]) -> int | None:
    for key in ("published-print", "published-online", "issued", "created"):
        parts = ((item.get(key) or {}).get("date-parts") or [[None]])[0]
        if parts and parts[0]:
            return year_str(parts[0])
    return None


def _crossref_paper(item: dict[str, Any]) -> Paper:
    authors = []
    for a in item.get("author", [])[:20]:
        name = " ".join(filter(None, [a.get("given"), a.get("family")])) or a.get("name")
        if name:
            authors.append(name)
    links = item.get("link") or []
    pdf_url = next((l.get("URL") for l in links if l.get("content-type") == "application/pdf"), None)
    return Paper(
        title=(item.get("title") or ["Untitled"])[0].strip(),
        source="Crossref",
        authors=authors,
        year=_crossref_date(item),
        venue=(item.get("container-title") or [None])[0],
        citations=item.get("is-referenced-by-count"),
        doi=item.get("DOI"),
        url=item.get("URL") or (f"https://doi.org/{item['DOI']}" if item.get("DOI") else None),
        pdf_url=pdf_url,
        abstract=truncate(re.sub(r"<[^>]+>", " ", item.get("abstract") or ""), 2000),
        keywords=(item.get("subject") or [])[:10],
        language=item.get("language"),
        publication_type=item.get("type"),
        source_id=item.get("DOI"),
        extra={"publisher": item.get("publisher")},
    )


async def crossref_search(query: str, limit: int = 10, from_year: int | None = None) -> list[Paper]:
    # `select` is validated against a per-route allowlist: an unknown field returns HTTP 400,
    # not a silently ignored one. Keep this list to fields /works actually supports.
    params: dict[str, Any] = {
        "query": query,
        "rows": limit,
        "select": "DOI,title,author,issued,container-title,is-referenced-by-count,URL,type,subject,abstract,link,published-print,published-online,publisher",
    }
    if from_year:
        params["filter"] = f"from-pub-date:{from_year}-01-01"
    if http.MAILTO:
        params["mailto"] = http.MAILTO
    payload = await http.request_json("https://api.crossref.org/works", params, source="Crossref")
    return [_crossref_paper(i) for i in (payload.get("message") or {}).get("items", [])]


async def crossref_get(doi: str) -> Paper | None:
    payload = await http.request_json(
        f"https://api.crossref.org/works/{doi.strip().removeprefix('https://doi.org/')}",
        {"mailto": http.MAILTO} if http.MAILTO else None,
        source="Crossref",
    )
    item = payload.get("message") if payload else None
    return _crossref_paper(item) if item else None


# --------------------------------------------------------------------------------------
# Europe PMC — https://europepmc.org/RestfulWebService  (life sciences, keyless)
# --------------------------------------------------------------------------------------

def _epmc_paper(item: dict[str, Any]) -> Paper:
    authors = (item.get("authorString") or "").rstrip(".").split(", ") if item.get("authorString") else []
    abstract = item.get("abstractText")
    if abstract:
        abstract = re.sub(r"<[^>]+>", " ", abstract)
    return Paper(
        title=(item.get("title") or "Untitled").strip().rstrip("."),
        source="Europe PMC",
        authors=[a for a in authors if a][:20],
        year=year_str(item.get("pubYear")),
        venue=item.get("journalTitle") or None,
        citations=int(item["citedByCount"]) if str(item.get("citedByCount", "")).isdigit() else None,
        doi=item.get("doi"),
        url=(f"https://doi.org/{item['doi']}" if item.get("doi") else (f"https://europepmc.org/article/{item.get('source', 'MED')}/{item.get('id')}" if item.get("id") else None)),
        pdf_url=(f"https://europepmc.org/api/fulltextRepo?pprId={item['id']}&type=FILE&fileName=EMS.pdf" if item.get("isOpenAccess") == "Y" else None),
        is_open_access=item.get("isOpenAccess") == "Y",
        abstract=truncate(abstract, 2000),
        publication_type=item.get("pubType"),
        source_id=item.get("id"),
        extra={k: item.get(k) for k in ("pmid", "pmcid") if item.get(k)},
    )


async def europe_pmc_search(query: str, limit: int = 10, from_year: int | None = None) -> list[Paper]:
    query = f"({query}) AND (PUB_YEAR:[{from_year} TO 3000])" if from_year else query
    payload = await http.request_json(
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        {"query": query, "format": "json", "pageSize": limit, "resultType": "core", "sort": "CITED desc"},
        source="Europe PMC",
    )
    return [_epmc_paper(i) for i in ((payload.get("resultList") or {}).get("result") or [])]


async def europe_pmc_get(identifier: str) -> Paper | None:
    raw = identifier.strip()
    if raw.lower().startswith("pmid:"):
        query = f"EXT_ID:{raw.split(':', 1)[1]}"
    elif raw.lower().startswith("pmcid:"):
        query = raw.split(":", 1)[1]
    elif raw.lower().startswith("doi:"):
        query = f"DOI:\"{raw.split(':', 1)[1]}\""
    elif raw.lower().startswith("10."):
        query = f'DOI:"{raw}"'
    else:
        query = raw
    payload = await http.request_json(
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        {"query": query, "format": "json", "pageSize": 1, "resultType": "core"},
        source="Europe PMC",
    )
    results = ((payload.get("resultList") or {}).get("result") or [])
    return _epmc_paper(results[0]) if results else None


# --------------------------------------------------------------------------------------
# DOAJ — https://doaj.org/api/docs  (open-access journals only, keyless)
# --------------------------------------------------------------------------------------

def _as_name(value: Any) -> str | None:
    """Some sources nest a name as {"name": ...}, others send a bare string."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        return value.get("name") or None
    return None


def _doaj_paper(item: dict[str, Any]) -> Paper:
    bib = item.get("bibjson") or {}
    identifiers = {i.get("type"): i.get("id") for i in bib.get("identifier", [])}
    links = bib.get("link") or []
    journal = bib.get("journal") or {}
    authors = []
    for a in bib.get("author", [])[:20]:
        name = a.get("name") or " ".join(filter(None, [a.get("given"), a.get("family")]))
        if name:
            authors.append(name)
    fulltext = next((l.get("url") for l in links if l.get("type") == "fulltext"), None)
    language = journal.get("language")
    return Paper(
        title=(bib.get("title") or "Untitled").strip(),
        source="DOAJ",
        authors=authors,
        year=year_str(bib.get("year")),
        venue=_as_name(journal.get("title")),
        citations=None,
        doi=identifiers.get("doi"),
        url=fulltext or (f"https://doi.org/{identifiers['doi']}" if identifiers.get("doi") else item.get("id")),
        pdf_url=fulltext,
        is_open_access=True,
        abstract=truncate(re.sub(r"<[^>]+>", " ", bib.get("abstract") or ""), 2000),
        keywords=[k for k in (bib.get("keywords") or [])][:10],
        language=language if isinstance(language, str) else None,
        source_id=item.get("id"),
        extra={"publisher": _as_name(journal.get("publisher"))},
    )


async def doaj_search(query: str, limit: int = 10, from_year: int | None = None) -> list[Paper]:
    search = f'bibjson.title:({query}) OR bibjson.abstract:({query})'
    if from_year:
        search = f"({search}) AND bibjson.year:[{from_year} TO 3000]"
    payload = await http.request_json(
        "https://doaj.org/api/search/articles/" + httpx_quote(search),
        {"pageSize": limit},
        source="DOAJ",
    )
    return [_doaj_paper(r) for r in payload.get("results", [])]


def httpx_quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


# --------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------

SEARCHERS: dict[str, SourceFn] = {
    OPENALEX: openalex_search,
    ARXIV: arxiv_search,
    SEMANTIC_SCHOLAR: semantic_scholar_search,
    CROSSREF: crossref_search,
    EUROPE_PMC: europe_pmc_search,
    DOAJ: doaj_search,
}

LOOKUPS: dict[str, LookupFn] = {
    OPENALEX: openalex_get,
    ARXIV: arxiv_get,
    SEMANTIC_SCHOLAR: semantic_scholar_get,
    CROSSREF: crossref_get,
    EUROPE_PMC: europe_pmc_get,
}

SOURCE_NOTES: dict[str, str] = {
    OPENALEX: "OpenAlex — ~250M works, all disciplines, citation counts. Best default.",
    ARXIV: "arXiv — physics, maths, CS, quantitative biology preprints. Always open access, no citation counts.",
    SEMANTIC_SCHOLAR: "Semantic Scholar — AI/CS strength, citation counts and influential-citation counts.",
    CROSSREF: "Crossref — publisher-deposited metadata for journal articles, books, conference papers.",
    EUROPE_PMC: "Europe PMC — life sciences and biomedicine, includes full-text links and PubMed IDs.",
    DOAJ: "DOAJ — open-access journals only; every record is free to read.",
}

# Human label per source adapter, used to rank merged results and to build the sort order.
DISPLAY_NAME: dict[str, str] = {
    OPENALEX: "OpenAlex",
    ARXIV: "arXiv",
    SEMANTIC_SCHOLAR: "Semantic Scholar",
    CROSSREF: "Crossref",
    EUROPE_PMC: "Europe PMC",
    DOAJ: "DOAJ",
}

# When several sources return the same paper, the copy from the lowest-ranked source wins
# the merge — so richer metadata (citations, abstracts) leads the output.
DISPLAY_RANK: dict[str, int] = {name: i for i, name in enumerate(DISPLAY_NAME.values())}


async def search_one(source: str, query: str, limit: int, from_year: int | None) -> list[Paper]:
    return await SEARCHERS[source](query, limit, from_year)
