"""Shared fixtures.

Kept in conftest so both the tool tests and the provider tests can build against a known
registry state, without either file owning the other's setup.
"""

from __future__ import annotations

import pytest

from papersearch_mcp import config
from papersearch_mcp.providers import Provider
from papersearch_mcp.providers import registry as registry_module


@pytest.fixture(autouse=True)
def reset_config():
    """Clear the config cache around every test.

    Config is read once per process and cached, so a test that changes the environment would
    otherwise leak its value into every test that follows.
    """
    config.reload_config()
    yield
    config.reload_config()


@pytest.fixture()
def rediscover():
    """Repopulate the registry from the shipped provider modules.

    `@register` runs at import time, so once a module is in sys.modules, clearing the
    registry leaves it empty. These modules are already imported and their classes still
    reference the live registry object, so rebinding fresh instances is enough.
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

    modules = (openalex, arxiv, crossref, europe_pmc, semantic_scholar, doaj, unpaywall)

    def repopulate():
        registry_module._reset_for_tests()
        for module in modules:
            for value in vars(module).values():
                if isinstance(value, type) and issubclass(value, Provider) and value is not Provider:
                    registry_module._register_for_tests(value())

    repopulate()
    return repopulate
