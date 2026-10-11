"""URL safety checks shared by the downloader and the full-text fetcher.

The server runs inside a deployment network. Anything that fetches a caller-supplied URL
is therefore a potential pivot into that network: cloud metadata endpoints (169.254.169.254)
and internal services are the classic targets. Every URL is resolved and checked *before*
the request, and redirects are validated individually rather than followed blindly, because
a public hostname can redirect to a private one.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


class UnsafeUrl(ValueError):
    """Raised for a URL the server refuses to fetch."""


def is_private_address(address: str) -> bool:
    """True for any address that is not globally routable.

    Uses the stdlib's own classification rather than hand-rolled ranges, so loopback,
    RFC1918, link-local, CGNAT, unique-local v6, multicast and reserved blocks are all
    covered — including the cloud metadata addresses.
    """
    try:
        ip = ipaddress.ip_address(address.split("%")[0])  # strip a zone index such as fe80::1%eth0
    except ValueError:
        return True  # unparseable: refuse rather than guess
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return True
    # 100.64.0.0/10 (CGNAT) is not covered by is_private on every Python version.
    if ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"):
        return True
    if ip.version == 6 and ip in ipaddress.ip_network("fc00::/7"):
        return True
    return False


def assert_public_url(url: str) -> None:
    """Validate scheme, host and every resolved address. Raises UnsafeUrl on refusal."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrl(f"Only http and https URLs can be fetched (got {parsed.scheme or 'no'} scheme).")
    if not parsed.hostname:
        raise UnsafeUrl("That URL has no host.")

    for address in resolve(parsed.hostname):
        if is_private_address(address):
            raise UnsafeUrl(f"{parsed.hostname} resolves to the non-public address {address}; refusing to fetch it.")


def resolve(hostname: str) -> list[str]:
    """Resolve to IPs. A literal address is returned as-is; resolution failures are refused."""
    try:
        ipaddress.ip_address(hostname)
        return [hostname]
    except ValueError:
        pass

    import socket

    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise UnsafeUrl(f"Cannot resolve {hostname}: {exc}") from exc
    return sorted({info[4][0] for info in infos})
