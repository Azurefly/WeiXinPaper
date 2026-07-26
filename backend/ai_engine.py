from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from logger_config import get_logger
from secure_http import SecureHttpError, request_bytes
from test_mode import enabled as test_adapter_enabled

logger = get_logger("ai_engine")


class AIEngineError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class AIConfigurationRequired(AIEngineError):
    def __init__(self) -> None:
        super().__init__("ai_not_configured", "请先在“AI”页面配置可用的 OpenAI 兼容模型")


@dataclass(frozen=True)
class AIConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float
    max_tokens: int


class AIEngine:
    def __init__(self, config: dict[str, Any]):
        self.test_mode = test_adapter_enabled("STUDIO_TEST_AI")
        self.config = AIConfig(
            base_url=str(config.get("baseUrl") or "https://api.openai.com/v1").rstrip("/"),
            api_key=str(config.get("apiKey") or ""),
            model=str(config.get("model") or "gpt-4.1-mini"),
            temperature=float(config.get("temperature", 0.4)),
            max_tokens=int(config.get("maxTokens", 4096)),
        )

    @staticmethod
    def _sanitize_prompt_input(text: str) -> str:
        if not isinstance(text, str) or not text:
            return text
        original = text
        # 中和 "ignore previous instructions" 等指令覆盖型注入（忽略大小写）
        text = re.sub(r"ignore\s+previous\s+instructions", "[filtered]", text, flags=re.IGNORECASE)
        # 中和角色扮演型注入：以 "you are a..." / "you are now..." 等开头的角色指派
        text = re.sub(r"(?i)\byou are (a|an|now|no longer)\b[^\n]*", "[filtered]", text)
        # 中和行首的 "system:" / "assistant:" 等角色前缀
        text = re.sub(r"(?im)^\s*(system|assistant)\s*:", "[filtered]", text)
        if text != original:
            logger.warning("检测到提示词注入模式，已对用户输入进行过滤")
        return text

    def _chat(self, system: str, user: str, *, json_mode: bool = False) -> str:
        if self.test_mode:
            return self._test_response(system, user, json_mode=json_mode)
        if not self.config.api_key:
            raise AIConfigurationRequired()
        # 仅过滤用户可控输入；system 提示词由开发者控制，不做处理
        user = self._sanitize_prompt_input(user)
        url = self.config.base_url + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        logger.info("AI 请求: model=%s json_mode=%s prompt_len=%d", self.config.model, json_mode, len(user))
        start_time = time.monotonic()
        max_retries = 3
        retryable_statuses = {429, 500, 502, 503, 504}
        status = 0
        raw = ""
        elapsed = 0.0
        for attempt in range(max_retries + 1):
            try:
                response = request_bytes(
                    url,
                    method="POST",
                    body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {self.config.api_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "User-Agent": "WeiXinGZH-Studio/2.1.3",
                    },
                    timeout=90,
                    max_bytes=4_000_000,
                    require_https=True,
                    reject_redirects=True,
                )
            except SecureHttpError as exc:
                elapsed = (time.monotonic() - start_time) * 1000
                logger.error("AI 安全连接失败 (耗时 %.0fms): %s", elapsed, exc.code)
                raise AIEngineError("ai_connection_failed", f"模型接口连接失败（安全校验：{exc.code}）") from exc
            except Exception as exc:
                elapsed = (time.monotonic() - start_time) * 1000
                logger.error("AI 请求异常 (耗时 %.0fms): %s", elapsed, exc)
                raise AIEngineError("ai_request_failed", f"模型接口请求失败") from exc
            elapsed = (time.monotonic() - start_time) * 1000
            status = response.status
            raw = response.body.decode("utf-8", errors="replace")
            # 仅对限流(429)与服务端错误(5xx)重试；其余 4xx 与连接错误立即失败
            if status in retryable_statuses and attempt < max_retries:
                delay = 1.0 * (2 ** attempt) + random.random()
                logger.warning(
                    "AI 返回 HTTP %d，第 %d/%d 次重试 (等待 %.1fs)",
                    status, attempt + 1, max_retries, delay,
                )
                time.sleep(delay)
                continue
            break
        if len(raw) > 4_000_000:
            raise AIEngineError("ai_response_too_large", "模型接口响应过大")
        if status < 200 or status >= 300:
            logger.error("AI 返回 HTTP %d (耗时 %.0fms)", status, elapsed)
            raise AIEngineError("ai_http_error", f"模型接口返回 HTTP {status}")
        try:
            body = json.loads(raw)
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            logger.error("AI 响应解析失败 (耗时 %.0fms): %s", elapsed, exc)
            raise AIEngineError("ai_invalid_response", "模型接口返回了无法识别的响应") from exc
        if not isinstance(content, str) or not content.strip():
            logger.warning("AI 返回空内容 (耗时 %.0fms)", elapsed)
            raise AIEngineError("ai_empty_response", "模型没有返回有效内容")
        usage = body.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", "?")
        completion_tokens = usage.get("completion_tokens", "?")
        logger.info("AI 响应成功 (耗时 %.0fms, tokens=%s/%s, content_len=%d)", elapsed, prompt_tokens, completion_tokens, len(content))
        return content.strip()

    def _test_response(self, system: str, user: str, *, json_mode: bool) -> str:
        import time
        delay = float(os.environ.get("STUDIO_TEST_AI_DELAY", "0") or 0)
        if delay > 0:
            time.sleep(delay)
        seed = hashlib.sha256(user.encode("utf-8")).hexdigest()[:8]
        if "生成文章框架" in system:
            return json.dumps(
                {
                    "title": f"测试文章 {seed}",
                    "summary": "这是自动化验收使用的确定性摘要。",
                    "outline": ["问题背景", "核心变化", "实际影响", "使用建议", "总结"],
                },
                ensure_ascii=False,
            )
        if "审校" in system:
            return json.dumps(
                {
                    "checks": [
                        {"id": "facts", "label": "事实与来源", "status": "passed", "message": "引用内容与输入一致"},
                        {"id": "structure", "label": "结构完整", "status": "passed", "message": "结构清晰"},
                        {"id": "wechat", "label": "公众号可读性", "status": "passed", "message": "段落长度适中"},
                    ]
                },
                ensure_ascii=False,
            )
        if json_mode:
            return json.dumps({"ok": True}, ensure_ascii=False)
        citation = " [来源1]" if "所有事实性陈述必须来自编号来源" in system else ""
        return (
            f"# 测试文章 {seed}\n\n"
            f"## 问题背景\n\n这是一段由确定性测试适配器生成的内容，仅在自动化测试环境启用。{citation}\n\n"
            f"![配图建议：工作流架构示意图](placeholder)\n\n"
            f"## 核心变化\n\n系统使用统一工作流完成资料整理、框架生成、正文写作与审校。{citation}\n\n"
            f"## 实际影响\n\n用户只需要提供一个来源或主题，后续步骤由服务端状态机连续推进。{citation}\n\n"
            f"## 使用建议\n\n发布前仍应由人工核对事实、标题、摘要和封面。{citation}\n\n"
            f"## 总结\n\n统一入口减少了重复操作，同时保留了失败诊断和人工接管能力。{citation}\n\n"
            f"这段补充内容用于验证正文保存、版本冲突、人工终审和发布门禁都在同一条真实链路上工作。{citation}"
        )

    @staticmethod
    def _json_object(raw: str) -> dict[str, Any]:
        value = raw.strip()
        if value.startswith("```"):
            value = re.sub(r"^```(?:json)?\s*", "", value)
            value = re.sub(r"\s*```$", "", value)
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise AIEngineError("ai_json_invalid", "模型没有按要求返回 JSON") from exc
        if not isinstance(parsed, dict):
            raise AIEngineError("ai_json_invalid", "模型 JSON 响应必须是对象")
        return parsed

    def plan(self, goal: str, source_text: str, strict_facts: bool, requirements: str = "") -> dict[str, Any]:
        if strict_facts:
            policy = (
                "严格事实模式：只能使用编号来源中可核验的信息；不得补充常识性事实或推测；"
                "框架中的事实性章节必须能够映射到 [来源N]。"
            )
        else:
            policy = (
                "普通创作模式：优先使用来源；允许提出明确标注为‘分析’或‘建议’的非事实性内容，"
                "但不得虚构数据、人物、日期或引用。"
            )
        system = (
            "你是资深微信公众号内容策划。请基于用户目标和提供材料生成文章框架。"
            f"{policy}返回 JSON，字段为 title、summary、outline；outline 是 4 到 8 个字符串。"
        )
        requirement_block = (
            f"文章生成要求：\n{requirements.strip()}\n\n"
            "请在标题、摘要和框架中严格落实上述要求。"
            if requirements and requirements.strip()
            else "文章生成要求：未指定，请根据目标和来源自主发挥。"
        )
        user = (
            f"创作目标：\n{goal}\n\n"
            f"{requirement_block}\n\n"
            f"来源内容：\n{source_text[:80_000]}\n\n事实策略：{policy}"
        )
        data = self._json_object(self._chat(system, user, json_mode=True))
        outline = data.get("outline")
        if not isinstance(outline, list) or not all(isinstance(item, str) and item.strip() for item in outline):
            raise AIEngineError("outline_invalid", "模型返回的文章框架无效")
        return {
            "title": str(data.get("title") or goal[:40] or "未命名文章").strip()[:120],
            "summary": str(data.get("summary") or "").strip()[:300],
            "outline": [str(item).strip()[:120] for item in outline[:8]],
        }

    @staticmethod
    def _validate_strict_draft(content: str, source_text: str) -> None:
        valid_sources = {int(value) for value in re.findall(r"\[来源(\d+)\]", source_text)}
        cited_sources = {int(value) for value in re.findall(r"\[来源(\d+)\]", content)}
        if not valid_sources or not cited_sources:
            raise AIEngineError("strict_facts_citation_missing", "严格事实正文缺少可核验的 [来源N] 引用")
        invalid = cited_sources - valid_sources
        if invalid:
            raise AIEngineError("strict_facts_citation_invalid", f"正文引用了不存在的来源编号：{sorted(invalid)}")
        uncovered: list[str] = []
        for paragraph in re.split(r"\n\s*\n", content):
            clean = paragraph.strip()
            if not clean or clean.startswith("#") or clean.startswith("```"):
                continue
            plain = re.sub(r"[`*_>#\-]", "", clean).strip()
            if len(plain) < 30:
                continue
            if not re.search(r"\[来源\d+\]", clean) and "现有来源无法确认" not in clean:
                uncovered.append(plain[:60])
        if uncovered:
            raise AIEngineError(
                "strict_facts_coverage_incomplete",
                "严格事实正文存在未标注来源的长段落：" + "；".join(uncovered[:3]),
            )

    def draft(
        self,
        goal: str,
        source_text: str,
        plan: dict[str, Any],
        target_length: int,
        *,
        strict_facts: bool = False,
        requirements: str = "",
    ) -> str:
        if strict_facts:
            policy = (
                "严格事实模式：所有事实性陈述必须来自编号来源，并在对应段落末尾标注 [来源N]；"
                "证据不足时明确写‘现有来源无法确认’，不得推断补齐。"
            )
        else:
            policy = (
                "普通创作模式：事实仍需忠于来源；分析和建议必须用明确措辞区分，"
                "不得把推断写成已证实事实。"
            )
        system = (
            "你是专业微信公众号作者。请根据创作目标、来源材料和文章框架写出可直接编辑的 Markdown 正文。"
            f"{policy}标题使用一级标题，章节使用二级标题。"
            "在正文的合适位置插入 2 到 4 张配图，使用 Markdown 图片语法 ![图片描述](图片URL)。"
            "图片 URL 必须是来源内容中出现的真实图片地址；如果来源中没有可用图片，"
            "则在合适位置插入 ![配图建议：描述](placeholder) 作为占位符，"
            "描述应具体说明该处适合放什么类型的插图（如流程图、示意图、数据图等）。"
            "图片应独占一行，放在相关段落之后。"
        )
        requirement_block = (
            f"文章生成要求：\n{requirements.strip()}\n\n"
            "请在正文风格、篇幅、重点和表达方式上严格落实上述要求。"
            if requirements and requirements.strip()
            else "文章生成要求：未指定，请根据目标和来源自主发挥。"
        )
        user = (
            f"目标字数：约 {target_length} 字\n"
            f"创作目标：{goal}\n"
            f"{requirement_block}\n"
            f"标题：{plan['title']}\n"
            f"摘要：{plan['summary']}\n"
            f"框架：{json.dumps(plan['outline'], ensure_ascii=False)}\n\n"
            f"事实策略：{policy}\n\n"
            f"可信来源：\n{source_text[:90_000]}"
        )
        content = self._chat(system, user)
        if len(content) < 200:
            raise AIEngineError("draft_too_short", "模型生成的正文过短")
        if strict_facts:
            self._validate_strict_draft(content, source_text)
        return content

    def review(self, body_markdown: str, source_text: str) -> list[dict[str, str]]:
        system = (
            "你是公众号发布前审校员。必须检查以下必检项，每项都不可省略：\n"
            "1. 合规性：是否包含政治敏感、违法广告法、谣言等风险内容\n"
            "2. 原创性：是否存在大段直接搬运来源原文的情况\n"
            "3. 事实准确性：事实性陈述是否与来源一致，有无虚构数据/人物/日期\n"
            "4. 结构完整性：标题、摘要、正文是否完整，逻辑是否通顺\n"
            "5. 公众号可读性：段落长度、配图位置是否适合移动端阅读\n"
            "返回 JSON：{\"checks\":[{\"id\":...,\"label\":...,\"status\":\"passed|warning|failed\",\"message\":...}]}。"
        )
        user = f"来源：\n{source_text[:50_000]}\n\n正文：\n{body_markdown[:80_000]}"
        data = self._json_object(self._chat(system, user, json_mode=True))
        checks = data.get("checks")
        if not isinstance(checks, list):
            raise AIEngineError("review_invalid", "模型返回的审校结果无效")
        result: list[dict[str, str]] = []
        for index, item in enumerate(checks[:10]):
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "warning")
            if status not in {"passed", "warning", "failed"}:
                status = "warning"
            result.append(
                {
                    "id": str(item.get("id") or f"check-{index + 1}"),
                    "label": str(item.get("label") or "审校项")[:80],
                    "status": status,
                    "message": str(item.get("message") or "")[:300],
                }
            )
        if not result:
            raise AIEngineError("review_invalid", "模型没有返回审校项")
        return result
