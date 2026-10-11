# academic-research-mcp

An MCP server for academic literature that needs **no API key at all**. Search papers, follow
citations, find related work, locate a free copy, read open-access text, and save PDFs —
using only public sources.

Built for AI agents that need to cite real papers instead of inventing them.

## Why no key

Every source here is public infrastructure: OpenAlex, arXiv, Crossref, Europe PMC, DOAJ,
Unpaywall. No signup, no quota, nothing to leak. One optional contact address lets you join
the "polite pools" that OpenAlex and Crossref reserve for identified callers.

## Tools

| Tool | What it does |
|---|---|
| `search_papers` | Multi-source search, deduplicated. The main entry point. |
| `get_paper` | Full record for a DOI, arXiv id, PMID, PMCID, or OpenAlex id. |
| `find_similar` | "More like this" — and says whether it used computed relatedness or a keyword fallback. |
| `get_citations` | Who cites a paper, or what it cites. |
| `find_fulltext` | A free, legal copy of a paywalled paper. |
| `read_fulltext` | Body text of an open-access article, by section. |
| `download_pdf` | Save a paper's PDF locally and return the path. |
| `list_sources` | What is available right now, and what each source can do. |

Eight tools, deliberately. Research on MCP server design puts practical tool-selection
accuracy around 10–15 tools per context, so this stays well inside that and does not add a
tool where an argument would do.

## Install

```bash
uvx --from git+https://github.com/EgiStr/academic-research-mcp academic-research-mcp
```

Once published to PyPI this shortens to `uvx academic-research-mcp`.

### Claude Desktop / Claude Code

```json
{
  "mcpServers": {
    "academic-research": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/EgiStr/academic-research-mcp", "academic-research-mcp"],
      "env": {
        "RESEARCH_MCP_MAILTO": "you@example.org"
      }
    }
  }
}
```

See `examples/` for ready-made configs, including one for dxmie-slide.

### HTTP transport

```bash
RESEARCH_MCP_TRANSPORT=streamable-http academic-research-mcp
```

## Configuration

**Important:** some MCP hosts spawn the server with no inherited environment, so an env var
you set in your shell may never arrive. Configuration is therefore read from two places, and
a config file works even when the environment does not.

Resolution order, first hit wins: the environment, then a config file.

Config file locations, most specific first:

1. `$RESEARCH_MCP_CONFIG` if set
2. `./.academic-research-mcp.env` (the project directory)
3. `~/.academic-research-mcp/config.env`

```ini
# ~/.academic-research-mcp/config.env
RESEARCH_MCP_MAILTO=you@example.org
RESEARCH_MCP_STORAGE=/data/papers
RESEARCH_MCP_TIMEOUT=30
```

| Variable | Default | Purpose |
|---|---|---|
| `RESEARCH_MCP_MAILTO` | *(empty)* | Contact address for the polite pools. Only `find_fulltext` and `download_pdf` need it. |
| `RESEARCH_MCP_STORAGE` | `~/.academic-research-mcp` | Root for downloads; PDFs land in `papers/` below it. |
| `RESEARCH_MCP_TIMEOUT` | `25` | Per-request timeout in seconds. |
| `RESEARCH_MCP_MIN_INTERVAL` | `0.34` | Minimum gap between requests to the same host. |
| `RESEARCH_MCP_CACHE_TTL` | `300` | How long a response is reused. |
| `RESEARCH_MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http`. |

## Adding a source

The architecture is two layers, which is what makes a source a file rather than a change.

**Layer 1** (`search.py`, `server.py`) holds the tools. No tool names a source. Each asks the
registry for providers that declare the capability it needs.

**Layer 2** (`providers/`) holds one module per source, each declaring what it can do.

So adding a source is three steps and touches no existing file:

**1. Write the module.**

```python
# src/academic_research_mcp/providers/zenodo.py
from .. import http
from ..models import Paper, year_str
from .base import Capability, Provider, ProviderMeta
from .registry import register


@register                       # this call is the whole integration
class Zenodo(Provider):
    meta = ProviderMeta(
        name="zenodo",
        label="Zenodo",
        homepage="https://developers.zenodo.org",
        summary="Open repository for datasets, software and preprints.",
        capabilities=frozenset({Capability.SEARCH, Capability.LOOKUP}),
        reliability=75,          # 0-100; drives both merge order and auto-search membership
    )

    async def search(self, query: str, limit: int, from_year: int | None) -> list[Paper]:
        payload = await http.request_json(
            "https://zenodo.org/api/records",
            {"q": query, "size": limit},
            source=self.meta.label,
        )
        return [self._to_paper(item) for item in payload.get("hits", {}).get("hits", [])]

    async def lookup(self, identifier: str) -> Paper | None:
        ...

    @staticmethod
    def _to_paper(item: dict) -> Paper:
        return Paper(
            title=item["metadata"]["title"],
            source="Zenodo",
            year=year_str(item["metadata"].get("publication_date")),
            doi=item.get("doi"),
            url=item.get("links", {}).get("self_html"),
            is_open_access=True,
        )
```

**2. Nothing else.** The registry discovers modules with `pkgutil.iter_modules`, so the new
file is live on the next start. There is no central list to update — which is the point: a
central list is exactly what someone forgets to edit.

**3. Run `uv run pytest`.** `test_providers.py` asserts that every module on disk registers,
that no provider claims a capability it does not implement, and that the set of modules is
the set you expect. Adding a file will fail that last test until you update its expected list
— deliberately, so a new source is a conscious act.

### Capabilities

Declare only what you implement. `validate()` runs at import and **fails loudly** if the two
disagree, rather than surfacing as `NotImplementedError` in front of a user:

```
Unpaywall claims Capability.DOWNLOAD but does not override download().
Either implement it or drop the capability from meta.
```

| Capability | Powers | Method to implement |
|---|---|---|
| `SEARCH` | `search_papers` | `search(query, limit, from_year)` |
| `LOOKUP` | `get_paper` | `lookup(identifier)` |
| `CITATIONS` | `get_citations` | `citations(doi, direction, limit)` |
| `SIMILAR` | `find_similar` | `similar(paper, limit)` |
| `FULLTEXT_LINK` | `find_fulltext` | `fulltext_link(doi)` |
| `FULLTEXT_TEXT` | `read_fulltext` | `fulltext_text(identifier, max_chars)` |
| `DOWNLOAD` | `download_pdf` | `download(paper, destination)` |
| `BROWSE` | *(available, unused)* | `browse(topic, limit)` |

### The `reliability` score

One number, two jobs:

- **Merge order.** When two sources return the same paper, the higher-reliability one leads
  the merged record, so its richer metadata wins.
- **Auto-search membership.** A provider scoring below 60 is excluded from `source="auto"`
  but stays available by name. This is how Semantic Scholar is handled: excellent coverage,
  but keyless requests get rate-limited, so it is opt-in rather than dragging down every
  search.

### Removing or pausing a source

Deleting a module removes it completely. When you want to keep the code — a source that lost
its free tier, or one you are testing — mark it instead:

```python
meta = ProviderMeta(
    ...,
    enabled=False,                              # not registered at all
    deprecated="free tier withdrawn 2026",      # registered but hidden from search
)
```

A deprecated provider keeps its code, drops out of `list_sources`, contributes a reason
instead of results, and never enters auto-search. That is a better answer than a silent
failure or a deleted file, because the next person can see *why*.

## Design notes

**One source failing is not "no results".** Every fan-out returns partial results plus the
failure, so the model sees both. An empty set and a dead source look identical otherwise.

**Config is read when used, not when imported.** An earlier version cached `MAILTO` at import
time; a host that spawned the server without an environment then reported "no contact address
set" while the user was looking at theirs. Values are resolved per call now, which is what
lets a config file work at all.

**Similarity is labelled honestly.** `find_similar` distinguishes computed relatedness
(OpenAlex related works, Semantic Scholar recommendations) from a keyword fallback on the
title. They are not equally good evidence and the output never implies they are.

**Downloaded PDFs are never overwritten.** Filenames are derived from author/year/title with
a numeric suffix on collision, and only responses with real PDF magic bytes are kept — a
paywall landing page returns HTTP 200 with HTML, and that is not a paper.

**Outbound URLs are checked.** `download_pdf` accepts a URL, so private and link-local
addresses are refused before the request, including a public hostname that resolves to one.
Redirects are re-validated; response size is capped.

**One module per source is the testing seam.** Parsers are pure input→`Paper`, so they are
tested directly, offline, and a renamed upstream field shows up as a unit test failure rather
than a bad answer in a conversation.

## Ide tool tambahan

Kandidat untuk ditambahkan, dengan alasan mengapa belum ada. Delapan tool saat ini sudah
mendekati batas praktis akurasi pemilihan tool (~10–15 per konteks), jadi tambahan layak
dipertimbangkan hanya jika jelas lebih berguna daripada menambah kebingungan.

**Paling layak ditambahkan**

- **`compare_papers`** — beri 2–5 identifier, kembalikan tabel perbandingan: tahun, venue,
  sitasi, metode, dan perbedaan. Menjawab "mana yang lebih kuat dasarnya?" dalam satu
  panggilan alih-alih 5 panggilan `get_paper` yang hasilnya harus dirakit sendiri oleh model.
- **`find_seminal`** — diberi topik, kembalikan paper paling berpengaruh (sitasi tinggi,
  tahun lama) untuk topik itu. Ini yang biasanya benar-benar dicari orang, dan saat ini harus
  disusun manual dari `search_papers` + mengurutkan sitasi.
- **`author_papers`** — semua publikasi seorang penulis, terurut. `search_papers` dengan nama
  penulis mencampur penulis senama; ini bisa memakai endpoint penulis OpenAlex yang
  terdisambiguasi.
- **`export_bibtex`** — ekspor hasil pencarian terakhir sebagai BibTeX/RIS. Menutup celah
  terakhir antara agent dan penulisan sitasi nyata; model tidak perlu mengarang format.

**Berguna tapi ada syaratnya**

- **`find_open_version_of`** — versi gratis dari paper berbayar *yang dipegang pengguna*,
  bukan hanya DOI. Butuh input file, di luar cakupan saat ini.
- **`watch_topic`** — paper baru untuk topik tertentu sejak tanggal X. Sumber MCP bersifat
  *pull*, bukan *push*; ini sebenarnya lebih cocok jadi skrip terjadwal yang memanggil server.
- **`cite_context`** — potongan teks di mana sebuah paper dikutip. Butuh layanan full-text
  berbayar (Scite, Dimensions).
- **`similar_by_abstract`** — cari mirip dari teks abstrak/paragraf, bukan dari paper.
  Membutuhkan embedding, yang berarti model atau vektor store eksternal.

**Sengaja tidak ditambahkan**

- **`save_to_library`** — manajemen state pengguna, bukan pencarian. Lebih baik jadi alat
  terpisah.
- **`ai_summarize`** — meringkas adalah pekerjaan model yang memanggil server ini. Server yang
  meringkas akan menambah satu lapis API berbayar dan kehilangan sifat "tanpa key".
- **Tool per sumber** (`search_arxiv`, `search_openalex`, ...) — persis pemborosan yang
  dihindari arsitektur ini: satu tool dengan argumen `source` menggantikannya, dan menambah
  sumber tidak menambah tool.



- **Coverage varies by field.** OpenAlex is broad, arXiv is preprints, Europe PMC is
  biomedicine. For humanities, Crossref and OpenAlex are the strongest here.
- **`read_fulltext` is life-sciences only**, because Europe PMC is the only source here with
  machine-readable article text.
- **`find_fulltext` needs `RESEARCH_MCP_MAILTO`.** Unpaywall requires a caller identity; it is
  free and it only asks who is asking.
- **`related_works` is occasionally wrong.** OpenAlex returned an unrelated record for one
  test paper. The tool names its source so you can judge; it does not invent confidence.
- **Keyless is not unthrottled.** Requests are serialised per host with a minimum interval and
  a short cache. Heavy parallel use will be slower than a keyed API, by design.
- **Citation counts differ between sources.** They are reported as each source gives them and
  never averaged.

## Development

```bash
uv sync --extra dev
uv run pytest              # offline, fast — network is mocked
uv run pytest -m live      # opt-in; hits the real APIs
uv run ruff check src tests
uv build
```

`-m live` is the alarm for upstream change: it fails when a source renames a field or drops a
free tier, which is exactly when you want to know.

## License

MIT
