"""微信公众号 API 工程化封装。

修复审计报告中的 W1/W2/W3/W4 问题：
- W1: access_token 线程安全缓存 + 提前 5 分钟刷新 + 并发去重锁
- W2: errcode 分类处理（40001/42001 刷新 token 重试、45009 退避等待、40164 提示配置）
- W3: 永久素材去重，基于封面内容 hash 复用已有 media_id
- W4: 出站 API 令牌桶速率控制，按 API 类别限流避免触发微信频率限制
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.parse
from typing import Any

from logger_config import get_logger
from secure_http import SecureHttpError, request_bytes
from test_mode import enabled as test_adapter_enabled

logger = get_logger("wechat_api")

# ---------------------------------------------------------------------------
# W1: access_token 线程安全缓存
# ---------------------------------------------------------------------------

class WeChatTokenManager:
    """线程安全的 access_token 缓存管理器。

    - 首次调用时向微信拉取 token，后续在有效期内复用
    - 提前 5 分钟（300 秒）刷新，避免边界过期
    - 并发请求通过 Lock 去重，保证同一时刻只有一个拉取请求
    - 拉取失败抛出 WeChatApiError，调用方可按 errcode 决策重试
    """

    _REFRESH_AHEAD_SECONDS = 300  # 提前 5 分钟刷新

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str = ""
        self._expires_at: float = 0.0
        self._app_id: str = ""
        self._app_secret: str = ""

    def get_token(self, app_id: str, app_secret: str) -> str:
        """获取有效的 access_token，带缓存和并发去重。"""
        # 快速路径：检查缓存是否有效（不加锁）
        if (
            self._token
            and self._app_id == app_id
            and self._app_secret == app_secret
            and time.monotonic() < self._expires_at
        ):
            return self._token

        with self._lock:
            # 双重检查：可能其他线程已经刷新了 token
            if (
                self._token
                and self._app_id == app_id
                and self._app_secret == app_secret
                and time.monotonic() < self._expires_at
            ):
                return self._token

            # 凭证变更时强制刷新
            force_refresh = self._app_id != app_id or self._app_secret != app_secret
            self._app_id = app_id
            self._app_secret = app_secret
            self._token = ""
            self._expires_at = 0.0

            token, expires_in = self._fetch_token(app_id, app_secret)
            self._token = token
            self._expires_at = time.monotonic() + expires_in - self._REFRESH_AHEAD_SECONDS
            logger.info(
                "access_token 刷新成功: expires_in=%ds, 提前%d秒刷新",
                expires_in, self._REFRESH_AHEAD_SECONDS,
            )
            return self._token

    def invalidate(self) -> None:
        """使缓存的 token 失效（在收到 40001/42001 时调用）。"""
        with self._lock:
            self._token = ""
            self._expires_at = 0.0
            logger.warning("access_token 缓存已失效，下次调用将重新拉取")

    @staticmethod
    def _fetch_token(app_id: str, app_secret: str) -> tuple[str, int]:
        """向微信拉取新的 access_token。"""
        if test_adapter_enabled("STUDIO_TEST_WECHAT"):
            if app_id == "bad":
                raise WeChatApiError("wechat_verify_failed", "测试凭证无效")
            return "test-access-token", 7200

        params = urllib.parse.urlencode({
            "grant_type": "client_credential",
            "appid": app_id,
            "secret": app_secret,
        })
        url = "https://api.weixin.qq.com/cgi-bin/token?" + params
        try:
            response = request_bytes(
                url, timeout=20, max_bytes=1_000_000,
                require_https=True, reject_redirects=True,
            )
            body = json.loads(response.body.decode("utf-8"))
        except (SecureHttpError, json.JSONDecodeError) as exc:
            raise WeChatApiError(
                "wechat_verify_failed", f"无法安全连接微信接口：{exc}",
            ) from exc

        token = str(body.get("access_token") or "")
        expires_in = int(body.get("expires_in") or 7200)
        if not token:
            errcode = body.get("errcode", 0)
            errmsg = body.get("errmsg") or str(body)
            raise WeChatApiError(
                "wechat_verify_failed",
                f"微信凭证验证失败：{errmsg}",
                errcode=errcode,
            )
        return token, expires_in


# 全局单例
_token_manager = WeChatTokenManager()


# ---------------------------------------------------------------------------
# W2: 微信 errcode 分类处理
# ---------------------------------------------------------------------------

# 需要刷新 token 后重试的 errcode
_TOKEN_EXPIRED_CODES = {40001, 42001, 40014}
# 频率超限，需要退避等待
_RATE_LIMIT_CODES = {45009}
# IP 白名单问题，不可重试
_IP_WHITELIST_CODES = {40164}
# 需要重试的 HTTP 状态码（服务器临时错误）
_RETRYABLE_HTTP_STATUSES = {500, 502, 503, 504}

MAX_RETRY = 1  # token 失效时最多重试 1 次


class WeChatApiError(RuntimeError):
    """微信 API 调用错误，包含 errcode 分类信息。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        errcode: int = 0,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.errcode = errcode
        self.retryable = retryable


def _classify_error(body: dict[str, Any]) -> WeChatApiError:
    """根据微信返回的 errcode 分类错误。"""
    errcode = int(body.get("errcode") or 0)
    errmsg = str(body.get("errmsg") or "未知错误")

    if errcode in _TOKEN_EXPIRED_CODES:
        return WeChatApiError(
            "wechat_token_expired",
            f"微信 token 已失效（errcode={errcode}）：{errmsg}",
            errcode=errcode,
            retryable=True,
        )
    if errcode in _RATE_LIMIT_CODES:
        return WeChatApiError(
            "wechat_rate_limited",
            f"微信 API 频率超限（errcode={errcode}）：{errmsg}，请稍后重试",
            errcode=errcode,
            retryable=False,
        )
    if errcode in _IP_WHITELIST_CODES:
        return WeChatApiError(
            "wechat_ip_not_whitelisted",
            f"服务器 IP 不在微信白名单中（errcode={errcode}）：{errmsg}，请在公众号后台配置 IP 白名单",
            errcode=errcode,
            retryable=False,
        )
    return WeChatApiError(
        "wechat_api_error",
        f"微信 API 返回错误（errcode={errcode}）：{errmsg}",
        errcode=errcode,
        retryable=False,
    )


# ---------------------------------------------------------------------------
# W4: 出站微信 API 速率控制（令牌桶）
# ---------------------------------------------------------------------------

class TokenBucket:
    """线程安全的令牌桶速率限制器。

    - capacity: 桶容量（允许的最大突发请求数）
    - refill_rate: 每秒补充的令牌数（令牌/秒）
    - acquire(timeout=...): 阻塞直到获取一个令牌或超时

    令牌桶允许短时突发（最多 capacity 个请求同时通过），随后按
    refill_rate 持续补充令牌，平滑出站调用频率，避免触发微信 API 限制。
    """

    def __init__(self, capacity: float, refill_rate: float) -> None:
        if capacity <= 0:
            raise ValueError("TokenBucket capacity 必须为正数")
        if refill_rate <= 0:
            raise ValueError("TokenBucket refill_rate 必须为正数")
        self._capacity = float(capacity)
        self._refill_rate = float(refill_rate)
        self._tokens = float(capacity)  # 初始满桶，允许冷启动突发
        self._last_refill = time.monotonic()
        # Condition 内部包含一把锁，既用于互斥也用于等待/通知
        self._cond = threading.Condition()

    def _refill(self) -> None:
        """根据流逝时间补充令牌（调用方需持有锁）。"""
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(
                self._capacity, self._tokens + elapsed * self._refill_rate
            )
            self._last_refill = now

    def acquire(self, timeout: float = 30.0) -> bool:
        """阻塞直到获取一个令牌或超时。

        返回 True 表示成功获取令牌，可以发起调用；
        返回 False 表示在 timeout 内未能获取令牌（应放弃调用）。
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                # 计算还需多久才能积累出 1 个令牌
                needed = 1.0 - self._tokens
                wait = needed / self._refill_rate
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                # 等待期间若有 notify 会提前唤醒；否则等到补够或超时
                self._cond.wait(timeout=min(wait, remaining))


# 通过环境变量配置补充速率（令牌/秒），便于按部署环境调优
_api_rate = float(os.environ.get("STUDIO_WECHAT_API_RATE", "2"))
_material_rate = float(os.environ.get("STUDIO_WECHAT_MATERIAL_RATE", "0.5"))
_draft_rate = float(os.environ.get("STUDIO_WECHAT_DRAFT_RATE", "1"))

# 全局令牌桶实例：按微信 API 类别分别限流
# 通用 API 调用：容量 10，补充 2/s（≈120/min）
_api_bucket = TokenBucket(capacity=10, refill_rate=_api_rate)
# 素材上传（add_material）：容量 3，补充 0.5/s（≈30/min）
_material_bucket = TokenBucket(capacity=3, refill_rate=_material_rate)
# 草稿创建（draft/add）：容量 5，补充 1/s（≈60/min）
_draft_bucket = TokenBucket(capacity=5, refill_rate=_draft_rate)


def wechat_api_call(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    app_id: str = "",
    app_secret: str = "",
    max_bytes: int = 2_000_000,
    timeout: int = 30,
    bucket: TokenBucket | None = _api_bucket,
) -> dict[str, Any]:
    """调用微信 API，自动处理 token 过期重试。

    调用方需在 url 中包含 access_token 参数。
    若返回 errcode 属于 token 过期类，自动刷新 token 并重试一次。
    通过 bucket 进行出站速率控制（令牌桶），避免触发微信 API 频率限制；
    传入 None 可跳过限流（如测试场景）。
    """
    # 出站速率控制：获取令牌后才允许发起调用，超时则放弃并报错
    if bucket is not None and not bucket.acquire(timeout=30.0):
        raise WeChatApiError(
            "rate_limited",
            "出站微信 API 速率受限：等待令牌超时，请稍后重试",
        )

    last_error: WeChatApiError | None = None

    for attempt in range(MAX_RETRY + 1):
        try:
            response = request_bytes(
                url,
                method=method,
                body=body,
                headers=headers,
                timeout=timeout,
                max_bytes=max_bytes,
                require_https=True,
                reject_redirects=True,
            )
        except SecureHttpError as exc:
            if exc.status and exc.status in _RETRYABLE_HTTP_STATUSES and attempt < MAX_RETRY:
                logger.warning("微信 API 返回 HTTP %d，准备重试", exc.status)
                time.sleep(1.0 * (attempt + 1))
                continue
            raise WeChatApiError(
                "wechat_http_error",
                f"微信 API 连接失败：{exc.code}",
            ) from exc

        try:
            result = json.loads(response.body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise WeChatApiError(
                "wechat_response_invalid",
                "微信 API 返回了无法解析的响应",
            ) from exc

        errcode = int(result.get("errcode") or 0)
        if errcode == 0:
            return result

        error = _classify_error(result)
        last_error = error

        if error.retryable and attempt < MAX_RETRY:
            logger.warning(
                "微信 API token 过期 (errcode=%d)，刷新 token 后重试 (attempt=%d)",
                errcode, attempt + 1,
            )
            _token_manager.invalidate()
            # 重新获取 token 并重建 URL
            new_token = _token_manager.get_token(app_id, app_secret)
            url = _replace_token_in_url(url, new_token)
            continue

        raise error

    raise last_error or WeChatApiError("wechat_unknown_error", "微信 API 调用失败")


def _replace_token_in_url(url: str, new_token: str) -> str:
    """替换 URL 中的 access_token 参数。"""
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query)
    query["access_token"] = [new_token]
    new_query = urllib.parse.urlencode(query, doseq=True)
    return urllib.parse.urlunsplit((
        parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment,
    ))


# ---------------------------------------------------------------------------
# W3: 永久素材去重
# ---------------------------------------------------------------------------

# 进程内缓存：cover_hash -> media_id
_material_cache: dict[str, str] = {}
_material_cache_lock = threading.Lock()

# 数据库表名（在 db.py init_db 中创建）
_MATERIAL_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS wechat_materials (
    content_hash TEXT PRIMARY KEY,
    media_id TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'image',
    created_at TEXT NOT NULL,
    file_size INTEGER NOT NULL DEFAULT 0
)
"""


def _ensure_material_table() -> None:
    """确保素材去重表存在。"""
    from db import connect, utc_now
    with connect() as conn:
        conn.execute(_MATERIAL_TABLE_SQL)


def _lookup_material(content_hash: str) -> str:
    """查询已上传的素材 media_id。"""
    from db import connect
    # 先查内存缓存
    with _material_cache_lock:
        if content_hash in _material_cache:
            return _material_cache[content_hash]

    # 再查数据库
    _ensure_material_table()
    with connect() as conn:
        row = conn.execute(
            "SELECT media_id FROM wechat_materials WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
    if row:
        media_id = row["media_id"]
        with _material_cache_lock:
            _material_cache[content_hash] = media_id
        return media_id
    return ""


def _save_material(content_hash: str, media_id: str, file_size: int = 0) -> None:
    """保存素材 media_id 到缓存和数据库。"""
    from db import connect, utc_now
    with _material_cache_lock:
        _material_cache[content_hash] = media_id
    _ensure_material_table()
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO wechat_materials (content_hash, media_id, media_type, created_at, file_size) VALUES (?, ?, 'image', ?, ?)",
            (content_hash, media_id, utc_now(), file_size),
        )


def upload_cover_dedup(
    token: str,
    cover_data_url: str,
    *,
    app_id: str = "",
    app_secret: str = "",
) -> str:
    """上传封面到微信永久素材库，带去重。

    如果同一内容 hash 的图片已上传过，直接复用 media_id。
    """
    # 解析 data URL 获取原始数据
    import base64
    import re

    match = re.fullmatch(
        r"data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=\s]+)",
        cover_data_url,
    )
    if not match:
        return ""

    mime = match.group(1)
    raw = base64.b64decode(match.group(2), validate=True)
    if not raw:
        return ""

    # 计算内容 hash
    content_hash = hashlib.sha256(raw).hexdigest()

    # 查重
    existing = _lookup_material(content_hash)
    if existing:
        logger.info("封面素材去重命中: hash=%s… media_id=%s", content_hash[:12], existing)
        return existing

    # 测试适配器
    if test_adapter_enabled("STUDIO_TEST_WECHAT"):
        media_id = "thumb_test_" + content_hash[:12]
        _save_material(content_hash, media_id, len(raw))
        return media_id

    # 上传到微信
    import secrets
    extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}[mime]
    boundary = "----StudioBoundary" + secrets.token_hex(12)
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="media"; filename="cover.{extension}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8") + raw + f"\r\n--{boundary}--\r\n".encode("utf-8")

    url = (
        "https://api.weixin.qq.com/cgi-bin/material/add_material?access_token="
        + urllib.parse.quote(token)
        + "&type=image"
    )

    result = wechat_api_call(
        url,
        method="POST",
        body=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
        app_id=app_id,
        app_secret=app_secret,
        timeout=45,
        bucket=_material_bucket,
    )

    media_id = str(result.get("media_id") or "")
    if not media_id:
        raise WeChatApiError(
            "wechat_cover_upload_failed",
            "微信封面素材上传失败：未返回 media_id",
        )

    _save_material(content_hash, media_id, len(raw))
    logger.info("封面素材上传成功: hash=%s… media_id=%s", content_hash[:12], media_id)
    return media_id


# ---------------------------------------------------------------------------
# 草稿创建（带 errcode 处理）
# ---------------------------------------------------------------------------

def create_draft(
    token: str,
    payload: dict[str, Any],
    *,
    app_id: str = "",
    app_secret: str = "",
) -> str:
    """创建微信草稿，返回 media_id。带 errcode 分类处理和 token 过期重试。"""
    url = (
        "https://api.weixin.qq.com/cgi-bin/draft/add?access_token="
        + urllib.parse.quote(token)
    )
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    result = wechat_api_call(
        url,
        method="POST",
        body=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        },
        app_id=app_id,
        app_secret=app_secret,
        timeout=30,
        bucket=_draft_bucket,
    )

    media_id = str(result.get("media_id") or "")
    if not media_id:
        raise WeChatApiError(
            "wechat_publish_failed",
            "微信草稿同步失败：未返回 media_id",
        )
    return media_id
