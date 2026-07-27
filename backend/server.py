from __future__ import annotations

import argparse
import collections
import base64
import hashlib
import hmac
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
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ai_engine import AIEngine, AIEngineError
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
from logger_config import get_logger, query_logs
from runtime_security import validate_runtime_security
from secure_http import SecureHttpError, request_bytes
from test_mode import enabled as test_adapter_enabled
from source_fetcher import fetch_source
from wechat_api import (
    WeChatApiError,
    _token_manager as _wechat_token_manager,
    create_draft as _wechat_create_draft,
    upload_cover_dedup as _wechat_upload_cover_dedup,
)
from workflow import cancel_workflow, create_workflow, mark_interrupted_tasks, retry_workflow

access_logger = get_logger("access")

ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = ROOT / "web"
VERSION = "2.1.3"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


class ApiProblem(RuntimeError):
    def __init__(self, status: int, code: str, message: str, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


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
        if exc.status is not None and 300 <= exc.status < 500:
            return {"ok": True, "message": f"模型服务可达（HTTP {exc.status}）"}
        raise ApiProblem(400, "ai_verify_failed", f"无法安全连接模型服务（{exc.code}）") from exc

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
    content_html = _markdown_to_wechat_html(current["bodyMarkdown"])
    actual_preview_hash = hashlib.sha256(content_html.encode("utf-8")).hexdigest()
    if not preview_hash or not hmac.compare_digest(preview_hash, actual_preview_hash):
        raise ApiProblem(409, "publish_preview_stale", "发布预览已过期，请重新预览")
    if not current["reviewApproved"]:
        raise ApiProblem(409, "review_required", "同步草稿前必须完成人工终审")
    if current["reviewRevision"] != revision or current["reviewFingerprint"] != actual_fingerprint:
        raise ApiProblem(409, "review_stale", "正文或版本已变化，请重新完成人工终审")
    _validate_publish_content(current, content_html)
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

    def inline(text: str) -> str:
        escaped = escape_text(text)
        escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(
            r"\[([^]]+)]\((https://[^)\s]+)\)",
            lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
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
            src = escape_text(img_match.group(2))
            if img_match.group(2) == "placeholder":
                output.append(f'<p class="img-suggestion">📷 {alt}</p>')
            else:
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
            if first_ip:
                return first_ip
        # 回退到 X-Real-IP
        xri = self.headers.get("X-Real-IP", "").strip()
        if xri:
            return xri
        return direct_ip

    def _check_rate_limit(self) -> None:
        if self.command not in MUTATING:
            return
        now = time.monotonic()
        key = self._get_client_ip()
        with self._rate_lock:
            window = self._rate_windows.setdefault(key, collections.deque())
            while window and now - window[0] > 60:
                window.popleft()
            if len(window) >= 120:
                raise ApiProblem(429, "rate_limited", "写操作过于频繁，请稍后重试")
            window.append(now)

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
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        self.send_header("Cache-Control", "no-store" if self.path.startswith("/api/") else "no-cache")

    def _authenticate(self) -> bool:
        username = os.environ.get("STUDIO_AUTH_USER", "")
        password = os.environ.get("STUDIO_AUTH_PASSWORD", "")
        if not username and not password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="WeiXinGZH Studio"')
            self._security_headers()
            self.end_headers()
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            supplied_user, supplied_password = decoded.split(":", 1)
        except Exception:
            supplied_user = supplied_password = ""
        if not (hmac.compare_digest(supplied_user, username) and hmac.compare_digest(supplied_password, password)):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="WeiXinGZH Studio"')
            self._security_headers()
            self.end_headers()
            return False
        return True

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

    def _send_json(self, status: int, value: Any) -> None:
        body = _json_bytes(value)
        # 捕获响应摘要用于访问日志
        try:
            resp_str = body.decode("utf-8", errors="replace")
            self._resp_status = status
            self._resp_preview = resp_str[:500] if len(resp_str) > 500 else resp_str
        except Exception:  # noqa: BLE001
            self._resp_status = status
            self._resp_preview = "<binary>"
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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
        # 捕获请求体摘要用于访问日志
        try:
            req_str = raw.decode("utf-8", errors="replace")
            self._req_preview = req_str[:500] if len(req_str) > 500 else req_str
        except Exception:  # noqa: BLE001
            self._req_preview = "<binary>"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiProblem(400, "invalid_json", "请求 JSON 无效") from exc
        if not isinstance(value, dict):
            raise ApiProblem(400, "invalid_json", "请求 JSON 必须是对象")
        return value

    def _handle(self) -> None:
        if not self._authenticate():
            return
        _req_start = time.monotonic()
        # 初始化请求/响应摘要
        self._req_preview = ""
        self._resp_status = 0
        self._resp_preview = ""
        try:
            self._check_origin()
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
            self._send_json(500, {"error": {"code": "internal_error", "message": "服务器发生未预期错误", "detail": repr(exc)}})
            access_logger.error("%s %s %s -> 500 internal_error (%.0fms): %s", self.address_string(), self.command, self.path, (time.monotonic() - _req_start) * 1000, exc, exc_info=exc)
        finally:
            # 仅对 API 请求记录结构化访问日志
            if self.path.startswith("/api/"):
                self._log_access(_req_start)

    def _log_access(self, req_start: float) -> None:
        """记录包含请求参数和响应摘要的结构化访问日志。"""
        duration_ms = (time.monotonic() - req_start) * 1000
        status = self._resp_status or 0
        # 解析路径和查询参数
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query_str = parsed.query

        # 构建日志消息：方法 路径 -> 状态码 (耗时)
        # 请求参数：query string + 请求体摘要
        # 响应摘要：响应体前 500 字符
        parts = [
            f'{self.command} {path}',
            f'-> {status}' if status else '-> (no response)',
            f'({duration_ms:.0f}ms)',
        ]
        if query_str:
            parts.append(f'query=[{query_str}]')
        if self._req_preview:
            parts.append(f'req_body={self._req_preview}')
        if self._resp_preview:
            parts.append(f'resp_body={self._resp_preview}')

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
            if action == "refresh-source" and method == "POST":
                self._require_current_revision(project)
                self._refresh_source(project)
                return
            if action == "preview" and method == "GET":
                html = _markdown_to_wechat_html(project["bodyMarkdown"])
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


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    validate_runtime_security(host)
    init_db()
    mark_interrupted_tasks()
    return ThreadingHTTPServer((host, port), StudioHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="公众号 AI Studio 2.1.3")
    parser.add_argument("--host", default=os.environ.get("STUDIO_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("STUDIO_PORT", "5000")))
    args = parser.parse_args()
    validate_runtime_security(args.host)
    server = create_server(args.host, args.port)
    print(f"公众号 AI Studio {VERSION} 已启动：http://{args.host}:{args.port}/")
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
