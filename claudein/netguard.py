"""Who may connect at all (plan 4.1): this PC, home networks and Tailscale. Everyone else is dropped."""

from __future__ import annotations

import ipaddress

_ALLOWED = [
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8",
        "::1/128",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fe80::/10",
        "fc00::/7",
        "100.64.0.0/10",  # Tailscale
        "fd7a:115c:a1e0::/48",  # Tailscale
    )
]


def _parse(address: str | None):
    if not address:
        return None
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop an IPv6 zone such as %12
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def allowed(address: str | None) -> bool:
    ip = _parse(address)
    return ip is not None and any(ip in net for net in _ALLOWED)


def is_loopback(address: str | None) -> bool:
    ip = _parse(address)
    return ip is not None and ip.is_loopback
