"""Configuration, resolved from the environment and then from a config file.

An MCP host spawns this server as a child process, and several hosts pass no `env` at all
(the Python MCP client's `StdioServerParameters(env=None)` does exactly that), so a value
set in the parent's environment never reaches us. Configuration therefore cannot rely on the
environment alone, or `KEYLESS_RESEARCH_MCP_MAILTO` silently does nothing and `find_fulltext` fails
with "no contact address set" while the user is looking right at theirs.

Resolution order, first hit wins:
  1. the real environment
  2. a `.env`-style config file (see `config_paths()`)

A config file also covers the case a host strips the environment deliberately, and keeps
secrets out of a repository's checked-in MCP JSON.
"""

from __future__ import annotations

import os
from pathlib import Path

# Read once per process. `reload_config()` exists for tests and for a host that wants to
# re-read after editing the file.
_values: dict[str, str] | None = None

APP_DIR_NAME = "keyless-research-mcp"
ENV_PREFIX = "KEYLESS_RESEARCH_MCP_"


def config_paths() -> list[Path]:
    """Config files, most specific first."""
    paths: list[Path] = []
    override = os.environ.get(f"{ENV_PREFIX}CONFIG")
    if override:
        paths.append(Path(override).expanduser())
    paths.append(Path.cwd() / ".keyless-research-mcp.env")
    paths.append(Path.home() / f".{APP_DIR_NAME}" / "config.env")
    return paths


def _parse_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines. Deliberately minimal: no interpolation, no quoting games."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = raw.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _load() -> dict[str, str]:
    global _values
    if _values is None:
        merged: dict[str, str] = {}
        # Least specific first so a nearer file overrides a farther one.
        for path in reversed(config_paths()):
            merged.update(_parse_env_file(path))
        merged.update({k: v for k, v in os.environ.items() if k.startswith(ENV_PREFIX)})
        _values = merged
    return _values


def reload_config() -> None:
    """Drop the cache so the next `get()` re-reads the environment and the files."""
    global _values
    _values = None


def get(name: str, default: str = "") -> str:
    """A configuration value by short name, e.g. `get("MAILTO")`."""
    return _load().get(name if name.startswith(ENV_PREFIX) else f"{ENV_PREFIX}{name}", default).strip()


def get_float(name: str, default: float) -> float:
    try:
        return float(get(name, str(default)))
    except ValueError:
        return default


def app_dir() -> Path:
    """The application's own directory: the storage root, where downloads and the default
    config file live. `KEYLESS_RESEARCH_MCP_STORAGE` replaces it outright."""
    configured = get("STORAGE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / f".{APP_DIR_NAME}"


def source_of(name: str) -> str:
    """Where a value came from, for an error message that tells the user what to edit."""
    for path in config_paths():
        if path.exists() and f"{ENV_PREFIX}{name}" in _parse_env_file(path):
            return str(path)
    return "the environment"
