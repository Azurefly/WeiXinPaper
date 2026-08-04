from __future__ import annotations

from contextlib import closing
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from source_fetcher import SourceFetchError, extract_snapshot, validate_url  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Client:
    def __init__(self, base: str):
        self.base = base
        self.cookie: str | None = None

    def request(self, path: str, method: str = "GET", body=None, headers=None, timeout=10):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": self.base,
            "Host": urllib.parse.urlsplit(self.base).hostname + ":"
                + str(urllib.parse.urlsplit(self.base).port),
            **(headers or {}),
        }
        if self.cookie:
            request_headers["Cookie"] = self.cookie
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers=request_headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
                # 捕获 Set-Cookie
                set_cookie = response.headers.get("Set-Cookie")
                if set_cookie:
                    # 提取 cookie 名值对（分号前的部分）
                    self.cookie = set_cookie.split(";")[0]
                value = json.loads(raw) if "json" in content_type else raw.decode("utf-8")
                return response.status, value, response.headers
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            value = json.loads(raw) if "json" in exc.headers.get("Content-Type", "") else raw.decode("utf-8")
            return exc.code, value, exc.headers

    def wait_task(self, task_id: str, timeout: float = 8.0):
        deadline = time.monotonic() + timeout
        current = None
        while time.monotonic() < deadline:
            status, current, _ = self.request(f"/api/v2/tasks/{task_id}")
            assert status == 200
            if current["status"] not in {"queued", "running"}:
                return current
            time.sleep(0.05)
        raise AssertionError(f"task did not finish: {current}")


@contextmanager
def running_server(*, timeout_seconds: int = 1200, delay: float = 0.0, extra_env: dict[str, str] | None = None):
    temp = tempfile.TemporaryDirectory()
    port = free_port()
    env = os.environ.copy()
    env.update(
        {
            "STUDIO_DB": str(Path(temp.name) / "studio.db"),
            "STUDIO_MASTER_KEY_FILE": str(Path(temp.name) / "master.key"),
            "STUDIO_TEST_AI": "1",
            "STUDIO_ENABLE_TEST_ADAPTERS": "1",
            "STUDIO_TEST_WECHAT": "1",
            "STUDIO_WORKFLOW_TIMEOUT": str(timeout_seconds),
            "STUDIO_TEST_AI_DELAY": str(delay),
            "PYTHONUNBUFFERED": "1",
        }
    )
    if extra_env:
        env.update(extra_env)
    process = subprocess.Popen(
        [sys.executable, "server.py", "--host", "127.0.0.1", "--port", str(port)],
        cwd=BACKEND,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    client = Client(base)
    try:
        for _ in range(100):
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                raise RuntimeError(f"server exited early: {output}")
            try:
                status, _, _ = client.request("/api/v2/health")
                if status == 200:
                    break
            except Exception:
                pass
            time.sleep(0.05)
        else:
            raise RuntimeError("server did not start")

        # 认证：读取初始密码 → 登录 → 修改密码（移除 must_change 标志）
        password_file = Path(temp.name) / ".initial_password"
        if password_file.exists():
            initial_password = password_file.read_text(encoding="utf-8").strip()
            if initial_password:
                status, login_data, _ = client.request(
                    "/api/v2/auth/login",
                    method="POST",
                    body={"username": "admin", "password": initial_password},
                )
                if status == 200 and login_data.get("ok"):
                    # 修改密码以移除 must_change_password 标志
                    new_password = "TestPass123!"
                    client.request(
                        "/api/v2/auth/change-password",
                        method="POST",
                        body={
                            "oldPassword": initial_password,
                            "newPassword": new_password,
                            "confirmPassword": new_password,
                        },
                    )

        yield client, Path(temp.name) / "studio.db", base
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
        if process.stdout:
            process.stdout.close()
        temp.cleanup()


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = running_server()
        cls.client, cls.db_path, cls.base = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def create_article(self, auto_review=True):
        status, result, _ = self.client.request(
            "/api/v2/workflows",
            "POST",
            {"sourceInput": "写一篇关于统一工作流与公众号创作效率的文章", "autoReview": auto_review},
        )
        self.assertEqual(status, 202)
        task = self.client.wait_task(result["task"]["id"])
        self.assertEqual(task["status"], "succeeded", task)
        status, project, _ = self.client.request(f"/api/v2/projects/{result['project']['id']}")
        self.assertEqual(status, 200)
        return project, task

    def test_01_static_and_bootstrap_are_real(self):
        status, html, headers = self.client.request("/")
        self.assertEqual(status, 200)
        self.assertIn("公众号 AI Studio", html)
        self.assertIn("default-src 'self'", headers.get("Content-Security-Policy", ""))
        status, app_js, _ = self.client.request("/assets/app.js")
        self.assertEqual(status, 200)
        self.assertIn("唯一创作入口", app_js)
        self.assertNotIn("mockApi", app_js)
        status, data, _ = self.client.request("/api/v2/bootstrap")
        self.assertEqual(data["projects"], [])
        self.assertEqual(data["tasks"], [])
        self.assertEqual(data["version"], "2.1.3")

    def test_02_legacy_creation_is_blocked(self):
        for path in ["/api/v2/projects", "/api/v2/generation/jobs"]:
            status, data, _ = self.client.request(path, "POST", {})
            self.assertEqual(status, 410)
            self.assertEqual(data["error"]["code"], "workflow_required")

    def test_03_workflow_respects_auto_review(self):
        project, task = self.create_article(auto_review=False)
        self.assertGreaterEqual(len(project["bodyMarkdown"]), 200)
        self.assertEqual(project["review"], [])
        self.assertEqual(task["progress"], 100)
        messages = [event["message"] for event in task["events"]]
        self.assertTrue(any("跳过自动审校" in message for message in messages))
        reviewed, _ = self.create_article(auto_review=True)
        self.assertGreaterEqual(len(reviewed["review"]), 3)

    def test_04_revision_conflict_preserves_both_versions(self):
        project, _ = self.create_article()
        status, updated, _ = self.client.request(
            f"/api/v2/projects/{project['id']}",
            "PATCH",
            {"bodyMarkdown": project["bodyMarkdown"] + "\n\n人工补充。"},
            {"If-Match": str(project["revision"])},
        )
        self.assertEqual(status, 200)
        self.assertGreater(updated["revision"], project["revision"])
        status, conflict, _ = self.client.request(
            f"/api/v2/projects/{project['id']}",
            "PATCH",
            {"summary": "过期更新"},
            {"If-Match": str(project["revision"])},
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "revision_conflict")
        self.assertEqual(conflict["error"]["detail"]["server"]["bodyMarkdown"], updated["bodyMarkdown"])

    def test_05_manual_review_and_publish_gate(self):
        project, _ = self.create_article()
        status, preview, _ = self.client.request(f"/api/v2/projects/{project['id']}/preview")
        self.assertEqual(status, 200)
        status, problem, _ = self.client.request(
            f"/api/v2/projects/{project['id']}/publish",
            "POST",
            {"revision": project["revision"], "bodyFingerprint": preview["bodyFingerprint"], "previewHash": preview["previewHash"]},
        )
        self.assertEqual(status, 409)
        self.assertEqual(problem["error"]["code"], "review_required")
        status, saved, _ = self.client.request(
            "/api/v2/settings/wechat/verify-and-save",
            "POST",
            {"accountName": "测试公众号", "appId": "good", "appSecret": "secret", "thumbMediaId": "thumb"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(saved["appSecretSet"])
        status, reviewed, _ = self.client.request(
            f"/api/v2/projects/{project['id']}/review",
            "POST",
            {"approved": True, "revision": project["revision"], "bodyFingerprint": preview["bodyFingerprint"]},
        )
        self.assertEqual(status, 200)
        self.assertTrue(reviewed["reviewApproved"])
        status, preview, _ = self.client.request(f"/api/v2/projects/{project['id']}/preview")
        status, published, _ = self.client.request(
            f"/api/v2/projects/{project['id']}/publish",
            "POST",
            {"revision": reviewed["revision"], "bodyFingerprint": preview["bodyFingerprint"], "previewHash": preview["previewHash"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(published["status"], "current")
        self.assertTrue(published["remoteId"].startswith("media_test_"))

    def test_06_wechat_credentials_are_transactional(self):
        status, _, _ = self.client.request(
            "/api/v2/settings/wechat/verify-and-save",
            "POST",
            {"accountName": "稳定账号", "appId": "good", "appSecret": "stable", "thumbMediaId": "thumb"},
        )
        self.assertEqual(status, 200)
        status, failed, _ = self.client.request(
            "/api/v2/settings/wechat/verify-and-save",
            "POST",
            {"accountName": "错误账号", "appId": "bad", "appSecret": "wrong"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(failed["error"]["code"], "wechat_verify_failed")
        with closing(sqlite3.connect(self.db_path)) as conn:
            raw = conn.execute("SELECT value_json FROM settings WHERE key='wechat'").fetchone()[0]
        stored = json.loads(raw)
        self.assertEqual(stored["appId"], "good")
        self.assertTrue(stored["appSecret"].startswith("enc:v1:"))
        self.assertNotIn("stable", raw)

    def test_07_article_lifecycle(self):
        project, _ = self.create_article()
        status, copied, _ = self.client.request(f"/api/v2/projects/{project['id']}/copy", "POST", {})
        self.assertEqual(status, 201)
        self.assertNotEqual(copied["id"], project["id"])
        self.assertTrue(copied["title"].endswith("（副本）"))
        status, archived, _ = self.client.request(f"/api/v2/projects/{project['id']}/archive", "POST", {})
        self.assertTrue(archived["archived"])
        status, restored, _ = self.client.request(f"/api/v2/projects/{project['id']}/restore", "POST", {})
        self.assertFalse(restored["archived"])
        status, exported, headers = self.client.request(f"/api/v2/projects/{project['id']}/export")
        self.assertEqual(status, 200)
        self.assertIn("# 测试文章", exported)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        status, _, _ = self.client.request(f"/api/v2/projects/{copied['id']}", "DELETE", {})
        self.assertEqual(status, 200)
        status, deleted, _ = self.client.request(f"/api/v2/projects/{copied['id']}")
        self.assertEqual(status, 200)
        self.assertTrue(deleted["deleted"])
        status, listing, _ = self.client.request("/api/v2/projects?includeDeleted=true")
        self.assertTrue(any(item["id"] == copied["id"] for item in listing["items"]))
        status, purged, _ = self.client.request(f"/api/v2/projects/{copied['id']}/purge", "DELETE", {})
        self.assertEqual(status, 200)
        self.assertTrue(purged["purged"])

    def test_08_ai_settings_persist_and_mask_secret(self):
        status, settings, _ = self.client.request(
            "/api/v2/settings",
            "PATCH",
            {"ai": {"providerId": "openai-compatible", "baseUrl": "https://example.invalid/v1", "apiKey": "sk-secret", "model": "model-x", "temperature": 0.2, "autoReview": False}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(settings["ai"]["model"], "model-x")
        self.assertTrue(settings["ai"]["apiKeySet"])
        self.assertEqual(settings["ai"]["apiKey"], "")
        status, verify, _ = self.client.request("/api/v2/settings/ai/verify", "POST", {"apiKey": ""})
        self.assertEqual(status, 200)
        self.assertTrue(verify["ok"])

    def test_09_source_security_and_extraction(self):
        for value in ["http://127.0.0.1/", "http://localhost/", "file:///etc/passwd", "http://user:pass@example.com/"]:
            with self.assertRaises(SourceFetchError):
                validate_url(value)
        html = b"""<!doctype html><html><head><title>Example Article</title><meta name='author' content='Alice'></head><body><main><h1>Example Article</h1><p>This is a sufficiently long article paragraph used to validate extraction behavior and content hashing.</p><p>Another paragraph makes the extracted body long enough for a reliable snapshot.</p></main></body></html>"""
        snapshot = extract_snapshot("https://example.com/a", "https://example.com/a", html, "text/html; charset=utf-8")
        self.assertEqual(snapshot.title, "Example Article")
        self.assertEqual(snapshot.extraction_method, "article_main")
        self.assertEqual(len(snapshot.content_hash), 64)

    def test_10_security_headers_and_origin_guard(self):
        status, problem, _ = self.client.request(
            "/api/v2/workflows",
            "POST",
            {"sourceInput": "test"},
            {"Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(problem["error"]["code"], "origin_forbidden")

    def test_11_cancel_is_server_side(self):
        with running_server(delay=0.35) as (client, _, _):
            status, result, _ = client.request("/api/v2/workflows", "POST", {"sourceInput": "取消测试", "autoReview": True})
            self.assertEqual(status, 202)
            task_id = result["task"]["id"]
            status, _, _ = client.request(f"/api/v2/tasks/{task_id}/cancel", "POST", {})
            self.assertEqual(status, 200)
            task = client.wait_task(task_id)
            self.assertEqual(task["status"], "cancelled")

    def test_12_timeout_is_server_side(self):
        with running_server(timeout_seconds=0, delay=0.05) as (client, _, _):
            status, result, _ = client.request("/api/v2/workflows", "POST", {"sourceInput": "超时测试", "autoReview": True})
            self.assertEqual(status, 202)
            task = client.wait_task(result["task"]["id"])
            self.assertEqual(task["status"], "timeout")
            self.assertEqual(task["errorCode"], "workflow_timeout")

    def test_13_remote_start_requires_auth(self):
        env = os.environ.copy()
        env.pop("STUDIO_ALLOW_REMOTE", None)
        process = subprocess.run(
            [sys.executable, "server.py", "--host", "0.0.0.0", "--port", str(free_port())],
            cwd=BACKEND,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("远程监听被拒绝", process.stdout + process.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
