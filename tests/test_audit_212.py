from __future__ import annotations

from contextlib import closing
import gzip
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import db as db_module  # noqa: E402
import secure_http  # noqa: E402
import server as server_module  # noqa: E402
import test_mode  # noqa: E402
from db import connect, get_project, init_db, list_projects, record_project_version  # noqa: E402
from ai_engine import AIEngine, AIEngineError  # noqa: E402
from secrets_store import decrypt, encrypt  # noqa: E402
from source_fetcher import SourceFetchError, SourceSnapshot, _gunzip_limited  # noqa: E402
from tests.test_release import free_port, running_server  # noqa: E402


class _FakeSocket:
    def __init__(self, peer: str):
        self.peer = peer

    def getpeername(self):
        return (self.peer, 443)

    def settimeout(self, _timeout):
        return None


class _FakeResponse:
    def __init__(self, status: int, headers=None, body: bytes = b"{}"):
        self.status = status
        self._headers = headers or []
        self._body = body
        self._read = False

    def getheaders(self):
        return self._headers

    def read(self, _size=-1):
        if self._read:
            return b""
        self._read = True
        return self._body


class _FakeConnection:
    status = 200
    response_headers = []
    peer = "93.184.216.34"
    captured_headers = None

    def __init__(self, _host, _ip, _port, _timeout):
        self.sock = _FakeSocket(self.peer)

    def request(self, _method, _path, body=None, headers=None):
        _ = body
        type(self).captured_headers = dict(headers or {})

    def getresponse(self):
        return _FakeResponse(type(self).status, type(self).response_headers)

    def close(self):
        return None


class Audit212Tests(unittest.TestCase):
    def test_remote_listener_is_rejected_by_both_entrypoints(self):
        env = os.environ.copy()
        env.update({"STUDIO_HOST": "0.0.0.0", "STUDIO_PORT": str(free_port()), "STUDIO_NO_BROWSER": "1"})
        for command, cwd in [
            ([sys.executable, "server.py", "--host", "0.0.0.0", "--port", str(free_port())], BACKEND),
            ([sys.executable, "start.py"], ROOT),
        ]:
            result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=8)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("远程监听被拒绝", result.stdout + result.stderr)

    def test_secure_http_rejects_redirect_without_following_credentials(self):
        _FakeConnection.status = 302
        _FakeConnection.response_headers = [("Location", "https://evil.example/steal")]
        _FakeConnection.peer = "93.184.216.34"
        with mock.patch.object(secure_http, "resolve_public", return_value=["93.184.216.34"]), mock.patch.object(
            secure_http, "_PinnedHTTPSConnection", _FakeConnection
        ):
            with self.assertRaises(secure_http.SecureHttpError) as ctx:
                secure_http.request_bytes(
                    "https://api.example.com/models",
                    headers={"Authorization": "Bearer top-secret"},
                    reject_redirects=True,
                )
        self.assertEqual(ctx.exception.code, "redirect_forbidden")
        self.assertEqual(_FakeConnection.captured_headers["Authorization"], "Bearer top-secret")

    def test_secure_http_detects_peer_change_after_dns_validation(self):
        _FakeConnection.status = 200
        _FakeConnection.response_headers = []
        _FakeConnection.peer = "93.184.216.35"
        with mock.patch.object(secure_http, "resolve_public", return_value=["93.184.216.34"]), mock.patch.object(
            secure_http, "_PinnedHTTPSConnection", _FakeConnection
        ):
            with self.assertRaises(secure_http.SecureHttpError) as ctx:
                secure_http.request_bytes("https://api.example.com/models")
        self.assertEqual(ctx.exception.code, "peer_mismatch")

    def test_publish_uses_frozen_revision_and_records_stale_receipt(self):
        with running_server(extra_env={"STUDIO_TEST_WECHAT_DELAY": "0.4"}) as (client, db_path, _base):
            _, created, _ = client.request("/api/v2/workflows", "POST", {"sourceInput": "并发发布快照测试"})
            client.wait_task(created["task"]["id"])
            _, project, _ = client.request(f"/api/v2/projects/{created['project']['id']}")
            _, preview, _ = client.request(f"/api/v2/projects/{project['id']}/preview")
            client.request(
                "/api/v2/settings/wechat/verify-and-save",
                "POST",
                {"accountName": "并发测试", "appId": "good", "appSecret": "secret", "thumbMediaId": "thumb"},
            )
            _, reviewed, _ = client.request(
                f"/api/v2/projects/{project['id']}/review",
                "POST",
                {"approved": True, "revision": project["revision"], "bodyFingerprint": preview["bodyFingerprint"]},
            )
            _, preview, _ = client.request(f"/api/v2/projects/{project['id']}/preview")
            result_holder: dict[str, object] = {}

            def publish():
                result_holder["value"] = client.request(
                    f"/api/v2/projects/{project['id']}/publish",
                    "POST",
                    {
                        "revision": reviewed["revision"],
                        "bodyFingerprint": preview["bodyFingerprint"],
                        "previewHash": preview["previewHash"],
                    },
                )

            thread = threading.Thread(target=publish)
            thread.start()
            time.sleep(0.12)
            status, edited, _ = client.request(
                f"/api/v2/projects/{project['id']}",
                "PATCH",
                {"title": reviewed["title"] + "（并发修改）"},
                {"If-Match": str(reviewed["revision"])},
            )
            self.assertEqual(status, 200)
            thread.join(timeout=4)
            publish_status, publish_result, _ = result_holder["value"]
            self.assertEqual(publish_status, 200)
            self.assertEqual(publish_result["status"], "stale")
            _, current, _ = client.request(f"/api/v2/projects/{project['id']}")
            self.assertEqual(current["revision"], edited["revision"])
            self.assertEqual(current["publishStatus"], "not_synced")
            with closing(sqlite3.connect(db_path)) as conn:
                receipt = conn.execute(
                    "SELECT revision,status,remote_id FROM publish_receipts WHERE project_id=? ORDER BY id DESC LIMIT 1",
                    (project["id"],),
                ).fetchone()
            self.assertEqual(receipt[0], reviewed["revision"])
            self.assertEqual(receipt[1], "stale")
            self.assertTrue(receipt[2].startswith("media_test_"))

    def test_publish_preview_and_review_are_revision_bound(self):
        with running_server() as (client, _db, _base):
            _, created, _ = client.request("/api/v2/workflows", "POST", {"sourceInput": "预览一致性测试"})
            client.wait_task(created["task"]["id"])
            _, project, _ = client.request(f"/api/v2/projects/{created['project']['id']}")
            _, preview, _ = client.request(f"/api/v2/projects/{project['id']}/preview")
            self.assertEqual(preview["bodyFingerprint"], hashlib.sha256(project["bodyMarkdown"].encode()).hexdigest())
            self.assertEqual(preview["previewHash"], hashlib.sha256(preview["html"].encode()).hexdigest())
            status, problem, _ = client.request(
                f"/api/v2/projects/{project['id']}/review",
                "POST",
                {"approved": True, "revision": project["revision"], "bodyFingerprint": "0" * 64},
            )
            self.assertEqual(status, 409)
            self.assertEqual(problem["error"]["code"], "review_fingerprint_mismatch")


    def test_strict_facts_requires_valid_source_coverage(self):
        source = "[来源1] 这是一段可核验的来源内容。"
        valid = "# 标题\n\n这是一段超过三十个字符并且明确绑定来源的事实性正文内容，用于严格事实校验。 [来源1]"
        AIEngine._validate_strict_draft(valid, source)
        with self.assertRaises(AIEngineError) as missing:
            AIEngine._validate_strict_draft(
                "# 标题\n\n这是一段超过三十个字符但完全没有绑定来源的事实性正文内容，用于触发校验失败。",
                source,
            )
        self.assertEqual(missing.exception.code, "strict_facts_citation_missing")
        with self.assertRaises(AIEngineError) as invalid:
            AIEngine._validate_strict_draft(
                "# 标题\n\n这是一段超过三十个字符却引用了不存在来源编号的事实性正文内容。 [来源2]",
                source,
            )
        self.assertEqual(invalid.exception.code, "strict_facts_citation_invalid")

    def test_strict_facts_topic_without_evidence_is_blocked(self):
        with running_server() as (client, _db, _base):
            status, _, _ = client.request(
                "/api/v2/settings",
                "PATCH",
                {"general": {"strictFacts": True}},
            )
            self.assertEqual(status, 200)
            status, created, _ = client.request("/api/v2/workflows", "POST", {"sourceInput": "没有来源的严格事实主题"})
            self.assertEqual(status, 202)
            task = client.wait_task(created["task"]["id"])
            self.assertEqual(task["status"], "failed")
            self.assertEqual(task["errorCode"], "strict_facts_no_evidence")

    def test_task_retry_scope_and_single_active_task(self):
        with running_server(delay=0.4) as (client, db_path, _base):
            _, created, _ = client.request("/api/v2/workflows", "POST", {"sourceInput": "重试并发测试"})
            task = client.wait_task(created["task"]["id"])
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute("UPDATE tasks SET status='failed',error_code='forced' WHERE id=?", (task["id"],))
                conn.commit()
            status, retry, _ = client.request(
                f"/api/v2/tasks/{task['id']}/retry",
                "POST",
                {"retryMode": "full"},
            )
            self.assertEqual(status, 202)
            self.assertEqual(retry["task"]["retryMode"], "full")
            status, problem, _ = client.request(
                f"/api/v2/tasks/{task['id']}/retry",
                "POST",
                {"retryMode": "review_only"},
            )
            self.assertEqual(status, 400)
            self.assertIn("活跃任务", problem["error"]["message"])
            client.wait_task(retry["task"]["id"], timeout=10)

    def test_successful_task_cannot_be_retried(self):
        with running_server() as (client, _db, _base):
            _, created, _ = client.request("/api/v2/workflows", "POST", {"sourceInput": "成功任务不可重试"})
            task = client.wait_task(created["task"]["id"])
            status, problem, _ = client.request(
                f"/api/v2/tasks/{task['id']}/retry", "POST", {"retryMode": "full"}
            )
            self.assertEqual(status, 400)
            self.assertIn("仅失败", problem["error"]["message"])

    def test_settings_reject_string_boolean_and_out_of_range_values(self):
        with running_server() as (client, _db, _base):
            status, problem, _ = client.request(
                "/api/v2/settings", "PATCH", {"general": {"strictFacts": "false"}}
            )
            self.assertEqual(status, 400)
            self.assertEqual(problem["error"]["code"], "invalid_settings")
            status, problem, _ = client.request(
                "/api/v2/settings", "PATCH", {"general": {"defaultLength": 100_000}}
            )
            self.assertEqual(status, 400)
            status, problem, _ = client.request(
                "/api/v2/settings",
                "PATCH",
                {"ai": {"providerId": "openai-compatible", "baseUrl": "https://api.openai.com/v1", "model": "x", "temperature": 9}},
            )
            self.assertEqual(status, 400)

    def test_gzip_bomb_is_stopped_during_streaming_decompression(self):
        payload = gzip.compress(b"A" * 2_100_000)
        with self.assertRaises(SourceFetchError) as ctx:
            _gunzip_limited(payload)
        self.assertEqual(ctx.exception.code, "content_too_large")


    def test_source_refresh_invalidates_all_downstream_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            old_db = os.environ.get("STUDIO_DB")
            old_key = os.environ.get("STUDIO_MASTER_KEY_FILE")
            os.environ["STUDIO_DB"] = str(Path(temp) / "refresh.db")
            os.environ["STUDIO_MASTER_KEY_FILE"] = str(Path(temp) / "key")
            try:
                init_db()
                now = "2026-01-01T00:00:00+00:00"
                with connect() as conn:
                    conn.execute(
                        """INSERT INTO projects(
                            id,title,goal,source_input,source_kind,status,summary,outline_json,body_markdown,
                            review_json,review_fingerprint,review_approved,review_revision,reviewed_at,
                            publish_status,publish_remote_id,published_revision,publish_fingerprint,publish_preview_hash,
                            revision,created_at,updated_at
                        ) VALUES(?,?,?,?,?,'draft',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            "p-refresh", "旧标题", "目标", "https://example.com/article", "url",
                            "旧摘要", '["旧框架"]', "旧正文", '[{"status":"passed"}]', "old-fp", 1, 4, now,
                            "synced", "media-old", 4, "old-pub-fp", "old-preview", 4, now, now,
                        ),
                    )
                    conn.execute(
                        """INSERT INTO source_snapshots(
                            id,content_hash,source_url,final_url,title,content_text,preview,fetched_at,extraction_method
                        ) VALUES('src-old','oldhash','https://example.com/article','https://example.com/article','旧来源','旧内容','旧预览',?,'test')""",
                        (now,),
                    )
                    conn.execute("INSERT INTO project_sources(project_id,snapshot_id,source_order) VALUES('p-refresh','src-old',0)")
                new_snapshot = SourceSnapshot(
                    "https://example.com/article", "https://example.com/article", "新来源标题", "发布方", "作者", "",
                    "新的来源正文内容" * 30, "新的来源预览", "newhash", "test",
                )
                captured = {}
                class FakeHandler:
                    def _send_json(self, status, value):
                        captured["status"] = status
                        captured["value"] = value
                with mock.patch.object(server_module, "fetch_source", return_value=new_snapshot):
                    server_module.StudioHandler._refresh_source(FakeHandler(), get_project("p-refresh"))
                refreshed = captured["value"]["project"]
                self.assertEqual(captured["status"], 200)
                self.assertTrue(captured["value"]["changed"])
                self.assertEqual(refreshed["title"], "新来源标题")
                self.assertEqual(refreshed["summary"], "")
                self.assertEqual(refreshed["outline"], [])
                self.assertEqual(refreshed["bodyMarkdown"], "")
                self.assertEqual(refreshed["review"], [])
                self.assertFalse(refreshed["reviewApproved"])
                self.assertEqual(refreshed["publishStatus"], "not_synced")
                self.assertEqual(refreshed["revision"], 5)
            finally:
                if old_db is None:
                    os.environ.pop("STUDIO_DB", None)
                else:
                    os.environ["STUDIO_DB"] = old_db
                if old_key is None:
                    os.environ.pop("STUDIO_MASTER_KEY_FILE", None)
                else:
                    os.environ["STUDIO_MASTER_KEY_FILE"] = old_key

    def test_cover_data_url_validation_and_test_upload_binding(self):
        import base64
        png = b"\x89PNG\r\n\x1a\n" + b"safe-cover"
        data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        mime, decoded = server_module._parse_cover_data_url(data_url)
        self.assertEqual(mime, "image/png")
        self.assertEqual(decoded, png)
        with mock.patch.dict(os.environ, {"STUDIO_ENABLE_TEST_ADAPTERS": "1", "STUDIO_TEST_WECHAT": "1"}, clear=False):
            media_id = server_module._wechat_upload_cover("test-token", data_url)
        self.assertTrue(media_id.startswith("thumb_test_"))
        with self.assertRaises(server_module.ApiProblem):
            server_module._parse_cover_data_url("data:image/png;base64," + base64.b64encode(b"not-png").decode("ascii"))

    def test_source_snapshot_identity_includes_url(self):
        with tempfile.TemporaryDirectory() as temp:
            old_db = os.environ.get("STUDIO_DB")
            old_key = os.environ.get("STUDIO_MASTER_KEY_FILE")
            os.environ["STUDIO_DB"] = str(Path(temp) / "studio.db")
            os.environ["STUDIO_MASTER_KEY_FILE"] = str(Path(temp) / "key")
            try:
                init_db()
                now = "2026-01-01T00:00:00+00:00"
                with connect() as conn:
                    for project_id in ["p1", "p2"]:
                        conn.execute(
                            "INSERT INTO projects(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                            (project_id, project_id, now, now),
                        )
                snapshots = [
                    SourceSnapshot("https://a.example/x", "https://a.example/x", "A", "", "", "", "same body", "same body", "h" * 64, "test"),
                    SourceSnapshot("https://b.example/x", "https://b.example/x", "B", "", "", "", "same body", "same body", "h" * 64, "test"),
                ]
                import workflow

                with mock.patch.object(workflow, "fetch_source", side_effect=snapshots):
                    workflow._snapshot_source("p1", "https://a.example/x")
                    workflow._snapshot_source("p2", "https://b.example/x")
                with connect() as conn:
                    rows = conn.execute("SELECT source_url,content_hash FROM source_snapshots ORDER BY source_url").fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual({row["source_url"] for row in rows}, {"https://a.example/x", "https://b.example/x"})
            finally:
                if old_db is None:
                    os.environ.pop("STUDIO_DB", None)
                else:
                    os.environ["STUDIO_DB"] = old_db
                if old_key is None:
                    os.environ.pop("STUDIO_MASTER_KEY_FILE", None)
                else:
                    os.environ["STUDIO_MASTER_KEY_FILE"] = old_key

    def test_version_restore_invalidates_review_and_publish(self):
        with running_server() as (client, _db, _base):
            _, created, _ = client.request("/api/v2/workflows", "POST", {"sourceInput": "版本恢复测试"})
            client.wait_task(created["task"]["id"])
            _, project, _ = client.request(f"/api/v2/projects/{created['project']['id']}")
            original_body = project["bodyMarkdown"]
            status, edited, _ = client.request(
                f"/api/v2/projects/{project['id']}",
                "PATCH",
                {"bodyMarkdown": original_body + "\n\n人工第二版"},
                {"If-Match": str(project["revision"])},
            )
            self.assertEqual(status, 200)
            _, versions, _ = client.request(f"/api/v2/projects/{project['id']}/versions")
            target = next(item for item in versions["items"] if item["revision"] == project["revision"])
            status, restored, _ = client.request(
                f"/api/v2/projects/{project['id']}/versions/{target['revision']}/restore", "POST", {}
            )
            self.assertEqual(status, 200)
            self.assertEqual(restored["bodyMarkdown"], original_body)
            self.assertFalse(restored["reviewApproved"])
            self.assertEqual(restored["publishStatus"], "not_synced")
            self.assertGreater(restored["revision"], edited["revision"])

    def test_secret_file_permissions_and_ciphertext_hint_source(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "key"
            path.write_bytes(os.urandom(32))
            os.chmod(path, 0o644)
            old = os.environ.get("STUDIO_MASTER_KEY_FILE")
            os.environ["STUDIO_MASTER_KEY_FILE"] = str(path)
            try:
                ciphertext = encrypt("my-real-secret")
                self.assertNotIn("my-real-secret", ciphertext)
                self.assertEqual(decrypt(ciphertext), "my-real-secret")
                if os.name != "nt":
                    self.assertEqual(path.stat().st_mode & 0o077, 0)
            finally:
                if old is None:
                    os.environ.pop("STUDIO_MASTER_KEY_FILE", None)
                else:
                    os.environ["STUDIO_MASTER_KEY_FILE"] = old


    def test_migration_failure_restores_sqlite_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "legacy.db"
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY,title TEXT NOT NULL)")
                conn.execute("INSERT INTO projects(id,title) VALUES('legacy','迁移前文章')")
                conn.commit()
            old_db = os.environ.get("STUDIO_DB")
            os.environ["STUDIO_DB"] = str(path)
            try:
                with mock.patch.object(db_module, "_create_schema", side_effect=RuntimeError("forced migration failure")):
                    with self.assertRaises(RuntimeError):
                        init_db()
                with closing(sqlite3.connect(path)) as conn:
                    row = conn.execute("SELECT id,title FROM projects").fetchone()
                    self.assertEqual(row, ("legacy", "迁移前文章"))
                    self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertTrue(list(path.parent.glob(path.name + ".pre-2.1.2-v0-*.bak")))
            finally:
                if old_db is None:
                    os.environ.pop("STUDIO_DB", None)
                else:
                    os.environ["STUDIO_DB"] = old_db

    def test_capacity_version_retention_and_pagination(self):
        with tempfile.TemporaryDirectory() as temp:
            old_db = os.environ.get("STUDIO_DB")
            old_key = os.environ.get("STUDIO_MASTER_KEY_FILE")
            os.environ["STUDIO_DB"] = str(Path(temp) / "capacity.db")
            os.environ["STUDIO_MASTER_KEY_FILE"] = str(Path(temp) / "key")
            try:
                init_db()
                now = "2026-01-01T00:00:00+00:00"
                with connect() as conn:
                    for index in range(5):
                        conn.execute(
                            "INSERT INTO projects(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                            (f"p{index}", f"文章{index}", now, f"2026-01-01T00:00:0{index}+00:00"),
                        )
                    for index in range(130):
                        record_project_version(conn, "p0", f"save-{index}")
                self.assertEqual(len(list_projects(limit=2, offset=1)), 2)
                with connect() as conn:
                    count = conn.execute("SELECT COUNT(*) FROM project_versions WHERE project_id='p0'").fetchone()[0]
                self.assertEqual(count, 100)
            finally:
                if old_db is None:
                    os.environ.pop("STUDIO_DB", None)
                else:
                    os.environ["STUDIO_DB"] = old_db
                if old_key is None:
                    os.environ.pop("STUDIO_MASTER_KEY_FILE", None)
                else:
                    os.environ["STUDIO_MASTER_KEY_FILE"] = old_key

    def test_test_adapters_require_source_only_marker(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(test_mode, "_MARKER", Path(temp) / "missing"):
            with mock.patch.dict(os.environ, {"STUDIO_ENABLE_TEST_ADAPTERS": "1", "STUDIO_TEST_AI": "1"}, clear=False):
                self.assertFalse(test_mode.enabled("STUDIO_TEST_AI"))

    def test_frontend_contains_serial_save_preview_and_unload_gate(self):
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("saveChains: new Map()", js)
        self.assertIn("while (state.pendingSaves.has(projectId))", js)
        self.assertIn("event.returnValue = ''", js)
        self.assertIn("previewHash: state.preview.previewHash", js)
        self.assertIn("bodyFingerprint: state.preview.bodyFingerprint", js)
        self.assertIn("pendingFields", js)
        self.assertIn(".progress > i", js)


if __name__ == "__main__":
    unittest.main()
