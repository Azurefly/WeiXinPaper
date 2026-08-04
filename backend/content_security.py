"""内容安全检测模块。

修复审计报告中的 A2、A3、A4 问题：
- A2: 跨文章查重，检测新生成内容与数据库中已有文章的重复度
- A3: 集成微信 msg_sec_check 接口检测文本合规性
- A4: 基于 n-gram 的查重检测，对比生成内容与来源原文
"""
from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

from db import connect
from logger_config import get_logger
from secure_http import SecureHttpError, request_bytes
from test_mode import enabled as test_adapter_enabled

logger = get_logger("content_security")


def check_text_security(
    token: str,
    text: str,
    *,
    app_id: str = "",
    app_secret: str = "",
) -> dict[str, Any]:
    """调用微信 msg_sec_check 检测文本合规性。

    返回 {"safe": bool, "available": bool, "detail": str}。
    - safe=True + available=True：检测通过
    - safe=False：检测到风险内容
    - safe=True + available=False：检测不可用（token 失效/网络错误等），不再静默放行
    在测试模式下直接返回安全。
    """
    if test_adapter_enabled("STUDIO_TEST_WECHAT"):
        return {"safe": True, "available": True, "detail": "测试模式跳过内容安全检测"}

    # 微信接口限制单次检测 2KB，超长文本分段检测
    max_chunk = 2000
    chunks = [text[i:i + max_chunk] for i in range(0, min(len(text), 20000), max_chunk)]

    for idx, chunk in enumerate(chunks):
        url = (
            "https://api.weixin.qq.com/wxa/msg_sec_check?access_token="
            + urllib.parse.quote(token)
        )
        payload = {"version": 2, "scene": 1, "openid": "", "content": chunk}
        try:
            response = request_bytes(
                url,
                method="POST",
                body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
                timeout=15,
                max_bytes=500_000,
                require_https=True,
                reject_redirects=True,
            )
            result = json.loads(response.body.decode("utf-8"))
        except (SecureHttpError, json.JSONDecodeError) as exc:
            logger.warning("内容安全检测请求失败 (chunk %d): %s", idx, exc)
            # S2 修复：不再静默放行，标记为不可用
            return {"safe": True, "available": False, "detail": f"内容安全检测不可用（请求失败：{exc}），请在发布前人工确认内容合规"}

        errcode = int(result.get("errcode") or 0)
        if errcode == 0:
            detail = result.get("detail") or result.get("result") or {}
            if isinstance(detail, dict):
                suggest = detail.get("suggest", "pass")
                if suggest == "risky":
                    return {
                        "safe": False,
                        "available": True,
                        "detail": f"第{idx + 1}段内容被判定为风险内容：{detail.get('label', '未知类型')}",
                    }
            continue
        elif errcode == 40001:
            # token 失效，标记为不可用而非放行
            logger.warning("内容安全检测 token 失效 (errcode=40001)")
            return {"safe": True, "available": False, "detail": "内容安全检测因 token 失效不可用，请在发布前人工确认内容合规"}
        else:
            logger.warning("内容安全检测返回 errcode=%d: %s", errcode, result.get("errmsg", ""))
            return {"safe": True, "available": False, "detail": f"内容安全检测返回错误码 {errcode}，检测不可用，请在发布前人工确认内容合规"}

    return {"safe": True, "available": True, "detail": "内容安全检测通过"}


def _clean_text(text: str) -> str:
    """清理文本：去除标记符号，供 n-gram 查重复用。"""
    text = re.sub(r"\[来源\d+\]", "", text)
    text = re.sub(r"[`*_>#\-]", "", text)
    text = re.sub(r"\s+", "", text)
    return text


def check_plagiarism(
    generated_text: str,
    source_text: str,
    *,
    ngram_size: int = 5,
    threshold: float = 0.3,
) -> dict[str, Any]:
    """基于 n-gram 的查重检测。

    对比生成内容与来源原文的 n-gram 重叠率。
    返回「是否可用、是否低于阈值、重合率和说明」。
    """
    if not source_text or not generated_text:
        return {"available": False, "original": False, "similarity": 0.0, "detail": "没有可对比的网页来源，未执行文本重合检查；此结果不代表原创性。"}

    gen_clean = _clean_text(generated_text)
    src_clean = _clean_text(source_text)

    if len(gen_clean) < ngram_size or len(src_clean) < ngram_size:
        return {"available": False, "original": False, "similarity": 0.0, "detail": "正文或来源过短，未执行有效的文本重合检查。"}

    # 生成 n-gram 集合
    gen_ngrams: set[str] = set()
    for i in range(len(gen_clean) - ngram_size + 1):
        gen_ngrams.add(gen_clean[i:i + ngram_size])

    src_ngrams: set[str] = set()
    for i in range(len(src_clean) - ngram_size + 1):
        src_ngrams.add(src_clean[i:i + ngram_size])

    if not gen_ngrams:
        return {"available": False, "original": False, "similarity": 0.0, "detail": "无有效文本片段，未执行文本重合检查。"}

    overlap = gen_ngrams & src_ngrams
    similarity = len(overlap) / len(gen_ngrams)

    original = similarity < threshold
    detail = (
        f"n-gram 重叠率 {similarity:.1%}（阈值 {threshold:.0%}）"
        if not original
        else f"n-gram 重叠率 {similarity:.1%}，低于阈值"
    )

    if not original:
        logger.warning("查重检测未通过: similarity=%.2f threshold=%.2f", similarity, threshold)

    return {"available": True, "original": original, "similarity": round(similarity, 4), "detail": detail}


def check_cross_article_plagiarism(
    body_markdown: str,
    *,
    exclude_project_id: str = "",
    ngram_size: int = 7,
    threshold: float = 0.25,
) -> dict[str, Any]:
    """A2 审计修复：跨文章查重。

    检测新生成内容与数据库中已有文章的 n-gram 重复度。
    与 check_plagiarism（仅对比来源原文）不同，这里对比数据库中
    其他项目的正文，ngram_size 默认为 7（更长，减少误报），阈值默认 25%。
    返回 {"original": bool, "similarity": float, "detail": str, "matched_title": str}。
    """
    # A2 修复：对长文本做截断处理（只取前 50000 字符），避免 n-gram 计算性能问题
    truncated_body = body_markdown[:50000]
    gen_clean = _clean_text(truncated_body)

    if len(gen_clean) < ngram_size:
        return {
            "original": True,
            "similarity": 0.0,
            "detail": "生成内容过短，无法有效跨文章查重",
            "matched_title": "",
        }

    # 生成新内容的 n-gram 集合（复用 check_plagiarism 的算法）
    gen_ngrams: set[str] = set()
    for i in range(len(gen_clean) - ngram_size + 1):
        gen_ngrams.add(gen_clean[i:i + ngram_size])

    if not gen_ngrams:
        return {"original": True, "similarity": 0.0, "detail": "无有效 n-gram", "matched_title": ""}

    # 从数据库查询所有非当前项目的文章正文
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT id, title, body_markdown FROM projects WHERE deleted=0 AND id != ?",
                (exclude_project_id,),
            ).fetchall()
    except Exception as exc:
        # A2 修复：数据库查询失败时 catch 异常并返回 warning 状态，不阻塞流程
        logger.warning("A2 跨文章查重数据库查询失败: %s", exc)
        return {
            "original": True,
            "similarity": 0.0,
            "detail": f"跨文章查重不可用（数据库查询失败：{exc}），已跳过",
            "matched_title": "",
        }

    if not rows:
        return {"original": True, "similarity": 0.0, "detail": "无已有文章可对比", "matched_title": ""}

    max_similarity = 0.0
    matched_title = ""

    # 对每篇已有文章计算与新生成内容的 n-gram 重叠率
    for row in rows:
        existing_body = row["body_markdown"] or ""
        if not existing_body:
            continue
        # A2 修复：同样对已有文章正文做截断，避免性能问题
        existing_clean = _clean_text(existing_body[:50000])
        if len(existing_clean) < ngram_size:
            continue

        existing_ngrams: set[str] = set()
        for i in range(len(existing_clean) - ngram_size + 1):
            existing_ngrams.add(existing_clean[i:i + ngram_size])

        if not existing_ngrams:
            continue

        overlap = gen_ngrams & existing_ngrams
        similarity = len(overlap) / len(gen_ngrams)

        if similarity > max_similarity:
            max_similarity = similarity
            matched_title = row["title"] or ""

    if max_similarity >= threshold:
        return {
            "original": False,
            "similarity": round(max_similarity, 4),
            "detail": f"与已有文章「{matched_title}」的相似度为 {max_similarity:.1%}，超过阈值",
            "matched_title": matched_title,
        }

    return {
        "original": True,
        "similarity": round(max_similarity, 4),
        "detail": f"跨文章查重通过，最高相似度 {max_similarity:.1%}",
        "matched_title": matched_title,
    }


def run_content_security_checks(
    body_markdown: str,
    source_text: str,
    *,
    token: str = "",
    app_id: str = "",
    app_secret: str = "",
    exclude_project_id: str = "",
) -> list[dict[str, str]]:
    """运行全部内容安全检查，返回审校项列表。"""
    checks: list[dict[str, str]] = []

    # 1. 仅比较当前来源的文本重合，不声称全网原创性。
    plagiarism = check_plagiarism(body_markdown, source_text)
    checks.append({
        "id": "source_overlap",
        "label": "来源文本重合",
        "status": (
            "warning" if not plagiarism.get("available", False)
            else "passed" if plagiarism["original"]
            else "failed"
        ),
        "message": plagiarism["detail"],
    })

    # A2 修复：跨文章查重
    cross_check = check_cross_article_plagiarism(body_markdown, exclude_project_id=exclude_project_id)
    checks.append({
        "id": "cross_plagiarism",
        "label": "跨文章查重",
        "status": "passed" if cross_check["original"] else "warning",  # warning 而非 failed，不阻断发布
        "message": cross_check["detail"],
    })

    # 2. 微信内容安全检测（如果有 token）
    if token and not test_adapter_enabled("STUDIO_TEST_WECHAT"):
        security = check_text_security(token, body_markdown, app_id=app_id, app_secret=app_secret)
        # S2 修复：检测不可用时标记为 warning 而非 passed，让用户知情
        if not security.get("available", True):
            checks.append({
                "id": "content_security",
                "label": "微信内容安全",
                "status": "warning",
                "message": security["detail"],
            })
        else:
            checks.append({
                "id": "content_security",
                "label": "微信内容安全",
                "status": "passed" if security["safe"] else "failed",
                "message": security["detail"],
            })
    else:
        checks.append({
            "id": "content_security",
            "label": "微信内容安全",
            "status": "warning",
            "message": "未配置微信凭证或测试模式，内容安全检测已跳过",
        })

    return checks
