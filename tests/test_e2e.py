from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
SCREENSHOTS = Path(os.environ.get("STUDIO_SCREENSHOT_DIR") or (Path(tempfile.gettempdir()) / "weixin-studio-e2e"))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_server(base: str, process: subprocess.Popen[str]) -> None:
    for _ in range(120):
        if process.poll() is not None:
            raise RuntimeError(process.stdout.read() if process.stdout else "server exited")
        try:
            with urllib.request.urlopen(base + "/api/v2/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("server did not start")


def main() -> None:
    try:
        from playwright.sync_api import expect, sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "未安装 Playwright；请先执行 `python -m pip install playwright` "
            "和 `python -m playwright install chromium`。"
        ) from exc

    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        env = os.environ.copy()
        env.update(
            {
                "STUDIO_DB": str(Path(temp) / "e2e.db"),
                "STUDIO_MASTER_KEY_FILE": str(Path(temp) / "master.key"),
                "STUDIO_TEST_AI": "1",
                "STUDIO_TEST_WECHAT": "1",
                "STUDIO_ENABLE_TEST_ADAPTERS": "1",
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
            with sync_playwright() as p:
                launch_options: dict[str, object] = {"headless": True}
                if os.environ.get("CHROMIUM_PATH"):
                    launch_options["executable_path"] = os.environ["CHROMIUM_PATH"]
                browser = p.chromium.launch(**launch_options)
                context = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
                page = context.new_page()
                page.set_default_timeout(12_000)
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                page.goto(base + "/", wait_until="domcontentloaded", timeout=20_000)
                expect(page).to_have_title("公众号 AI Studio")
                initial_password = (Path(temp) / ".initial_password").read_text(encoding="utf-8").strip()
                page.locator("#login-password").fill(initial_password)
                page.locator("#login-form").press("Enter")
                expect(page.locator("#change-password-form")).to_be_visible()
                new_password = "StudioE2e9A"
                page.locator("#cp-old").fill(initial_password)
                page.locator("#cp-new").fill(new_password)
                page.locator("#cp-confirm").fill(new_password)
                page.locator("#change-password-form").press("Enter")
                expect(page.get_by_text("唯一创作入口", exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="开始创作 →", exact=True)).to_have_count(1)
                expect(page.locator("nav.nav button")).to_have_count(5)
                page.screenshot(path=str(SCREENSHOTS / "create-desktop.png"), full_page=True)

                page.locator("#source-input").fill("写一篇关于统一工作流和公众号创作效率的文章")
                page.get_by_role("button", name="开始创作 →", exact=True).click()
                page.wait_for_url("**#/workspace?**")
                body = page.locator("#project-body")
                expect(body).to_be_visible()
                page.wait_for_function("() => document.querySelector('#project-body')?.value.length >= 200", timeout=15_000)

                # 连续编辑必须进入串行保存队列，最终内容不能被轮询覆盖。
                original = body.input_value()
                for suffix in ["\n\n浏览器端人工补充一。", "\n浏览器端人工补充二。", "\n浏览器端人工补充三。"]:
                    body.fill(body.input_value() + suffix)
                    page.wait_for_timeout(100)
                expect(page.locator("#save-state")).to_have_text("已保存", timeout=10_000)
                assert body.input_value().startswith(original)
                assert body.input_value().endswith("浏览器端人工补充三。")

                # 预览来自服务端统一渲染器，人工终审绑定当前 revision。
                page.locator("#refresh-preview").click()
                expect(page.locator("#publish-preview")).to_contain_text("浏览器端人工补充三", timeout=5_000)
                page.locator("#review-approved").check()
                expect(page.get_by_text("当前 revision 已完成人工终审", exact=True)).to_be_visible(timeout=5_000)
                expect(page.locator("#publish-button")).to_be_enabled()
                page.screenshot(path=str(SCREENSHOTS / "workspace-desktop.png"), full_page=True)

                # 版本历史必须可见且可恢复，不再是后端隐藏能力。
                page.locator("#show-versions").click()
                expect(page.get_by_text("版本历史", exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="恢复此版本").first).to_be_visible()
                page.locator("#close-versions").click()

                # AI 页面无第二创作入口，并区分配置/连接/最近验证状态。
                page.evaluate("location.hash = '#/ai'")
                expect(page.locator(".page-head h2", has_text="AI 能力")).to_be_visible()
                expect(page.get_by_role("button", name="开始创作 →", exact=True)).to_have_count(0)
                expect(page.get_by_text("配置、可连接、最近验证成功是三个独立状态。", exact=True)).to_be_visible()

                # 备份恢复属于系统设置，不应混在 AI 供应商页面。
                expect(page.locator("#data-export-btn")).to_have_count(0)
                page.evaluate("location.hash = '#/settings'")
                expect(page.locator("#data-export-btn")).to_be_visible()

                # 文章中心和任务诊断真实可用。
                page.evaluate("location.hash = '#/articles'")
                expect(page.locator(".page-head h2", has_text="文章中心")).to_be_visible()
                expect(page.get_by_role("button", name="打开", exact=True)).to_have_count(1)
                page.get_by_role("button", name="打开", exact=True).click()
                page.locator("#open-task").click()
                expect(page.locator(".page-head h2", has_text="任务诊断")).to_be_visible()
                expect(page.get_by_text("统一工作流已完成", exact=True)).to_be_visible()

                # 移动端复用已登录 context，验证真实响应式页面。
                mobile = context.new_page()
                mobile.set_viewport_size({"width": 390, "height": 844})
                mobile.goto(base + "/#/create", wait_until="domcontentloaded", timeout=20_000)
                expect(mobile.get_by_role("button", name="开始创作 →", exact=True)).to_have_count(1)
                mobile.screenshot(path=str(SCREENSHOTS / "create-mobile.png"), full_page=True)
                mobile.close()
                context.close()
                browser.close()
                if errors:
                    raise AssertionError("browser page errors: " + " | ".join(errors))
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            if process.stdout:
                process.stdout.close()

    print("browser_e2e: OK")


if __name__ == "__main__":
    main()
