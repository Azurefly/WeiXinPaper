from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Client:
    def __init__(self, base: str):
        self.base = base
        self.csrf_token = ""
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def request(self, path: str, method: str = "GET", body: dict | None = None):
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json", "Content-Type": "application/json", "Origin": self.base}
        if self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        request = urllib.request.Request(self.base + path, data=payload, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=10) as response:
                raw = response.read()
                value = json.loads(raw) if raw else {}
                if isinstance(value, dict) and value.get("csrfToken"):
                    self.csrf_token = str(value["csrfToken"])
                return response.status, value
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, json.loads(raw) if raw else {}


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        port = free_port()
        env = os.environ.copy()
        env.update({"STUDIO_DB": str(Path(temp) / "runtime.db"), "STUDIO_MASTER_KEY_FILE": str(Path(temp) / "master.key"), "STUDIO_NO_BROWSER": "1"})
        for name in ("STUDIO_TEST_AI", "STUDIO_TEST_WECHAT", "STUDIO_ENABLE_TEST_ADAPTERS"):
            env.pop(name, None)
        process = subprocess.Popen(
            [sys.executable, "start.py"],
            cwd=ROOT,
            env={**env, "STUDIO_PORT": str(port)},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    with urllib.request.urlopen(base + "/api/v2/health", timeout=1) as response:
                        health = json.loads(response.read())
                    break
                except Exception:
                    if process.poll() is not None:
                        raise RuntimeError(process.stdout.read() if process.stdout else "runtime exited")
                    time.sleep(0.05)
            else:
                raise RuntimeError("runtime did not start")
            assert health["ok"] and health["version"] == "2.1.3"
            with urllib.request.urlopen(base + "/") as response:
                html = response.read().decode("utf-8")
            assert "公众号 AI Studio" in html
            client = Client(base)
            setup_password = "RuntimeStudio9A"
            status, setup = client.request(
                "/api/v2/auth/setup",
                "POST",
                {
                    "username": "admin",
                    "password": setup_password,
                    "confirmPassword": setup_password,
                },
            )
            assert status == 201 and setup["ok"] and not setup["mustChangePassword"], setup
            status, created = client.request(
                "/api/v2/workflows",
                "POST",
                {"sourceInput": "运行包验收", "autoReview": False},
            )
            assert status == 202, created
            task_id = created["task"]["id"]
            for _ in range(100):
                status, task = client.request("/api/v2/tasks/" + task_id)
                assert status == 200, task
                if task["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.05)
            assert task["status"] == "blocked", task
            assert task["errorCode"] == "ai_not_configured", task
            print("runtime_http_check: OK")
            print("runtime_no_fake_ai_check: OK")
            print("运行版验收通过。")
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
            if process.stdout:
                process.stdout.close()


if __name__ == "__main__":
    main()
