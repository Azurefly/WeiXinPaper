from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}


def is_loopback_host(host: str) -> bool:
    value = (host or "").strip().strip("[]").lower()
    if value in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def validate_runtime_security(host: str) -> None:
    """Validate the only supported deployment model.

    The bundled HTTP server is deliberately loopback-only. Remote access must be
    provided by a trusted HTTPS reverse proxy on the same machine. This avoids
    ever sending Basic Auth credentials over the bundled plaintext listener.
    """

    if not is_loopback_host(host):
        raise SystemExit(
            "远程监听被拒绝：内置服务仅允许绑定 127.0.0.1/::1。"
            "如需局域网访问，请使用同机可信反向代理提供 HTTPS，并将请求转发到回环地址。"
        )

    public_origin = os.environ.get("STUDIO_PUBLIC_ORIGIN", "").strip().rstrip("/")
    if public_origin:
        parsed = urlsplit(public_origin)
        if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise SystemExit("STUDIO_PUBLIC_ORIGIN 必须是无用户信息的 HTTPS Origin")
        if not os.environ.get("STUDIO_AUTH_USER") or not os.environ.get("STUDIO_AUTH_PASSWORD"):
            raise SystemExit("启用 STUDIO_PUBLIC_ORIGIN 时必须同时配置 STUDIO_AUTH_USER 和 STUDIO_AUTH_PASSWORD")
