"""Provider registry with import-time auto-discovery.

`@register` puts a provider in the registry as soon as its module is imported, and
`discover()` imports every module in this package. Together they mean adding a file is
the whole installation step: no list in `server.py` to update, no `__init__` to edit, no
central dispatch table to keep in sync with reality.

That indirection is worth it because a central list is exactly where this kind of code
rots: someone adds a provider, forgets the list, and the source is silently dead. Here,
a provider that exists is a provider that is reachable, or it failed `validate()` loudly
at startup.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import sys
from typing import Iterable, Type

from .base import Capability, Provider

log = logging.getLogger(__name__)

_REGISTRY: dict[str, Provider] = {}
_DISCOVERED = False


class ProviderError(RuntimeError):
    """A lookup that could not be satisfied, with a message fit to show the model."""


def register(cls: Type[Provider]) -> Type[Provider]:
    """Class decorator that adds a provider to the registry and validates it."""
    meta = getattr(cls, "meta", None)
    if meta is None:
        raise TypeError(f"{cls.__name__} has no `meta`; every provider needs a ProviderMeta.")

    instance = cls()
    instance.validate()

    if meta.name in _REGISTRY:
        existing = type(_REGISTRY[meta.name]).__name__
        raise ValueError(f"Duplicate provider name {meta.name!r}: {existing} and {cls.__name__}.")
    if not meta.enabled:
        # Registered but unreachable: a deliberate off-switch rather than a deletion.
        return cls
    _REGISTRY[meta.name] = instance
    return cls


def discover(force: bool = False) -> None:
    """Import every module in the providers package so `@register` runs.

    Import failures are logged and skipped rather than raised: one broken provider module
    — a missing optional dependency, say — must not take down the whole server.
    """
    global _DISCOVERED
    if _DISCOVERED and not force:
        return
    package = importlib.import_module(__package__)

    # pkgutil caches the directory listing through the package's path finder, so a module
    # added while the process is running would be missed. Invalidating the caches makes
    # discovery reflect the filesystem — which is what "drop in a file" promises, and what
    # lets the test suite prove it.
    importlib.invalidate_caches()
    for finder_cache in list(sys.path_importer_cache.values()):
        if hasattr(finder_cache, "invalidate_caches"):
            try:
                finder_cache.invalidate_caches()
            except Exception:  # noqa: BLE001 - a stubborn finder must not stop discovery
                pass

    for module in pkgutil.iter_modules(package.__path__):
        if module.name.startswith("_") or module.name == "base":
            continue
        full_name = f"{__package__}.{module.name}"
        try:
            # A module already in sys.modules will not re-run its @register, so import it
            # if it is new and otherwise leave it alone. Force only controls whether we
            # sweep again, not whether modules are re-executed: reloading a provider would
            # rebind it to a fresh copy of this registry and silently duplicate state.
            if full_name not in sys.modules:
                importlib.import_module(full_name)
        except Exception as exc:  # noqa: BLE001 - a bad provider must not break the server
            log.warning("Skipping provider module %s: %s", module.name, exc)
    _DISCOVERED = True


def all_providers() -> list[Provider]:
    discover()
    return list(_REGISTRY.values())


def active_providers(*, include_deprecated: bool = False) -> list[Provider]:
    """Providers eligible to serve requests, best-reliability first."""
    discover()
    items = [p for p in _REGISTRY.values() if include_deprecated or p.meta.deprecated is None]
    return sorted(items, key=lambda p: -p.meta.reliability)


def with_capability(capability: Capability, *, include_deprecated: bool = False) -> list[Provider]:
    discover()
    return [p for p in active_providers(include_deprecated=include_deprecated) if p.has(capability)]


def auto_search_providers() -> list[Provider]:
    """The set an unqualified multi-source search fans out to."""
    return [p for p in with_capability(Capability.SEARCH) if p.meta.included_in_auto()]


def get(name: str) -> Provider | None:
    """Resolve a provider by slug, label, or alias, case-insensitively."""
    discover()
    key = (name or "").strip().lower()
    if not key:
        return None
    if key in _REGISTRY:
        return _REGISTRY[key]
    for provider in _REGISTRY.values():
        candidates = {provider.meta.label.lower(), *(a.lower() for a in provider.meta.aliases)}
        if key in candidates:
            return provider
    return None


def resolve_many(names: Iterable[str]) -> tuple[list[Provider], list[str]]:
    """Resolve a list of names, returning the providers found and the names that missed."""
    found: list[Provider] = []
    missing: list[str] = []
    for name in names:
        provider = get(name)
        if provider is None:
            missing.append(name)
        elif provider not in found:
            found.append(provider)
    return found, missing


def known_names() -> list[str]:
    discover()
    return sorted(_REGISTRY)


def _reset_for_tests() -> None:
    """Clear the registry. Tests build their own provider sets with this."""
    _REGISTRY.clear()


def _register_for_tests(provider: Provider) -> None:
    """Insert an already-built provider instance, bypassing the class decorator."""
    _REGISTRY[provider.meta.name] = provider
