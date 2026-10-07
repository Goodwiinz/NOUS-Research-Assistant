"""Destination guard for server-initiated HTTP calls to user-supplied URLs.

Rejects anything that is not public HTTPS: non-https schemes, URL credentials,
``localhost`` names, and literal or resolved addresses that are not globally
routable (loopback, RFC 1918, CGNAT 100.64/10, link-local including the cloud
metadata address 169.254.169.254, ULA fc00::/7, site-local fec0::/10, multicast, reserved,
unspecified). IPv6 forms that embed an IPv4 address (IPv4-mapped, 6to4,
Teredo, NAT64 64:ff9b::/96) are checked against the embedded address too.
A hostname is rejected if any of its resolved addresses is non-public.

This is a pre-flight check: the HTTP client resolves the name again, so
callers must also disable redirects. It does not pin the resolved address.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Callable, Iterable
from urllib.parse import urlsplit

Resolver = Callable[[str], Iterable[str]]

_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


class UnsafeDestinationError(ValueError):
    """Raised when a URL must not be contacted from the server."""


def _default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    embedded: list[ipaddress.IPv4Address] = []
    if ip.ipv4_mapped is not None:
        embedded.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        embedded.append(ip.sixtofour)
    if ip.teredo is not None:
        embedded.extend(ip.teredo)
    if ip in _NAT64_PREFIX:
        embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return embedded


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # Python marks deprecated site-local addresses as global, although
        # networks can still route them to internal services.
        if ip.is_site_local:
            return False
        return all(_is_public_ip(inner) for inner in _embedded_ipv4(ip))
    return True


def _is_public(address: str) -> bool:
    # Strip an IPv6 zone id ("fe80::1%eth0"); scoped addresses are never public.
    if "%" in address:
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return _is_public_ip(ip)


def assert_public_https_url(url: str, *, resolver: Resolver | None = None) -> None:
    """Raise UnsafeDestinationError unless ``url`` is https to a public address.

    ``resolver`` maps a hostname to its addresses; it defaults to a blocking
    ``socket.getaddrinfo`` call, so async callers should run this in a thread.
    """
    if not url or not isinstance(url, str):
        raise UnsafeDestinationError("empty webhook url")
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError as exc:
        raise UnsafeDestinationError("malformed webhook url") from exc
    if parts.scheme != "https":
        raise UnsafeDestinationError("webhook url must use https")
    if parts.username or parts.password:
        raise UnsafeDestinationError("webhook url must not embed credentials")
    if not host:
        raise UnsafeDestinationError("webhook url has no host")
    bare_host = host.rstrip(".").lower()
    if bare_host == "localhost" or bare_host.endswith(".localhost"):
        raise UnsafeDestinationError("webhook url targets localhost")

    # Literal IP: check directly. Hostname: every resolved address must be public.
    addresses: list[str]
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        resolve = resolver or _default_resolver
        try:
            addresses = list(resolve(host))
        except (OSError, UnicodeError) as exc:
            raise UnsafeDestinationError("webhook host did not resolve") from exc
        if not addresses:
            raise UnsafeDestinationError("webhook host did not resolve")

    for address in addresses:
        if not _is_public(address):
            raise UnsafeDestinationError("webhook url resolves to a non-public address")
