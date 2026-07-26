from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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
            request = urllib.request.Request(
                base + "/api/v2/workflows",
                data=json.dumps({"sourceInput": "运行包验收", "autoReview": False}, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request) as response:
                created = json.loads(response.read())
            task_id = created["task"]["id"]
            for _ in range(100):
                with urllib.request.urlopen(base + "/api/v2/tasks/" + task_id) as response:
                    task = json.loads(response.read())
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
