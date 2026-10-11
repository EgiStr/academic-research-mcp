"""Tests for download_pdf and the URL safety checks behind it.

A server that fetches caller-supplied URLs is a network pivot unless it refuses private
addresses, so these tests attack the guard rather than trusting it.
"""

from __future__ import annotations

import pytest

from keyless_research_mcp import config, download, security
from keyless_research_mcp.models import Paper

# --------------------------------------------------------------------------- security

@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "0.0.0.0",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata: the one that matters most
        "100.64.0.1",  # CGNAT, which is_private misses on older Pythons
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
    ],
)
def test_is_private_address_refuses_every_non_public_range(address):
    assert security.is_private_address(address) is True, f"{address} must be refused"


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_is_private_address_allows_public_addresses(address):
    assert security.is_private_address(address) is False


def test_is_private_address_refuses_an_unparseable_host():
    assert security.is_private_address("not-an-ip") is True


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.org/x.pdf", "javascript:alert(1)", "notaurl"])
def test_assert_public_url_refuses_non_http_schemes(url):
    with pytest.raises(security.UnsafeUrlError):
        security.assert_public_url(url)


def test_assert_public_url_refuses_a_literal_private_address():
    with pytest.raises(security.UnsafeUrlError, match="non-public address"):
        security.assert_public_url("http://169.254.169.254/latest/meta-data/")


def test_assert_public_url_refuses_a_host_resolving_to_a_private_address(monkeypatch):
    """A public-looking name pointing at an internal address must still be refused."""
    monkeypatch.setattr(security, "resolve", lambda hostname: ["10.0.0.5"])
    with pytest.raises(security.UnsafeUrlError, match="non-public address"):
        security.assert_public_url("http://internal.example.com/paper.pdf")


def test_assert_public_url_accepts_a_public_host(monkeypatch):
    monkeypatch.setattr(security, "resolve", lambda hostname: ["93.184.216.34"])
    security.assert_public_url("https://example.org/paper.pdf")  # must not raise


def test_assert_public_url_rejects_a_url_without_a_host():
    with pytest.raises(security.UnsafeUrlError, match="no host"):
        security.assert_public_url("http:///paper.pdf")


# --------------------------------------------------------------------------- filenames

@pytest.mark.parametrize(
    ("title", "expected_fragment"),
    [
        ("Deep Learning: A Review!", "Deep-Learning-A-Review"),
        ("../../etc/passwd", "etc-passwd"),
        ("a" * 200, "a"),
        ("", "paper"),
    ],
)
def test_safe_stem_neutralises_hostile_titles(title, expected_fragment):
    stem = download.safe_stem(Paper(title=title, source="OpenAlex", authors=["Ada Lovelace"], year=2024))
    assert expected_fragment in stem
    assert ".." not in stem, "no path traversal"
    assert "/" not in stem and "\\" not in stem
    assert not stem.startswith(".")
    assert len(stem) <= 120


def test_safe_stem_uses_first_author_surname_and_year():
    stem = download.safe_stem(Paper(title="Graph RAG", source="OpenAlex", authors=["Yuntong Hu"], year=2025))
    assert stem.startswith("Hu-2025-")


def test_safe_stem_handles_a_record_with_no_authors_or_year():
    stem = download.safe_stem(Paper(title="Anonymous Work", source="OpenAlex"))
    assert stem.startswith("unknown-nd-")


def test_unique_path_never_overwrites(tmp_path):
    first = download.unique_path(tmp_path, "paper")
    first.write_bytes(b"%PDF-1.4")
    second = download.unique_path(tmp_path, "paper")
    assert second != first
    assert second.name == "paper-2.pdf"

    second.write_bytes(b"%PDF-1.4")
    assert download.unique_path(tmp_path, "paper").name == "paper-3.pdf"


# --------------------------------------------------------------------------- fetch_pdf

class FakeStream:
    """Stands in for httpx's streaming response context manager."""

    def __init__(self, status_code=200, body=b"%PDF-1.4 content", headers=None, url="https://example.org/x.pdf"):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aiter_bytes(self):
        yield self._body


class FakeClient:
    def __init__(self, stream):
        self._stream = stream

    def stream(self, *args, **kwargs):
        return self._stream

    def build_request(self, *args, **kwargs):
        return None

    async def send(self, request, **kwargs):
        return self._stream


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    """Point the storage root at a temp dir and return the papers/ directory it implies."""
    monkeypatch.setenv("KEYLESS_RESEARCH_MCP_STORAGE", str(tmp_path))
    config.reload_config()
    papers = tmp_path / "papers"
    papers.mkdir(parents=True, exist_ok=True)
    return papers


def patch_client(monkeypatch, stream, *, allow_public=True):
    """Route fetch_pdf at a fake client, and optionally make the URL guard refuse."""
    if allow_public:
        monkeypatch.setattr(security, "assert_public_url", lambda url: None)
    else:

        def blocked(url):
            raise security.UnsafeUrlError("blocked")

        monkeypatch.setattr(security, "assert_public_url", blocked)
    monkeypatch.setattr(download.http, "_slot", lambda: (FakeClient(stream), None))


async def test_fetch_pdf_writes_the_file(storage, monkeypatch):
    patch_client(monkeypatch, FakeStream(body=b"%PDF-1.4 real content"))
    result = await download.fetch_pdf("https://example.org/x.pdf", "MyPaper")
    assert result["ok"] is True
    assert result["bytes"] == len(b"%PDF-1.4 real content")
    written = storage / "MyPaper.pdf"
    assert written.read_bytes() == b"%PDF-1.4 real content"


async def test_fetch_pdf_refuses_a_non_pdf_body(storage, monkeypatch):
    """Landing pages are the usual failure: HTTP 200, HTML body."""
    patch_client(monkeypatch, FakeStream(body=b"<html><body>Paywall</body></html>"))
    result = await download.fetch_pdf("https://example.org/x", "MyPaper")
    assert result["ok"] is False
    assert "not a PDF" in result["reason"]
    assert not list(storage.glob("*.pdf")), "nothing must be written for a non-PDF"


async def test_fetch_pdf_reports_an_http_error(storage, monkeypatch):
    patch_client(monkeypatch, FakeStream(status_code=403, body=b""))
    result = await download.fetch_pdf("https://example.org/x.pdf", "MyPaper")
    assert result["ok"] is False
    assert "403" in result["reason"]


async def test_fetch_pdf_refuses_a_blocked_url_without_writing(storage, monkeypatch):
    patch_client(monkeypatch, FakeStream(), allow_public=False)
    result = await download.fetch_pdf("http://169.254.169.254/x.pdf", "MyPaper")
    assert result["ok"] is False
    assert "blocked" in result["reason"]
    assert not list(storage.glob("*.pdf"))


async def test_fetch_pdf_enforces_the_size_limit(storage, monkeypatch):
    monkeypatch.setattr(download, "MAX_PDF_BYTES", 100)
    patch_client(monkeypatch, FakeStream(body=b"%PDF" + b"x" * 500))
    result = await download.fetch_pdf("https://example.org/big.pdf", "Big")
    assert result["ok"] is False
    assert "limit" in result["reason"]


async def test_fetch_pdf_does_not_overwrite_an_existing_file(storage, monkeypatch):
    (storage / "MyPaper.pdf").write_bytes(b"%PDF-1.4 existing")
    patch_client(monkeypatch, FakeStream(body=b"%PDF-1.4 new"))
    result = await download.fetch_pdf("https://example.org/x.pdf", "MyPaper")
    assert result["ok"] is True
    assert result["path"].endswith("MyPaper-2.pdf")
    assert (storage / "MyPaper.pdf").read_bytes() == b"%PDF-1.4 existing"


async def test_storage_dir_follows_the_environment(monkeypatch, tmp_path):
    """KEYLESS_RESEARCH_MCP_STORAGE is the root; PDFs land one level below it in papers/."""
    monkeypatch.setenv("KEYLESS_RESEARCH_MCP_STORAGE", str(tmp_path))
    config.reload_config()
    storage = download.storage_dir()
    assert storage.parent == tmp_path
    assert storage.name == "papers"
