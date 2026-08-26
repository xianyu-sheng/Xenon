"""Network safety primitives shared by read-only network tools.

The network tools deliberately keep URL validation separate from HTTP client
code.  This gives future tool families one small, auditable boundary for SSRF
policy and redirect handling while preserving the old ``tool_node`` exports.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


MAX_REDIRECTS = 5


class SSRFRedirectError(Exception):
    """Raised when a redirect target fails SSRF validation."""


class SecurityError(Exception):
    """Security policy violation raised by tool validation."""


RFC1918_NETWORKS: list[ipaddress._BaseNetwork] = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fc00::/7"),
]


def is_rfc1918_private(ip: ipaddress._BaseAddress) -> bool:
    """Check RFC 1918/RFC 6598 private ranges without overblocking 198.18/15."""
    return any(ip in network for network in RFC1918_NETWORKS)


def is_internal_ip(ip: ipaddress._BaseAddress) -> bool:
    """Return whether an address is not an externally routable destination."""
    return bool(
        ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or is_rfc1918_private(ip)
    )


def resolve_host_ips(host: str) -> list[str]:
    """Resolve a host to all unique IPv4/IPv6 addresses in stable order."""
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    resolved: list[str] = []
    for info in infos:
        ip = info[4][0].split("%", 1)[0]
        if ip not in resolved:
            resolved.append(ip)
    return resolved


SSRF_DOMAIN_ALLOWLIST: frozenset[str] = frozenset(
    {
        "wttr.in",
        "weather.com.cn",
        "api.github.com",
        "raw.githubusercontent.com",
        "httpbin.org",
        "postman-echo.com",
    }
)


def ssrf_check_url(url: str) -> tuple[bool, str]:
    """Validate a URL before opening a network connection."""
    try:
        parsed = urlparse(url)
    except Exception as exc:  # pragma: no cover - urllib rarely raises
        return False, f"URL 解析失败: {exc}"
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"仅允许 http/https 协议，拒绝: {scheme or '(空)'}"
    host = parsed.hostname
    if not host:
        return False, "URL 缺少 host"

    host_lower = host.lower()
    if host_lower in SSRF_DOMAIN_ALLOWLIST or any(
        host_lower.endswith("." + allowed) for allowed in SSRF_DOMAIN_ALLOWLIST
    ):
        return True, ""

    ips = resolve_host_ips(host)
    if not ips:
        return False, f"无法解析 host: {host}"
    for ip_string in ips:
        try:
            ip = ipaddress.ip_address(ip_string)
        except ValueError:
            continue
        if is_internal_ip(ip):
            return False, f"禁止访问内网/保留地址: {host} -> {ip_string}"
    return True, ""


def fetch_with_redirect_check(
    client,
    url: str,
    headers: dict | None = None,
    *,
    check_url=None,
):
    """Follow redirects one hop at a time, validating every Location target.

    ``check_url`` is injectable so compatibility wrappers can preserve their
    historical monkeypatch seam without duplicating redirect policy.
    """
    import httpx

    current = url
    request_headers = headers or {"User-Agent": "Xenon/0.2"}
    checker = check_url or ssrf_check_url
    for _ in range(MAX_REDIRECTS + 1):
        response = client.get(current, headers=request_headers)
        if not response.is_redirect:
            return response
        location = response.headers.get("location", "")
        if not location:
            return response
        next_url = str(httpx.URL(current).join(location))
        ok, reason = checker(next_url)
        if not ok:
            raise SSRFRedirectError(f"{next_url}: {reason}")
        current = next_url
    raise SSRFRedirectError(f"重定向次数超过上限 ({MAX_REDIRECTS})")
