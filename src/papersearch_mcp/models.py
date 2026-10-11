"""The one record shape every source is normalised into."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Author:
    name: str
    orcid: str | None = None


@dataclass
class Paper:
    """A normalised scholarly record.

    Fields absent from a given source stay None/empty rather than being invented, so a
    caller can always tell "this source did not report citations" from "zero citations".
    """

    title: str
    source: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    citations: int | None = None
    doi: str | None = None
    url: str | None = None
    pdf_url: str | None = None
    is_open_access: bool | None = None
    abstract: str | None = None
    keywords: list[str] = field(default_factory=list)
    language: str | None = None
    publication_type: str | None = None
    source_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Drop empty fields so tool output stays compact for the model."""
        raw = asdict(self)
        out: dict[str, Any] = {}
        for key, value in raw.items():
            if value is None or value == [] or value == {}:
                continue
            out[key] = value
        return out


def truncate(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def year_str(value: Any) -> int | None:
    """Coerce the many year encodings (int, str, '2024-05-01') into an int."""
    if value is None:
        return None
    if isinstance(value, int):
        return value if 1400 < value < 2200 else None
    match = str(value)[:4]
    return int(match) if match.isdigit() and 1400 < int(match) < 2200 else None
