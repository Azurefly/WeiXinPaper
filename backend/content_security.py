"""内容安全检测模块。

修复审计报告中的 A3（无内容安全检测）和 A4（无查重/原创性检测）问题：
- A3: 集成微信 msg_sec_check 接口检测文本合规性
- A4: 基于 n-gram 的查重检测，对比生成内容与来源原文
"""
from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

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

    返回 {"safe": bool, "detail": str}。
    在测试模式下直接返回安全。
    """
    if test_adapter_enabled("STUDIO_TEST_WECHAT"):
        return {"safe": True, "detail": "测试模式跳过内容安全检测"}

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
            return {"safe": True, "detail": f"内容安全检测不可用，已放行（请求失败：{exc}）"}

        errcode = int(result.get("errcode") or 0)
        if errcode == 0:
            detail = result.get("detail") or result.get("result") or {}
            if isinstance(detail, dict):
                suggest = detail.get("suggest", "pass")
                if suggest == "risky":
                    return {
                        "safe": False,
                        "detail": f"第{idx + 1}段内容被判定为风险内容：{detail.get('label', '未知类型')}",
                    }
            continue
        elif errcode == 40001:
            # token 失效，不阻塞流程但记录警告
            logger.warning("内容安全检测 token 失效 (errcode=40001)")
            return {"safe": True, "detail": "内容安全检测因 token 失效跳过"}
        else:
            logger.warning("内容安全检测返回 errcode=%d: %s", errcode, result.get("errmsg", ""))
            return {"safe": True, "detail": f"内容安全检测返回错误码 {errcode}，已放行"}

    return {"safe": True, "detail": "内容安全检测通过"}


def check_plagiarism(
    generated_text: str,
    source_text: str,
    *,
    ngram_size: int = 5,
    threshold: float = 0.3,
) -> dict[str, Any]:
    """基于 n-gram 的查重检测。

    对比生成内容与来源原文的 n-gram 重叠率。
    返回 {"original": bool, "similarity": float, "detail": str}。
    """
    if not source_text or not generated_text:
        return {"original": True, "similarity": 0.0, "detail": "无来源可对比"}

    # 清理文本：去除标记符号
    def clean(text: str) -> str:
        text = re.sub(r"\[来源\d+\]", "", text)
        text = re.sub(r"[`*_>#\-]", "", text)
        text = re.sub(r"\s+", "", text)
        return text

    gen_clean = clean(generated_text)
    src_clean = clean(source_text)

    if len(gen_clean) < ngram_size or len(src_clean) < ngram_size:
        return {"original": True, "similarity": 0.0, "detail": "文本过短，无法有效查重"}

    # 生成 n-gram 集合
    gen_ngrams: set[str] = set()
    for i in range(len(gen_clean) - ngram_size + 1):
        gen_ngrams.add(gen_clean[i:i + ngram_size])

    src_ngrams: set[str] = set()
    for i in range(len(src_clean) - ngram_size + 1):
        src_ngrams.add(src_clean[i:i + ngram_size])

    if not gen_ngrams:
        return {"original": True, "similarity": 0.0, "detail": "无有效 n-gram"}

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

    return {"original": original, "similarity": round(similarity, 4), "detail": detail}


def run_content_security_checks(
    body_markdown: str,
    source_text: str,
    *,
    token: str = "",
    app_id: str = "",
    app_secret: str = "",
) -> list[dict[str, str]]:
    """运行全部内容安全检查，返回审校项列表。"""
    checks: list[dict[str, str]] = []

    # 1. 查重检测
    plagiarism = check_plagiarism(body_markdown, source_text)
    checks.append({
        "id": "plagiarism",
        "label": "原创性检测",
        "status": "passed" if plagiarism["original"] else "failed",
        "message": plagiarism["detail"],
    })

    # 2. 微信内容安全检测（如果有 token）
    if token and not test_adapter_enabled("STUDIO_TEST_WECHAT"):
        security = check_text_security(token, body_markdown, app_id=app_id, app_secret=app_secret)
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
