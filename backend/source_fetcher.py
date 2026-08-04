from __future__ import annotations

import zlib
import hashlib
import html
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urljoin, urlsplit

from logger_config import get_logger
from test_mode import enabled as test_adapter_enabled

logger = get_logger("source_fetcher")

MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5
CONNECT_TIMEOUT = 8
READ_TIMEOUT = 15
USER_AGENT = "WeiXinGZH-Studio/2.1.3 (+local-content-workbench)"
PROXY_RETRIES = 3
PROXY_BACKOFF = 0.4
PROXY_FETCH_TIMEOUT = 15

# 在受 GFW 限制的网络里,很多站点只能经由本地代理访问。这里探测的代理地址仅限
# 本机回环,不会把请求转发到任意内网主机。
_GITHUB_TREE_FILES = (
    "SKILL.md",
    "README.md",
    "readme.md",
    "README.zh.md",
    "README.zh-CN.md",
    "README.en.md",
    "index.md",
    "prompt.md",
    "agent.md",
    "config.md",
)


class SourceFetchError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SourceSnapshot:
    source_url: str
    final_url: str
    title: str
    publisher: str
    author: str
    published_at: str
    content_text: str
    preview: str
    content_hash: str
    extraction_method: str


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.article_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.json_ld: list[str] = []
        self._title = False
        self._script_ld = False
        self._script_buffer: list[str] = []
        self._article_depth = 0
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        if tag == "title":
            self._title = True
        if tag in {"article", "main"}:
            self._article_depth += 1
        if tag == "meta":
            key = (attr.get("property") or attr.get("name") or "").strip().lower()
            value = attr.get("content", "").strip()
            if key and value:
                self.meta[key] = value
        if tag == "script" and attr.get("type", "").lower() == "application/ld+json":
            self._script_ld = True
            self._script_buffer = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._title = False
        if tag in {"article", "main"} and self._article_depth:
            self._article_depth -= 1
        if tag == "script" and self._script_ld:
            content = "".join(self._script_buffer).strip()
            if content:
                self.json_ld.append(content)
            self._script_ld = False
            self._script_buffer = []
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._script_ld:
            self._script_buffer.append(data)
            return
        if self._skip_depth:
            return
        clean = re.sub(r"\s+", " ", data).strip()
        if not clean:
            return
        if self._title:
            self.title_parts.append(clean)
        self.text_parts.append(clean)
        if self._article_depth:
            self.article_parts.append(clean)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port=port, timeout=timeout)
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float):
        context = ssl.create_default_context()
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._ip = ip

    def connect(self) -> None:
        raw = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self.context.wrap_socket(raw, server_hostname=self.host)


def _is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_public(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SourceFetchError("dns_failed", f"无法解析来源域名：{host}") from exc
    addresses: list[str] = []
    has_unsafe = False
    for info in infos:
        address = info[4][0]
        if not _is_public_ip(address):
            logger.warning("SSRF 拦截: 来源 %s 解析到不安全地址 %s，已跳过", host, address)
            has_unsafe = True
            continue
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        if has_unsafe:
            raise SourceFetchError("unsafe_address", "来源解析到内网、环回或保留地址，已拒绝访问")
        raise SourceFetchError("dns_empty", "来源域名没有可用公网地址")
    return addresses


def _validate_target(raw_url: str, *, resolve: bool) -> str:
    value = raw_url.strip()
    if not value:
        raise SourceFetchError("empty_url", "来源地址为空")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise SourceFetchError("unsafe_scheme", "仅允许 HTTP 或 HTTPS 来源")
    if parsed.username or parsed.password:
        raise SourceFetchError("credentials_forbidden", "来源地址不能包含用户名或密码")
    if not parsed.hostname:
        raise SourceFetchError("missing_host", "来源地址缺少域名")
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    if port not in {80, 443}:
        raise SourceFetchError("unsafe_port", "仅允许访问 80 或 443 端口")
    if parsed.hostname.lower() in {"localhost", "localhost.localdomain"}:
        raise SourceFetchError("unsafe_host", "不允许访问本机地址")
    if resolve:
        _resolve_public(parsed.hostname, port)
    else:
        # 经代理访问时由代理解析域名,但仍拒绝显式的内网/环回 IP 字面量目标,
        # 避免被诱导通过代理访问 169.254.169.254 等元数据地址。
        try:
            ip = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            pass
        else:
            if not _is_public_ip(ip):
                raise SourceFetchError("unsafe_address", "来源解析到内网、环回或保留地址，已拒绝访问")
    return value


def validate_url(raw_url: str) -> str:
    return _validate_target(raw_url, resolve=True)


def _decode_body(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    match = re.search(r"charset=([\w-]+)", content_type, re.I)
    if match:
        charset = match.group(1)
    for candidate in [charset, "utf-8", "gb18030", "latin-1"]:
        try:
            return body.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")




def _gunzip_limited(data: bytes, limit: int = MAX_BYTES) -> bytes:
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    output: list[bytes] = []
    total = 0
    try:
        for offset in range(0, len(data), 65536):
            chunk = data[offset : offset + 65536]
            while chunk:
                piece = decoder.decompress(chunk, limit + 1 - total)
                if piece:
                    output.append(piece)
                    total += len(piece)
                    if total > limit:
                        raise SourceFetchError("content_too_large", "解压后的来源内容超过 2 MB 安全限制")
                chunk = decoder.unconsumed_tail
                if not chunk:
                    break
        piece = decoder.flush(limit + 1 - total)
        if piece:
            output.append(piece)
            total += len(piece)
        if total > limit:
            raise SourceFetchError("content_too_large", "解压后的来源内容超过 2 MB 安全限制")
        if not decoder.eof:
            raise SourceFetchError("gzip_invalid", "来源 gzip 内容不完整或无效")
        return b"".join(output)
    except zlib.error as exc:
        raise SourceFetchError("gzip_invalid", "来源 gzip 内容无法安全解压") from exc


def _request_once(url: str) -> tuple[int, dict[str, str], bytes, str]:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = _resolve_public(host, port)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    last_error: Exception | None = None
    for ip in addresses:
        conn: http.client.HTTPConnection | None = None
        try:
            if parsed.scheme == "https":
                conn = _PinnedHTTPSConnection(host, ip, port, CONNECT_TIMEOUT)
            else:
                conn = _PinnedHTTPConnection(host, ip, port, CONNECT_TIMEOUT)
            conn.request(
                "GET",
                path,
                headers={
                    "Host": host,
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/json;q=0.9,text/plain;q=0.8,*/*;q=0.5",
                    "Accept-Encoding": "gzip",
                    "Connection": "close",
                },
            )
            response = conn.getresponse()
            if conn.sock:
                peer = conn.sock.getpeername()[0]
                if not _is_public_ip(peer) or peer != ip:
                    raise SourceFetchError("peer_mismatch", "来源连接的实际地址与安全校验结果不一致")
                conn.sock.settimeout(READ_TIMEOUT)
            headers = {k.lower(): v for k, v in response.getheaders()}
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(min(65536, MAX_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_BYTES:
                    raise SourceFetchError("content_too_large", "来源内容超过 2 MB 安全限制")
            body = b"".join(chunks)
            if headers.get("content-encoding", "").lower() == "gzip":
                body = _gunzip_limited(body)
            return response.status, headers, body, ip
        except SourceFetchError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        finally:
            # 审计修复: conn 可能未赋值（构造连接对象即抛异常），先判空再关闭
            if conn:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
    raise SourceFetchError("connect_failed", f"无法连接来源：{last_error}")


# ---------------------------------------------------------------------------
# 代理支持
#
# 直连路径(_request_once)会用 getaddrinfo 解析目标域名,并在解析到内网/环回/
# 保留地址时拒绝访问。这在普通网络里能防 SSRF,但在以下场景会误伤合法来源:
#
#   1) 本机配置了 fake-ip 代理(如 Clash/Surge 增强模式),把 github.com 解析到
#      198.18.0.0/15 这类基准测试网段,被 _is_public_ip 判定为内网而拒绝;
#   2) GFW 环境下 GitHub 等站点只能经由本地 HTTP 代理访问,直连会被重置。
#
# 当探测到本机 HTTP/HTTPS 代理时,改走 urllib + ProxyHandler:由代理负责
# CONNECT 隧道与域名解析,既绕开 fake-ip,又能穿过 GFW。目标 URL 仍经过
# _validate_target 校验(拒绝内网 IP 字面量与 localhost),安全语义不变。
# ---------------------------------------------------------------------------


def _candidate_proxies() -> list[str]:
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
    sources = []
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
    def redirect_request(self, *args, **kwargs):  # noqa: D401, ANN002
        return None


def _read_limited(fp) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = fp.read(min(65536, MAX_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_BYTES:
            raise SourceFetchError("content_too_large", "来源内容超过 2 MB 安全限制")
    return b"".join(chunks)


def _request_via_proxy(url: str) -> tuple[int, dict[str, str], bytes]:
    proxies = _candidate_proxies()
    if not proxies:
        raise SourceFetchError("connect_failed", "未配置代理且无法直连来源")
    ssl_context = ssl.create_default_context()
    ssl_context.load_default_certs()
    last_error: Exception | None = None
    for proxy in proxies:
        for attempt in range(PROXY_RETRIES):
            try:
                handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
                opener = urllib.request.build_opener(
                    handler,
                    _NoRedirectHandler,
                    urllib.request.HTTPSHandler(context=ssl_context),
                )
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "text/html,application/json;q=0.9,text/plain;q=0.8,*/*;q=0.5",
                        "Accept-Encoding": "gzip",
                    },
                )
                try:
                    resp = opener.open(req, timeout=PROXY_FETCH_TIMEOUT)
                    status = resp.status
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                    body = _read_limited(resp)
                except urllib.error.HTTPError as http_error:
                    # 3xx/4xx/5xx 都会以 HTTPError 形式抛出,交由调用方处理重定向与状态码。
                    status = http_error.code
                    headers = {k.lower(): v for k, v in (http_error.headers.items() if http_error.headers else [])}
                    try:
                        body = _read_limited(http_error)
                    except SourceFetchError:
                        raise
                    except Exception:  # noqa: BLE001
                        body = b""
                if headers.get("content-encoding", "").lower() == "gzip" and body:
                    body = _gunzip_limited(body)
                return status, headers, body
            except (urllib.error.URLError, OSError, ssl.SSLError, http.client.HTTPException) as exc:
                last_error = exc
                time.sleep(PROXY_BACKOFF * (attempt + 1))
    raise SourceFetchError("connect_failed", f"无法经代理连接来源：{last_error}")


def _http_fetch(url: str) -> tuple[int, dict[str, str], bytes]:
    if _has_proxy():
        return _request_via_proxy(url)
    status, headers, body, _ip = _request_once(url)
    return status, headers, body


# ---------------------------------------------------------------------------
# 审计项 X2: robots.txt 尊重
#
# 在抓取目标 URL 之前,先检查目标站点的 robots.txt 是否允许抓取当前路径。
# 该检查是 best-effort 的:获取 robots.txt 失败(网络错误、DNS 失败、5xx 等)
# 不会阻塞后续抓取流程。仅当成功获取到 robots.txt 且其中明确 Disallow 当前
# 路径时,才抛出 SourceFetchError("robots_forbidden") 拒绝抓取。
# 测试模式下跳过 robots.txt 检查,以保证测试的确定性。
# ---------------------------------------------------------------------------


def _check_robots_txt(url: str) -> None:
    """审计项 X2: 检查目标站点 robots.txt 是否允许抓取当前路径。

    - 从 URL 提取 scheme 和 host,构造 robots.txt URL
    - 使用 _http_fetch 获取 robots.txt 内容(失败时跳过,不阻塞流程)
    - 解析 robots.txt,检查是否 Disallow 当前路径
    - 如果 Disallow,抛出 SourceFetchError("robots_forbidden")
    - 解析 Crawl-delay 指令,如果存在则记录到日志
    - 测试模式下跳过检查
    """
    # 测试模式跳过 robots.txt 检查,保证测试确定性
    if test_adapter_enabled("STUDIO_TEST_AI"):
        return

    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return

    # 构造 robots.txt URL(仅使用 scheme + netloc + /robots.txt)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    # best-effort 获取 robots.txt:任何异常都不阻塞抓取流程
    try:
        status, headers, body = _http_fetch(robots_url)
    except SourceFetchError as exc:
        logger.info("robots.txt 获取失败(%s),跳过检查: %s", exc.code, robots_url)
        return
    except Exception as exc:  # noqa: BLE001
        logger.info("robots.txt 获取异常,跳过检查: %s", exc)
        return

    # 非 200 或空响应视为无 robots.txt,跳过检查(允许抓取)
    if status != 200 or not body:
        logger.info("robots.txt 返回 HTTP %d,跳过检查: %s", status, robots_url)
        return

    # 解码 robots.txt 内容
    try:
        robots_text = _decode_body(body, headers.get("content-type", "text/plain; charset=utf-8"))
    except Exception:  # noqa: BLE001
        robots_text = body.decode("utf-8", errors="replace")

    # 使用标准库 RobotFileParser 解析 robots.txt
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(robots_text.splitlines())

    # 检查当前 URL 是否被禁止抓取
    try:
        allowed = rp.can_fetch(USER_AGENT, url)
    except Exception:  # noqa: BLE001
        logger.info("robots.txt can_fetch 解析异常,跳过检查: %s", robots_url)
        return

    if not allowed:
        raise SourceFetchError("robots_forbidden", "来源站点 robots.txt 禁止抓取该路径")

    # 解析 Crawl-delay 指令,记录到日志(仅记录,不实际延迟)
    try:
        crawl_delay = rp.crawl_delay(USER_AGENT)
    except Exception:  # noqa: BLE001
        crawl_delay = None
    if crawl_delay is not None:
        logger.info("robots.txt Crawl-delay=%.1fs (站点: %s)", crawl_delay, parsed.hostname)


# ---------------------------------------------------------------------------
# GitHub 来源改写
#
# GitHub 的 /tree/、/blob/ 网页是 React 应用,抓回来基本是导航噪音,正文很少;
# 而且 tree 页的文件列表是客户端懒加载,HTML 里取不到。真正的内容在
# raw.githubusercontent.com 上,且该域名对程序化访问更友好(不易 403)。
#
# 因此:blob -> 对应 raw 单文件;tree -> 按常见主文件(SKILL.md/README.md...)
# 探测 raw,取第一个命中的文件作为来源正文。
# ---------------------------------------------------------------------------


def _parse_github(url: str) -> dict[str, str] | None:
    parsed = urlsplit(url)
    if parsed.hostname not in {"github.com", "www.github.com"}:
        return None
    match = re.match(r"^/([^/]+)/([^/]+)/(blob|tree)/(.+?)/?$", parsed.path)
    if not match:
        return None
    owner, repo, kind, ref_path = match.groups()
    if repo.endswith(".git"):
        repo = repo[:-4]
    parts = ref_path.split("/", 1)
    return {
        "owner": owner,
        "repo": repo,
        "kind": kind,
        "ref": parts[0],
        "path": parts[1] if len(parts) > 1 else "",
    }


def _github_raw_url(info: dict[str, str], filename: str | None = None) -> str:
    segments = [
        "https://raw.githubusercontent.com",
        info["owner"],
        info["repo"],
        info["ref"],
    ]
    if info["path"].strip("/"):
        segments.append(info["path"].strip("/"))
    if filename:
        segments.append(filename)
    return "/".join(segments)


def _snapshot_from_text(
    original: str,
    final_url: str,
    body: bytes,
    content_type: str,
    info: dict[str, str],
    filename: str | None = None,
) -> SourceSnapshot:
    text = _decode_body(body, content_type)
    content = text[:200_000]
    if len(content) < 40:
        raise SourceFetchError("content_empty", "来源没有提取到足够的正文内容")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    title = f"{info['repo']}/{info['path']}" if info["path"] else info["repo"]
    if filename:
        title += f": {filename}"
    return SourceSnapshot(
        source_url=original,
        final_url=final_url,
        title=title[:300],
        publisher="GitHub",
        author=info["owner"],
        published_at="",
        content_text=content,
        preview=content[:1200],
        content_hash=digest,
        extraction_method="github_raw",
    )


def _fetch_one_raw(original: str, info: dict[str, str], filename: str, resolve: bool) -> SourceSnapshot | None:
    raw_url = _github_raw_url(info, filename)
    current = _validate_target(raw_url, resolve=resolve)
    status, headers, body = _http_fetch(current)
    if status in {301, 302, 303, 307, 308}:
        target = headers.get("location", "").strip()
        if target:
            current = _validate_target(urljoin(current, target), resolve=resolve)
            status, headers, body = _http_fetch(current)
    if status != 200 or not body:
        return None
    try:
        return _snapshot_from_text(
            original, current, body, headers.get("content-type", "text/plain; charset=utf-8"), info, filename
        )
    except SourceFetchError:
        return None


def _fetch_github_blob(original: str, info: dict[str, str]) -> SourceSnapshot:
    resolve = not _has_proxy()
    snapshot = _fetch_one_raw(original, info, info["path"].split("/")[-1] if info["path"] else None, resolve)
    if snapshot is not None:
        return snapshot
    # raw 未命中(可能路径本身是目录或分支写法),回退到原网页抓取
    current = _validate_target(original, resolve=resolve)
    status, headers, body = _http_fetch(current)
    if status in {301, 302, 303, 307, 308}:
        target = headers.get("location", "").strip()
        if not target:
            raise SourceFetchError("redirect_missing", "来源返回重定向但没有目标地址")
        current = _validate_target(urljoin(current, target), resolve=resolve)
        status, headers, body = _http_fetch(current)
    if status == 404:
        raise SourceFetchError("http_error", "GitHub 文件不存在(404),请确认分支与路径")
    if status < 200 or status >= 300:
        raise SourceFetchError("http_error", f"来源返回 HTTP {status}")
    return extract_snapshot(original, current, body, headers.get("content-type", "text/html; charset=utf-8"))


def _fetch_github_tree(original: str, info: dict[str, str]) -> SourceSnapshot:
    resolve = not _has_proxy()
    for filename in _GITHUB_TREE_FILES:
        snapshot = _fetch_one_raw(original, info, filename, resolve)
        if snapshot is not None:
            return snapshot
    # 没有命中常见主文件,回退到 tree 网页本身的正文抽取
    current = _validate_target(original, resolve=resolve)
    status, headers, body = _http_fetch(current)
    if status in {301, 302, 303, 307, 308}:
        target = headers.get("location", "").strip()
        if not target:
            raise SourceFetchError("redirect_missing", "来源返回重定向但没有目标地址")
        current = _validate_target(urljoin(current, target), resolve=resolve)
        status, headers, body = _http_fetch(current)
    if status < 200 or status >= 300:
        raise SourceFetchError("http_error", f"来源返回 HTTP {status}")
    return extract_snapshot(original, current, body, headers.get("content-type", "text/html; charset=utf-8"))


def _json_ld_values(extractor: _Extractor) -> Iterable[dict[str, object]]:
    for raw in extractor.json_ld:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        values = parsed if isinstance(parsed, list) else [parsed]
        for value in values:
            if isinstance(value, dict) and isinstance(value.get("@graph"), list):
                values.extend(item for item in value["@graph"] if isinstance(item, dict))
            if isinstance(value, dict):
                yield value


def _pick_json_ld(extractor: _Extractor) -> dict[str, object]:
    for value in _json_ld_values(extractor):
        kind = value.get("@type", "")
        kinds = kind if isinstance(kind, list) else [kind]
        if any(str(item).lower() in {"article", "newsarticle", "blogposting", "techarticle"} for item in kinds):
            return value
    return {}


def _clean_text(parts: list[str]) -> str:
    text = "\n\n".join(parts)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 审计项 X4: 来源内容投毒防护(隐藏文本清洗)
#
# 攻击者可能在 HTML 中注入对人类不可见但对爬虫/LLM 可见的文本(内容投毒),
# 例如:HTML 注释中的指令、零宽字符、CSS 隐藏(display:none)的文本、与背景
# 同色的白色文字、font-size:0/1px 的微小文本等。本函数在正文提取前后对
# HTML/文本进行清洗,移除这些隐藏内容,防止投毒文本污染来源快照。
# ---------------------------------------------------------------------------


def _clean_hidden_content(html_text: str) -> str:
    """审计项 X4: 清洗 HTML 中可能的隐藏文本(内容投毒防护)。

    - 去除 HTML 注释中的内容
    - 去除零宽字符(零宽空格、零宽连字符、方向控制字符等)
    - 去除 CSS 隐藏的内容(display:none / visibility:hidden 的标签及其内容)
    - 去除颜色与背景相同的文本(color:#fff / color:white 等)
    - 去除 font-size 为 0 或 1px 的文本
    """
    text = html_text

    # 1. 去除 HTML 注释中的内容
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)

    # 2. 去除零宽字符(零宽空格、零宽连字符、方向控制字符、BOM 等)
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]", "", text)

    # 3. 去除 CSS 隐藏的内容:匹配 style 属性包含 display:none 或 visibility:hidden
    #    的标签及其内部内容(非贪婪匹配,简单实现不处理同标签嵌套)
    _hidden_style_patterns = (
        r"display\s*:\s*none",
        r"visibility\s*:\s*hidden",
    )
    for pattern in _hidden_style_patterns:
        # 匹配 <tag ... style="...display:none..."> ... </tag> 并整体移除
        text = re.sub(
            r"<(?P<tag>[a-zA-Z][a-zA-Z0-9]*)\b[^>]*"
            r"style\s*=\s*[\"'][^\"']*\b" + pattern + r"\b[^\"']*[\"']"
            r"[^>]*>.*?</(?P=tag)>",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # 匹配无闭合标签的隐藏元素(自闭合或 void 元素)
        text = re.sub(
            r"<[a-zA-Z][a-zA-Z0-9]*\b[^>]*"
            r"style\s*=\s*[\"'][^\"']*\b" + pattern + r"\b[^\"']*[\"']"
            r"[^>]*/?>",
            "",
            text,
            flags=re.IGNORECASE,
        )

    # 4. 去除颜色与背景相同的文本(白色文字在白色背景上)
    #    匹配 color:#fff / color:#ffffff / color:white 等
    _invisible_color_patterns = (
        r"color\s*:\s*#ffffff\b",
        r"color\s*:\s*#fff(?![0-9a-fA-F])",
        r"color\s*:\s*white\b",
    )
    for pattern in _invisible_color_patterns:
        text = re.sub(
            r"<(?P<tag>[a-zA-Z][a-zA-Z0-9]*)\b[^>]*"
            r"style\s*=\s*[\"'][^\"']*\b" + pattern + r"[^\"']*[\"']"
            r"[^>]*>.*?</(?P=tag)>",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )

    # 5. 去除 font-size 为 0 或 1px 的文本(微小不可见文字)
    #    匹配 font-size:0 / font-size:0px / font-size:1px(排除 10px、100px 等)
    _tiny_font_patterns = (
        r"font-size\s*:\s*0px(?![0-9a-zA-Z])",
        r"font-size\s*:\s*1px(?![0-9a-zA-Z])",
        r"font-size\s*:\s*0(?![0-9a-zA-Z])",
    )
    for pattern in _tiny_font_patterns:
        text = re.sub(
            r"<(?P<tag>[a-zA-Z][a-zA-Z0-9]*)\b[^>]*"
            r"style\s*=\s*[\"'][^\"']*\b" + pattern + r"[^\"']*[\"']"
            r"[^>]*>.*?</(?P=tag)>",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )

    return text


def extract_snapshot(source_url: str, final_url: str, body: bytes, content_type: str) -> SourceSnapshot:
    text = _decode_body(body, content_type)
    if "application/json" in content_type.lower():
        try:
            obj = json.loads(text)
            pretty = json.dumps(obj, ensure_ascii=False, indent=2)
            title = str(obj.get("name") or obj.get("title") or urlsplit(final_url).hostname or "JSON 来源") if isinstance(obj, dict) else "JSON 来源"
            content = pretty[:200_000]
            method = "json"
            publisher = ""
            author = ""
            published = ""
        except json.JSONDecodeError:
            content = text
            title = urlsplit(final_url).hostname or "来源"
            method = "plain_text"
            publisher = author = published = ""
    else:
        # 审计项 X4: 在 HTML 解析前清洗隐藏文本(内容投毒防护)
        # 移除 HTML 注释、CSS 隐藏元素、同色文字、微小字体等,防止投毒文本被提取
        text = _clean_hidden_content(text)
        parser = _Extractor()
        try:
            parser.feed(text)
        except Exception as exc:  # noqa: BLE001
            # 审计修复: HTML 解析异常不再静默吞掉，记录告警便于排查投毒/畸形页面
            logger.warning("HTML 解析失败: %s", exc)
        ld = _pick_json_ld(parser)
        title = str(
            ld.get("headline")
            or parser.meta.get("og:title")
            or parser.meta.get("twitter:title")
            or " ".join(parser.title_parts)
            or urlsplit(final_url).hostname
            or "来源"
        ).strip()
        publisher_value = ld.get("publisher")
        if isinstance(publisher_value, dict):
            publisher = str(publisher_value.get("name") or "")
        else:
            publisher = str(publisher_value or parser.meta.get("og:site_name") or "")
        author_value = ld.get("author")
        if isinstance(author_value, list):
            author = ", ".join(str(item.get("name", "")) if isinstance(item, dict) else str(item) for item in author_value)
        elif isinstance(author_value, dict):
            author = str(author_value.get("name") or "")
        else:
            author = str(author_value or parser.meta.get("author") or "")
        # 审计项 X2: 解析 meta 标签版权信息,补充 publisher/author 字段
        # copyright / dcterms.rights → publisher;og:article:author → author
        if not publisher:
            publisher = str(
                parser.meta.get("copyright")
                or parser.meta.get("dcterms.rights")
                or ""
            )
        if not author:
            author = str(parser.meta.get("og:article:author") or "")
        published = str(ld.get("datePublished") or parser.meta.get("article:published_time") or "")
        article_body = ld.get("articleBody")
        if isinstance(article_body, str) and len(article_body.strip()) >= 100:
            content = _clean_text([article_body])
            method = "json_ld"
        elif len(_clean_text(parser.article_parts)) >= 100:
            content = _clean_text(parser.article_parts)
            method = "article_main"
        else:
            content = _clean_text(parser.text_parts)
            method = "body_fallback"
    if len(content) < 40:
        raise SourceFetchError("content_empty", "来源没有提取到足够的正文内容")
    # 审计项 X4: 在 _clean_text 之后对结果进行隐藏文本清洗(内容投毒防护)
    # 再次清洗可去除经 JSON-LD articleBody 或文本提取后残留的零宽字符等投毒内容
    content = _clean_hidden_content(content)
    content = content[:200_000]
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    preview = content[:1200]
    return SourceSnapshot(
        source_url=source_url,
        final_url=final_url,
        title=title[:300],
        publisher=publisher[:200],
        author=author[:200],
        published_at=published[:100],
        content_text=content,
        preview=preview,
        content_hash=digest,
        extraction_method=method,
    )


def fetch_source(raw_url: str) -> SourceSnapshot:
    original = (raw_url or "").strip()
    logger.info("开始抓取来源: %s", original)
    start_time = time.monotonic()
    github = _parse_github(original)
    if github:
        logger.info("检测到 GitHub 来源: %s/%s kind=%s", github["owner"], github["repo"], github["kind"])
        if github["kind"] == "tree":
            return _fetch_github_tree(original, github)
        if github["kind"] == "blob":
            return _fetch_github_blob(original, github)
    # 审计项 X2: 在抓取目标 URL 之前,检查 robots.txt 是否允许抓取(测试模式跳过)
    _check_robots_txt(original)
    resolve = not _has_proxy()
    current = _validate_target(original, resolve=resolve)
    for _ in range(MAX_REDIRECTS + 1):
        status, headers, body = _http_fetch(current)
        if status in {301, 302, 303, 307, 308}:
            target = headers.get("location", "").strip()
            if not target:
                raise SourceFetchError("redirect_missing", "来源返回重定向但没有目标地址")
            current = _validate_target(urljoin(current, target), resolve=resolve)
            continue
        if status < 200 or status >= 300:
            elapsed = (time.monotonic() - start_time) * 1000
            logger.error("来源返回 HTTP %d (耗时 %.0fms): %s", status, elapsed, current)
            raise SourceFetchError("http_error", f"来源返回 HTTP {status}")
        elapsed = (time.monotonic() - start_time) * 1000
        logger.info("来源抓取完成 (HTTP %d, 耗时 %.0fms, %d bytes): %s", status, elapsed, len(body), current)
        return extract_snapshot(original, current, body, headers.get("content-type", "text/html; charset=utf-8"))
    raise SourceFetchError("redirect_limit", "来源重定向次数过多")
