from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from tests.test_release import free_port, running_server, Client  # noqa: E402
from logger_config import get_logger, query_logs, clear_logs, redact_sensitive, RingBufferHandler  # noqa: E402


class LogUnitTests(unittest.TestCase):
    """logger_config 模块单元测试。"""

    def test_redact_sensitive_masks_bearer_token(self):
        text = "Authorization: Bearer sk-abc123def456"
        result = redact_sensitive(text)
        self.assertNotIn("sk-abc123def456", result)
        self.assertIn("***REDACTED***", result)

    def test_redact_sensitive_masks_api_key(self):
        text = "api_key=sk-test123456789012345"
        result = redact_sensitive(text)
        self.assertNotIn("sk-test123456789012345", result)

    def test_redact_sensitive_masks_access_token(self):
        text = 'access_token="abc_def_123"'
        result = redact_sensitive(text)
        self.assertNotIn("abc_def_123", result)

    def test_redact_sensitive_masks_password(self):
        text = "password=mysecret123"
        result = redact_sensitive(text)
        self.assertNotIn("mysecret123", result)

    def test_redact_preserves_normal_text(self):
        text = "AI 请求: model=gpt-4o json_mode=False prompt_len=500"
        result = redact_sensitive(text)
        self.assertEqual(text, result)

    def test_ring_buffer_evicts_oldest(self):
        handler = RingBufferHandler(maxlen=5)
        import logging
        logger = logging.getLogger("test_evict")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        for i in range(8):
            logger.info("message %d", i)
        results = handler.query(limit=100)
        self.assertEqual(len(results), 5)
        self.assertIn("message 7", results[-1]["message"])
        self.assertIn("message 3", results[0]["message"])
        self.assertNotIn("message 2", [r["message"] for r in results])

    def test_query_filters_by_level(self):
        handler = RingBufferHandler(maxlen=100)
        import logging
        logger = logging.getLogger("test_level")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.info("info message")
        logger.warning("warn message")
        logger.error("error message")
        errors = handler.query(level="ERROR")
        self.assertEqual(len(errors), 1)
        self.assertIn("error message", errors[0]["message"])
        warnings = handler.query(level="WARNING")
        self.assertEqual(len(warnings), 1)

    def test_query_filters_by_keyword(self):
        handler = RingBufferHandler(maxlen=100)
        import logging
        logger = logging.getLogger("test_keyword")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.info("source_fetcher started")
        logger.error("ai_engine timeout")
        results = handler.query(keyword="timeout")
        self.assertEqual(len(results), 1)
        self.assertIn("timeout", results[0]["message"])

    def test_query_filters_by_task_id(self):
        handler = RingBufferHandler(maxlen=100)
        import logging
        from logger_config import TaskLoggerAdapter
        logger = logging.getLogger("test_task")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        adapter_a = TaskLoggerAdapter(logger, {"task_id": "tsk_aaa"})
        adapter_b = TaskLoggerAdapter(logger, {"task_id": "tsk_bbb"})
        adapter_a.info("task A message")
        adapter_b.info("task B message")
        results = handler.query(task_id="tsk_aaa")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["task_id"], "tsk_aaa")

    def test_stack_trace_captured(self):
        handler = RingBufferHandler(maxlen=100)
        import logging
        logger = logging.getLogger("test_stack")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            raise ValueError("test exception")
        except ValueError:
            logger.error("error with stack", exc_info=True)
        results = handler.query(level="ERROR")
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["stack"])
        self.assertIn("ValueError", results[0]["stack"])
        self.assertIn("test exception", results[0]["stack"])


class LogApiTests(unittest.TestCase):
    """日志 API 集成测试。"""

    @classmethod
    def setUpClass(cls):
        cls.ctx = running_server()
        cls.client, cls.db_path, cls.base = cls.ctx.__enter__()
        # 清空日志确保测试干净
        clear_logs()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def test_01_logs_api_returns_json(self):
        status, data, _ = self.client.request("/api/v2/logs")
        self.assertEqual(status, 200)
        self.assertIn("logs", data)
        self.assertIn("total", data)
        self.assertIsInstance(data["logs"], list)

    def test_02_logs_are_generated_by_workflow(self):
        # 创建一篇文章触发工作流，应产生日志
        status, result, _ = self.client.request(
            "/api/v2/workflows",
            "POST",
            {"sourceInput": "日志测试文章", "autoReview": True},
        )
        self.assertEqual(status, 202)
        self.client.wait_task(result["task"]["id"])
        # 查询日志
        status, data, _ = self.client.request("/api/v2/logs?limit=50")
        self.assertEqual(status, 200)
        self.assertGreater(data["total"], 0)
        # 应包含工作流相关日志
        all_msgs = " ".join(log.get("message", "") for log in data["logs"])
        self.assertTrue(
            any(keyword in all_msgs for keyword in ["工作流", "AI", "来源", "access"]),
            f"未找到工作流相关日志，实际日志: {all_msgs[:200]}",
        )

    def test_03_level_filter(self):
        status, data, _ = self.client.request("/api/v2/logs?level=ERROR")
        self.assertEqual(status, 200)
        for log in data["logs"]:
            self.assertEqual(log["level"], "ERROR")

    def test_04_keyword_search(self):
        status, data, _ = self.client.request("/api/v2/logs?q=workflow&limit=20")
        self.assertEqual(status, 200)
        for log in data["logs"]:
            searchable = f"{log.get('message', '')} {log.get('stack', '')} {log.get('module', '')}".lower()
            self.assertIn("workflow", searchable.lower())

    def test_05_limit_parameter(self):
        status, data, _ = self.client.request("/api/v2/logs?limit=3")
        self.assertEqual(status, 200)
        self.assertLessEqual(len(data["logs"]), 3)

    def test_06_task_specific_logs(self):
        # 创建任务
        status, result, _ = self.client.request(
            "/api/v2/workflows",
            "POST",
            {"sourceInput": "任务日志测试", "autoReview": True},
        )
        task_id = result["task"]["id"]
        self.client.wait_task(task_id)
        # 查询该任务的日志
        status, data, _ = self.client.request(f"/api/v2/tasks/{task_id}/logs?limit=50")
        self.assertEqual(status, 200)
        # 至少应有该任务的日志
        self.assertGreater(data["total"], 0)
        for log in data["logs"]:
            self.assertEqual(log["task_id"], task_id)

    def test_07_since_filter(self):
        # 获取当前所有日志中最新的时间戳
        status, data, _ = self.client.request("/api/v2/logs?limit=1")
        self.assertEqual(status, 200)
        if data["logs"]:
            since = data["logs"][0]["timestamp"]
            # 用最新时间戳作为 since 过滤，应返回 0 条
            status, filtered, _ = self.client.request(f"/api/v2/logs?since={since}&limit=10")
            self.assertEqual(status, 200)
            for log in filtered["logs"]:
                self.assertGreaterEqual(log["timestamp"], since)

    def test_08_invalid_level_fallback(self):
        status, data, _ = self.client.request("/api/v2/logs?level=INVALID")
        self.assertEqual(status, 200)
        # 无效级别回退为 ALL
        self.assertGreaterEqual(data["total"], 0)

    def test_09_limit_clamped_to_max(self):
        status, data, _ = self.client.request("/api/v2/logs?limit=99999")
        self.assertEqual(status, 200)
        self.assertLessEqual(len(data["logs"]), 1000)

    def test_10_logs_contain_module_field(self):
        status, data, _ = self.client.request("/api/v2/logs?limit=5")
        self.assertEqual(status, 200)
        if data["logs"]:
            for log in data["logs"]:
                self.assertIn("module", log)
                self.assertTrue(log["module"])

    def test_11_sensitive_data_redacted(self):
        # 直接通过 logger 写入包含敏感信息的日志
        log = get_logger("test_redact", "tsk_test")
        log.info("Authorization: Bearer sk-test123456789012345")
        status, data, _ = self.client.request("/api/v2/logs?q=redact&limit=10")
        self.assertEqual(status, 200)
        all_text = " ".join(log.get("message", "") for log in data["logs"])
        # 原始 key 不应出现
        if "sk-test123456789012345" in all_text:
            self.fail("敏感数据未被脱敏")

    def test_12_access_log_present(self):
        # 发送几个请求后检查 access 日志
        self.client.request("/api/v2/health")
        status, data, _ = self.client.request("/api/v2/logs?q=access&limit=20")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
