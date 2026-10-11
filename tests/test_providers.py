"""Tests for the provider architecture itself.

This is the file that keeps the refactor honest. The whole point of the registry is that a
provider is a self-contained module, so these tests assert exactly that: capabilities route
correctly, a malformed provider fails loudly instead of silently, and no central list has
to be maintained by hand.
"""

from __future__ import annotations

import pytest

from papersearch_mcp import providers as reg
from papersearch_mcp.models import Paper
from papersearch_mcp.providers import Capability, Provider, ProviderMeta
from papersearch_mcp.providers import registry as registry_module


@pytest.fixture(autouse=True)
def clean_registry():
    """Each test gets an empty registry, so built-ins never leak into the fixtures."""
    saved = dict(registry_module._REGISTRY)
    registry_module._reset_for_tests()
    yield
    registry_module._reset_for_tests()
    registry_module._REGISTRY.update(saved)


@pytest.fixture()
def rediscover():
    """Re-populate the registry from the shipped provider modules.

    `@register` runs at import time, so once a module is in sys.modules a cleared registry
    stays empty. Rebinding the classes is enough here: the modules are already imported and
    their classes still reference the live registry object.
    """
    from papersearch_mcp.providers import (
        arxiv,
        crossref,
        doaj,
        europe_pmc,
        openalex,
        semantic_scholar,
        unpaywall,
    )

    def repopulate():
        registry_module._reset_for_tests()
        for module in (openalex, arxiv, crossref, europe_pmc, semantic_scholar, doaj, unpaywall):
            for value in vars(module).values():
                if isinstance(value, type) and issubclass(value, Provider) and value is not Provider:
                    registry_module._register_for_tests(value())

    return repopulate


def make_provider(name="demo", label="Demo", caps=(Capability.SEARCH,), **meta_kwargs):
    """Build a provider whose declared capabilities are all implemented."""

    class Demo(Provider):
        meta = ProviderMeta(
            name=name,
            label=label,
            homepage="https://example.org",
            summary="test",
            capabilities=frozenset(caps),
            **meta_kwargs,
        )

        async def search(self, query, limit, from_year):
            return [Paper(title="Demo Paper", source="Demo")]

        async def lookup(self, identifier):
            return Paper(title="Demo Paper", source="Demo")

        async def citations(self, doi, direction, limit):
            return []

        async def fulltext_link(self, doi):
            return {"ok": False}

        async def fulltext_text(self, identifier, max_chars):
            return {"ok": False}

        async def download(self, paper, destination):
            return {"ok": False}

        async def similar(self, paper, limit):
            return []

        async def browse(self, topic, limit):
            return []

    return Demo()


# --------------------------------------------------------------------------- registration

def test_register_adds_a_provider_to_the_registry():
    demo = make_provider()
    registry_module._register_for_tests(demo)
    assert reg.get("demo") is demo
    # known_names() triggers discovery, which may add the shipped providers alongside it.
    assert "demo" in reg.known_names()


def test_register_rejects_a_duplicate_name():
    registry_module._register_for_tests(make_provider())

    with pytest.raises(ValueError, match="Duplicate provider name"):

        @reg.register
        class Clashing(Provider):
            meta = ProviderMeta(
                name="demo",
                label="Other",
                homepage="",
                summary="",
                capabilities=frozenset({Capability.SEARCH}),
            )

            async def search(self, query, limit, from_year):
                return []


def test_register_rejects_a_class_without_meta():
    with pytest.raises(TypeError, match="has no `meta`"):

        @reg.register
        class NoMeta(Provider):
            async def search(self, query, limit, from_year):
                return []


def test_disabled_provider_is_not_registered():
    @reg.register
    class Off(Provider):
        meta = ProviderMeta(
            name="off",
            label="Off",
            homepage="",
            summary="",
            capabilities=frozenset({Capability.SEARCH}),
            enabled=False,
        )

        async def search(self, query, limit, from_year):
            return []

    assert reg.get("off") is None, "enabled=False is an off-switch, so it must not be reachable"


# --------------------------------------------------------------------------- validation

def test_validate_rejects_a_capability_that_is_not_implemented():
    """The bug this catches: meta claims DOWNLOAD, no download() exists, and the mistake
    would otherwise surface as NotImplementedError in front of a user."""
    with pytest.raises(TypeError, match=r"claims Capability\.DOWNLOAD but does not override download"):

        @reg.register
        class Liar(Provider):
            meta = ProviderMeta(
                name="liar",
                label="Liar",
                homepage="",
                summary="",
                capabilities=frozenset({Capability.SEARCH, Capability.DOWNLOAD}),
            )

            async def search(self, query, limit, from_year):
                return []


def test_validate_rejects_a_provider_with_no_capabilities():
    with pytest.raises(TypeError, match="declares no capabilities"):

        @reg.register
        class Empty(Provider):
            meta = ProviderMeta(name="empty", label="Empty", homepage="", summary="", capabilities=frozenset())


def test_validate_accepts_a_correctly_declared_provider():
    make_provider().validate()  # must not raise


# --------------------------------------------------------------------------- routing

def test_with_capability_returns_only_providers_that_declare_it():
    registry_module._register_for_tests(make_provider("searcher", "Searcher", (Capability.SEARCH,)))
    registry_module._register_for_tests(make_provider("graph", "Graph", (Capability.SEARCH, Capability.CITATIONS)))

    assert {p.meta.name for p in reg.with_capability(Capability.CITATIONS)} == {"graph"}
    assert {p.meta.name for p in reg.with_capability(Capability.SEARCH)} == {"searcher", "graph"}


def test_with_capability_sorts_by_reliability():
    registry_module._register_for_tests(make_provider("low", "Low", reliability=10))
    registry_module._register_for_tests(make_provider("high", "High", reliability=99))
    assert [p.meta.name for p in reg.with_capability(Capability.SEARCH)] == ["high", "low"]


def test_deprecated_providers_are_excluded_unless_asked_for():
    """A source that lost its free tier is marked, not deleted: it should stop being used
    while still being explainable."""
    registry_module._register_for_tests(make_provider("alive", "Alive", reliability=90))
    registry_module._register_for_tests(make_provider("dead", "Dead", reliability=90, deprecated="free tier withdrawn 2026"))

    assert {p.meta.name for p in reg.with_capability(Capability.SEARCH)} == {"alive"}
    assert {p.meta.name for p in reg.with_capability(Capability.SEARCH, include_deprecated=True)} == {"alive", "dead"}


# --------------------------------------------------------------------------- auto-search

def test_auto_search_includes_only_reliable_providers_by_default():
    registry_module._register_for_tests(make_provider("reliable", "Reliable", reliability=90))
    registry_module._register_for_tests(make_provider("flaky", "Flaky", reliability=20))
    assert {p.meta.name for p in reg.auto_search_providers()} == {"reliable"}


def test_auto_search_can_be_overridden_per_provider():
    registry_module._register_for_tests(make_provider("forced", "Forced", reliability=5, auto_search=True))
    registry_module._register_for_tests(make_provider("excluded", "Excluded", reliability=99, auto_search=False))
    assert {p.meta.name for p in reg.auto_search_providers()} == {"forced"}


def test_auto_search_excludes_deprecated_even_when_forced():
    registry_module._register_for_tests(make_provider("forced", "Forced", reliability=5, auto_search=True))
    registry_module._register_for_tests(make_provider("dead", "Dead", reliability=99, auto_search=True, deprecated="gone"))
    assert {p.meta.name for p in reg.auto_search_providers()} == {"forced"}


def test_auto_search_only_returns_search_capable_providers():
    registry_module._register_for_tests(make_provider("resolver", "Resolver", (Capability.FULLTEXT_LINK,), reliability=99))
    assert reg.auto_search_providers() == []


# --------------------------------------------------------------------------- resolution

def test_get_resolves_by_name_label_and_alias_case_insensitively():
    registry_module._register_for_tests(make_provider("openalex", "OpenAlex", aliases=("oa", "open-alex")))
    for key in ("openalex", "OpenAlex", "OPENALEX", "oa", "open-alex"):
        assert reg.get(key) is not None, f"{key} should resolve"


def test_get_returns_none_for_unknown_or_blank_names():
    assert reg.get("nope") is None
    assert reg.get("") is None
    assert reg.get("   ") is None


def test_resolve_many_reports_the_names_that_missed():
    registry_module._register_for_tests(make_provider("known", "Known"))
    found, missing = reg.resolve_many(["known", "unknown", "known"])
    assert [p.meta.name for p in found] == ["known"], "duplicates collapse"
    assert missing == ["unknown"]


# --------------------------------------------------------------------------- real providers

def test_the_shipped_providers_all_register_and_validate(rediscover):
    "The registry auto-discovery must find every provider module in the package."
    rediscover()
    names = set(reg.known_names())
    expected = {"openalex", "arxiv", "crossref", "europe-pmc", "semantic-scholar", "doaj", "unpaywall"}
    missing = expected - names
    assert not missing, f"providers failed to register: {missing}"


def test_every_shipped_provider_declares_a_capability_it_implements(rediscover):
    "Discovery logs and skips a malformed provider, so absence is the symptom."
    rediscover()
    providers = reg.all_providers()
    assert providers, "discovery found nothing at all"
    for provider in providers:
        provider.validate()


def test_shipped_providers_have_unique_labels(rediscover):
    "Paper.source carries the label, and merge precedence keys on it."
    rediscover()
    labels = [p.meta.label for p in reg.all_providers()]
    assert labels, "discovery found nothing"
    assert len(labels) == len(set(labels)), f"duplicate labels: {labels}"


def test_discovery_is_driven_by_the_filesystem_not_a_central_list():
    """The architectural claim, tested.

    A central list rots: someone adds a provider, forgets the list, and the source is
    silently dead. This pins the invariant that discovery walks the package directory, so
    the set of providers is the set of modules.
    """
    import pkgutil
    from pathlib import Path

    package = Path(registry_module.__file__).parent
    on_disk = {
        name for _, name, _ in pkgutil.iter_modules([str(package)]) if not name.startswith("_") and name not in {"base", "registry"}
    }
    assert on_disk == {
        "arxiv",
        "crossref",
        "doaj",
        "europe_pmc",
        "openalex",
        "semantic_scholar",
        "unpaywall",
    }, f"provider modules on disk changed: {sorted(on_disk)} — add the new one to this list on purpose"
