"""PDF download and local storage.

Two rules shape this module, both about not becoming a liability:

1. **A download target must be public.** The server must not be usable as a proxy into a
   private network, so every URL is resolved and private/link-local addresses are refused
   *before* the request, and redirects are re-checked rather than followed blindly.

2. **Downloads are bounded and named.** A caller asks for a paper, not for a URL, so files
   are written into a storage directory with a predictable, collision-free name derived from
   the paper — never from untrusted input verbatim.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

import httpx

from . import config, http
from .models import Paper

# Only real PDFs, and not terabytes of them.
MAX_PDF_BYTES = 60 * 1024 * 1024
PDF_MAGIC = b"%PDF"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def storage_dir() -> Path:
    """Where PDFs land. Override with RESEARCH_MCP_STORAGE, in the env or a config file."""
    return config.app_dir() / "papers"


def safe_stem(paper: Paper) -> str:
    """A filesystem-safe name for a paper: first author, year, a title fragment.

    Derived data is normalised and truncated, so a hostile title cannot escape the
    storage directory or collide with a sibling file's name.
    """
    author = paper.authors[0].split()[-1] if paper.authors else "unknown"
    year = str(paper.year or "nd")
    title = unicodedata.normalize("NFKD", paper.title).encode("ascii", "ignore").decode()
    title = _UNSAFE.sub("-", title).strip("-.")[:60] or "paper"
    stem = f"{author}-{year}-{title}".strip("-.")
    # A leading dot would hide the file; a trailing dot is invalid on Windows.
    return stem.lstrip(".") or "paper"


def unique_path(directory: Path, stem: str) -> Path:
    """Never overwrite an existing file; suffix instead so re-downloads are additive."""
    candidate = directory / f"{stem}.pdf"
    index = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{index}.pdf"
        index += 1
    return candidate


async def fetch_pdf(url: str, destination_stem: str, *, source: str = "provider") -> dict[str, Any]:
    """Download `url` into the storage directory, verifying it is actually a PDF.

    Returns a result dict rather than raising, because a failed download is an expected
    outcome the model should be told about, not an exception that aborts the tool call.
    """
    from .security import assert_public_url

    try:
        assert_public_url(url)
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}

    directory = storage_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "reason": f"Cannot create storage directory {directory}: {exc}"}

    target = unique_path(directory, destination_stem)

    client, _ = http._slot()
    try:
        # Stream so a hostile or misconfigured server cannot exhaust memory: bail out the
        # moment the body exceeds the cap, without ever buffering the whole thing.
        async with client.stream(
            "GET",
            url,
            headers={"Accept": "application/pdf,*/*", "User-Agent": http.user_agent()},
            follow_redirects=False,
            timeout=60.0,
        ) as response:
            redirects = 0
            while 300 <= response.status_code < 400 and redirects < 5:
                location = response.headers.get("location")
                if not location:
                    break
                from urllib.parse import urljoin

                next_url = urljoin(str(response.url), location)
                assert_public_url(next_url)
                redirects += 1
                response = await client.send(
                    client.build_request("GET", next_url, headers={"Accept": "application/pdf,*/*"}),
                    stream=True,
                    follow_redirects=False,
                )

            if response.status_code >= 400:
                return {"ok": False, "reason": f"{source}: HTTP {response.status_code} for {url}"}

            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    return {"ok": False, "reason": f"File exceeds the {MAX_PDF_BYTES // (1024 * 1024)} MB limit."}
                chunks.append(chunk)
            body = b"".join(chunks)
    except httpx.HTTPError as exc:
        return {"ok": False, "reason": f"{source}: download failed ({exc.__class__.__name__})"}
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}

    if not body.startswith(PDF_MAGIC):
        # HTML error pages are the usual culprit: a landing page, not a file.
        head = body[:200].decode("utf-8", "replace").replace("\n", " ")
        return {"ok": False, "reason": f"That URL returned {len(body)} bytes that are not a PDF (starts: {head!r})."}

    try:
        target.write_bytes(body)
    except OSError as exc:
        return {"ok": False, "reason": f"Cannot write {target}: {exc}"}

    return {"ok": True, "path": str(target), "bytes": len(body), "url": url, "source": source}
