from __future__ import annotations

import http.client
import ipaddress
import os
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

from logger_config import get_logger

logger = get_logger("secure_http")


class SecureHttpError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int | None = None, location: str = ""):
        super().__init__(message)
        self.code = code
        self.status = status
        self.location = location


@dataclass(frozen=True)
class SecureHttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    peer_ip: str


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port=port, timeout=timeout)
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self) -> None:
        raw = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def resolve_public(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SecureHttpError("dns_failed", f"无法解析外部服务域名：{host}") from exc
    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        if not is_public_ip(address):
            raise SecureHttpError("unsafe_address", "外部服务解析到内网、环回或保留地址，已拒绝连接")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise SecureHttpError("dns_empty", "外部服务域名没有可用公网地址")
    return addresses


# ---------------------------------------------------------------------------
# 代理支持
#
# 与 source_fetcher.py 同理:本机配置了 fake-ip 代理(如 Clash/Surge 增强模式)
# 时,socket.getaddrinfo 会把外部域名解析到 198.18.0.0/15 等基准测试网段,
# 被 is_public_ip 判定为保留地址而拒绝连接。
#
# 当探测到本机 HTTP/HTTPS 代理时,改走 urllib + ProxyHandler:由代理负责
# CONNECT 隧道与域名解析,绕开 fake-ip。URL 层面的安全校验(scheme/port/
# hostname)仍然执行,只是跳过本地 DNS 解析和 IP 钉扎。
# ---------------------------------------------------------------------------


def _candidate_proxies() -> list[str]:
    """收集候选代理地址,顺序:环境变量 > macOS 系统配置 > getproxies。"""
    result: list[str] = []
    env_proxy = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )
    if env_proxy:
        result.append(env_proxy)
    sysconf = getattr(urllib.request, "getproxies_macosx_sysconf", None)
    sources: list = []
    if sysconf:
        try:
            sources.append(sysconf())
        except Exception:  # noqa: BLE001
            pass
    try:
        sources.append(urllib.request.getproxies())
    except Exception:  # noqa: BLE001
        pass
    for prox in sources:
        for key in ("https", "http"):
            value = prox.get(key) if isinstance(prox, dict) else None
            if value and value not in result:
                result.append(value)
    return result


def _has_proxy() -> bool:
    return bool(_candidate_proxies())


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁止自动跟随重定向,由调用方决定是否拒绝。"""

    def redirect_request(self, *args, **kwargs):  # noqa: D401, ANN002
        return None


def _request_via_proxy(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 30,
    max_bytes: int = 4_000_000,
    reject_redirects: bool = True,
) -> SecureHttpResponse:
    """经由本地 HTTP 代理发送请求,由代理负责 DNS 解析和 CONNECT 隧道。"""
    proxies = _candidate_proxies()
    if not proxies:
        raise SecureHttpError("connection_failed", "未配置代理且无法直连外部服务")

    ssl_context = ssl.create_default_context()
    ssl_context.load_default_certs()
    last_error: Exception | None = None

    for proxy in proxies:
        try:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            opener = urllib.request.build_opener(
                handler,
                _NoRedirectHandler,
                urllib.request.HTTPSHandler(context=ssl_context),
            )
            req = urllib.request.Request(url, method=method.upper(), data=body)
            for key, value in (headers or {}).items():
                req.add_header(key, value)
            try:
                resp = opener.open(req, timeout=timeout)
                status = resp.status
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            except urllib.error.HTTPError as http_error:
                status = http_error.code
                resp_headers = {k.lower(): v for k, v in (http_error.headers.items() if http_error.headers else [])}
                resp = http_error

            # 重定向拦截
            if 300 <= status < 400 and reject_redirects:
                raise SecureHttpError(
                    "redirect_forbidden",
                    "外部服务返回重定向，已阻止携带凭证继续请求",
                    status=status,
                    location=resp_headers.get("location", ""),
                )

            # 读取响应体(带大小限制)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = resp.read(min(65536, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise SecureHttpError("response_too_large", "外部服务响应超过安全大小限制")

            peer_ip = ""
            try:
                # 尝试获取真实对端 IP（可能不可用）
                if hasattr(resp, "fp") and hasattr(resp.fp, "raw"):
                    sock = getattr(resp.fp.raw, "_sock", None)
                    if sock:
                        peer_ip = sock.getpeername()[0]
            except Exception:  # noqa: BLE001
                pass

            return SecureHttpResponse(status, resp_headers, b"".join(chunks), peer_ip)

        except SecureHttpError:
            raise
        except (urllib.error.URLError, OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = exc
            logger.warning("代理请求失败 (proxy=%s): %s", proxy, exc)
            continue

    raise SecureHttpError("connection_failed", f"无法经代理连接外部服务：{last_error}") from last_error


def request_bytes(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 30,
    max_bytes: int = 4_000_000,
    require_https: bool = True,
    reject_redirects: bool = True,
) -> SecureHttpResponse:
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or (require_https and scheme != "https"):
        raise SecureHttpError("unsafe_scheme", "外部服务地址必须使用 HTTPS")
    if parsed.username or parsed.password or not parsed.hostname:
        raise SecureHttpError("invalid_url", "外部服务地址无效")
    port = parsed.port or (443 if scheme == "https" else 80)
    if port not in ({443} if require_https else {80, 443}):
        raise SecureHttpError("unsafe_port", "外部服务端口不在允许范围")

    # 代理路径:当检测到本机 HTTP/HTTPS 代理时,由代理负责 DNS 解析和 CONNECT 隧道,
    # 绕开 fake-ip 导致的 unsafe_address 误拦截。URL 层面的安全校验已在上方完成。
    if _has_proxy():
        logger.info("检测到代理配置,通过代理发送请求: %s", parsed.hostname)
        return _request_via_proxy(
            url,
            method=method,
            headers=headers,
            body=body,
            timeout=timeout,
            max_bytes=max_bytes,
            reject_redirects=reject_redirects,
        )

    addresses = resolve_public(parsed.hostname, port)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    host_header = parsed.hostname if port in {80, 443} else f"{parsed.hostname}:{port}"
    request_headers = {**dict(headers or {}), "Host": host_header, "Connection": "close"}
    last_error: Exception | None = None
    for ip in addresses:
        conn: http.client.HTTPConnection
        try:
            conn = (
                _PinnedHTTPSConnection(parsed.hostname, ip, port, timeout)
                if scheme == "https"
                else _PinnedHTTPConnection(parsed.hostname, ip, port, timeout)
            )
            conn.request(method.upper(), path, body=body, headers=request_headers)
            response = conn.getresponse()
            if conn.sock:
                peer = conn.sock.getpeername()[0]
                if peer != ip or not is_public_ip(peer):
                    raise SecureHttpError("peer_mismatch", "实际连接地址与安全校验结果不一致")
                conn.sock.settimeout(timeout)
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            if 300 <= response.status < 400 and reject_redirects:
                raise SecureHttpError(
                    "redirect_forbidden",
                    "外部服务返回重定向，已阻止携带凭证继续请求",
                    status=response.status,
                    location=response_headers.get("location", ""),
                )
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(min(65536, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise SecureHttpError("response_too_large", "外部服务响应超过安全大小限制")
            return SecureHttpResponse(response.status, response_headers, b"".join(chunks), ip)
        except SecureHttpError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        finally:
            try:
                conn.close()  # type: ignore[possibly-undefined]
            except Exception:
                pass
    raise SecureHttpError("connection_failed", f"无法安全连接外部服务：{last_error}") from last_error
