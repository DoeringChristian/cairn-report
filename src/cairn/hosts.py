"""Host names for what Cairn binds and prints: the local addresses, and the name other machines reach this one by."""

from __future__ import annotations

import socket

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
WILDCARDS = ("0.0.0.0", "::", "")


def reachable(host: str) -> str:
    """The host to print in a URL for something bound to `host`: this machine's name for a wildcard bind
    (`0.0.0.0` is not an address anyone can connect to), else `host` itself."""
    if host in WILDCARDS:
        return socket.gethostname() or "127.0.0.1"
    return host


def url_host(host: str) -> str:
    """`reachable(host)`, bracketed when it is an IPv6 address."""
    name = reachable(host)
    return f"[{name}]" if ":" in name else name
