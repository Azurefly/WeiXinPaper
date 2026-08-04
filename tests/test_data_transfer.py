from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from tests.test_release import running_server


class DataTransferTests(unittest.TestCase):
    """数据导出/导入端到端测试。"""

    def test_export_import_roundtrip(self):
        """导出数据 → 在新数据库导入 → 验证数据一致性。"""
        # 第一步：在服务器 A 中创建测试数据并导出
        with running_server() as (client_a, db_a, base_a):
            # 创建一篇文章
            status, created, _ = client_a.request(
                "/api/v2/workflows", "POST",
                {"sourceInput": "数据导出导入测试文章", "autoReview": False},
            )
            self.assertEqual(status, 202)
            task = client_a.wait_task(created["task"]["id"])
            self.assertEqual(task["status"], "succeeded")
            project_id = created["project"]["id"]

            # 修改通用设置
            client_a.request("/api/v2/settings", "PATCH", {
                "general": {"defaultLength": 3000, "strictFacts": True, "allowNetwork": True}
            })

            # 导出数据
            status, export_body, headers = client_a.request("/api/v2/data/export")
            # 导出端点返回 JSON 文本（非 JSON 对象，因为 Content-Type 是 application/json）
            if isinstance(export_body, str):
                export_data = json.loads(export_body)
            else:
                export_data = export_body
            self.assertEqual(export_data["format"], "studio-backup")
            self.assertEqual(export_data["version"], 1)
            self.assertGreater(len(export_data["projects"]), 0)

            # 验证项目数据完整性
            proj_entry = export_data["projects"][0]
            self.assertEqual(proj_entry["project"]["id"], project_id)
            self.assertIn("title", proj_entry["project"])
            self.assertIn("body_markdown", proj_entry["project"])
            self.assertGreater(len(proj_entry["tasks"]), 0)
            self.assertGreater(len(proj_entry["tasks"][0]["events"]), 0)

            # 验证设置已导出（不含密钥）
            self.assertIn("general", export_data["settings"])
            self.assertEqual(export_data["settings"]["general"]["defaultLength"], 3000)
            self.assertIn("ai", export_data["settings"])
            self.assertNotIn("apiKey", export_data["settings"]["ai"])

        # 第二步：在服务器 B（新数据库）中导入数据
        with running_server() as (client_b, db_b, base_b):
            # 导入前确认数据库为空
            status, bootstrap, _ = client_b.request("/api/v2/bootstrap")
            self.assertEqual(bootstrap["projects"], [])

            # 执行导入（合并模式）
            status, result, _ = client_b.request(
                "/api/v2/data/import", "POST",
                {"data": export_data, "mode": "merge"},
                timeout=30,
            )
            self.assertEqual(status, 200)
            self.assertTrue(result["ok"])
            self.assertEqual(result["imported"]["projects"], 1)
            self.assertGreater(result["imported"]["tasks"], 0)
            self.assertTrue(result["imported"]["settings"])

            # 验证导入后的项目
            status, bootstrap, _ = client_b.request("/api/v2/bootstrap")
            self.assertEqual(len(bootstrap["projects"]), 1)
            imported_project = bootstrap["projects"][0]
            self.assertEqual(imported_project["id"], project_id)
            self.assertEqual(imported_project["title"], proj_entry["project"]["title"])

            # 验证通用设置已导入
            status, settings, _ = client_b.request("/api/v2/settings")
            self.assertEqual(settings["general"]["defaultLength"], 3000)
            self.assertTrue(settings["general"]["strictFacts"])

            # 再次导入（合并模式应跳过已存在的项目）
            status, result2, _ = client_b.request(
                "/api/v2/data/import", "POST",
                {"data": export_data, "mode": "merge"},
                timeout=30,
            )
            self.assertEqual(status, 200)
            self.assertEqual(result2["imported"]["projects"], 0)
            self.assertEqual(result2["imported"]["skipped"], 1)

            # 覆盖模式导入
            status, result3, _ = client_b.request(
                "/api/v2/data/import", "POST",
                {"data": export_data, "mode": "replace"},
                timeout=30,
            )
            self.assertEqual(status, 200)
            self.assertEqual(result3["imported"]["projects"], 1)
            self.assertEqual(result3["imported"]["skipped"], 0)

    def test_import_rejects_invalid_format(self):
        """导入无效格式的数据应返回 400。"""
        with running_server() as (client, _db, _base):
            status, body, _ = client.request(
                "/api/v2/data/import", "POST",
                {"data": {"format": "unknown", "version": 1}, "mode": "merge"},
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_backup")

    def test_export_does_not_contain_secrets(self):
        """导出数据中不应包含密码哈希、会话令牌、API 密钥。"""
        with running_server() as (client, db, _base):
            # 配置 AI 设置（含密钥）
            client.request("/api/v2/settings", "PATCH", {
                "ai": {
                    "providerId": "openai-compatible",
                    "baseUrl": "https://api.openai.com/v1",
                    "apiKey": "sk-secret-key-12345",
                    "model": "gpt-test",
                    "temperature": 0.5,
                }
            })

            # 导出
            status, export_body, _ = client.request("/api/v2/data/export")
            if isinstance(export_body, str):
                export_data = json.loads(export_body)
            else:
                export_data = export_body

            # 验证不含密钥
            export_str = json.dumps(export_data)
            self.assertNotIn("sk-secret-key-12345", export_str)
            self.assertNotIn("password_hash", export_str)
            self.assertNotIn("token_hash", export_str)
            self.assertNotIn("apiKey", export_data.get("settings", {}).get("ai", {}))

    def test_export_includes_versions_and_receipts(self):
        """导出数据应包含版本历史和发布回执。"""
        with running_server() as (client, db, _base):
            # 创建文章
            status, created, _ = client.request(
                "/api/v2/workflows", "POST",
                {"sourceInput": "版本历史导出测试", "autoReview": True},
            )
            client.wait_task(created["task"]["id"])
            project_id = created["project"]["id"]

            # 编辑文章（生成版本历史）
            _, project, _ = client.request(f"/api/v2/projects/{project_id}")
            client.request(
                f"/api/v2/projects/{project_id}", "PATCH",
                {"bodyMarkdown": project["bodyMarkdown"] + "\n\n补充内容。"},
                {"If-Match": str(project["revision"])},
            )

            # 导出
            status, export_body, _ = client.request("/api/v2/data/export")
            if isinstance(export_body, str):
                export_data = json.loads(export_body)
            else:
                export_data = export_body

            proj = export_data["projects"][0]
            # 版本历史应非空
            self.assertGreater(len(proj["versions"]), 0)


if __name__ == "__main__":
    unittest.main()
