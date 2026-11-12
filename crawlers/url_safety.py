"""Network-bound URL validation shared by every crawler transport."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse


class UnsafeURL(ValueError):
    pass


_SECOND_LEVEL_SUFFIXES = {
    "co.uk", "org.uk", "com.au", "net.au", "co.jp", "co.kr", "or.kr",
    "com.br", "com.cn", "com.sg", "co.nz", "co.za",
}


def registrable_domain(host: str) -> str:
    labels = (host or "").strip(".").lower().split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    suffix2 = ".".join(labels[-2:])
    return ".".join(labels[-3:]) if suffix2 in _SECOND_LEVEL_SUFFIXES else suffix2


def same_site(a: str, b: str) -> bool:
    ah = urlparse(a).hostname if "://" in a else a.split("/")[0]
    bh = urlparse(b).hostname if "://" in b else b.split("/")[0]
    return bool(ah and bh and registrable_domain(ah) == registrable_domain(bh))


def _public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def ensure_public_url(url: str, *, resolve: bool = True) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURL("unsupported scheme")
    if parsed.username or parsed.password:
        raise UnsafeURL("credentials in URL are not allowed")
    host = (parsed.hostname or "").strip(".").lower()
    if not host or host == "localhost" or host.endswith((".localhost", ".local")):
        raise UnsafeURL("unsafe URL host")
    try:
        host_is_public_ip = _public_ip(host)
    except ValueError:
        host_is_public_ip = None       # hostname, resolve below
    if host_is_public_ip is False:
        raise UnsafeURL("private or non-routable address")
    if host_is_public_ip is True:
        return url
    if resolve:
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(
                host, parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM)}
        except socket.gaierror as exc:
            raise UnsafeURL(f"host does not resolve: {exc}") from exc
        if not addresses or any(not _public_ip(address) for address in addresses):
            raise UnsafeURL("hostname resolves to a private or non-routable address")
    return url


def get_with_safe_redirects(session, url: str, *, timeout: float,
                            headers: dict | None = None, max_redirects: int = 5,
                            resolve: bool = True):
    """GET while validating DNS and every redirect destination."""
    original = ensure_public_url(url, resolve=resolve)
    current = original
    for _ in range(max_redirects + 1):
        response = session.get(current, timeout=timeout, headers=headers,
                               allow_redirects=False)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        target = ensure_public_url(urljoin(current, response.headers.get("Location", "")),
                                   resolve=resolve)
        if not same_site(original, target):
            raise UnsafeURL("redirect left the submitted company's registrable domain")
        current = target
    raise UnsafeURL("too many redirects")
