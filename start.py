from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from runtime_security import validate_runtime_security  # noqa: E402
from server import VERSION, create_server  # noqa: E402


def main() -> None:
    host = os.environ.get("STUDIO_HOST", "127.0.0.1")
    # P1-24: 端口解析失败时回退到默认值 5000，避免启动崩溃
    try:
        port = int(os.environ.get("STUDIO_PORT", "5000"))
    except (TypeError, ValueError):
        port = 5000
    validate_runtime_security(host)
    server = create_server(host, port)
    url_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{url_host}:{port}/"
    if os.environ.get("STUDIO_NO_BROWSER", "") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"公众号 AI Studio {VERSION} 已启动：{url}")
    print("按 Ctrl+C 停止服务。")
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
