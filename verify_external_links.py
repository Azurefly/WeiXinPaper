from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import secrets
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

from ai_engine import AIEngine, AIEngineError  # noqa: E402
from secure_http import SecureHttpError, request_bytes  # noqa: E402

VERSION = "2.1.3"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def safe_error(exc: Exception) -> dict[str, Any]:
    return {
        "status": "failed",
        "code": getattr(exc, "code", exc.__class__.__name__),
        "message": str(exc)[:500],
    }


def verify_ai() -> dict[str, Any]:
    base = os.environ.get("STUDIO_VERIFY_AI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
    key = os.environ.get("STUDIO_VERIFY_AI_KEY", "")
    model = os.environ.get("STUDIO_VERIFY_AI_MODEL", "gpt-4.1-mini").strip()
    if not key:
        return {"status": "skipped", "reason": "missing_STUDIO_VERIFY_AI_KEY", "baseUrl": base, "model": model}
    try:
        engine = AIEngine({"baseUrl": base, "apiKey": key, "model": model, "temperature": 0.2})
        goal = "验证真实模型的框架、正文和审校链路"
        evidence = (
            "[来源1] 公众号 AI Studio 2.1.3 使用版本指纹、串行保存、服务端分页和不可变发布快照"
            "保证编辑与发布一致性。"
        )
        plan = engine.plan(goal, evidence, True)
        draft = engine.draft(goal, evidence, plan, 800, strict_facts=True)
        review = engine.review(draft, evidence)
        if not plan.get("outline") or len(draft.strip()) < 100 or not isinstance(review, list):
            raise RuntimeError("real completion returned incomplete workflow output")
        return {
            "status": "succeeded",
            "baseUrl": base,
            "model": model,
            "outlineItems": len(plan.get("outline") or []),
            "draftChars": len(draft),
            "draftSha256": hashlib.sha256(draft.encode("utf-8")).hexdigest(),
            "reviewItems": len(review),
            "reviewStatuses": sorted({str(item.get("status") or "") for item in review}),
        }
    except (AIEngineError, SecureHttpError, RuntimeError, ValueError, TypeError) as exc:
        return {**safe_error(exc), "baseUrl": base, "model": model}


def _wechat_json(url: str, *, payload: dict[str, Any] | None = None, timeout: int = 60) -> tuple[dict[str, Any], bytes]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    response = request_bytes(
        url,
        method="POST" if body is not None else "GET",
        body=body,
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        timeout=timeout,
        max_bytes=3_000_000,
        require_https=True,
        reject_redirects=True,
    )
    parsed = json.loads(response.body.decode("utf-8", errors="replace"))
    return parsed, response.body


def _cover_signature(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise RuntimeError("wechat cover must be PNG, JPEG, GIF or WEBP")


def _multipart_cover(path: Path) -> tuple[bytes, str]:
    data = path.read_bytes()
    if not data or len(data) > 10_000_000:
        raise RuntimeError("wechat cover must be between 1 byte and 10 MB")
    detected_mime, suffix = _cover_signature(data)
    guessed = mimetypes.guess_type(path.name)[0]
    mime = guessed if guessed in {"image/png", "image/jpeg", "image/gif", "image/webp"} else detected_mime
    boundary = "----WeiXinGZHStudio" + secrets.token_hex(16)
    filename = (path.stem[:80] or "cover") + (path.suffix.lower() if path.suffix else suffix)
    chunks = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="media"; filename="{filename}"\r\n'.encode("utf-8"),
        f"Content-Type: {mime}\r\n\r\n".encode(),
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    return b"".join(chunks), boundary


def _upload_wechat_cover(token: str, cover_path: Path) -> dict[str, Any]:
    body, boundary = _multipart_cover(cover_path)
    response = request_bytes(
        "https://api.weixin.qq.com/cgi-bin/material/add_material?" + urlencode({"access_token": token, "type": "image"}),
        method="POST",
        body=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json"},
        timeout=90,
        max_bytes=3_000_000,
        require_https=True,
        reject_redirects=True,
    )
    parsed = json.loads(response.body.decode("utf-8", errors="replace"))
    media_id = str(parsed.get("media_id") or "")
    if not media_id:
        raise RuntimeError(f"wechat material upload failed: errcode={parsed.get('errcode')} errmsg={parsed.get('errmsg')}")
    return {
        "mediaId": media_id,
        "responseSha256": hashlib.sha256(response.body).hexdigest(),
        "coverSha256": hashlib.sha256(cover_path.read_bytes()).hexdigest(),
    }


def _cleanup_wechat(token: str, draft_media_id: str, cover_media_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {"requested": True}
    draft_body, _ = _wechat_json(
        "https://api.weixin.qq.com/cgi-bin/draft/delete?access_token=" + token,
        payload={"media_id": draft_media_id},
    )
    result["draftDelete"] = {"errcode": draft_body.get("errcode"), "errmsg": draft_body.get("errmsg")}
    if cover_media_id:
        cover_body, _ = _wechat_json(
            "https://api.weixin.qq.com/cgi-bin/material/del_material?access_token=" + token,
            payload={"media_id": cover_media_id},
        )
        result["coverDelete"] = {"errcode": cover_body.get("errcode"), "errmsg": cover_body.get("errmsg")}
    return result


def verify_wechat() -> dict[str, Any]:
    app_id = os.environ.get("STUDIO_VERIFY_WECHAT_APPID", "")
    secret = os.environ.get("STUDIO_VERIFY_WECHAT_SECRET", "")
    configured_thumb = os.environ.get("STUDIO_VERIFY_WECHAT_THUMB_MEDIA_ID", "").strip()
    cover_file = os.environ.get("STUDIO_VERIFY_WECHAT_COVER_FILE", "").strip()
    confirmed = os.environ.get("STUDIO_VERIFY_EXTERNAL_FULL", "") == "1"
    cleanup = os.environ.get("STUDIO_VERIFY_WECHAT_CLEANUP", "") == "1"
    if not app_id or not secret or (not configured_thumb and not cover_file):
        return {
            "status": "skipped",
            "reason": "missing_wechat_credentials_and_cover_or_thumb_media_id",
            "required": [
                "STUDIO_VERIFY_WECHAT_APPID",
                "STUDIO_VERIFY_WECHAT_SECRET",
                "STUDIO_VERIFY_WECHAT_COVER_FILE or STUDIO_VERIFY_WECHAT_THUMB_MEDIA_ID",
            ],
        }
    if not confirmed:
        return {"status": "skipped", "reason": "set_STUDIO_VERIFY_EXTERNAL_FULL_1_to_create_real_draft"}
    try:
        token_url = "https://api.weixin.qq.com/cgi-bin/token?" + urlencode(
            {"grant_type": "client_credential", "appid": app_id, "secret": secret}
        )
        token_body, token_raw = _wechat_json(token_url)
        token = str(token_body.get("access_token") or "")
        if not token:
            raise RuntimeError(f"wechat token failed: errcode={token_body.get('errcode')} errmsg={token_body.get('errmsg')}")

        upload: dict[str, Any] = {"usedExistingThumbMediaId": bool(configured_thumb)}
        thumb_media_id = configured_thumb
        uploaded_cover_id = ""
        if not thumb_media_id:
            path = Path(cover_file).expanduser().resolve()
            if not path.is_file():
                raise RuntimeError("STUDIO_VERIFY_WECHAT_COVER_FILE does not exist")
            upload = _upload_wechat_cover(token, path)
            thumb_media_id = uploaded_cover_id = str(upload["mediaId"])

        marker = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        payload = {
            "articles": [
                {
                    "title": f"AI Studio 2.1.3 外部链路验证 {marker}",
                    "author": "AI Studio",
                    "digest": "自动创建的测试草稿，用于验证 token、封面永久素材上传与 draft/add 真实链路。",
                    "content": "<h1>外部链路验证</h1><p>此草稿用于验证公众号真实封面素材与 draft/add 接口。</p>",
                    "content_source_url": "",
                    "thumb_media_id": thumb_media_id,
                    "need_open_comment": 0,
                    "only_fans_can_comment": 0,
                }
            ]
        }
        draft_body, draft_raw = _wechat_json(
            "https://api.weixin.qq.com/cgi-bin/draft/add?access_token=" + token,
            payload=payload,
        )
        media_id = str(draft_body.get("media_id") or "")
        if not media_id:
            raise RuntimeError(f"wechat draft/add failed: errcode={draft_body.get('errcode')} errmsg={draft_body.get('errmsg')}")
        result: dict[str, Any] = {
            "status": "succeeded",
            "tokenResponseSha256": hashlib.sha256(token_raw).hexdigest(),
            "cover": upload,
            "draftMediaId": media_id,
            "draftResponseSha256": hashlib.sha256(draft_raw).hexdigest(),
        }
        if cleanup:
            result["cleanup"] = _cleanup_wechat(token, media_id, uploaded_cover_id)
        return result
    except (SecureHttpError, RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
        return safe_error(exc)


def main() -> None:
    result = {
        "product": "公众号 AI Studio",
        "version": VERSION,
        "generatedAt": utc_now(),
        "hostname": socket.gethostname(),
        "ai": verify_ai(),
        "wechat": verify_wechat(),
        "note": (
            "结果不记录 API Key、AppSecret 或 access_token；微信成功验证会真实上传封面永久素材（如未提供现有 media_id）"
            "并创建一篇草稿。设置 STUDIO_VERIFY_WECHAT_CLEANUP=1 可在验证后请求清理。"
        ),
    }
    output = json.dumps(result, ensure_ascii=False, indent=2)
    path = os.environ.get("STUDIO_VERIFY_RESULT_FILE", "").strip()
    if path:
        Path(path).write_text(output + "\n", encoding="utf-8")
    print(output)
    failed = any(result[name]["status"] == "failed" for name in ("ai", "wechat"))
    skipped = any(result[name]["status"] == "skipped" for name in ("ai", "wechat"))
    if failed or (skipped and os.environ.get("STUDIO_VERIFY_REQUIRE_ALL", "") == "1"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
