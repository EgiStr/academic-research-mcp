"""The provider contract.

Every source of papers is a `Provider` subclass in `providers/`. A provider declares what
it *can* do (`Capability`) rather than implementing a fixed set of methods, so the caller
asks "who can search?" instead of assuming everyone can. That is what makes adding and
removing a source a one-file change.

Adding a provider:

    1. Drop a module in `providers/` with a `Provider` subclass.
    2. Decorate it with `@register`.
    3. That's it — the registry discovers it at import time.

Removing one:

    Delete its module, or set `enabled=False` in its `ProviderMeta`. Nothing else refers
    to it by name, so nothing else needs editing.

A provider that once worked and now does not (an API withdrew its free tier, say) should
be marked `deprecated="reason"` rather than deleted: it keeps working if it still can,
it disappears from `list_sources`, and it is skipped by multi-source search — which tells
you *why* rather than silently returning fewer results.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..models import Paper


class Capability(str, enum.Enum):
    """What a provider can do.

    Asking for a capability a provider lacks is a programming error caught at startup by
    `validate()`, not a runtime surprise for the model.
    """

    SEARCH = "search"
    """Free-text search. The only capability required of every provider."""

    LOOKUP = "lookup"
    """Resolve a single identifier (DOI, PMID, arXiv id, ...) to one record."""

    CITATIONS = "citations"
    """Return the works citing a paper, or the paper's reference list."""

    FULLTEXT_LINK = "fulltext_link"
    """Given a DOI, find a free-to-read copy elsewhere (e.g. Unpaywall)."""

    FULLTEXT_TEXT = "fulltext_text"
    """Return the body text of an open-access article."""

    DOWNLOAD = "download"
    """Fetch a PDF into local storage."""

    SIMILAR = "similar"
    """Find papers related to a given one."""

    BROWSE = "browse"
    """List recent papers without a query (e.g. a category feed)."""


@dataclass
class ProviderMeta:
    """Static description of a provider, used for routing and for telling the model."""

    name: str
    """Stable slug used in tool arguments and config, e.g. "openalex"."""

    label: str
    """Human name shown in results, e.g. "OpenAlex". `Paper.source` carries this."""

    homepage: str
    """Where the API is documented."""

    summary: str
    """One line describing coverage; shown by list_sources."""

    capabilities: frozenset[Capability]
    """What this provider implements. Must match the methods it actually defines."""

    requires_mailto: bool = False
    """Whether a contact address is required (not merely polite) to work at all."""

    reliability: int = 50
    """0-100, how dependable the free tier is. Decides merge precedence and auto-search
    membership: sources that rate-limit keyless callers hard score low and stay opt-in."""

    min_interval: float | None = None
    """Per-provider override for the gap between requests, in seconds, for hosts that
    need more courtesy than the global default."""

    enabled: bool = True
    """Set False to disable a provider without deleting its code."""

    deprecated: str | None = None
    """If set, the provider is skipped by multi-source search and list_sources shows the
    reason. Use for a source that lost its free tier rather than deleting it."""

    aliases: tuple[str, ...] = field(default_factory=tuple)
    """Alternative names accepted in tool arguments, e.g. ("s2", "semanticscholar")."""

    auto_search: bool | None = None
    """Whether "auto" fans out to this provider. Defaults to `reliability >= 60`."""

    def included_in_auto(self) -> bool:
        if self.auto_search is not None:
            return self.auto_search
        return self.reliability >= 60 and self.deprecated is None


class Provider:
    """Base class. Subclasses define only the methods for the capabilities they claim."""

    meta: ClassVar[ProviderMeta]

    # ------------------------------------------------------------------ capabilities

    async def search(self, query: str, limit: int, from_year: int | None) -> list[Paper]:
        raise NotImplementedError

    async def lookup(self, identifier: str) -> Paper | None:
        raise NotImplementedError

    async def citations(self, doi: str, direction: str, limit: int) -> list[Paper]:
        raise NotImplementedError

    async def fulltext_link(self, doi: str) -> dict[str, Any]:
        raise NotImplementedError

    async def fulltext_text(self, identifier: str, max_chars: int) -> dict[str, Any]:
        raise NotImplementedError

    async def download(self, paper: Paper, destination: str) -> dict[str, Any]:
        raise NotImplementedError

    async def similar(self, paper: Paper, limit: int) -> list[Paper]:
        raise NotImplementedError

    async def browse(self, topic: str, limit: int) -> list[Paper]:
        raise NotImplementedError

    # ------------------------------------------------------------------ helpers

    def has(self, capability: Capability) -> bool:
        return capability in self.meta.capabilities

    def validate(self) -> None:
        """Fail fast if meta and implementation disagree.

        A capability claimed but not implemented would surface as a NotImplementedError in
        front of a user; catching it at import time makes the mistake obvious and local.
        """
        for capability in self.meta.capabilities:
            method = _METHOD_FOR[capability]
            if getattr(type(self), method) is getattr(Provider, method):
                raise TypeError(
                    f"{type(self).__name__} claims Capability.{capability.name} but does not "
                    f"override {method}(). Either implement it or drop the capability from meta."
                )
        if not self.meta.capabilities:
            raise TypeError(f"{type(self).__name__} declares no capabilities; at minimum Capability.SEARCH.")


_METHOD_FOR: dict[Capability, str] = {
    Capability.SEARCH: "search",
    Capability.LOOKUP: "lookup",
    Capability.CITATIONS: "citations",
    Capability.FULLTEXT_LINK: "fulltext_link",
    Capability.FULLTEXT_TEXT: "fulltext_text",
    Capability.DOWNLOAD: "download",
    Capability.SIMILAR: "similar",
    Capability.BROWSE: "browse",
}
