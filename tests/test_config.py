"""Tests for configuration resolution.

These guard a regression that was invisible until the server ran under a real MCP host: the
host spawned it with no environment at all, so every keyless_research_mcp_* value the user set was
ignored and `find_fulltext` reported "no contact address" while the user looked right at
theirs. Configuration therefore has to work without the environment.
"""

from __future__ import annotations

import pytest

from keyless_research_mcp import config, download, http


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv("KEYLESS_RESEARCH_MCP_MAILTO", raising=False)
    config.reload_config()
    yield
    config.reload_config()


def test_environment_is_read(monkeypatch):
    monkeypatch.setenv("KEYLESS_RESEARCH_MCP_MAILTO", "env@example.org")
    config.reload_config()
    assert http.mailto() == "env@example.org"


def test_config_file_is_read_when_the_environment_is_absent(tmp_path, monkeypatch):
    """The MCP-host case: no passed environment, config on disk."""
    cfg = tmp_path / "config.env"
    cfg.write_text("KEYLESS_RESEARCH_MCP_MAILTO=file@example.org\n", encoding="utf-8")
    monkeypatch.setenv("KEYLESS_RESEARCH_MCP_CONFIG", str(cfg))
    config.reload_config()
    assert http.mailto() == "file@example.org"


def test_environment_overrides_the_config_file(tmp_path, monkeypatch):
    cfg = tmp_path / "config.env"
    cfg.write_text("KEYLESS_RESEARCH_MCP_MAILTO=file@example.org\n", encoding="utf-8")
    monkeypatch.setenv("KEYLESS_RESEARCH_MCP_CONFIG", str(cfg))
    monkeypatch.setenv("KEYLESS_RESEARCH_MCP_MAILTO", "env@example.org")
    config.reload_config()
    assert http.mailto() == "env@example.org"


def test_config_file_ignores_comments_blanks_and_export_prefix(tmp_path, monkeypatch):
    cfg = tmp_path / "config.env"
    cfg.write_text(
        "# a comment\n"
        "\n"
        "KEYLESS_RESEARCH_MCP_MAILTO=first@example.org\n"
        "export KEYLESS_RESEARCH_MCP_STORAGE=/tmp/exported\n"
        "NOT_PREFIXED=ignored\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYLESS_RESEARCH_MCP_CONFIG", str(cfg))
    config.reload_config()
    assert http.mailto() == "first@example.org"
    assert str(config.app_dir()).endswith("exported")


def test_config_file_tolerates_quotes(tmp_path, monkeypatch):
    cfg = tmp_path / "config.env"
    cfg.write_text('KEYLESS_RESEARCH_MCP_MAILTO="quoted@example.org"\n', encoding="utf-8")
    monkeypatch.setenv("KEYLESS_RESEARCH_MCP_CONFIG", str(cfg))
    config.reload_config()
    assert http.mailto() == "quoted@example.org"


def test_a_missing_config_file_is_not_an_error(monkeypatch):
    monkeypatch.setenv("KEYLESS_RESEARCH_MCP_CONFIG", "/nonexistent/config.env")
    config.reload_config()
    assert http.mailto() == ""


def test_mailto_is_reread_after_a_reload(monkeypatch):
    """Not cached at import: a value written after startup must take effect."""
    assert http.mailto() == ""
    monkeypatch.setenv("KEYLESS_RESEARCH_MCP_MAILTO", "late@example.org")
    config.reload_config()
    assert http.mailto() == "late@example.org"


def test_numeric_values_fall_back_when_unparseable(monkeypatch):
    monkeypatch.setenv("KEYLESS_RESEARCH_MCP_TIMEOUT", "not-a-number")
    config.reload_config()
    assert http.default_timeout() == 25.0


def test_storage_defaults_under_the_home_directory(monkeypatch):
    monkeypatch.delenv("KEYLESS_RESEARCH_MCP_STORAGE", raising=False)
    monkeypatch.setattr(config, "config_paths", list)
    config.reload_config()
    assert download.storage_dir() == config.Path.home() / ".keyless-research-mcp" / "papers"


def test_user_agent_carries_the_contact_address(monkeypatch):
    """Polite pools route by this; an anonymous agent gets throttled harder."""
    monkeypatch.setenv("KEYLESS_RESEARCH_MCP_MAILTO", "ua@example.org")
    config.reload_config()
    assert "mailto:ua@example.org" in http.user_agent()


def test_user_agent_omits_an_absent_contact_address(monkeypatch):
    monkeypatch.delenv("KEYLESS_RESEARCH_MCP_MAILTO", raising=False)
    config.reload_config()
    assert "mailto:" not in http.user_agent()
