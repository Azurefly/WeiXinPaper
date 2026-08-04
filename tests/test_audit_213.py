from __future__ import annotations

from contextlib import closing

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BACKEND))

import verify_external_links  # noqa: E402
import build_release  # noqa: E402
from content_security import run_content_security_checks  # noqa: E402
from tests.test_release import running_server  # noqa: E402


class Audit213Tests(unittest.TestCase):
    def test_projects_api_supports_total_search_and_real_paging(self):
        with running_server() as (client, db_path, _):
            now = "2026-07-23T00:00:00+00:00"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.executemany(
                    "INSERT INTO projects(id,title,summary,created_at,updated_at) VALUES(?,?,?,?,?)",
                    [
                        (
                            f"page-{index:03d}",
                            f"分页文章 {index:03d}" + (" 唯一搜索词" if index == 73 else ""),
                            f"摘要 {index}",
                            now,
                            f"2026-07-23T00:{index // 60:02d}:{index % 60:02d}+00:00",
                        )
                        for index in range(125)
                    ],
                )
                conn.commit()
            status, first, _ = client.request("/api/v2/projects?includeArchived=false&limit=50&offset=0")
            self.assertEqual(status, 200)
            self.assertEqual(first["total"], 125)
            self.assertEqual(len(first["items"]), 50)
            self.assertTrue(first["hasMore"])
            status, last, _ = client.request("/api/v2/projects?includeArchived=false&limit=50&offset=100")
            self.assertEqual(status, 200)
            self.assertEqual(len(last["items"]), 25)
            self.assertFalse(last["hasMore"])
            status, searched, _ = client.request("/api/v2/projects?includeArchived=false&q=%E5%94%AF%E4%B8%80%E6%90%9C%E7%B4%A2%E8%AF%8D")
            self.assertEqual(status, 200)
            self.assertEqual(searched["total"], 1)
            self.assertIn("唯一搜索词", searched["items"][0]["title"])

    def test_bootstrap_contains_project_counts(self):
        with running_server() as (client, _, _):
            status, data, _ = client.request("/api/v2/bootstrap")
            self.assertEqual(status, 200)
            self.assertEqual(data["version"], "2.1.3")
            self.assertEqual(data["projectTotal"], 0)
            self.assertEqual(data["projectCounts"], {"active": 0, "all": 0, "deleted": 0})

    def test_external_ai_verifier_calls_full_plan_draft_review_chain(self):
        calls: list[tuple] = []

        class FakeEngine:
            def __init__(self, config):
                calls.append(("init", config["model"]))

            def plan(self, goal, evidence, strict):
                calls.append(("plan", goal, evidence, strict))
                return {"title": "标题", "summary": "摘要", "outline": ["一", "二", "三", "四"]}

            def draft(self, goal, evidence, plan, target_length, *, strict_facts=False):
                calls.append(("draft", goal, evidence, plan, target_length, strict_facts))
                return ("正文段落 [来源1]\n\n" * 20).strip()

            def review(self, draft, evidence):
                calls.append(("review", draft, evidence))
                return [{"status": "passed"}]

        with mock.patch.object(verify_external_links, "AIEngine", FakeEngine), mock.patch.dict(
            os.environ,
            {"STUDIO_VERIFY_AI_KEY": "test-key", "STUDIO_VERIFY_AI_MODEL": "test-model"},
            clear=False,
        ):
            result = verify_external_links.verify_ai()
        self.assertEqual(result["status"], "succeeded")
        draft_call = next(item for item in calls if item[0] == "draft")
        self.assertIsInstance(draft_call[3], dict)
        self.assertEqual(draft_call[4], 800)
        self.assertTrue(draft_call[5])

    def test_wechat_cover_upload_multipart_uses_real_file_signature(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cover.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 128)
            body, boundary = verify_external_links._multipart_cover(path)
            self.assertIn(boundary.encode(), body)
            self.assertIn(b'Content-Disposition: form-data; name="media"', body)
            self.assertIn(b"Content-Type: image/png", body)
            self.assertIn(path.read_bytes(), body)

    def test_capacity_and_platform_verifiers_are_shipped(self):
        for name in (
            "verify_capacity.py",
            "verify_browser_service.py",
            "verify_browser_service.cmd",
            "verify_windows_dpapi.py",
            "verify_windows_dpapi.cmd",
        ):
            self.assertTrue((ROOT / name).is_file(), name)
        capacity_source = (ROOT / "verify_capacity.py").read_text(encoding="utf-8")
        self.assertIn("10_000", capacity_source)
        self.assertIn("integrity_check", capacity_source)

    def test_topic_review_does_not_claim_originality_without_source(self):
        checks = run_content_security_checks("这是一段足够长的主题创作正文。" * 20, "")
        source_check = next(item for item in checks if item["id"] == "source_overlap")
        self.assertEqual(source_check["status"], "warning")
        self.assertIn("不代表原创性", source_check["message"])
        self.assertNotIn("score", source_check)

    def test_release_builder_excludes_every_runtime_data_file(self):
        for name in (
            "data/studio.db",
            "data/studio.db.backup_123",
            "data/studio.log",
            "data/studio.log.2026-08-04",
            "data/.master.key",
            "data/.initial_password",
        ):
            self.assertTrue(build_release.excluded(ROOT / name), name)

    def test_source_release_contains_desktop_build_chain(self):
        required = {
            ".github",
            "build_assets",
            "build_scripts",
            "desktop.py",
            "requirements-desktop.txt",
        }
        self.assertTrue(required.issubset(build_release.SOURCE_TOP))

    def test_frontend_uses_server_side_pagination(self):
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("articlePageSize: 50", js)
        self.assertIn("deletedOnly", js)
        self.assertIn("state.articleTotal", js)
        self.assertIn("article-prev", js)
        self.assertIn("article-next", js)
        self.assertNotIn("&limit=500`,", js)


if __name__ == "__main__":
    unittest.main()
