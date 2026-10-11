# academic-research-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server for searching and reading academic literature — **with no API keys, ever**.

Point it at an MCP client and the model can search papers, resolve a DOI, follow citations, and read open-access full text. Every source it uses is public and free, so there is nothing to sign up for and no credential to leak.

```bash
uvx academic-research-mcp
```

## Why it has no API keys

The usual academic APIs (Semantic Scholar, Scopus) need a key to be useful. This server is built on sources that do not:

| Source | Coverage | Notes |
|---|---|---|
| **OpenAlex** | ~250M works, all disciplines | Default. Citation counts, open-access flags |
| **arXiv** | Physics, maths, CS, quant bio | Preprints; always open access |
| **Crossref** | Publisher-deposited metadata | Journals, books, conference papers |
| **Europe PMC** | Life sciences, biomedicine | Full-text links, PubMed IDs |
| **Semantic Scholar** | AI/CS strength | See the rate-limit note below |
| **DOAJ** | Open-access journals | Every record is free to read |

The only thing any of them asks for is a contact address, used for the "polite pool" and by Unpaywall. Set it once:

```bash
RESEARCH_MCP_MAILTO=you@example.org
```

Without it everything still works except `find_fulltext`, which tells you so instead of failing quietly.

> **A note on Semantic Scholar.** Keyless requests to it are frequently rate-limited (HTTP 429). That is why it is *not* in the default multi-source search — name it explicitly when you want it. One rate-limited source never sinks a search: the others still return, and the failure is reported alongside the results.

## Tools

| Tool | What it does |
|---|---|
| `search_papers` | Multi-source search, deduplicated. `source="auto"` fans out over four sources and merges duplicates |
| `get_paper` | Full record for a DOI, arXiv id, PMID, PMCID, or OpenAlex id. Routes by identifier shape |
| `get_citations` | The papers citing a work, or the work's reference list |
| `find_fulltext` | A free, legal copy of a paywalled paper (Unpaywall) |
| `read_fulltext` | Body text of an open-access article, by section |
| `list_sources` | What is available and how to query it |

Results are returned as compact prose, not JSON — the model cites from it directly:

```
[1] GRAG: Graph Retrieval-Augmented Generation
    2025 · Yuntong Hu, Zhihan Lei, Zheng Zhang, Bo Pan et al. · Findings of the ACL: NAACL
    source OpenAlex · 78 citations · open access · conference-paper
    url: https://doi.org/10.18653/v1/2025.findings-naacl.232
    pdf: https://aclanthology.org/2025.findings-naacl.232.pdf
    doi:10.18653/v1/2025.findings-naacl.232
    abstract: Naive Retrieval-Augmented Generation (RAG) focuses on individual documents…
```

## Install

### Any MCP client

```json
{
  "mcpServers": {
    "academic-research": {
      "command": "uvx",
      "args": ["academic-research-mcp"],
      "env": {
        "RESEARCH_MCP_MAILTO": "you@example.org"
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add academic-research --env RESEARCH_MCP_MAILTO=you@example.org -- uvx academic-research-mcp
```

### From a clone

```bash
git clone https://github.com/EgiStr/academic-research-mcp
cd academic-research-mcp
uv sync
uv run academic-research-mcp
```

### HTTP transport

For a remote deployment, switch off stdio:

```bash
RESEARCH_MCP_TRANSPORT=streamable-http uv run academic-research-mcp
```

This serves `/mcp` on `127.0.0.1:8000`. Put authentication in front of it before exposing it — the server itself is unauthenticated by design, since it holds no secrets.

## Development

```bash
uv sync --extra dev
uv run pytest              # unit tests, fully offline
uv run pytest -m live      # tests against the real APIs (opt-in)
uv run mcp dev src/academic_research_mcp/server.py   # MCP Inspector
```

The split matters. Unit tests mock the network and cover the logic where bugs actually live — parsing and deduplication. The `live` tests exist for a different reason: they catch **upstream field-name drift**. If OpenAlex renames `display_name`, a mock would never notice; `test_source_field_names_have_not_drifted` will.

### Configuration

All optional, all read from the environment:

| Variable | Default | Purpose |
|---|---|---|
| `RESEARCH_MCP_MAILTO` | *(empty)* | Contact address. Required only by `find_fulltext` |
| `RESEARCH_MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `RESEARCH_MCP_TIMEOUT` | `25` | Per-request timeout, seconds |
| `RESEARCH_MCP_MIN_INTERVAL` | `0.34` | Minimum gap between requests to one host |
| `RESEARCH_MCP_CACHE_TTL` | `300` | Response cache lifetime, seconds |

No API keys are read. There is no code path that looks for one.

## Design notes

**Courtesy is enforced, not assumed.** These sources are free, which makes it easy to abuse them. Requests are serialised per host with a minimum interval, cached for five minutes, and sent with a descriptive `User-Agent`. The cache key includes query parameters, so a repeated search costs the API nothing.

**Deduplication is real work.** The same paper comes back from four sources with four identifiers and four slightly different titles. `dedupe.py` merges on DOI where available, otherwise on a normalised title, and keeps the richest field from each copy — the longest author list, the longest abstract, the highest citation count. Without it, a model cites one paper four times.

**Failures are partial, not fatal.** One source being down or rate-limited must not empty a result set. `search_papers` collects per-source errors, returns what it got, and reports what failed in the same response.

**A failed lookup explains itself.** "Nothing found" is useless to a model. Errors say which sources were tried, what each said, and what to do differently.

**One event loop, one client.** The shared `httpx.AsyncClient` and throttle lock are keyed by the running event loop. A cached client bound to a loop that has since closed is a real failure mode — it raises `RuntimeError: Event loop is closed` on the *next* request, which is exactly the kind of bug that only appears in production.

## Limitations

- No full-text search: these sources index metadata and abstracts, not article bodies.
- Citation graph is OpenAlex's; coverage depends on the work being indexed with references.
- `read_fulltext` only works for articles Europe PMC holds in open access.
- Keyless means rate-limited. For heavy automated use, add an API key to your own fork rather than hammering the shared pool.
- Metadata is only as good as the source's: preprints lack citation counts, and Crossref records sometimes have no author list.

## Licence

MIT. The literature it retrieves is not — check each paper's licence before redistributing it. `find_fulltext` links to copies the publisher or a repository has chosen to make free; reading through those links is what it is for.
