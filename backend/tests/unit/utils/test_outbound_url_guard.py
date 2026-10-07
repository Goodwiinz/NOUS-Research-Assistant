"""Tests for the outbound webhook destination guard (GOO-409 / F1)."""

from __future__ import annotations

import pytest

from src.utils.outbound_url_guard import UnsafeDestinationError, assert_public_https_url


def _public(host: str) -> list[str]:
    return ["93.184.216.34"]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/hook",  # scheme
        "ftp://example.com/hook",
        "file:///etc/passwd",
        "https://127.0.0.1/hook",
        "https://localhost/hook",
        "https://api.localhost/hook",
        "https://10.0.0.5/hook",
        "https://172.16.3.4/hook",
        "https://192.168.1.1/hook",
        "https://100.64.0.1/hook",  # CGNAT / shared address space
        "https://169.254.169.254/latest/meta-data",
        "https://224.0.0.1/hook",  # multicast
        "https://[::1]/hook",
        "https://[fd00::1]/hook",
        "https://[fe80::1]/hook",
        "https://[::ffff:127.0.0.1]/hook",  # IPv4-mapped loopback
        "https://[::ffff:a9fe:a9fe]/hook",  # IPv4-mapped metadata
        "https://[64:ff9b::a00:1]/hook",  # NAT64 embedding 10.0.0.1
        "https://0.0.0.0/hook",
        "https://user:pw@example.com/hook",  # credentials in URL
        "not a url",
        "",
    ],
)
def test_rejects_unsafe_destinations(url: str) -> None:
    with pytest.raises(UnsafeDestinationError):
        assert_public_https_url(url, resolver=_public)


def test_rejects_hostname_resolving_to_private_ip() -> None:
    with pytest.raises(UnsafeDestinationError):
        assert_public_https_url(
            "https://internal.example.com/hook", resolver=lambda host: ["10.1.2.3"]
        )


def test_rejects_hostname_with_mixed_resolution() -> None:
    # One public, one private answer: must reject (split answers).
    with pytest.raises(UnsafeDestinationError):
        assert_public_https_url(
            "https://mixed.example.com/hook",
            resolver=lambda host: ["93.184.216.34", "127.0.0.1"],
        )


@pytest.mark.parametrize(
    "address", ["fec0::1", "fedc::1", "feff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"]
)
@pytest.mark.parametrize("literal", [True, False])
def test_rejects_site_local_ipv6(address: str, literal: bool) -> None:
    url = f"https://[{address}]/hook" if literal else "https://mixed.example.com/hook"
    with pytest.raises(UnsafeDestinationError):
        assert_public_https_url(url, resolver=lambda host: ["93.184.216.34", address])


def test_accepts_public_ipv6_literal() -> None:
    assert_public_https_url("https://[2001:4860:4860::8888]/hook", resolver=_public)


def test_rejects_empty_resolution() -> None:
    with pytest.raises(UnsafeDestinationError):
        assert_public_https_url("https://empty.example.com/", resolver=lambda host: [])


def test_accepts_public_https() -> None:
    assert_public_https_url("https://hooks.example.com/path?x=1", resolver=_public)


def test_accepts_public_ipv4_mapped_literal() -> None:
    assert_public_https_url("https://[::ffff:5db8:d822]/hook", resolver=_public)


def test_resolver_failure_is_rejected() -> None:
    def failing(host: str) -> list[str]:
        raise OSError("nxdomain")

    with pytest.raises(UnsafeDestinationError):
        assert_public_https_url("https://nope.example.com/", resolver=failing)
