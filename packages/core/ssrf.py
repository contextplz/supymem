"""Block private / link-local / metadata destinations (docs/supymem/20)."""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

BLOCKED_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)

BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "instance-data",
    }
)


class UnsafeRemoteUrl(ValueError):
    """URL is not a public HTTP(S) destination."""


def _blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if os.environ.get("SUPYMEM_MODE", "").strip().lower() == "local":
        from core.research_web.guard import _blocked_reason

        if _blocked_reason(getattr(ip, "ipv4_mapped", None) or ip) is not None:
            return True
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
        return True
    if ip.is_unspecified:
        return True
    return any(ip in net for net in BLOCKED_NETWORKS)


def assert_safe_remote_url(url: str) -> str:
    """Raise UnsafeRemoteUrl unless the URL resolves only to public unicast addresses."""
    if os.environ.get("SUPYMEM_MODE", "").strip().lower() == "local":
        try:
            parsed = urlparse(url)
            if (parsed.username is not None or parsed.password is not None or parsed.port == 0
                    or any(ord(character) <= 32 or character == "\\" for character in url)):
                raise ValueError
        except ValueError as exc:
            raise UnsafeRemoteUrl("Choose a public HTTP or HTTPS URL without credentials, then retry.") from exc
    else:
        parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeRemoteUrl("only http/https are allowed")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise UnsafeRemoteUrl("host required")
    if host in BLOCKED_HOSTS or host.endswith(".localhost"):
        raise UnsafeRemoteUrl("blocked host")
    if host == "169.254.169.254" or host.endswith(".internal"):
        raise UnsafeRemoteUrl("blocked host")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and _blocked_ip(literal):
        raise UnsafeRemoteUrl("blocked address")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeRemoteUrl("unresolvable host") from exc
    if not infos:
        raise UnsafeRemoteUrl("unresolvable host")
    for info in infos:
        addr = info[4][0]
        ip = ipaddress.ip_address(addr)
        if _blocked_ip(ip):
            raise UnsafeRemoteUrl("resolved to a blocked address")
    return url
