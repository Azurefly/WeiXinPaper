from __future__ import annotations

import argparse
import collections
import base64
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ai_engine import AIEngine, AIEngineError
from auth_password import (
    hash_password,
    validate_password_strength,
    verify_password,
)
from auth_session import (
    SESSION_COOKIE_NAME,
    build_clear_cookie,
    build_cookie,
    cleanup_expired_sessions,
    create_session,
    destroy_all_user_sessions,
    destroy_session,
    get_session,
    parse_cookie,
    update_session_must_change,
)
from db import (
    connect,
    count_projects,
    get_health_checks,
    get_project,
    get_setting,
    get_task,
    init_db,
    list_projects,
    list_task_events,
    list_tasks,
    record_project_version,
    row_to_project,
    set_health_check,
    set_setting,
    settings_bundle,
    utc_now,
)
from data_transfer import export_data, import_data
from logger_config import get_logger, query_logs
from runtime_security import validate_runtime_security
from secure_http import SecureHttpError, request_bytes
from test_mode import enabled as test_adapter_enabled
from source_fetcher import SourceFetchError, fetch_source
from wechat_api import (
    WeChatApiError,
    _token_manager as _wechat_token_manager,
    create_draft as _wechat_create_draft,
    upload_cover_dedup as _wechat_upload_cover_dedup,
    wechat_api_call as _wechat_api_call,
)
from workflow import cancel_workflow, create_workflow, mark_interrupted_tasks, retry_workflow

access_logger = get_logger("access")
logger = get_logger("server")

# PyInstaller 打包后，资源在 sys._MEIPASS 临时目录中
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    ROOT = Path(sys._MEIPASS)
else:
    ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = ROOT / "web"
VERSION = "2.1.3"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# #154 CSRF 防护：双重提交 Cookie 模式
CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"


def _generate_csrf_token() -> str:
    """生成 CSRF 令牌（URL 安全的随机串）。"""
    return secrets.token_urlsafe(32)


def _build_csrf_cookie(token: str) -> str:
    """构建 csrf_token 的 Set-Cookie 头值。

    注意：不设置 HttpOnly —— 前端 JS 需读取该 Cookie 后通过 X-CSRF-Token 头回传，
    与 Cookie 中的值做双重提交校验。SameSite=Strict 阻止跨站自动携带。
    """
    secure = "; Secure" if os.environ.get("STUDIO_PUBLIC_ORIGIN", "").strip().lower().startswith("https://") else ""
    return f"{CSRF_COOKIE_NAME}={token}; Path=/; Max-Age=28800; SameSite=Strict{secure}"


def _build_clear_csrf_cookie() -> str:
    """构建清除 csrf_token 的 Set-Cookie 头值。"""
    secure = "; Secure" if os.environ.get("STUDIO_PUBLIC_ORIGIN", "").strip().lower().startswith("https://") else ""
    return f"{CSRF_COOKIE_NAME}=; Path=/; Max-Age=0; SameSite=Strict{secure}"


class ApiProblem(RuntimeError):
    def __init__(self, status: int, code: str, message: str, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _env_int(name: str, default: int) -> int:
    # P1-24: 安全读取环境变量整数，转换失败时返回默认值
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _mask_settings(bundle: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(bundle))
    ai = result.setdefault("ai", {})
    api_key = str(ai.get("apiKey") or "")
    stored_hint = str(ai.get("apiKeyHintStored") or "")
    ai["apiKeySet"] = bool(api_key)
    ai["apiKeyHint"] = ("••••" + (stored_hint or api_key[-4:])) if api_key else ""
    ai["apiKey"] = ""
    ai.pop("apiKeyHintStored", None)
    wechat = result.setdefault("wechat", {})
    secret = str(wechat.get("appSecret") or "")
    stored_secret_hint = str(wechat.get("appSecretHintStored") or "")
    wechat["appSecretSet"] = bool(secret)
    wechat["appSecretHint"] = ("••••" + (stored_secret_hint or secret[-4:])) if secret else ""
    wechat["appSecret"] = ""
    wechat.pop("appSecretHintStored", None)
    return result


def _require_bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ApiProblem(400, "invalid_settings", f"{name} 必须是布尔值")
    return value


def _require_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        raise ApiProblem(400, "invalid_settings", f"{name} 必须是整数")
    if value < minimum or value > maximum:
        raise ApiProblem(400, "invalid_settings", f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _require_number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiProblem(400, "invalid_settings", f"{name} 必须是数字")
    number = float(value)
    if number < minimum or number > maximum:
        raise ApiProblem(400, "invalid_settings", f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return number


def _require_text(value: Any, name: str, maximum: int, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ApiProblem(400, "invalid_request", f"{name} 必须是字符串")
    if len(value) > maximum:
        raise ApiProblem(400, "invalid_request", f"{name} 不能超过 {maximum} 个字符")
    if not allow_empty and not value.strip():
        raise ApiProblem(400, "invalid_request", f"{name} 不能为空")
    return value


def _validate_general_settings(incoming: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    allowed = {"defaultLength", "strictFacts", "allowNetwork"}
    unknown = set(incoming) - allowed
    if unknown:
        raise ApiProblem(400, "invalid_settings", f"不支持的通用设置：{', '.join(sorted(unknown))}")
    merged = dict(existing)
    if "defaultLength" in incoming:
        merged["defaultLength"] = _require_int(incoming["defaultLength"], "默认字数", 300, 20_000)
    if "strictFacts" in incoming:
        merged["strictFacts"] = _require_bool(incoming["strictFacts"], "严格事实模式")
    if "allowNetwork" in incoming:
        merged["allowNetwork"] = _require_bool(incoming["allowNetwork"], "允许联网")
    return merged


def _validate_ai_settings(incoming: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    allowed = {"providerId", "baseUrl", "apiKey", "model", "temperature", "autoReview", "maxTokens"}
    unknown = set(incoming) - allowed
    if unknown:
        raise ApiProblem(400, "invalid_settings", f"不支持的 AI 设置：{', '.join(sorted(unknown))}")
    merged = _merge_secret(existing, incoming, "apiKey")
    if str(merged.get("providerId") or "") != "openai-compatible":
        raise ApiProblem(400, "invalid_provider", "当前仅支持 openai-compatible Provider ID")
    merged["model"] = _require_text(str(merged.get("model") or ""), "模型名称", 120, allow_empty=False).strip()
    if "temperature" in incoming:
        merged["temperature"] = _require_number(incoming["temperature"], "温度", 0, 2)
    if "autoReview" in incoming:
        merged["autoReview"] = _require_bool(incoming["autoReview"], "自动审校")
    if "maxTokens" in incoming:
        merged["maxTokens"] = int(_require_number(incoming["maxTokens"], "最大 tokens", 1024, 16384))
    if "apiKey" in incoming and not isinstance(incoming["apiKey"], str):
        raise ApiProblem(400, "invalid_settings", "API Key 必须是字符串")
    if len(str(merged.get("apiKey") or "")) > 1_024:
        raise ApiProblem(400, "invalid_settings", "API Key 过长")
    _validate_ai_endpoint(merged, existing, incoming)
    return merged


def _parse_cover_data_url(value: str) -> tuple[str, bytes] | None:
    if not value:
        return None
    if len(value) > 3_000_000:
        raise ApiProblem(400, "cover_too_large", "封面图片过大")
    match = re.fullmatch(r"data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=\s]+)", value)
    if not match:
        raise ApiProblem(400, "cover_invalid", "封面必须是 PNG、JPEG、WEBP 或 GIF Data URL")
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except Exception as exc:
        raise ApiProblem(400, "cover_invalid", "封面 Base64 数据无效") from exc
    if not raw or len(raw) > 2_000_000:
        raise ApiProblem(400, "cover_too_large", "封面解码后必须小于 2MB")
    signatures = {
        "image/png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": raw.startswith(b"\xff\xd8\xff"),
        "image/gif": raw.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP",
    }
    mime = match.group(1)
    if not signatures.get(mime, False):
        raise ApiProblem(400, "cover_invalid", "封面实际文件类型与声明不一致")
    return mime, raw



def _project_sources(project_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.* FROM project_sources ps
            JOIN source_snapshots s ON s.id = ps.snapshot_id
            WHERE ps.project_id = ? ORDER BY ps.source_order, s.fetched_at
            """,
            (project_id,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "sourceUrl": row["source_url"],
            "finalUrl": row["final_url"],
            "title": row["title"],
            "publisher": row["publisher"],
            "author": row["author"],
            "publishedAt": row["published_at"],
            "preview": row["preview"],
            "contentHash": row["content_hash"],
            "fetchedAt": row["fetched_at"],
            "extractionMethod": row["extraction_method"],
        }
        for row in rows
    ]


def _project_versions(project_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT revision, reason, created_at, snapshot_json FROM project_versions WHERE project_id=? ORDER BY id DESC LIMIT 50",
            (project_id,),
        ).fetchall()
    return [
        {
            "revision": row["revision"],
            "reason": row["reason"],
            "createdAt": row["created_at"],
            "snapshot": json.loads(row["snapshot_json"]),
        }
        for row in rows
    ]


def _body_fingerprint(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _merge_secret(existing: dict[str, Any], incoming: dict[str, Any], field: str) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if key.endswith("Set") or key.endswith("Hint"):
            continue
        if key == field and not str(value or "").strip():
            continue
        merged[key] = value
    return merged


def _test_adapter_enabled(name: str) -> bool:
    return test_adapter_enabled(name)

def _validate_ai_endpoint(config: dict[str, Any], existing: dict[str, Any] | None = None, incoming: dict[str, Any] | None = None) -> None:
    base=str(config.get("baseUrl") or "").strip().rstrip("/")
    parsed=urllib.parse.urlsplit(base)
    is_test = _test_adapter_enabled("STUDIO_TEST_AI")
    if not is_test:
        if parsed.scheme.lower() != "https":
            raise ApiProblem(400,"ai_base_url_invalid","AI Base URL 必须使用 HTTPS（明文 HTTP 不被允许）")
    else:
        if parsed.scheme.lower() not in {"http", "https"}:
            raise ApiProblem(400,"ai_base_url_invalid","AI Base URL 必须使用 HTTP 或 HTTPS")
    if parsed.username or parsed.password or not parsed.hostname:
        raise ApiProblem(400,"ai_base_url_invalid","AI Base URL 无效")
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    if port not in {80, 443}:
        raise ApiProblem(400,"ai_base_url_invalid","AI Base URL 端口必须在 80 或 443")
    if existing is not None and incoming is not None:
        old=str(existing.get("baseUrl") or "").rstrip("/")
        changed=bool(old and base and old != base)
        if changed and not str(incoming.get("apiKey") or "").strip():
            raise ApiProblem(400,"ai_key_reentry_required","修改 AI Base URL 时必须重新输入 API Key")

def _test_ai_connectivity(base_url: str, api_key: str) -> dict[str, Any]:
    """通过 secure_http 安全测试 AI 服务连通性"""
    url = base_url + "/models"
    try:
        response = request_bytes(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "WeiXinGZH-Studio/2.1.3",
            },
            timeout=20,
            max_bytes=1_000_000,
            require_https=True,
            reject_redirects=True,
        )
        return {"ok": True, "message": f"模型服务连接成功（HTTP {response.status}）"}
    except SecureHttpError as exc:
        status = exc.status
        # #141 返回结构化错误码，便于前端区分失败原因
        if status is not None and 300 <= status < 500:
            if status in (401, 403):
                raise ApiProblem(400, "invalid_key", "API Key 无效或无权限访问模型服务") from exc
            if status == 404:
                raise ApiProblem(400, "model_not_found", "模型服务未找到指定资源，请检查 Base URL 或模型名称") from exc
            if status == 429:
                raise ApiProblem(400, "quota_exceeded", "模型服务请求配额已用尽，请稍后重试或更换 API Key") from exc
            # 其余 3xx/4xx 表示服务可达（如 301、400），仅记录但不判定为失败
            return {"ok": True, "message": f"模型服务可达（HTTP {status}）"}
        if status is None:
            # 连接/解析类错误（DNS 失败、连接超时、SSL 错误等）
            raise ApiProblem(400, "network_error", f"无法连接模型服务（{exc.code}），请检查网络或 Base URL") from exc
        # 5xx 等服务端错误
        raise ApiProblem(400, "network_error", f"模型服务返回 HTTP {status}，暂时不可用") from exc

def _verify_ai(config: dict[str, Any]) -> dict[str, Any]:
    if not bool(get_setting("general").get("allowNetwork", True)) and not _test_adapter_enabled("STUDIO_TEST_AI"):
        raise ApiProblem(409, "network_disabled", "联网能力已关闭，不能验证 AI 连接")
    _validate_ai_endpoint(config)
    configured = bool(str(config.get("apiKey") or "")) or _test_adapter_enabled("STUDIO_TEST_AI")
    try:
        if _test_adapter_enabled("STUDIO_TEST_AI"):
            engine = AIEngine(config)
            result = engine.plan("健康检查", "测试来源", False)
            outcome = {"ok": bool(result.get("outline")), "message": "测试适配器可用"}
        else:
            key = str(config.get("apiKey") or "")
            if not key:
                raise ApiProblem(400, "ai_key_required", "请填写 AI API Key")
            base_url = str(config.get("baseUrl") or "").rstrip("/")
            outcome = _test_ai_connectivity(base_url, key)
        set_health_check("ai", configured=configured, reachable=bool(outcome.get("ok")), message=outcome["message"])
        return outcome
    except ApiProblem as exc:
        set_health_check("ai", configured=configured, reachable=False, message=str(exc))
        raise




def _wechat_token(app_id: str, app_secret: str) -> str:
    """通过 WeChatTokenManager 获取 access_token（带缓存和并发去重）。"""
    try:
        return _wechat_token_manager.get_token(app_id, app_secret)
    except WeChatApiError as exc:
        raise ApiProblem(400, exc.code, str(exc)) from exc


def _verify_and_save_wechat(incoming: dict[str, Any]) -> dict[str, Any]:
    if not bool(get_setting("general").get("allowNetwork", True)) and not _test_adapter_enabled("STUDIO_TEST_WECHAT"):
        raise ApiProblem(409, "network_disabled", "联网能力已关闭，不能验证微信公众号连接")
    if not isinstance(incoming, dict):
        raise ApiProblem(400, "invalid_settings", "微信公众号设置必须是对象")
    allowed = {"accountName", "appId", "appSecret", "thumbMediaId"}
    unknown = set(incoming) - allowed
    if unknown:
        raise ApiProblem(400, "invalid_settings", f"不支持的微信设置：{', '.join(sorted(unknown))}")
    existing = get_setting("wechat")
    merged = _merge_secret(existing, incoming, "appSecret")
    app_id = _require_text(str(merged.get("appId") or ""), "AppID", 128, allow_empty=False).strip()
    app_secret = _require_text(str(merged.get("appSecret") or ""), "AppSecret", 512, allow_empty=False).strip()
    if "accountName" in merged:
        merged["accountName"] = _require_text(str(merged.get("accountName") or ""), "公众号名称", 120)
    if "thumbMediaId" in merged:
        merged["thumbMediaId"] = _require_text(str(merged.get("thumbMediaId") or ""), "封面 Media ID", 256)
    try:
        _wechat_token(app_id, app_secret)
    except ApiProblem as exc:
        set_health_check("wechat", configured=True, reachable=False, message=str(exc))
        raise
    merged["verifiedAt"] = utc_now()
    set_setting("wechat", merged)
    set_health_check("wechat", configured=True, reachable=True, message="微信公众号凭证验证成功")
    return _mask_settings({"wechat": merged})["wechat"]


def _wechat_upload_cover(token: str, cover_data_url: str, *, app_id: str = "", app_secret: str = "") -> str:
    """通过 wechat_api 上传封面，带去重和 errcode 处理。"""
    try:
        return _wechat_upload_cover_dedup(
            token, cover_data_url, app_id=app_id, app_secret=app_secret,
        )
    except WeChatApiError as exc:
        raise ApiProblem(502, exc.code, str(exc)) from exc


def _validate_publish_content(project: dict[str, Any], content_html: str) -> None:
    title = str(project.get("title") or "").strip()
    summary = str(project.get("summary") or "").strip()
    body = str(project.get("bodyMarkdown") or "")
    if not title:
        raise ApiProblem(400, "publish_title_required", "发布标题不能为空")
    if len(title) > 64:
        raise ApiProblem(400, "publish_title_too_long", "发布标题不能超过 64 个字符")
    if len(summary) > 120:
        raise ApiProblem(400, "publish_summary_too_long", "发布摘要不能超过 120 个字符")
    if not body.strip():
        raise ApiProblem(400, "publish_body_required", "发布正文不能为空")
    if len(content_html.encode("utf-8")) > 1_000_000:
        raise ApiProblem(400, "publish_content_too_large", "渲染后的发布内容超过本地安全上限")
    # 仅拦截真实外部图片 URL（需上传至微信素材库），放行 placeholder 占位建议
    if re.search(r"!\[[^]]*]\(https?://[^)\s]+\)", body):
        raise ApiProblem(400, "inline_images_not_uploaded", "正文包含尚未上传到微信素材库的外部图片，请先移除或转换")


def _publish_snapshot(project: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    revision = request.get("revision")
    fingerprint = str(request.get("bodyFingerprint") or "")
    preview_hash = str(request.get("previewHash") or "")
    if type(revision) is not int:
        raise ApiProblem(428, "revision_required", "发布必须携带当前 revision")
    current = get_project(project["id"])
    if not current or current["revision"] != revision:
        raise ApiProblem(409, "revision_conflict", "发布前文章版本已变化", {"server": current})
    actual_fingerprint = _body_fingerprint(current["bodyMarkdown"])
    if not fingerprint or not hmac.compare_digest(fingerprint, actual_fingerprint):
        raise ApiProblem(409, "publish_fingerprint_mismatch", "发布正文指纹与服务端不一致")
    # #188 发布内容同样经过服务端消毒，与预览端点保持一致的 previewHash
    content_html = _render_wechat_html(current["bodyMarkdown"])
    actual_preview_hash = hashlib.sha256(content_html.encode("utf-8")).hexdigest()
    if not preview_hash or not hmac.compare_digest(preview_hash, actual_preview_hash):
        raise ApiProblem(409, "publish_preview_stale", "发布预览已过期，请重新预览")
    if not current["reviewApproved"]:
        raise ApiProblem(409, "review_required", "同步草稿前必须完成人工终审")
    if current["reviewRevision"] != revision or current["reviewFingerprint"] != actual_fingerprint:
        raise ApiProblem(409, "review_stale", "正文或版本已变化，请重新完成人工终审")
    _validate_publish_content(current, content_html)
    # P1-15: 设置 revision 占位，防止并发发布产生多个微信草稿
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE projects SET publish_status='syncing', updated_at=? WHERE id=? AND revision=? AND publish_status != 'synced'",
            (utc_now(), current["id"], revision),
        )
        if cursor.rowcount != 1:
            raise ApiProblem(409, "publish_in_progress", "文章正在发布中或版本已变化，请刷新后重试")
    return {
        "projectId": current["id"],
        "revision": revision,
        "title": current["title"],
        "summary": current["summary"],
        "bodyMarkdown": current["bodyMarkdown"],
        "bodyFingerprint": actual_fingerprint,
        "contentHtml": content_html,
        "previewHash": actual_preview_hash,
        "coverDataUrl": current["coverDataUrl"],
    }


def _wechat_publish(project: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    if not bool(get_setting("general").get("allowNetwork", True)) and not _test_adapter_enabled("STUDIO_TEST_WECHAT"):
        raise ApiProblem(409, "network_disabled", "联网能力已关闭，不能同步微信公众号草稿")
    snapshot = _publish_snapshot(project, request)
    settings = get_setting("wechat")
    app_id = str(settings.get("appId") or "")
    secret = str(settings.get("appSecret") or "")
    if not app_id or not secret:
        raise ApiProblem(400, "wechat_not_configured", "请先在设置中验证微信公众号凭证")
    token = _wechat_token(app_id, secret)
    thumb_media_id = _wechat_upload_cover(token, snapshot["coverDataUrl"], app_id=app_id, app_secret=secret)
    if not thumb_media_id:
        thumb_media_id = str(settings.get("thumbMediaId") or "").strip()
    if not thumb_media_id:
        raise ApiProblem(400, "wechat_cover_media_required", "请上传封面或填写已上传到微信素材库的封面 Media ID")

    response_payload: dict[str, Any]
    if _test_adapter_enabled("STUDIO_TEST_WECHAT"):
        delay = float(os.environ.get("STUDIO_TEST_WECHAT_DELAY", "0") or 0)
        if delay > 0:
            time.sleep(delay)
        remote_id = "media_test_" + uuid.uuid4().hex[:12]
        response_payload = {"media_id": remote_id, "testAdapter": True}
    else:
        payload = {
            "articles": [
                {
                    "title": snapshot["title"],
                    "author": "",
                    "digest": snapshot["summary"][:120],
                    "content": snapshot["contentHtml"],
                    "content_source_url": "",
                    "thumb_media_id": thumb_media_id,
                    "need_open_comment": 0,
                    "only_fans_can_comment": 0,
                }
            ]
        }
        try:
            remote_id = _wechat_create_draft(
                token, payload, app_id=app_id, app_secret=secret,
            )
        except WeChatApiError as exc:
            raise ApiProblem(502, exc.code, str(exc)) from exc
        response_payload = {"media_id": remote_id}

    now = utc_now()
    try:
        with connect() as conn:
            cursor = conn.execute(
                """
                UPDATE projects SET publish_status='synced',publish_remote_id=?,published_revision=?,
                    publish_fingerprint=?,publish_preview_hash=?,updated_at=?
                WHERE id=? AND revision=?
                """,
                (
                    remote_id,
                    snapshot["revision"],
                    snapshot["bodyFingerprint"],
                    snapshot["previewHash"],
                    now,
                    snapshot["projectId"],
                    snapshot["revision"],
                ),
            )
            receipt_status = "current" if cursor.rowcount == 1 else "stale"
            conn.execute(
                """
                INSERT INTO publish_receipts(project_id,revision,body_fingerprint,preview_hash,remote_id,status,response_json,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    snapshot["projectId"],
                    snapshot["revision"],
                    snapshot["bodyFingerprint"],
                    snapshot["previewHash"],
                    remote_id,
                    receipt_status,
                    json.dumps(response_payload, ensure_ascii=False),
                    now,
                ),
            )
    except Exception as db_exc:
        # P0-2: DB写入失败，尝试补偿删除微信侧草稿
        logger.error("发布DB写入失败，尝试补偿删除微信草稿: %s", db_exc)
        if not _test_adapter_enabled("STUDIO_TEST_WECHAT"):
            try:
                _wechat_api_call(
                    "https://api.weixin.qq.com/cgi-bin/draft/delete?access_token="
                    + urllib.parse.quote(token),
                    method="POST",
                    body=json.dumps(
                        {"media_id": remote_id}, ensure_ascii=False
                    ).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json; charset=utf-8",
                        "Accept": "application/json",
                    },
                    app_id=app_id,
                    app_secret=secret,
                )
                logger.info("补偿删除微信草稿成功: %s", remote_id)
            except Exception as comp_exc:
                logger.error("补偿删除微信草稿也失败: %s", comp_exc)
        raise ApiProblem(500, "publish_db_failed", f"发布记录写入失败，已尝试补偿删除微信草稿: {db_exc}") from db_exc
    return {
        "remoteId": remote_id,
        "syncedAt": now,
        "revision": snapshot["revision"],
        "bodyFingerprint": snapshot["bodyFingerprint"],
        "previewHash": snapshot["previewHash"],
        "status": receipt_status,
        "currentArticleUpdated": receipt_status == "current",
    }




def _markdown_to_wechat_html(markdown: str) -> str:
    def escape_text(text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
        )

    def safe_url(url: str) -> str:
        """P1-10: 仅允许 http/https/data:image 协议的 URL。"""
        url = url.strip()
        if url.startswith(("http://", "https://", "data:image/")):
            return url
        if url.startswith("#") or url.startswith("/"):
            return url  # 锚点和相对路径允许
        return ""  # 其他协议（javascript:等）拒绝

    def inline(text: str) -> str:
        escaped = escape_text(text)
        escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(
            r"\[([^]]+)]\((https://[^)\s]+)\)",
            lambda m: (
                f'<a href="{safe_url(m.group(2))}">{m.group(1)}</a>'
                if safe_url(m.group(2))
                else m.group(1)
            ),
            escaped,
        )
        return escaped

    lines = markdown.splitlines()
    output: list[str] = []
    list_type = ""
    in_code = False
    code_lines: list[str] = []

    def close_list() -> None:
        nonlocal list_type
        if list_type:
            output.append(f"</{list_type}>")
            list_type = ""

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            close_list()
            if in_code:
                code = "\n".join(code_lines).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                output.append(f"<pre><code>{code}</code></pre>")
                code_lines = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not stripped:
            close_list()
            continue
        # 图片语法 ![alt](url) — 必须在列表/标题/链接之前匹配
        img_match = re.match(r"^!\[([^\]]*)]\(([^)\s]+)\)\s*$", stripped)
        if img_match:
            close_list()
            alt = escape_text(img_match.group(1))
            raw_src = img_match.group(2)
            if raw_src == "placeholder":
                output.append(f'<p class="img-suggestion">📷 {alt}</p>')
            else:
                # P1-10: 校验图片 URL 协议白名单
                safe_src = safe_url(raw_src)
                if not safe_src:
                    # 非法协议（javascript: 等），渲染为占位建议
                    output.append(f'<p class="img-suggestion">📷 {alt}</p>')
                else:
                    src = escape_text(safe_src)
                    output.append(f'<p><img src="{src}" alt="{alt}" /></p>')
            continue
        ordered = re.match(r"^(\d+)\.\s+(.+)$", stripped)
        if stripped.startswith(("- ", "* ")) or ordered:
            wanted = "ol" if ordered else "ul"
            if list_type != wanted:
                close_list()
                output.append(f"<{wanted}>")
                list_type = wanted
            content = ordered.group(2) if ordered else stripped[2:]
            output.append(f"<li>{inline(content)}</li>")
            continue
        close_list()
        if stripped.startswith("### "):
            output.append(f"<h3>{inline(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            output.append(f"<h2>{inline(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            output.append(f"<h1>{inline(stripped[2:])}</h1>")
        elif stripped.startswith("> "):
            output.append(f"<blockquote>{inline(stripped[2:])}</blockquote>")
        else:
            output.append(f"<p>{inline(stripped)}</p>")
    close_list()
    if in_code:
        code = "\n".join(code_lines).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        output.append(f"<pre><code>{code}</code></pre>")
    return "".join(output)


def _sanitize_preview_html(html: str) -> str:
    """#188 服务端预览 HTML 消毒：移除危险标签与属性，防御 XSS。

    _markdown_to_wechat_html 已对文本做转义并校验 URL 协议白名单，此处作为
    纵深防御层，剥离 <script>/<iframe> 等危险标签、on* 事件属性与
    javascript:/vbscript: 协议，确保发送给客户端的预览 HTML 安全。
    """
    # 移除完整的危险标签块（script/iframe/object/embed/svg/math）
    html = re.sub(r"(?is)<\s*(script|iframe|object|embed|svg|math)\b[^>]*>.*?<\s*/\s*\1\s*>", "", html)
    # 移除自闭合或未闭合的危险标签
    html = re.sub(r"(?is)<\s*/?\s*(script|iframe|object|embed)\b[^>]*>", "", html)
    # 移除所有 on* 事件处理器属性（onclick、onerror 等）
    html = re.sub(r"(?i)\s+on[a-z]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", "", html)
    # 将 javascript:/vbscript: 协议的 href/src 置空
    html = re.sub(
        r"(?i)(href|src)\s*=\s*(\"javascript:[^\"]*\"|'javascript:[^']*'|javascript:[^\s>]+)",
        lambda m: m.group(1) + '=""',
        html,
    )
    html = re.sub(
        r"(?i)(href|src)\s*=\s*(\"vbscript:[^\"]*\"|'vbscript:[^']*'|vbscript:[^\s>]+)",
        lambda m: m.group(1) + '=""',
        html,
    )
    return html


def _render_wechat_html(markdown: str) -> str:
    """渲染 Markdown 为微信公众号 HTML，并做服务端消毒。

    #188 预览端点与发布流程统一使用本函数，保证 previewHash 一致，
    且发布到微信的内容同样经过服务端消毒。
    """
    return _sanitize_preview_html(_markdown_to_wechat_html(markdown))


def _check_published_stale_status() -> list[dict[str, Any]]:
    """#068 检查已发布文章的远程草稿是否已过期（stale）。

    检测 publish_status='synced' 但 published_revision 与当前 revision 不一致
    （即发布后被编辑）的文章，返回需要提醒的项目列表，供 SSE 轮询时推送。
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, revision, published_revision, publish_remote_id
            FROM projects
            WHERE deleted=0 AND publish_status='synced' AND published_revision != revision
            ORDER BY updated_at DESC LIMIT 50
            """
        ).fetchall()
    return [
        {
            "projectId": row["id"],
            "title": row["title"],
            "currentRevision": int(row["revision"]),
            "publishedRevision": int(row["published_revision"]),
            "remoteId": row["publish_remote_id"],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# 用户认证：一次性管理员初始化
# ---------------------------------------------------------------------------

INITIAL_USERNAME = "admin"
LEGACY_INITIAL_PASSWORD_FILE = ".initial_password"
_admin_setup_lock = threading.Lock()


def _is_unclaimed_legacy_admin(rows: list[sqlite3.Row]) -> bool:
    """识别旧版自动创建、但从未成功登录的随机密码账号。"""
    if len(rows) != 1:
        return False
    row = rows[0]
    return (
        row["username"] == INITIAL_USERNAME
        and bool(row["must_change_password"])
        and not str(row["last_login_at"] or "").strip()
    )


def admin_setup_required() -> bool:
    """无用户，或仅存在旧版未领取 admin 时，允许一次性初始化。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, username, must_change_password, last_login_at "
            "FROM users ORDER BY created_at LIMIT 2"
        ).fetchall()
    return not rows or _is_unclaimed_legacy_admin(rows)


def _remove_legacy_initial_password() -> None:
    """清理 2.1.3 及之前遗留的隐藏初始密码文件。"""
    try:
        from db import db_path

        (db_path().parent / LEGACY_INITIAL_PASSWORD_FILE).unlink(missing_ok=True)
    except OSError:
        pass

class StudioHandler(BaseHTTPRequestHandler):
    server_version = "WeiXinGZHStudio/2.1.3"
    _rate_lock = threading.Lock()
    _rate_windows: dict[str, collections.deque[float]] = {}

    def _get_client_ip(self) -> str:
        """获取真实客户端 IP。

        N4: 支持反向代理部署。当请求来自受信任的代理 IP 时，
        读取 X-Forwarded-For 头获取真实客户端 IP；
        否则回退到 TCP 连接的远端地址。
        """
        direct_ip = self.client_address[0]
        # 仅当配置了受信任代理列表时才解析 X-Forwarded-For
        trusted_proxies = os.environ.get("STUDIO_TRUSTED_PROXIES", "").strip()
        if not trusted_proxies:
            return direct_ip
        trusted_set = {ip.strip() for ip in trusted_proxies.split(",") if ip.strip()}
        if direct_ip not in trusted_set:
            return direct_ip
        # 从 X-Forwarded-For 取最左侧（最原始）的客户端 IP
        xff = self.headers.get("X-Forwarded-For", "").strip()
        if xff:
            # X-Forwarded-For 可能包含多个 IP: client, proxy1, proxy2
            # 取第一个（最原始的客户端 IP）
            first_ip = xff.split(",")[0].strip()
            # P1-8: 校验 XFF 值的 IP 格式，拒绝非法值
            if first_ip:
                try:
                    ipaddress.ip_address(first_ip)
                    return first_ip
                except ValueError:
                    pass  # Invalid IP format, fall through
        # 回退到 X-Real-IP
        xri = self.headers.get("X-Real-IP", "").strip()
        if xri:
            # P1-8: 同样校验 X-Real-IP 的格式
            try:
                ipaddress.ip_address(xri)
                return xri
            except ValueError:
                pass  # Invalid IP format, fall through
        return direct_ip

    def _check_rate_limit(self) -> None:
        if self.command not in MUTATING:
            return
        now = time.monotonic()
        key = self._get_client_ip()
        with self._rate_lock:
            # P1-8: 限制 _rate_windows 字典大小，防止内存增长
            if len(self._rate_windows) > 10000:
                # 清理空窗口
                stale_keys = [k for k, v in self._rate_windows.items() if not v]
                for k in stale_keys:
                    del self._rate_windows[k]
            window = self._rate_windows.setdefault(key, collections.deque())
            while window and now - window[0] > 60:
                window.popleft()
            if len(window) >= 120:
                raise ApiProblem(429, "rate_limited", "写操作过于频繁，请稍后重试")
            window.append(now)

    def _check_login_rate_limit(self) -> None:
        """登录限流：每 IP 每分钟最多 5 次尝试。

        S3 审计修复：将登录尝试计数从内存 dict 改为数据库持久化，
        进程重启后仍能保持限流状态，防止通过重启绕过限流。
        """
        ip = self._get_client_ip()
        now = utc_now()
        # 计算最近 1 分钟的时间边界
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=60)).replace(
            microsecond=0
        ).isoformat()
        with connect() as conn:
            # 顺便清理过期记录，避免表无限增长
            conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM login_attempts WHERE ip=? AND attempted_at >= ?",
                (ip, cutoff),
            ).fetchone()
            count = int(row["cnt"]) if row else 0
            if count >= 5:
                raise ApiProblem(429, "too_many_attempts", "登录尝试过于频繁，请稍后再试")
            conn.execute(
                "INSERT INTO login_attempts(ip, attempted_at) VALUES(?, ?)",
                (ip, now),
            )

    def _get_session_token(self) -> str | None:
        """从 Cookie 头中解析会话令牌。"""
        cookie_header = self.headers.get("Cookie", "")
        return parse_cookie(cookie_header, SESSION_COOKIE_NAME)

    def _require_session(self) -> dict[str, Any] | None:
        """验证会话，返回会话信息或发送 401 并返回 None。"""
        token = self._get_session_token()
        if not token:
            self._send_json(401, {"error": {"code": "unauthenticated", "message": "请先登录"}})
            return None
        session = get_session(token)
        if not session:
            self._send_json(401, {"error": {"code": "session_expired", "message": "会话已过期，请重新登录"}})
            return None
        return session

    def log_message(self, fmt: str, *args: Any) -> None:
        """禁用 BaseHTTPRequestHandler 默认日志，访问日志由 _log_access 统一记录。"""
        pass

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            # #161 CSP：合并审计要求的指令与既有的安全收紧指令。
            # 包含 font-src 'self' data: 与 style-src 'self' 'unsafe-inline'，
            # 同时保留 frame-ancestors/base-uri/form-action 收紧策略。
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        self.send_header("Cache-Control", "no-store" if self.path.startswith("/api/") else "no-cache")

    def _check_origin(self) -> None:
        if self.command not in MUTATING:
            return
        origin=self.headers.get("Origin","").strip()
        host_header=self.headers.get("Host","").strip().lower()
        bind_host,bind_port=self.server.server_address[:2]
        public=os.environ.get("STUDIO_PUBLIC_ORIGIN","").strip().rstrip("/")
        allowed_hosts={f"127.0.0.1:{bind_port}",f"localhost:{bind_port}",f"[::1]:{bind_port}"}
        allowed_origins={f"http://127.0.0.1:{bind_port}",f"http://localhost:{bind_port}",f"http://[::1]:{bind_port}"}
        if public:
            parsed=urllib.parse.urlsplit(public)
            if not parsed.scheme or not parsed.netloc: raise ApiProblem(500,"public_origin_invalid","STUDIO_PUBLIC_ORIGIN 配置无效")
            allowed_hosts.add(parsed.netloc.lower()); allowed_origins.add(public)
        if host_header not in allowed_hosts:
            raise ApiProblem(403,"host_forbidden","Host 不在服务端允许列表")
        # S3 CSRF 防护：MUTATING 请求必须携带 Origin 头，缺失即拒绝。
        # 浏览器对同源和跨域的 POST/PUT/DELETE/PATCH 请求始终发送 Origin 头，
        # 缺失 Origin 意味着请求可能来自非浏览器客户端（如 CSRF 攻击脚本）。
        if not origin:
            raise ApiProblem(403,"origin_required","缺少 Origin 头，无法验证请求来源")
        if origin.rstrip("/") not in allowed_origins:
            raise ApiProblem(403,"origin_forbidden","请求来源不被允许")

    def _check_csrf(self) -> None:
        """#154 双重提交 Cookie 模式的 CSRF 校验。

        对写操作（POST/PUT/PATCH/DELETE）校验 Cookie 中的 csrf_token 与
        X-CSRF-Token 请求头是否一致。登录端点豁免（首次签发令牌）。

        未携带 csrf_token Cookie 的客户端（如非浏览器脚本、测试客户端）不强制
        校验，此类请求依赖 _check_origin 的 Origin 校验与 SameSite=Strict
        会话 Cookie 提供防护；浏览器登录后会自动携带该 Cookie，因此必须回传
        匹配的 X-CSRF-Token 头，攻击者无法跨站读取 Cookie 值故无法伪造。
        """
        if self.command not in MUTATING:
            return
        request_path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        # 登录/初始化端点豁免：CSRF 令牌在此首次签发。
        # Origin/Host 校验仍在 _check_origin 中强制执行。
        if request_path in {"/api/v2/auth/login", "/api/v2/auth/setup"}:
            return
        cookie_token = parse_cookie(self.headers.get("Cookie", ""), CSRF_COOKIE_NAME)
        if not cookie_token:
            # 客户端未参与 CSRF 令牌流程，回退到 Origin + SameSite 防护
            return
        header_token = self.headers.get(CSRF_HEADER_NAME, "").strip()
        if not header_token:
            raise ApiProblem(403, "csrf_token_missing", "缺少 CSRF 令牌")
        if not hmac.compare_digest(cookie_token, header_token):
            raise ApiProblem(403, "csrf_token_mismatch", "CSRF 令牌校验失败")

    def _send_json(self, status: int, value: Any, *, extra_headers: dict[str, str] | None = None, set_cookies: list[str] | None = None) -> None:
        body = _json_bytes(value)
        # 访问日志只记录字节数，绝不复制响应正文。响应中可能包含文章、来源、
        # CSRF 令牌或第三方返回信息，截断后记录仍会造成隐私和凭证泄漏。
        self._resp_status = status
        self._resp_bytes = len(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for header_name, header_value in extra_headers.items():
                self.send_header(header_name, header_value)
        # #154 支持同时下发多个 Set-Cookie（如会话 Cookie 与 csrf_token Cookie）
        if set_cookies:
            for cookie in set_cookies:
                self.send_header("Set-Cookie", cookie)
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self, max_bytes: int = 3_000_000) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length", "0")
        try:
            length = int(length_header)
        except ValueError as exc:
            raise ApiProblem(400, "invalid_length", "Content-Length 无效") from exc
        if length < 0 or length > max_bytes:
            raise ApiProblem(413, "payload_too_large", "请求内容过大")
        raw = self.rfile.read(length) if length else b"{}"
        # 只记录大小，不记录原始请求体。登录密码、AI Key、微信 AppSecret 和文章
        # 正文都可能出现在这里，不能依赖事后正则脱敏来兜底。
        self._req_bytes = len(raw)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiProblem(400, "invalid_json", "请求 JSON 无效") from exc
        if not isinstance(value, dict):
            raise ApiProblem(400, "invalid_json", "请求 JSON 必须是对象")
        return value

    def _handle(self) -> None:
        _req_start = time.monotonic()
        # 初始化请求/响应摘要
        self._req_bytes = 0
        self._resp_status = 0
        self._resp_bytes = 0
        try:
            self._check_origin()
            self._check_csrf()
            self._check_rate_limit()
            if self.path.startswith("/api/"):
                self._route_api()
            else:
                self._serve_static()
        except ApiProblem as exc:
            self._send_json(exc.status, {"error": {"code": exc.code, "message": str(exc), "detail": exc.detail}})
        except KeyError as exc:
            self._send_json(404, {"error": {"code": "not_found", "message": str(exc).strip("'")}})
        except ValueError as exc:
            self._send_json(400, {"error": {"code": "invalid_request", "message": str(exc)}})
        except Exception as exc:  # noqa: BLE001
            # 内部异常细节只进入已脱敏服务端日志，不回传文件路径、SQL 或对象 repr。
            self._send_json(500, {"error": {"code": "internal_error", "message": "服务器发生未预期错误"}})
            access_logger.error("%s %s %s -> 500 internal_error (%.0fms): %s", self.address_string(), self.command, self.path, (time.monotonic() - _req_start) * 1000, exc, exc_info=exc)
        finally:
            # 仅对 API 请求记录结构化访问日志
            if self.path.startswith("/api/"):
                self._log_access(_req_start)

    def _log_access(self, req_start: float) -> None:
        """记录最小化访问日志，不记录正文、凭证或查询值。"""
        duration_ms = (time.monotonic() - req_start) * 1000
        status = self._resp_status or 0
        # 解析路径和查询参数
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        # 高频探针和日志查询不再制造新的访问日志，避免日志页面自我放大。
        if path in {"/api/v2/health", "/api/v2/logs", "/api/v2/tasks/events/stream"}:
            return
        query_keys = sorted(urllib.parse.parse_qs(parsed.query, keep_blank_values=True))

        # 构建日志消息：方法 路径 -> 状态码 (耗时)
        parts = [
            f'{self.command} {path}',
            f'-> {status}' if status else '-> (no response)',
            f'({duration_ms:.0f}ms)',
        ]
        if query_keys:
            parts.append(f'query_keys={query_keys}')
        if self._req_bytes:
            parts.append(f'req_bytes={self._req_bytes}')
        if self._resp_bytes:
            parts.append(f'resp_bytes={self._resp_bytes}')

        msg = ' '.join(parts)
        if status >= 500:
            access_logger.error("%s %s", self.address_string(), msg)
        elif status >= 400:
            access_logger.warning("%s %s", self.address_string(), msg)
        else:
            access_logger.info("%s %s", self.address_string(), msg)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

    def _route_api(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        method = self.command
        segments = [part for part in path.split("/") if part]
        query = urllib.parse.parse_qs(parsed.query)

        # --- 认证端点（无需会话） ---
        if path == "/api/v2/auth/setup" and method == "GET":
            self._send_json(200, {"needsSetup": admin_setup_required()})
            return
        if path == "/api/v2/auth/setup" and method == "POST":
            self._handle_admin_setup()
            return
        if path == "/api/v2/auth/login" and method == "POST":
            self._handle_login()
            return
        if path == "/api/v2/auth/session" and method == "GET":
            self._handle_session_info()
            return
        if path == "/api/v2/health" and method == "GET":
            ai = get_setting("ai")
            wechat = get_setting("wechat")
            checks = get_health_checks()
            ai_configured = bool(ai.get("apiKey")) or _test_adapter_enabled("STUDIO_TEST_AI")
            wechat_configured = bool(wechat.get("appId") and wechat.get("appSecret"))
            ai_state = {"configured": ai_configured, "reachable": False, "verifiedAt": "", "message": "尚未验证", **checks.get("ai", {})}
            wechat_state = {"configured": wechat_configured, "reachable": False, "verifiedAt": "", "message": "尚未验证", **checks.get("wechat", {})}
            self._send_json(
                200,
                {
                    "ok": True,
                    "version": VERSION,
                    "database": {"configured": True, "reachable": True, "message": "SQLite 可用"},
                    "ai": ai_state,
                    "wechat": wechat_state,
                    "aiConfigured": ai_configured,
                    "wechatConfigured": wechat_configured,
                    "networkAllowed": bool(get_setting("general").get("allowNetwork", True)),
                },
            )
            return

        # --- 以下端点需要会话认证 ---
        session = self._require_session()
        if not session:
            return

        if path == "/api/v2/auth/logout" and method == "POST":
            self._handle_logout()
            return
        if path == "/api/v2/auth/change-password" and method == "POST":
            self._handle_change_password(session)
            return

        # 首次登录必须先修改密码
        if session.get("must_change_password"):
            self._send_json(
                403,
                {"error": {"code": "password_change_required", "message": "请先修改初始密码"}},
            )
            return

        if path == "/api/v2/bootstrap" and method == "GET":
            self._send_json(
                200,
                {
                    "version": VERSION,
                    "projects": list_projects(include_archived=False, limit=50),
                    "projectTotal": count_projects(include_archived=False),
                    "projectCounts": {
                        "active": count_projects(include_archived=False),
                        "all": count_projects(include_archived=True),
                        "deleted": count_projects(include_deleted=True, deleted_only=True),
                    },
                    "tasks": list_tasks(limit=100),
                    "settings": _mask_settings(settings_bundle()),
                },
            )
            return

        if path == "/api/v2/workflows" and method == "POST":
            body = self._read_json()
            auto_review = body.get("autoReview")
            if auto_review is not None and type(auto_review) is not bool:
                raise ApiProblem(400, "invalid_auto_review", "autoReview 必须是布尔值")
            requirements = str(body.get("requirements") or "")
            result = create_workflow(str(body.get("sourceInput") or ""), auto_review=auto_review, requirements=requirements)
            self._send_json(202, result)
            return

        # U3 审计修复：工作流创建前的来源预览（不创建项目/任务）
        if path == "/api/v2/source/preview" and method == "POST":
            body = self._read_json()
            url = str(body.get("url") or "").strip()
            if not url:
                raise ApiProblem(400, "invalid_request", "缺少 url 参数")
            try:
                snapshot = fetch_source(url)
            except SourceFetchError as exc:
                raise ApiProblem(400, "source_fetch_failed", str(exc)) from exc
            self._send_json(
                200,
                {
                    "title": snapshot.title,
                    "preview": snapshot.preview,
                    "contentHash": snapshot.content_hash,
                    "publisher": snapshot.publisher,
                    "author": snapshot.author,
                },
            )
            return

        if path == "/api/v2/tasks" and method == "GET":
            limit = int(query.get("limit", ["100"])[0])
            offset = int(query.get("offset", ["0"])[0])
            items = list_tasks(limit=limit, offset=offset)
            status_filter = str(query.get("status", [""])[0])
            project_filter = str(query.get("projectId", [""])[0])
            if status_filter:
                items = [item for item in items if item["status"] == status_filter]
            if project_filter:
                items = [item for item in items if item["projectId"] == project_filter]
            self._send_json(200, {"items": items, "limit": limit, "offset": offset})
            return

        if path == "/api/v2/logs" and method == "GET":
            level = str(query.get("level", ["ALL"])[0]).upper()
            if level not in {"ALL", "INFO", "WARNING", "WARN", "ERROR", "DEBUG"}:
                level = "ALL"
            keyword = str(query.get("q", [""])[0]).strip()[:200]
            since = str(query.get("since", [""])[0]).strip()[:50]
            try:
                limit = int(query.get("limit", ["100"])[0])
            except ValueError:
                limit = 100
            limit = max(1, min(limit, 1000))
            logs = query_logs(level=level, q=keyword, since=since, limit=limit)
            self._send_json(200, {"total": len(logs), "logs": logs})
            return

        # P2 审计修复：SSE 实时推送活跃任务状态（必须在 tasks/{id} 路由之前匹配）
        if path == "/api/v2/tasks/events/stream" and method == "GET":
            # #186 SSE 端点需校验会话，会话过期则返回 401
            self._handle_sse_stream(self._get_session_token())
            return

        if len(segments) >= 4 and segments[:3] == ["api", "v2", "tasks"]:
            task_id = segments[3]
            if len(segments) == 4 and method == "GET":
                task = get_task(task_id)
                if not task:
                    raise KeyError("任务不存在")
                task["events"] = list_task_events(task_id)
                self._send_json(200, task)
                return
            if len(segments) == 5 and segments[4] == "logs" and method == "GET":
                try:
                    limit = int(query.get("limit", ["100"])[0])
                except ValueError:
                    limit = 100
                limit = max(1, min(limit, 1000))
                keyword = str(query.get("q", [""])[0]).strip()[:200]
                logs = query_logs(task_id=task_id, q=keyword, limit=limit)
                self._send_json(200, {"total": len(logs), "logs": logs})
                return
            if len(segments) == 5 and segments[4] == "cancel" and method == "POST":
                self._send_json(200, cancel_workflow(task_id))
                return
            if len(segments) == 5 and segments[4] == "retry" and method == "POST":
                body = self._read_json()
                self._send_json(202, retry_workflow(task_id, str(body.get("retryMode") or "review_only")))
                return
            # R1-resume 审计修复：恢复因服务重启中断的失败任务
            if len(segments) == 5 and segments[4] == "resume" and method == "POST":
                task = get_task(task_id)
                if not task:
                    raise KeyError("任务不存在")
                if task["status"] != "failed" or task["errorCode"] != "server_restarted":
                    raise ApiProblem(
                        409, "resume_not_allowed",
                        "仅因服务重启导致失败（error_code=server_restarted）的任务可以恢复",
                    )
                result = retry_workflow(task_id, retry_mode="full")
                self._send_json(202, result)
                return

        if path == "/api/v2/projects" and method == "GET":
            include_deleted = str(query.get("includeDeleted", ["false"])[0]).lower() == "true"
            include_archived = str(query.get("includeArchived", ["true"])[0]).lower() == "true"
            deleted_only = str(query.get("deletedOnly", ["false"])[0]).lower() == "true"
            search = str(query.get("q", [""])[0]).strip()[:200]
            try:
                limit = int(query.get("limit", ["50"])[0])
                offset = int(query.get("offset", ["0"])[0])
            except ValueError as exc:
                raise ApiProblem(400, "pagination_invalid", "分页参数必须是整数") from exc
            normalized_limit = max(1, min(limit, 1000))
            normalized_offset = max(0, offset)
            total = count_projects(
                include_archived=include_archived,
                include_deleted=include_deleted,
                deleted_only=deleted_only,
                search=search,
            )
            items = list_projects(
                include_archived=include_archived,
                include_deleted=include_deleted,
                deleted_only=deleted_only,
                search=search,
                limit=normalized_limit,
                offset=normalized_offset,
            )
            self._send_json(
                200,
                {
                    "items": items,
                    "limit": normalized_limit,
                    "offset": normalized_offset,
                    "total": total,
                    "hasMore": normalized_offset + len(items) < total,
                },
            )
            return
        if path == "/api/v2/projects" and method == "POST":
            raise ApiProblem(410, "workflow_required", "新文章只能通过统一创作入口创建")
        if path == "/api/v2/generation/jobs" and method == "POST":
            raise ApiProblem(410, "workflow_required", "旧生成接口已停用，请使用 /api/v2/workflows")

        # U4 审计修复：批量操作（归档/删除/恢复）
        if path == "/api/v2/projects/batch" and method == "POST":
            body = self._read_json()
            action = str(body.get("action") or "")
            ids = body.get("ids")
            if action not in ("archive", "delete", "restore"):
                raise ApiProblem(400, "invalid_action", "action 必须是 archive、delete 或 restore")
            if not isinstance(ids, list) or not ids:
                raise ApiProblem(400, "invalid_request", "ids 必须是非空数组")
            if len(ids) > 100:
                raise ApiProblem(400, "too_many_ids", "单次最多操作 100 个 ID")
            normalized_ids = [str(i) for i in ids if isinstance(i, str) and i.strip()]
            if not normalized_ids:
                raise ApiProblem(400, "invalid_request", "ids 不能为空")
            now = utc_now()
            # #189 逐项执行批量操作，记录失败项及原因，供前端展示哪些项目失败
            sql_map = {
                "archive": "UPDATE projects SET archived=1,revision=revision+1,updated_at=? WHERE id=? AND deleted=0",
                "delete": "UPDATE projects SET deleted=1,revision=revision+1,updated_at=? WHERE id=?",
                "restore": "UPDATE projects SET deleted=0,archived=0,revision=revision+1,updated_at=? WHERE id=?",
            }
            sql = sql_map[action]
            updated = 0
            failed: list[dict[str, str]] = []
            with connect() as conn:
                for project_id in normalized_ids:
                    cursor = conn.execute(sql, (now, project_id))
                    if cursor.rowcount == 1:
                        updated += 1
                    else:
                        failed.append({"id": project_id, "reason": "文章不存在或不满足操作条件"})
            self._send_json(200, {"updated": updated, "failed": failed})
            return

        if len(segments) >= 4 and segments[:3] == ["api", "v2", "projects"]:
            project_id = segments[3]
            project = get_project(project_id, include_deleted=True)
            if not project:
                raise KeyError("文章不存在")
            if len(segments) == 4 and method == "GET":
                project["sources"] = _project_sources(project_id)
                self._send_json(200, project)
                return
            if len(segments) == 4 and method == "PATCH":
                self._patch_project(project)
                return
            if len(segments) == 4 and method == "DELETE":
                if project["deleted"]:
                    self._send_json(200, {"deleted": True})
                    return
                with connect() as conn:
                    record_project_version(conn, project_id, "软删除前")
                    conn.execute(
                        "UPDATE projects SET deleted=1,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                        (utc_now(), project_id, project["revision"]),
                    )
                self._send_json(200, {"deleted": True})
                return

            if len(segments) == 7 and segments[4] == "versions" and segments[6] == "restore" and method == "POST":
                try:
                    version_revision = int(segments[5])
                except ValueError as exc:
                    raise ApiProblem(400, "version_invalid", "版本号无效") from exc
                self._send_json(200, self._restore_version(project, version_revision))
                return

            action = segments[4] if len(segments) == 5 else ""
            if action == "archive" and method == "POST":
                with connect() as conn:
                    conn.execute(
                        "UPDATE projects SET archived=1,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                        (utc_now(), project_id, project["revision"]),
                    )
                self._send_json(200, get_project(project_id, include_deleted=True))
                return
            if action == "restore" and method == "POST":
                with connect() as conn:
                    conn.execute(
                        "UPDATE projects SET archived=0,deleted=0,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                        (utc_now(), project_id, project["revision"]),
                    )
                self._send_json(200, get_project(project_id, include_deleted=True))
                return
            if action == "purge" and method == "DELETE":
                if not project["deleted"]:
                    raise ApiProblem(409, "soft_delete_required", "永久删除前必须先移入回收站")
                with connect() as conn:
                    conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
                self._send_json(200, {"purged": True})
                return
            if action == "copy" and method == "POST":
                if project["deleted"]:
                    raise ApiProblem(409, "project_deleted", "已删除文章不能复制")
                self._send_json(201, self._copy_project(project))
                return
            if action == "export" and method == "GET":
                content = project["bodyMarkdown"].encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                filename = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", project["title"]).strip("_") or "article"
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}.md")
                self.send_header("Content-Length", str(len(content)))
                self._security_headers()
                self.end_headers()
                self.wfile.write(content)
                return
            if action == "versions" and method == "GET":
                self._send_json(200, {"items": _project_versions(project_id)})
                return
            if action == "sources" and method == "GET":
                self._send_json(200, {"items": _project_sources(project_id)})
                return
            # #169 独立封面上传端点，避免大体积 base64 封面导致 PATCH 超时
            if action == "cover" and method == "POST":
                self._upload_cover(project)
                return
            # #125 AI 摘要生成端点
            if action == "summarize" and method == "POST":
                self._generate_summary(project)
                return
            if action == "refresh-source" and method == "POST":
                self._require_current_revision(project)
                self._refresh_source(project)
                return
            if action == "preview" and method == "GET":
                # #188 预览 HTML 经服务端消毒后再返回，并显式声明 JSON Content-Type
                html = _render_wechat_html(project["bodyMarkdown"])
                self._send_json(
                    200,
                    {
                        "revision": project["revision"],
                        "bodyFingerprint": _body_fingerprint(project["bodyMarkdown"]),
                        "html": html,
                        "previewHash": hashlib.sha256(html.encode("utf-8")).hexdigest(),
                        "coverDataUrl": project["coverDataUrl"],
                    },
                )
                return
            if action == "review" and method == "POST":
                body = self._read_json()
                expected = body.get("revision")
                fingerprint = str(body.get("bodyFingerprint") or "")
                approved = body.get("approved")
                if type(expected) is not int or expected != project["revision"]:
                    raise ApiProblem(409, "revision_conflict", "终审前文章版本已变化", {"server": project})
                if type(approved) is not bool:
                    raise ApiProblem(400, "review_invalid", "approved 必须是布尔值")
                actual = _body_fingerprint(project["bodyMarkdown"])
                if not fingerprint or not hmac.compare_digest(fingerprint, actual):
                    raise ApiProblem(409, "review_fingerprint_mismatch", "终审正文指纹与服务端不一致")
                now = utc_now()
                with connect() as conn:
                    record_project_version(conn, project_id, "人工终审前")
                    cursor = conn.execute(
                        """
                        UPDATE projects SET review_approved=?,review_fingerprint=?,review_revision=?,reviewed_at=?,
                            revision=revision+1,updated_at=? WHERE id=? AND revision=?
                        """,
                        (1 if approved else 0, actual, expected + 1 if approved else 0, now if approved else "", now, project_id, expected),
                    )
                    if cursor.rowcount != 1:
                        raise ApiProblem(409, "revision_conflict", "终审时文章版本已变化")
                self._send_json(200, get_project(project_id))
                return
            if action == "publish" and method == "POST":
                self._send_json(200, _wechat_publish(project, self._read_json()))
                return
            # D1 审计修复：并发发布 stale 状态处理端点
            if (
                len(segments) == 6
                and segments[4] == "publish"
                and segments[5] == "confirm-sync"
                and method == "POST"
            ):
                body = self._read_json()
                revision = body.get("revision")
                if type(revision) is not int:
                    raise ApiProblem(400, "revision_required", "必须携带 revision")
                with connect() as conn:
                    cursor = conn.execute(
                        "UPDATE projects SET publish_status='synced',updated_at=? "
                        "WHERE id=? AND revision=?",
                        (utc_now(), project_id, revision),
                    )
                    if cursor.rowcount != 1:
                        raise ApiProblem(409, "revision_conflict", "文章版本已变化，无法确认同步")
                self._send_json(200, get_project(project_id, include_deleted=True))
                return
            if (
                len(segments) == 6
                and segments[4] == "publish"
                and segments[5] == "delete-remote"
                and method == "POST"
            ):
                body = self._read_json()
                remote_id = str(body.get("remoteId") or "").strip()
                if not remote_id:
                    raise ApiProblem(400, "invalid_request", "缺少 remoteId")
                deleted = False
                message = "远程草稿未删除"
                settings = get_setting("wechat")
                app_id = str(settings.get("appId") or "")
                secret = str(settings.get("appSecret") or "")
                if app_id and secret:
                    try:
                        token = _wechat_token(app_id, secret)
                        _wechat_api_call(
                            "https://api.weixin.qq.com/cgi-bin/draft/delete?access_token="
                            + urllib.parse.quote(token),
                            method="POST",
                            body=json.dumps(
                                {"media_id": remote_id}, ensure_ascii=False
                            ).encode("utf-8"),
                            headers={
                                "Content-Type": "application/json; charset=utf-8",
                                "Accept": "application/json",
                            },
                            app_id=app_id,
                            app_secret=secret,
                        )
                        deleted = True
                        message = "远程草稿已删除"
                    except (WeChatApiError, ApiProblem) as exc:
                        message = f"微信 API 删除失败：{exc}"
                else:
                    message = "微信公众号未配置，跳过远程删除"
                self._send_json(200, {"deleted": deleted, "message": message})
                return

        # --- 数据导出/导入 ---
        if path == "/api/v2/data/export" and method == "GET":
            self._handle_data_export()
            return
        if path == "/api/v2/data/import" and method == "POST":
            self._handle_data_import()
            return

        if path == "/api/v2/settings" and method == "GET":
            self._send_json(200, _mask_settings(settings_bundle()))
            return
        if path == "/api/v2/settings" and method in {"PUT", "PATCH"}:
            incoming = self._read_json()
            unknown_sections = set(incoming) - {"ai", "general"}
            if unknown_sections:
                raise ApiProblem(400, "invalid_settings", f"不支持的设置分组：{', '.join(sorted(unknown_sections))}")
            if "ai" in incoming:
                if not isinstance(incoming["ai"], dict):
                    raise ApiProblem(400, "invalid_settings", "ai 设置必须是对象")
                set_setting("ai", _validate_ai_settings(incoming["ai"], get_setting("ai")))
            if "general" in incoming:
                if not isinstance(incoming["general"], dict):
                    raise ApiProblem(400, "invalid_settings", "general 设置必须是对象")
                set_setting("general", _validate_general_settings(incoming["general"], get_setting("general")))
            self._send_json(200, _mask_settings(settings_bundle()))
            return
        if path == "/api/v2/settings/ai/verify" and method == "POST":
            incoming = self._read_json()
            merged = _validate_ai_settings(incoming, get_setting("ai"))
            self._send_json(200, _verify_ai(merged))
            return
        if path == "/api/v2/settings/wechat/verify-and-save" and method == "POST":
            self._send_json(200, _verify_and_save_wechat(self._read_json()))
            return

        raise ApiProblem(404, "not_found", "接口不存在")

    # -----------------------------------------------------------------------
    # 认证端点处理方法
    # -----------------------------------------------------------------------

    def _handle_admin_setup(self) -> None:
        """首次运行时创建管理员，或领取旧版未登录的随机密码 admin。"""
        self._check_login_rate_limit()
        body = self._read_json(max_bytes=16_384)
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        confirm_password = str(body.get("confirmPassword") or "")
        if not re.fullmatch(r"[\w.-]{3,32}", username):
            raise ApiProblem(400, "invalid_username", "用户名需为 3~32 位字母、数字、中文、下划线、点或连字符")
        if password != confirm_password:
            raise ApiProblem(400, "password_mismatch", "两次输入的密码不一致")
        ok, message = validate_password_strength(password)
        if not ok:
            raise ApiProblem(400, "weak_password", message)

        password_hash = hash_password(password)
        with _admin_setup_lock:
            with connect() as conn:
                rows = conn.execute(
                    "SELECT id, username, must_change_password, last_login_at "
                    "FROM users ORDER BY created_at LIMIT 2"
                ).fetchall()
                now = utc_now()
                if not rows:
                    user_id = "usr_" + uuid.uuid4().hex[:20]
                    conn.execute(
                        "INSERT INTO users(id, username, password_hash, must_change_password, is_active, created_at, updated_at) "
                        "VALUES(?,?,?,0,1,?,?)",
                        (user_id, username, password_hash, now, now),
                    )
                elif _is_unclaimed_legacy_admin(rows):
                    user_id = rows[0]["id"]
                    conn.execute(
                        "UPDATE users SET username=?, password_hash=?, must_change_password=0, "
                        "is_active=1, updated_at=? WHERE id=?",
                        (username, password_hash, now, user_id),
                    )
                else:
                    raise ApiProblem(409, "already_initialized", "管理员已初始化，请直接登录")

        destroy_all_user_sessions(user_id)
        _remove_legacy_initial_password()
        token = create_session(user_id, username, False)
        csrf_token = _generate_csrf_token()
        access_logger.info("首次启动管理员 %s 已完成初始化", username)
        self._send_json(
            201,
            {
                "ok": True,
                "username": username,
                "mustChangePassword": False,
                "csrfToken": csrf_token,
            },
            set_cookies=[build_cookie(token), _build_csrf_cookie(csrf_token)],
        )

    def _handle_login(self) -> None:
        """处理登录请求。"""
        if admin_setup_required():
            raise ApiProblem(409, "setup_required", "请先在页面完成管理员初始化")
        self._check_login_rate_limit()
        body = self._read_json()
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        if not username or not password:
            raise ApiProblem(400, "invalid_credentials", "用户名和密码不能为空")

        with connect() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, must_change_password, is_active FROM users WHERE username=?",
                (username,),
            ).fetchone()

        if not row or not row["is_active"]:
            raise ApiProblem(401, "invalid_credentials", "用户名或密码错误")
        if not verify_password(password, row["password_hash"]):
            raise ApiProblem(401, "invalid_credentials", "用户名或密码错误")

        must_change = bool(row["must_change_password"])
        token = create_session(row["id"], row["username"], must_change)

        # 更新最后登录时间
        with connect() as conn:
            conn.execute(
                "UPDATE users SET last_login_at=?, updated_at=? WHERE id=?",
                (utc_now(), utc_now(), row["id"]),
            )

        # #154 签发 CSRF 令牌：写入 Cookie（供浏览器自动回传）并放入响应体（供前端读取）
        csrf_token = _generate_csrf_token()
        # 发送会话 Cookie 与 csrf_token Cookie（会话 Cookie 必须在前，保证仅读取首个 Set-Cookie 的客户端可用）
        self._send_json(
            200,
            {
                "ok": True,
                "username": row["username"],
                "mustChangePassword": must_change,
                "csrfToken": csrf_token,
            },
            set_cookies=[build_cookie(token), _build_csrf_cookie(csrf_token)],
        )

    def _handle_session_info(self) -> None:
        """获取当前会话状态。"""
        token = self._get_session_token()
        if not token:
            self._send_json(200, {"authenticated": False})
            return
        session = get_session(token)
        if not session:
            self._send_json(
                200,
                {"authenticated": False},
                set_cookies=[build_clear_cookie(), _build_clear_csrf_cookie()],
            )
            return
        self._send_json(
            200,
            {
                "authenticated": True,
                "username": session["username"],
                "mustChangePassword": session.get("must_change_password", False),
            },
        )

    def _handle_logout(self) -> None:
        """处理登出请求。"""
        token = self._get_session_token()
        if token:
            destroy_session(token)
        # #154 登出时同时清除会话 Cookie 与 csrf_token Cookie
        self._send_json(
            200,
            {"ok": True},
            set_cookies=[build_clear_cookie(), _build_clear_csrf_cookie()],
        )

    def _handle_change_password(self, session: dict[str, Any]) -> None:
        """处理修改密码请求。"""
        body = self._read_json()
        old_password = str(body.get("oldPassword") or "")
        new_password = str(body.get("newPassword") or "")
        confirm_password = str(body.get("confirmPassword") or "")

        if not old_password or not new_password:
            raise ApiProblem(400, "invalid_request", "请填写旧密码和新密码")
        if new_password != confirm_password:
            raise ApiProblem(400, "password_mismatch", "两次输入的新密码不一致")

        # 验证旧密码
        with connect() as conn:
            row = conn.execute(
                "SELECT id, password_hash FROM users WHERE id=?",
                (session["user_id"],),
            ).fetchone()
        if not row or not verify_password(old_password, row["password_hash"]):
            raise ApiProblem(401, "invalid_credentials", "旧密码不正确")

        # 校验新密码强度
        ok, message = validate_password_strength(new_password)
        if not ok:
            raise ApiProblem(400, "weak_password", message)

        # 新密码不能与旧密码相同
        if old_password == new_password:
            raise ApiProblem(400, "password_reused", "新密码不能与旧密码相同")

        # 更新密码
        new_hash = hash_password(new_password)
        with connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash=?, must_change_password=0, updated_at=? WHERE id=?",
                (new_hash, utc_now(), session["user_id"]),
            )

        # 销毁该用户的所有其他会话（强制重新登录）
        destroy_all_user_sessions(session["user_id"])

        _remove_legacy_initial_password()

        # 为当前请求创建新会话（已修改密码，无需再改）
        token = create_session(session["user_id"], session["username"], False)
        self._send_json(
            200,
            {"ok": True, "message": "密码修改成功"},
            extra_headers={"Set-Cookie": build_cookie(token)},
        )

    # -----------------------------------------------------------------------
    # 数据导出/导入处理方法
    # -----------------------------------------------------------------------

    def _handle_data_export(self) -> None:
        """处理全量数据导出请求，返回 JSON 附件。"""
        backup = export_data()
        body = _json_bytes(backup)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"studio-backup-{timestamp}.json"
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header(
            "Content-Disposition",
            f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}",
        )
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _handle_data_import(self) -> None:
        """处理数据导入请求。"""
        # #190 服务端请求体大小限制（100MB），防止客户端绕过前端 100MB 检查。
        # _read_json 内部也会校验 Content-Length，此处显式预检以给出明确错误码。
        IMPORT_MAX_BYTES = 100 * 1024 * 1024
        length_header = self.headers.get("Content-Length", "0")
        try:
            declared_length = int(length_header)
        except ValueError as exc:
            raise ApiProblem(400, "invalid_length", "Content-Length 无效") from exc
        if declared_length > IMPORT_MAX_BYTES:
            raise ApiProblem(
                413, "payload_too_large",
                f"导入文件超过服务端上限（{IMPORT_MAX_BYTES // (1024 * 1024)}MB）",
            )
        body = self._read_json(max_bytes=IMPORT_MAX_BYTES)  # 二次校验实际读取上限
        mode = str(body.get("mode") or "merge")
        import_ai_config = bool(body.get("importAiConfig"))
        data = body.get("data")
        if not data or not isinstance(data, dict):
            raise ApiProblem(400, "invalid_request", "缺少 data 字段")
        try:
            counts = import_data(data, mode=mode, import_ai_config=import_ai_config)
        except ValueError as exc:
            raise ApiProblem(400, "invalid_backup", str(exc)) from exc
        self._send_json(200, {"ok": True, "imported": counts})

    def _handle_sse_stream(self, session_token: str | None = None) -> None:
        """P2 审计修复：SSE 实时推送活跃任务状态。

        每隔 2 秒查询一次活跃任务（status IN ('queued', 'running')），
        以 text/event-stream 格式推送状态更新。连接保持直到客户端断开或 30 秒超时。
        #186 周期性校验会话，过期则推送 auth 事件并关闭连接。
        #068 周期性检查已发布文章的远程草稿过期状态并推送提醒。
        注意：不使用 _send_json，直接发送流式响应。
        """
        # 手动设置响应摘要用于访问日志
        self._resp_status = 200
        self._resp_bytes = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self._security_headers()
        self.end_headers()

        start = time.monotonic()
        auth_checked_at = time.monotonic()
        stale_checked_at = time.monotonic()
        while time.monotonic() - start < 30:
            # #186 每 5 秒重新校验会话，过期则推送 auth 事件并关闭
            if time.monotonic() - auth_checked_at >= 5:
                auth_checked_at = time.monotonic()
                if not session_token or not get_session(session_token):
                    try:
                        payload = json.dumps(
                            {"event": "auth_expired", "message": "会话已过期，请重新登录"},
                            ensure_ascii=False,
                        )
                        self.wfile.write(f"event: auth\ndata: {payload}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        pass
                    break
            # #068 每 10 秒检查已发布文章的远程草稿过期状态
            if time.monotonic() - stale_checked_at >= 10:
                stale_checked_at = time.monotonic()
                try:
                    stale_projects = _check_published_stale_status()
                    if stale_projects:
                        payload = json.dumps(
                            {"event": "publish_stale", "items": stale_projects},
                            ensure_ascii=False,
                        )
                        self.wfile.write(f"event: publish_stale\ndata: {payload}\n\n".encode("utf-8"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    break
                except Exception:  # noqa: BLE001
                    pass
            try:
                with connect() as conn:
                    rows = conn.execute(
                        """
                        SELECT id, status, current_step, progress, message
                        FROM tasks WHERE status IN ('queued', 'running')
                        ORDER BY updated_at DESC LIMIT 50
                        """
                    ).fetchall()
                if rows:
                    for row in rows:
                        payload = json.dumps(
                            {
                                "taskId": row["id"],
                                "status": row["status"],
                                "step": row["current_step"],
                                "progress": int(row["progress"]),
                                "message": row["message"],
                            },
                            ensure_ascii=False,
                        )
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                        self.wfile.flush()
                else:
                    # 没有活跃任务，发送心跳保持连接
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # 客户端已断开连接
                break
            except Exception:  # noqa: BLE001
                # 查询出错时发送心跳，不中断连接
                try:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    break
            time.sleep(2)

    def _require_current_revision(self, project: dict[str, Any]) -> int:
        supplied = self.headers.get("If-Match", "").strip().strip('"')
        try:
            expected = int(supplied)
        except ValueError as exc:
            raise ApiProblem(428, "revision_required", "该操作必须携带 If-Match revision") from exc
        if expected != project["revision"]:
            raise ApiProblem(409, "revision_conflict", "文章已被其他操作修改", {"server": project})
        return expected

    def _restore_version(self, project: dict[str, Any], version_revision: int) -> dict[str, Any]:
        if project["deleted"]:
            raise ApiProblem(409, "project_deleted", "请先从回收站恢复文章")
        with connect() as conn:
            row = conn.execute(
                "SELECT snapshot_json FROM project_versions WHERE project_id=? AND revision=? ORDER BY id DESC LIMIT 1",
                (project["id"], version_revision),
            ).fetchone()
            if not row:
                raise ApiProblem(404, "version_not_found", "指定版本不存在")
            snapshot = json.loads(row["snapshot_json"])
            record_project_version(conn, project["id"], f"恢复版本 {version_revision} 前")
            cursor = conn.execute(
                """
                UPDATE projects SET title=?,goal=?,source_input=?,source_kind=?,outline_json=?,body_markdown=?,summary=?,
                    cover_data_url=?,review_json='[]',review_fingerprint='',review_approved=0,review_revision=0,reviewed_at='',
                    publish_status='not_synced',publish_remote_id='',published_revision=0,publish_fingerprint='',publish_preview_hash='',
                    revision=revision+1,updated_at=? WHERE id=? AND revision=?
                """,
                (
                    _require_text(str(snapshot.get("title") or "未命名文章"), "标题", 120, allow_empty=False),
                    _require_text(str(snapshot.get("goal") or ""), "创作目标", 4_000),
                    _require_text(str(snapshot.get("sourceInput") or ""), "来源", 4_000),
                    "url" if snapshot.get("sourceKind") == "url" else "topic",
                    json.dumps(snapshot.get("outline") if isinstance(snapshot.get("outline"), list) else [], ensure_ascii=False),
                    _require_text(str(snapshot.get("bodyMarkdown") or ""), "正文", 500_000),
                    _require_text(str(snapshot.get("summary") or ""), "摘要", 300),
                    str(snapshot.get("coverDataUrl") or ""),
                    utc_now(),
                    project["id"],
                    project["revision"],
                ),
            )
            if cursor.rowcount != 1:
                raise ApiProblem(409, "revision_conflict", "恢复版本时文章已变化")
        return get_project(project["id"]) or {}

    def _patch_project(self, project: dict[str, Any]) -> None:
        if project["deleted"]:
            raise KeyError("文章不存在")
        body = self._read_json()
        expected = self._require_current_revision(project)
        fields = {
            "title": "title",
            "goal": "goal",
            "summary": "summary",
            "bodyMarkdown": "body_markdown",
            "coverDataUrl": "cover_data_url",
            "outline": "outline_json",
        }
        unknown = set(body) - set(fields)
        if unknown:
            raise ApiProblem(400, "invalid_project_fields", f"不支持的文章字段：{', '.join(sorted(unknown))}")
        updates: list[tuple[str, Any]] = []
        publish_content_changed = False
        for api_name, column in fields.items():
            if api_name not in body:
                continue
            value = body[api_name]
            if api_name == "title":
                value = _require_text(value, "标题", 120, allow_empty=False)
            elif api_name == "goal":
                value = _require_text(value, "创作目标", 4_000)
            elif api_name == "summary":
                value = _require_text(value, "摘要", 300)
            elif api_name == "bodyMarkdown":
                value = _require_text(value, "正文", 500_000)
            elif api_name == "coverDataUrl":
                value = _require_text(value, "封面", 3_000_000)
                _parse_cover_data_url(value)
            elif api_name == "outline":
                if not isinstance(value, list) or len(value) > 12:
                    raise ApiProblem(400, "outline_invalid", "文章框架必须是最多 12 项的数组")
                normalized: list[str] = []
                for item in value:
                    normalized.append(_require_text(item, "框架项", 120, allow_empty=False).strip())
                value = json.dumps(normalized, ensure_ascii=False)
            current_value = project.get(api_name)
            if api_name == "outline":
                current_value = json.dumps(project.get("outline") or [], ensure_ascii=False)
            if str(value) == str(current_value):
                continue
            if api_name in {"title", "summary", "bodyMarkdown", "coverDataUrl"}:
                publish_content_changed = True
            updates.append((column, value))
        if not updates:
            self._send_json(200, project)
            return
        with connect() as conn:
            record_project_version(conn, project["id"], "人工编辑前")
            if publish_content_changed:
                updates.extend(
                    [
                        ("review_approved", 0),
                        ("review_revision", 0),
                        ("reviewed_at", ""),
                        ("publish_status", "not_synced"),
                        ("publish_remote_id", ""),
                        ("published_revision", 0),
                        ("publish_fingerprint", ""),
                        ("publish_preview_hash", ""),
                    ]
                )
                if any(column == "body_markdown" for column, _ in updates):
                    updates.extend([("review_json", "[]"), ("review_fingerprint", "")])
            updates.extend([("revision", expected + 1), ("updated_at", utc_now())])
            sql = "UPDATE projects SET " + ", ".join(f"{column}=?" for column, _ in updates) + " WHERE id=? AND revision=?"
            cursor = conn.execute(sql, tuple(value for _, value in updates) + (project["id"], expected))
            if cursor.rowcount != 1:
                current = conn.execute("SELECT * FROM projects WHERE id=?", (project["id"],)).fetchone()
                raise ApiProblem(409, "revision_conflict", "文章已被其他操作修改", {"server": row_to_project(current)})
        self._send_json(200, get_project(project["id"]))

    def _upload_cover(self, project: dict[str, Any]) -> None:
        """#169 独立的封面上传端点，避免大体积 base64 封面导致 PATCH 超时。"""
        if project["deleted"]:
            raise KeyError("文章不存在")
        self._require_current_revision(project)
        body = self._read_json(max_bytes=3_500_000)
        cover_url = _require_text(str(body.get("coverDataUrl") or ""), "封面", 3_000_000, allow_empty=False)
        _parse_cover_data_url(cover_url)  # 校验封面格式与大小，非法时抛出 ApiProblem
        with connect() as conn:
            record_project_version(conn, project["id"], "封面上传前")
            cursor = conn.execute(
                "UPDATE projects SET cover_data_url=?, review_approved=0, review_revision=0, reviewed_at='', "
                "publish_status='not_synced', publish_remote_id='', published_revision=0, "
                "publish_fingerprint='', publish_preview_hash='', revision=revision+1, updated_at=? "
                "WHERE id=? AND revision=?",
                (cover_url, utc_now(), project["id"], project["revision"]),
            )
            if cursor.rowcount != 1:
                raise ApiProblem(409, "revision_conflict", "封面上传时文章版本已变化")
        self._send_json(200, get_project(project["id"]))

    def _generate_summary(self, project: dict[str, Any]) -> None:
        """#125 AI 摘要生成：根据正文生成摘要供前端编辑使用。"""
        if project["deleted"]:
            raise KeyError("文章不存在")
        if not bool(get_setting("general").get("allowNetwork", True)) and not _test_adapter_enabled("STUDIO_TEST_AI"):
            raise ApiProblem(409, "network_disabled", "联网能力已关闭，不能生成 AI 摘要")
        ai_config = get_setting("ai")
        if not (bool(ai_config.get("apiKey")) or _test_adapter_enabled("STUDIO_TEST_AI")):
            raise ApiProblem(400, "ai_not_configured", "请先在设置中配置 AI 模型")
        body_markdown = str(project.get("bodyMarkdown") or "")
        if not body_markdown.strip():
            raise ApiProblem(400, "body_required", "正文为空，无法生成摘要")
        engine = AIEngine(ai_config)
        try:
            summary = engine.summarize(body_markdown)
        except AIEngineError as exc:
            raise ApiProblem(400, exc.code, str(exc)) from exc
        self._send_json(200, {"summary": summary})

    def _copy_project(self, project: dict[str, Any]) -> dict[str, Any]:
        new_id = "prj_" + uuid.uuid4().hex[:20]
        now = utc_now()
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO projects(
                    id,title,goal,source_input,source_kind,status,archived,deleted,outline_json,body_markdown,
                    summary,cover_data_url,review_json,review_fingerprint,review_approved,publish_status,publish_remote_id,
                    revision,created_at,updated_at
                ) VALUES (?,?,?,?,?,'draft',0,0,?,?,?,?,?, ?,0,'not_synced','',1,?,?)
                """,
                (
                    new_id,
                    (project["title"] + "（副本）")[:120],
                    project["goal"],
                    project["sourceInput"],
                    project["sourceKind"],
                    json.dumps(project["outline"], ensure_ascii=False),
                    project["bodyMarkdown"],
                    project["summary"],
                    project["coverDataUrl"],
                    json.dumps(project["review"], ensure_ascii=False),
                    project["reviewFingerprint"],
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO project_sources(project_id,snapshot_id,source_order) SELECT ?,snapshot_id,source_order FROM project_sources WHERE project_id=?",
                (new_id, project["id"]),
            )
        return get_project(new_id) or {}

    def _refresh_source(self, project: dict[str, Any]) -> None:
        if not bool(get_setting("general").get("allowNetwork", True)):
            raise ApiProblem(409, "network_disabled", "联网读取来源已在设置中关闭")
        if project["sourceKind"] != "url":
            raise ApiProblem(400, "source_not_url", "该文章不是 URL 来源，无法重新读取")
        snapshot = fetch_source(project["sourceInput"])
        identity = hashlib.sha256(f"{snapshot.source_url}\n{snapshot.content_hash}".encode("utf-8")).hexdigest()
        snapshot_id = "src_" + identity[:24]
        old_sources = _project_sources(project["id"])
        changed = not old_sources or old_sources[0]["contentHash"] != snapshot.content_hash or old_sources[0]["sourceUrl"] != snapshot.source_url
        with connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO source_snapshots(
                    id,content_hash,source_url,final_url,title,publisher,author,published_at,
                    content_text,preview,fetched_at,extraction_method
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    snapshot_id,
                    snapshot.content_hash,
                    snapshot.source_url,
                    snapshot.final_url,
                    snapshot.title,
                    snapshot.publisher,
                    snapshot.author,
                    snapshot.published_at,
                    snapshot.content_text,
                    snapshot.preview,
                    utc_now(),
                    snapshot.extraction_method,
                ),
            )
            row = conn.execute(
                "SELECT id FROM source_snapshots WHERE source_url=? AND content_hash=?",
                (snapshot.source_url, snapshot.content_hash),
            ).fetchone()
            if not row:
                raise ApiProblem(500, "snapshot_write_failed", "来源快照写入失败")
            conn.execute("DELETE FROM project_sources WHERE project_id=?", (project["id"],))
            conn.execute(
                "INSERT INTO project_sources(project_id,snapshot_id,source_order) VALUES(?,?,0)",
                (project["id"], row["id"]),
            )
            if changed:
                record_project_version(conn, project["id"], "来源变化导致下游内容失效")
                cursor = conn.execute(
                    """
                    UPDATE projects SET title=?,summary='',outline_json='[]',body_markdown='',review_json='[]',
                        review_fingerprint='',review_approved=0,review_revision=0,reviewed_at='',
                        publish_status='not_synced',publish_remote_id='',published_revision=0,publish_fingerprint='',
                        publish_preview_hash='',revision=revision+1,updated_at=? WHERE id=? AND revision=?
                    """,
                    (
                        (snapshot.title or project["title"])[:120],
                        utc_now(),
                        project["id"],
                        project["revision"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise ApiProblem(409, "revision_conflict", "刷新来源时文章版本已变化")
        self._send_json(
            200,
            {"changed": changed, "items": _project_sources(project["id"]), "project": get_project(project["id"])},
        )

    def _serve_static(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        request_path = parsed.path
        if request_path in {"", "/", "/app"} or not request_path.startswith("/assets/"):
            file_path = WEB_ROOT / "index.html"
        else:
            relative = request_path.removeprefix("/assets/")
            if ".." in Path(relative).parts:
                raise ApiProblem(400, "invalid_path", "资源路径无效")
            file_path = WEB_ROOT / relative
        if not file_path.exists() or not file_path.is_file():
            raise ApiProblem(404, "not_found", "资源不存在")
        content = file_path.read_bytes()
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self._security_headers()
        self.end_headers()
        self.wfile.write(content)


def _ensure_login_attempts_table() -> None:
    """S3 审计修复：创建登录尝试记录表（数据库持久化限流）。"""
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS login_attempts(
                ip TEXT NOT NULL,
                attempted_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_time ON login_attempts(ip, attempted_at)"
        )


def _warn_test_adapters() -> None:
    """P0-1: 启动时检测测试适配器标记文件并警告。"""
    marker = Path(__file__).resolve().parent.parent / ".test-adapters-enabled"
    if not marker.exists():
        return
    if os.environ.get("STUDIO_ENABLE_TEST_ADAPTERS") == "1":
        logger.warning("⚠️ 测试适配器已激活（STUDIO_ENABLE_TEST_ADAPTERS=1），AI/微信/内容安全校验将被绕过！仅限开发测试环境使用。")
    else:
        logger.warning("⚠️ 检测到 .test-adapters-enabled 标记文件，但 STUDIO_ENABLE_TEST_ADAPTERS 未设置。测试适配器未激活。建议在生产部署中删除此文件。")


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    _warn_test_adapters()
    validate_runtime_security(host)
    init_db()
    _ensure_login_attempts_table()
    cleanup_expired_sessions()
    mark_interrupted_tasks()
    return ThreadingHTTPServer((host, port), StudioHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="公众号 AI Studio 2.1.3")
    parser.add_argument("--host", default=os.environ.get("STUDIO_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=_env_int("STUDIO_PORT", 5000))
    args = parser.parse_args()
    validate_runtime_security(args.host)
    server = create_server(args.host, args.port)
    print(f"公众号 AI Studio {VERSION} 已启动：http://{args.host}:{args.port}/")
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            from workflow import shutdown_executor
            shutdown_executor()
            logger.info("工作流线程池已关闭")
        except Exception:
            pass
        try:
            from db import stop_wal_checkpoint_thread
            stop_wal_checkpoint_thread()
            logger.info("WAL checkpoint 线程已停止")
        except Exception:
            pass
        server.server_close()


if __name__ == "__main__":
    main()
