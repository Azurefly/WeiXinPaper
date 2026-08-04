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
from typing import Any, Callable

from logger_config import get_logger
from secure_http import SecureHttpError, request_bytes
from test_mode import enabled as test_adapter_enabled

logger = get_logger("ai_engine")


def _env_int(name: str, default: int) -> int:
    """P1-24: 安全读取环境变量为 int，转换失败时返回默认值。"""
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


# #185 AI 请求超时可配置，默认 90 秒，避免长耗时请求无限制阻塞。
# 与 workflow.py 的分步超时（STUDIO_TIMEOUT_*）共同覆盖长运行操作。
AI_REQUEST_TIMEOUT = _env_int("STUDIO_AI_REQUEST_TIMEOUT", 90)


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
        # P1-24: int/float 转换保护，配置非法时回退到默认值
        try:
            _temperature = float(config.get("temperature", 0.4))
        except (TypeError, ValueError):
            _temperature = 0.4
        try:
            _max_tokens = int(config.get("maxTokens", 4096))
        except (TypeError, ValueError):
            _max_tokens = 4096
        self.config = AIConfig(
            base_url=str(config.get("baseUrl") or "https://api.openai.com/v1").rstrip("/"),
            api_key=str(config.get("apiKey") or ""),
            model=str(config.get("model") or "gpt-4.1-mini"),
            temperature=_temperature,
            max_tokens=_max_tokens,
        )
        # A5 修复：可选的 backup 模型配置，主模型连续失败2次后自动切换
        backup_base_url = str(config.get("backupBaseUrl") or "").rstrip("/")
        backup_api_key = str(config.get("backupApiKey") or "")
        backup_model = str(config.get("backupModel") or "")
        if backup_base_url and backup_api_key and backup_model:
            self.backup_config = AIConfig(
                base_url=backup_base_url,
                api_key=backup_api_key,
                model=backup_model,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )
        else:
            self.backup_config = None
        # #142 备用模型切换通知回调：主模型连续失败后切换到 backup 模型时触发，
        # 由 workflow 层注册以在任务时间线记录事件。回调签名为 (main_model, backup_model, reason)。
        self.on_backup_switch: Callable[[str, str, str], None] | None = None

    @staticmethod
    def _sanitize_prompt_input(text: str) -> str:
        """过滤用户输入中的 Prompt 注入模式。

        覆盖英文和中文的常见注入模式，包括：
        - 指令覆盖型：ignore previous instructions / 忽略之前的指令
        - 角色扮演型：you are a/now / 你现在是
        - 角色前缀型：system: / assistant: / 系统：/ 助手：
        - XML 标签型：<system>...</system> / [SYSTEM]
        - 零宽字符混淆
        """
        if not isinstance(text, str) or not text:
            return text
        original = text
        # 去除零宽字符和不可见 Unicode（防混淆绕过）
        text = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]", "", text)
        # 将全角英文字母/数字归一化为半角（防 Ｓystem: 绕过）
        text = text.translate(str.maketrans(
            "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ０１２３４５６７８９",
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
        ))
        # --- 英文注入模式 ---
        # 指令覆盖型
        text = re.sub(r"ignore\s+previous\s+instructions", "[filtered]", text, flags=re.IGNORECASE)
        text = re.sub(r"forget\s+(all\s+)?(previous|prior)\s+(instructions?|prompts?|rules?)", "[filtered]", text, flags=re.IGNORECASE)
        text = re.sub(r"disregard\s+(all\s+)?(previous|prior)\s+(instructions?|prompts?)", "[filtered]", text, flags=re.IGNORECASE)
        # 角色扮演型
        text = re.sub(r"(?i)\byou are (a|an|now|no longer)\b[^\n]*", "[filtered]", text)
        text = re.sub(r"(?i)\bact as (a|an|if)\b[^\n]*", "[filtered]", text)
        # 角色前缀型
        text = re.sub(r"(?im)^\s*(system|assistant|user|admin)\s*:", "[filtered]", text)
        # XML/标签型
        text = re.sub(r"(?i)<\/?(system|assistant|instruction|prompt|admin)\b[^>]*>", "[filtered]", text)
        text = re.sub(r"(?im)^\s*\[(system|assistant|instruction|admin)\]", "[filtered]", text)
        # --- 中文注入模式 ---
        text = re.sub(r"忽略(之前|之前所有|上面|以上|前面)的(指令|提示|规则|指示)", "[filtered]", text)
        text = re.sub(r"无视(之前|上面|以上|前面)的(指令|提示|规则|指示)", "[filtered]", text)
        text = re.sub(r"忘记(之前|上面|以上|前面)的(指令|提示|规则|指示)", "[filtered]", text)
        text = re.sub(r"(?i)你现在是(一个|一名)?[^\n]*", "[filtered]", text)
        text = re.sub(r"(?i)请你(扮演|充当|模拟)[^\n]*", "[filtered]", text)
        text = re.sub(r"(?im)^\s*(系统|助手|管理员)\s*[：:]", "[filtered]", text)
        if text != original:
            logger.warning("检测到提示词注入模式，已对用户输入进行过滤")
        return text

    def _notify_backup_switch(self, main_model: str, backup_model: str, reason: str) -> None:
        """#142 通知外部回调：主模型已切换到备用模型。"""
        if self.on_backup_switch is not None:
            try:
                self.on_backup_switch(main_model, backup_model, reason)
            except Exception:  # noqa: BLE001
                logger.warning("备用模型切换回调执行失败", exc_info=True)

    def _chat(self, system: str, user: str, *, json_mode: bool = False) -> str:
        if self.test_mode:
            return self._test_response(system, user, json_mode=json_mode)
        if not self.config.api_key:
            raise AIConfigurationRequired()
        # 仅过滤用户可控输入；system 提示词由开发者控制，不做处理
        user = self._sanitize_prompt_input(user)
        # A5 修复：主模型连续失败2次（HTTP 5xx 或连接错误）后切换到 backup 模型
        active_config = self.config
        switched_to_backup = False
        server_failures = 0  # 连续 5xx / 连接错误次数
        url = active_config.base_url + "/chat/completions"
        payload: dict[str, Any] = {
            "model": active_config.model,
            "temperature": active_config.temperature,
            "max_tokens": active_config.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        logger.info("AI 请求: model=%s json_mode=%s prompt_len=%d", active_config.model, json_mode, len(user))
        start_time = time.monotonic()
        max_retries = 3
        rate_limit_status = 429
        server_error_statuses = {500, 502, 503, 504}
        retryable_statuses = {rate_limit_status} | server_error_statuses
        # R4 修复：可重试的安全连接错误码（网络超时/连接失败），SSL 证书等安全错误不重试
        _RETRIABLE_SECURE_CODES = {"connection_failed", "dns_failed"}
        _connection_retry_used = False
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
                        "Authorization": f"Bearer {active_config.api_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "User-Agent": "WeiXinGZH-Studio/2.1.3",
                    },
                    timeout=AI_REQUEST_TIMEOUT,
                    max_bytes=4_000_000,
                    require_https=True,
                    reject_redirects=True,
                )
            except SecureHttpError as exc:
                elapsed = (time.monotonic() - start_time) * 1000
                # R4 修复：区分安全错误与网络超时
                # connection/timeout 类错误可重试1次；SSL 证书等安全错误立即失败
                if exc.code in _RETRIABLE_SECURE_CODES and not _connection_retry_used:
                    _connection_retry_used = True
                    server_failures += 1
                    # A5 修复：连续2次服务端/连接失败后切换到 backup 模型
                    if server_failures >= 2 and self.backup_config and not switched_to_backup:
                        switched_to_backup = True
                        active_config = self.backup_config
                        url = active_config.base_url + "/chat/completions"
                        payload["model"] = active_config.model
                        server_failures = 0
                        _connection_retry_used = False  # backup 模型也允许1次连接重试
                        logger.warning(
                            "主模型连续失败 2 次（连接错误），切换到 backup 模型: %s",
                            active_config.model,
                        )
                        self._notify_backup_switch(self.config.model, active_config.model, "连接错误")
                    else:
                        logger.warning(
                            "AI 连接失败 (code=%s, 耗时 %.0fms)，正在重试1次",
                            exc.code, elapsed,
                        )
                        time.sleep(1.0 + random.random())
                    continue
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
                # A5 修复：5xx 计入连续失败次数，达到2次后切换 backup 模型
                if status in server_error_statuses:
                    server_failures += 1
                    if server_failures >= 2 and self.backup_config and not switched_to_backup:
                        switched_to_backup = True
                        active_config = self.backup_config
                        url = active_config.base_url + "/chat/completions"
                        payload["model"] = active_config.model
                        server_failures = 0
                        logger.warning(
                            "主模型连续返回 5xx 2 次，切换到 backup 模型: %s",
                            active_config.model,
                        )
                        self._notify_backup_switch(self.config.model, active_config.model, "服务端 5xx 错误")
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
                        {"id": "facts", "label": "事实与来源", "status": "passed", "score": 95, "message": "引用内容与输入一致"},
                        {"id": "structure", "label": "结构完整", "status": "passed", "score": 90, "message": "结构清晰"},
                        {"id": "wechat", "label": "公众号可读性", "status": "passed", "score": 85, "message": "段落长度适中"},
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

    # --- A1 修复：非严格模式幻觉检测 ---
    # 检测正文中包含具体数字、日期、人名等事实性陈述但未标注来源的段落
    _FACTUAL_PATTERNS = [
        re.compile(r"\d{4}\s*年"),              # 2024年
        re.compile(r"\d+[.,，]?\d*\s*[%％]"),   # 百分比
        re.compile(r"\d+\s*[亿万]"),            # 数字+单位
        re.compile(r"据(统计|报告|调查|研究)"),  # 据统计
        re.compile(r"显示|表明|证实"),           # 显示/表明/证实
        re.compile(r"[A-Z][a-z]+\s[A-Z][a-z]+"),# 人名（英文）
    ]

    @classmethod
    def _detect_unsourced_facts(cls, content: str, source_text: str) -> list[str]:
        """检测非严格模式下可能包含虚构事实的段落。

        返回需要人工核查的段落摘要列表（可能为空）。
        不阻断流程，而是作为审校项展示给用户。
        """
        has_sources = "[来源" in source_text
        if not has_sources:
            return []  # 无来源可比对时跳过
        warnings: list[str] = []
        for paragraph in re.split(r"\n\s*\n", content):
            clean = paragraph.strip()
            if not clean or clean.startswith("#") or clean.startswith("```") or clean.startswith("!"):
                continue
            plain = re.sub(r"[`*_>#\-]", "", clean).strip()
            if len(plain) < 50:
                continue
            # 如果段落已标注来源，跳过
            if re.search(r"\[来源\d+\]", clean) or "现有来源无法确认" in clean:
                continue
            # 检测是否包含事实性陈述
            for pattern in cls._FACTUAL_PATTERNS:
                if pattern.search(plain):
                    warnings.append(plain[:80])
                    break
        return warnings

    # --- A3 修复：目标字数校验 ---
    @staticmethod
    def _check_word_count(content: str, target_length: int) -> dict[str, Any]:
        """校验生成正文的字数是否接近目标。"""
        # 统计中文字符 + 英文单词
        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", content))
        english_words = len(re.findall(r"[a-zA-Z]+", content))
        actual_length = chinese_chars + english_words
        if target_length <= 0:
            return {"ok": True, "actual": actual_length, "detail": ""}
        ratio = actual_length / target_length
        if ratio < 0.5:
            return {
                "ok": False,
                "actual": actual_length,
                "detail": f"正文 {actual_length} 字，仅达到目标字数 {target_length} 的 {ratio:.0%}，建议重新生成或补充内容",
            }
        if ratio < 0.7:
            return {
                "ok": False,
                "actual": actual_length,
                "detail": f"正文 {actual_length} 字，目标 {target_length} 字（{ratio:.0%}），偏短",
            }
        return {"ok": True, "actual": actual_length, "detail": f"正文 {actual_length} 字（目标 {target_length} 字）"}

    # --- X6 修复：系统提示词泄漏检测 ---
    _SYSTEM_PROMPT_MARKERS = [
        "严格事实模式", "普通创作模式", "返回 JSON",
        "你是资深微信公众号", "你是专业微信公众号作者", "你是公众号发布前审校员",
        "事实策略：", "文章生成要求：", "可信来源：",
    ]

    @classmethod
    def _detect_prompt_leakage(cls, content: str) -> bool:
        """检测 AI 输出是否泄漏了系统提示词。"""
        for marker in cls._SYSTEM_PROMPT_MARKERS:
            if marker in content:
                return True
        return False

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
        # A3 修复：字数校验（不阻断，但记录日志）
        word_check = self._check_word_count(content, target_length)
        if not word_check["ok"]:
            logger.warning("正文字数偏差: %s", word_check["detail"])
        # X6 修复：提示词泄漏检测
        if self._detect_prompt_leakage(content):
            logger.warning("检测到 AI 输出可能包含系统提示词泄漏，建议人工核查")
        if strict_facts:
            self._validate_strict_draft(content, source_text)
        return content

    def review(self, body_markdown: str, source_text: str) -> list[dict[str, Any]]:
        system = (
            "你是公众号发布前审校员。必须检查以下必检项，每项都不可省略：\n"
            "1. 合规性：是否包含政治敏感、违法广告法、谣言等风险内容\n"
            "2. 原创性：是否存在大段直接搬运来源原文的情况\n"
            "3. 事实准确性：事实性陈述是否与来源一致，有无虚构数据/人物/日期\n"
            "4. 结构完整性：标题、摘要、正文是否完整，逻辑是否通顺\n"
            "5. 公众号可读性：段落长度、配图位置是否适合移动端阅读\n"
            "每项检查需给出 0-100 的质量评分（score 字段）：passed 为 80-100，warning 为 60-79，failed 为 0-59。\n"
            "返回 JSON：{\"checks\":[{\"id\":...,\"label\":...,\"status\":\"passed|warning|failed\",\"score\":0-100,\"message\":...}]}。"
        )
        user = f"来源：\n{source_text[:50_000]}\n\n正文：\n{body_markdown[:80_000]}"
        data = self._json_object(self._chat(system, user, json_mode=True))
        checks = data.get("checks")
        if not isinstance(checks, list):
            raise AIEngineError("review_invalid", "模型返回的审校结果无效")
        result: list[dict[str, Any]] = []
        scores: list[int] = []
        for index, item in enumerate(checks[:10]):
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "warning")
            if status not in {"passed", "warning", "failed"}:
                status = "warning"
            # A4 修复：提取 score 字段，如果 AI 未返回则按 status 推断
            raw_score = item.get("score")
            try:
                score = int(raw_score)
            except (TypeError, ValueError):
                score = 100 if status == "passed" else (60 if status == "warning" else 0)
            score = max(0, min(100, score))
            scores.append(score)
            result.append(
                {
                    "id": str(item.get("id") or f"check-{index + 1}"),
                    "label": str(item.get("label") or "审校项")[:80],
                    "status": status,
                    "score": score,
                    "message": str(item.get("message") or "")[:300],
                }
            )
        if not result:
            raise AIEngineError("review_invalid", "模型没有返回审校项")

        # A1 修复：非严格模式幻觉检测——检测未标注来源的事实性陈述
        unsourced = self._detect_unsourced_facts(body_markdown, source_text)
        if unsourced:
            result.append({
                "id": "hallucination_check",
                "label": "事实性核查",
                "status": "warning",
                "score": 60,
                "message": f"检测到 {len(unsourced)} 个包含数据/日期/引用但未标注来源的段落，请人工核查是否为虚构内容：" + "；".join(unsourced[:3]),
            })
            scores.append(60)

        # X6 修复：系统提示词泄漏检测
        if self._detect_prompt_leakage(body_markdown):
            result.append({
                "id": "prompt_leakage",
                "label": "提示词泄漏检测",
                "status": "warning",
                "score": 60,
                "message": "正文可能包含系统提示词片段，请检查并删除指令性文字",
            })
            scores.append(60)

        # X1 修复：AI 生成内容版权声明审校项
        # 大语言模型可能在不经意间复现训练语料中的受版权保护文本，
        # 此审校项提醒用户在发布前人工核查版权风险。
        result.append({
            "id": "copyright_notice",
            "label": "版权与原创性声明",
            "status": "warning",
            "score": 60,
            "message": "本文由 AI 辅助生成，可能存在与已有作品相似的表述。发布前请人工核查内容原创性，"
                       "确保不侵犯他人著作权；如声明原创，请确认内容符合公众号原创规则。",
        })
        scores.append(60)

        # A4 修复：计算加权综合评分
        overall_score = sum(scores) // len(scores) if scores else 0
        if overall_score >= 80:
            overall_status = "passed"
        elif overall_score >= 60:
            overall_status = "warning"
        else:
            overall_status = "failed"
        result.append({
            "id": "overall_score",
            "label": "综合质量评分",
            "status": overall_status,
            "score": overall_score,
            "message": f"综合评分: {overall_score}/100",
        })

        return result

    def summarize(self, body_markdown: str, *, max_length: int = 120) -> str:
        """#125 根据正文生成摘要。

        使用 AI 从正文中提取/生成简洁摘要，供前端在编辑摘要字段时辅助使用。
        返回不超过 max_length 字符的摘要文本。
        """
        if not body_markdown or not body_markdown.strip():
            raise AIEngineError("summary_input_empty", "正文为空，无法生成摘要")
        system = (
            "你是公众号内容编辑。请根据提供的正文生成一段简洁的摘要，"
            f"字数不超过 {max_length} 字，概括文章核心观点与关键信息。"
            "直接输出摘要文本，不要包含标题、引号、Markdown 标记或额外说明。"
        )
        user = f"正文：\n{body_markdown[:80_000]}"
        summary = self._chat(system, user)
        summary = summary.strip().strip('"').strip("'").strip()
        if len(summary) > max_length:
            summary = summary[:max_length]
        if not summary:
            raise AIEngineError("summary_empty", "模型未能生成有效摘要")
        return summary
