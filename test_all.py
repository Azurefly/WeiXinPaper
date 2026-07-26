from __future__ import annotations

import compileall
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(command: list[str]) -> None:
    print(">", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> None:
    if not compileall.compile_dir(ROOT / "backend", quiet=1):
        raise SystemExit("Python 语法检查失败")
    run([sys.executable, "-m", "unittest", "-v", "tests.test_release", "tests.test_security_211", "tests.test_audit_212", "tests.test_audit_213"])
    node = shutil.which("node")
    if node:
        run([node, "--check", "web/app.js"])
    else:
        print("未安装 Node.js：浏览器原生 JavaScript 不依赖 Node，跳过 node --check。")
    if os.environ.get("RUN_BROWSER_E2E") == "1":
        run([sys.executable, "tests/test_e2e.py"])
        print("核心自动化与真实服务浏览器 E2E 均通过。")
    else:
        print("核心自动化通过；真实服务浏览器 E2E 未执行。")


if __name__ == "__main__":
    main()
