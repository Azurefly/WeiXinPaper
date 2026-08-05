from __future__ import annotations

from contextlib import closing

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
VERSION = "2.1.3"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_server(base: str, process: subprocess.Popen[str]) -> None:
    for _ in range(160):
        if process.poll() is not None:
            raise RuntimeError(process.stdout.read() if process.stdout else "server exited")
        try:
            with urllib.request.urlopen(base + "/api/v2/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.05)
    raise RuntimeError("server did not start")


def browser_path() -> str | None:
    configured = os.environ.get("STUDIO_BROWSER_PATH", "").strip()
    candidates = [
        configured,
        shutil.which("chromium") or "",
        shutil.which("chromium-browser") or "",
        shutil.which("google-chrome") or "",
        shutil.which("microsoft-edge") or "",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    ]
    for value in candidates:
        if value and Path(value).is_file():
            return str(Path(value))
    return None


def seed_database(path: Path) -> None:
    env = os.environ.copy()
    env["STUDIO_DB"] = str(path)
    env["STUDIO_MASTER_KEY_FILE"] = str(path.with_name("browser.master.key"))
    subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0,'backend'); from db import init_db; init_db()"],
        cwd=ROOT,
        env=env,
        check=True,
        timeout=60,
    )
    now = utc_now()
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            INSERT INTO projects(
                id,title,goal,source_input,source_kind,status,archived,deleted,outline_json,body_markdown,
                summary,cover_data_url,review_json,review_fingerprint,review_approved,publish_status,publish_remote_id,
                revision,created_at,updated_at
            ) VALUES(?,?,?,?,?,'draft',0,0,?,?,?,?,?, '',0,'not_synced','',1,?,?)
            """,
            (
                "browser_e2e_project",
                "真实服务浏览器验收文章",
                "验证浏览器、HTTP 服务、自动保存、预览和版本历史",
                "浏览器验收主题",
                "topic",
                json.dumps(["验收目标", "保存一致性", "发布预览"], ensure_ascii=False),
                "# 浏览器验收\n\n这是由验证工具预置的正文，用于测试真实服务 UI。",
                "真实服务浏览器 E2E 验收摘要",
                "",
                "[]",
                now,
                now,
            ),
        )
        conn.commit()


def run_validation() -> dict[str, Any]:
    try:
        from playwright.sync_api import expect, sync_playwright
    except ImportError:
        return {
            "product": "公众号 AI Studio",
            "version": VERSION,
            "generatedAt": utc_now(),
            "status": "skipped",
            "reason": "playwright_not_installed",
            "command": "python -m pip install playwright",
        }
    executable = browser_path()
    if not executable:
        return {
            "product": "公众号 AI Studio",
            "version": VERSION,
            "generatedAt": utc_now(),
            "status": "skipped",
            "reason": "chrome_edge_or_chromium_not_found",
        }
    with tempfile.TemporaryDirectory(prefix="studio-browser-e2e-") as temp:
        temp_root = Path(temp)
        db_path = temp_root / "browser.db"
        seed_database(db_path)
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        env = {key: value for key, value in os.environ.items() if not key.startswith("STUDIO_")}
        env.update(
            {
                "STUDIO_DB": str(db_path),
                "STUDIO_MASTER_KEY_FILE": str(temp_root / "master.key"),
                "PYTHONUNBUFFERED": "1",
            }
        )
        process = subprocess.Popen(
            [sys.executable, "server.py", "--host", "127.0.0.1", "--port", str(port)],
            cwd=BACKEND,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            wait_server(base, process)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=os.environ.get("STUDIO_BROWSER_HEADLESS", "1") != "0",
                    executable_path=executable,
                    args=["--no-sandbox", "--no-first-run", "--disable-extensions"],
                )
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                page = context.new_page()
                page.set_default_timeout(15_000)
                page_errors: list[str] = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                try:
                    page.goto(base + "/#/articles", wait_until="domcontentloaded", timeout=25_000)
                except Exception as exc:
                    message = str(exc)
                    status = "blocked" if "ERR_BLOCKED_BY_ADMINISTRATOR" in message else "failed"
                    return {
                        "product": "公众号 AI Studio",
                        "version": VERSION,
                        "generatedAt": utc_now(),
                        "status": status,
                        "reason": "browser_enterprise_policy" if status == "blocked" else "navigation_failed",
                        "browser": executable,
                        "message": message[:800],
                    }
                expect(page).to_have_title("公众号 AI Studio")
                setup_password = "StudioBrowser9A"
                expect(page.locator("#admin-setup-form")).to_be_visible()
                page.locator("#setup-password").fill(setup_password)
                page.locator("#setup-confirm").fill(setup_password)
                page.locator("#admin-setup-form").press("Enter")
                expect(page.locator("nav.nav button")).to_have_count(5)
                page.evaluate("location.hash = '#/articles'")
                expect(page.get_by_text("真实服务浏览器验收文章", exact=True)).to_be_visible()
                expect(page.get_by_text("共 1 篇", exact=True)).to_be_visible()
                page.get_by_role("button", name="打开", exact=True).click()
                page.wait_for_url("**#/workspace?**")
                body = page.locator("#project-body")
                expect(body).to_be_visible()
                body.fill(body.input_value() + "\n\n真实浏览器连续编辑已完成。")
                expect(page.locator("#save-state")).to_have_text("已保存", timeout=12_000)
                page.locator("#refresh-preview").click()
                expect(page.locator("#publish-preview")).to_contain_text("真实浏览器连续编辑已完成")
                page.locator("#review-approved").check()
                expect(page.get_by_text("当前 revision 已完成人工终审", exact=True)).to_be_visible()
                page.locator("#show-versions").click()
                expect(page.get_by_text("版本历史", exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="恢复此版本").first).to_be_visible()
                page.locator("#close-versions").click()
                if page_errors:
                    raise RuntimeError("page errors: " + " | ".join(page_errors))
                context.close()
                browser.close()
            return {
                "product": "公众号 AI Studio",
                "version": VERSION,
                "generatedAt": utc_now(),
                "status": "succeeded",
                "browser": executable,
                "baseUrl": "http://127.0.0.1:<ephemeral-port>",
                "checks": {
                    "realHttpNavigation": True,
                    "articlePaginationVisible": True,
                    "workspaceOpened": True,
                    "autosaveCompleted": True,
                    "serverPreviewUpdated": True,
                    "revisionReviewCompleted": True,
                    "versionHistoryVisible": True,
                    "pageErrors": 0,
                },
            }
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()


def main() -> None:
    try:
        result = run_validation()
    except Exception as exc:  # noqa: BLE001
        result = {
            "product": "公众号 AI Studio",
            "version": VERSION,
            "generatedAt": utc_now(),
            "status": "failed",
            "code": exc.__class__.__name__,
            "message": str(exc)[:800],
        }
    output = json.dumps(result, ensure_ascii=False, indent=2)
    path = os.environ.get("STUDIO_BROWSER_RESULT_FILE", "").strip()
    if path:
        Path(path).write_text(output + "\n", encoding="utf-8")
    print(output)
    if result["status"] == "failed" or (
        result["status"] in {"blocked", "skipped"} and os.environ.get("STUDIO_BROWSER_REQUIRE_PASS", "") == "1"
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
