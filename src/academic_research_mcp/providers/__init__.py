"""Provider package.

Import `from .registry import ...` for lookups; the registry discovers provider modules
automatically, so nothing here needs a manual list of providers. That is deliberate — see
`registry.discover()`.
"""

from .base import Capability, Provider, ProviderMeta
from .registry import (
    ProviderError,
    active_providers,
    all_providers,
    auto_search_providers,
    discover,
    get,
    known_names,
    register,
    resolve_many,
    with_capability,
)

__all__ = [
    "Capability",
    "Provider",
    "ProviderError",
    "ProviderMeta",
    "active_providers",
    "all_providers",
    "auto_search_providers",
    "discover",
    "get",
    "known_names",
    "register",
    "resolve_many",
    "with_capability",
]
