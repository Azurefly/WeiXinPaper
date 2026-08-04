"""会话管理：创建、验证、销毁用户会话。

会话令牌使用 secrets.token_urlsafe 生成，服务端仅存储 SHA-256 哈希。
会话持久化到 SQLite sessions 表，进程重启后仍可恢复有效会话。
"""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from db import connect, utc_now
from logger_config import get_logger

logger = get_logger("auth_session")

# 会话有效期：8 小时
SESSION_TTL_SECONDS = 8 * 3600

# Cookie 名称
SESSION_COOKIE_NAME = "studio_session"


def _secure_cookie_suffix() -> str:
    """HTTPS 反向代理模式下强制浏览器仅经安全连接发送 Cookie。"""
    public_origin = os.environ.get("STUDIO_PUBLIC_ORIGIN", "").strip().lower()
    return "; Secure" if public_origin.startswith("https://") else ""

# 内存缓存（减少数据库查询），进程重启后从数据库恢复
_sessions_cache: dict[str, dict[str, Any]] = {}
_sessions_lock = threading.Lock()
_cache_loaded = False


def _hash_token(token: str) -> str:
    """对会话令牌进行 SHA-256 哈希，仅存储哈希值。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load_cache_from_db() -> None:
    """从数据库加载未过期的会话到内存缓存。"""
    global _cache_loaded
    if _cache_loaded:
        return
    with _sessions_lock:
        if _cache_loaded:
            return
        now = utc_now()
        try:
            with connect() as conn:
                rows = conn.execute(
                    """
                    SELECT s.token_hash, s.user_id, s.expires_at,
                           u.username, u.must_change_password, u.is_active
                    FROM sessions s
                    JOIN users u ON u.id = s.user_id
                    WHERE s.expires_at > ?
                    """,
                    (now,),
                ).fetchall()
            for row in rows:
                _sessions_cache[row["token_hash"]] = {
                    "user_id": row["user_id"],
                    "username": row["username"],
                    "must_change_password": bool(row["must_change_password"]),
                    "is_active": bool(row["is_active"]),
                    "expires_at": row["expires_at"],
                }
            if rows:
                logger.info("从数据库恢复 %d 个有效会话", len(rows))
        except Exception as exc:  # noqa: BLE001
            logger.warning("从数据库加载会话缓存失败: %s", exc)
        _cache_loaded = True


def create_session(user_id: str, username: str, must_change: bool) -> str:
    """创建新会话，返回原始令牌（仅此一次可见）。

    服务端存储令牌的 SHA-256 哈希，令牌本身不持久化。
    """
    token = secrets.token_urlsafe(32)
    token_hash = _hash_token(token)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL_SECONDS)).replace(
        microsecond=0
    ).isoformat()
    now = utc_now()

    # 持久化到数据库
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES(?,?,?,?)",
            (token_hash, user_id, now, expires_at),
        )

    # 更新内存缓存
    with _sessions_lock:
        _sessions_cache[token_hash] = {
            "user_id": user_id,
            "username": username,
            "must_change_password": must_change,
            "is_active": True,
            "expires_at": expires_at,
        }

    logger.info("用户 %s 创建会话，有效期至 %s", username, expires_at)
    return token


def get_session(token: str) -> dict[str, Any] | None:
    """根据令牌获取会话信息。

    返回 None 表示会话无效或已过期。
    """
    if not token:
        return None

    _load_cache_from_db()
    token_hash = _hash_token(token)

    with _sessions_lock:
        session = _sessions_cache.get(token_hash)

    if not session:
        return None

    # 检查过期
    try:
        expires = datetime.fromisoformat(session["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= expires:
            destroy_session(token)
            return None
    except (ValueError, TypeError):
        return None

    # 检查用户是否仍然活跃
    if not session.get("is_active", True):
        destroy_session(token)
        return None

    return {
        "user_id": session["user_id"],
        "username": session["username"],
        "must_change_password": session["must_change_password"],
    }


def destroy_session(token: str) -> None:
    """销毁会话（登出）。"""
    if not token:
        return
    token_hash = _hash_token(token)

    with _sessions_lock:
        _sessions_cache.pop(token_hash, None)

    try:
        with connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
    except Exception as exc:  # noqa: BLE001
        logger.warning("销毁会话失败: %s", exc)


def update_session_must_change(token: str, must_change: bool) -> None:
    """更新会话中的 must_change_password 标志（修改密码后调用）。"""
    if not token:
        return
    token_hash = _hash_token(token)
    with _sessions_lock:
        session = _sessions_cache.get(token_hash)
        if session:
            session["must_change_password"] = must_change


def destroy_all_user_sessions(user_id: str) -> None:
    """销毁指定用户的所有会话（修改密码后强制重新登录）。"""
    with _sessions_lock:
        to_remove = [
            token_hash
            for token_hash, session in _sessions_cache.items()
            if session.get("user_id") == user_id
        ]
        for token_hash in to_remove:
            _sessions_cache.pop(token_hash, None)

    try:
        with connect() as conn:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    except Exception as exc:  # noqa: BLE001
        logger.warning("销毁用户 %s 的所有会话失败: %s", user_id, exc)


def cleanup_expired_sessions() -> int:
    """清理所有过期会话，返回清理数量。"""
    now = utc_now()
    count = 0
    try:
        with connect() as conn:
            cursor = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            count = cursor.rowcount
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理过期会话失败: %s", exc)

    if count > 0:
        with _sessions_lock:
            to_remove = [
                token_hash
                for token_hash, session in _sessions_cache.items()
                if session.get("expires_at", "") <= now
            ]
            for token_hash in to_remove:
                _sessions_cache.pop(token_hash, None)
        logger.info("清理 %d 个过期会话", count)

    return count


def parse_cookie(cookie_header: str, name: str) -> str | None:
    """从 Cookie 头中解析指定名称的值。"""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            if key.strip() == name:
                return value.strip()
    return None


def build_cookie(token: str) -> str:
    """构建 Set-Cookie 头值。

    回环 HTTP 模式不设置 Secure；配置 HTTPS 公网 Origin 后自动启用 Secure。
    HttpOnly 防止 JS 读取，SameSite=Strict 防止 CSRF。
    """
    return (
        f"{SESSION_COOKIE_NAME}={token}; "
        f"Path=/; "
        f"Max-Age={SESSION_TTL_SECONDS}; "
        f"HttpOnly; "
        f"SameSite=Strict{_secure_cookie_suffix()}"
    )


def build_clear_cookie() -> str:
    """构建清除 Cookie 的 Set-Cookie 头值。"""
    return f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{_secure_cookie_suffix()}"
