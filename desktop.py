#!/usr/bin/env python3
"""公众号 AI Studio — 桌面端启动器

基于 PyWebView 将 Web 应用封装为原生桌面窗口，提供企业级生命周期管理：

- 单实例锁定（防止重复启动）
- 数据/日志隔离到 OS 标准目录（首次运行自动迁移现有数据）
- 后端服务线程化运行 + 就绪健康检查
- 启动加载页 + 就绪后自动导航
- 窗口尺寸持久化（周期性保存）
- 优雅关闭（服务器线程安全停止 + 超时保护）
- 信号处理（SIGINT / SIGTERM）
- 原生错误对话框
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------

# PyInstaller 打包后，资源在 sys._MEIPASS 临时目录中
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    ROOT = Path(sys._MEIPASS)
    BACKEND = ROOT / "backend"
else:
    ROOT = Path(__file__).resolve().parent
    BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

APP_NAME = "公众号AIStudio"
APP_TITLE = "公众号 AI Studio"
SINGLE_INSTANCE_PORT = 50199
SERVER_START_PORT = 5001  # 项目约定端口，避开 macOS AirPlay
SERVER_START_TIMEOUT = 20.0  # 服务器就绪等待超时（秒）
SERVER_SHUTDOWN_TIMEOUT = 5.0  # 服务器关闭超时（秒）

LOADING_HTML = """\
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;
       background:linear-gradient(135deg,#f2f6f7,#eef0fb)}
  .wrap{text-align:center}
  .logo{width:64px;height:64px;border-radius:18px;background:linear-gradient(135deg,#0f6368,#084d52);
        display:flex;align-items:center;justify-content:center;margin:0 auto 20px;
        font-size:32px;color:#fff;box-shadow:0 8px 24px rgba(15,99,104,.22)}
  h1{font-size:22px;color:#173438;margin-bottom:8px}
  p{color:#576b6e;font-size:14px;line-height:1.7}
  .dots{display:inline-flex;gap:6px;margin-top:16px}
  .dots i{width:8px;height:8px;border-radius:50%;background:#0f6368;opacity:.3;
          animation:bounce 1.2s ease-in-out infinite}
  .dots i:nth-child(2){animation-delay:.15s}
  .dots i:nth-child(3){animation-delay:.3s}
  @keyframes bounce{0%,80%,100%{opacity:.3;transform:scale(.8)}40%{opacity:1;transform:scale(1.2)}}
</style></head><body>
<div class="wrap">
  <div class="logo">公</div>
  <h1>公众号 AI Studio</h1>
  <p>正在启动服务…</p>
  <div class="dots"><i></i><i></i><i></i></div>
</div>
</body></html>
"""

ERROR_HTML_TEMPLATE = """\
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;background:#f2f6f7}
  .card{max-width:480px;padding:36px;background:#fff;border-radius:18px;
        box-shadow:0 8px 28px rgba(26,68,72,.08);text-align:center}
  .icon{font-size:42px;margin-bottom:16px}
  h1{font-size:20px;color:#b03a42;margin-bottom:10px}
  p{color:#576b6e;font-size:14px;line-height:1.7;white-space:pre-wrap}
</style></head><body>
<div class="card"><div class="icon">⚠</div><h1>启动失败</h1><p>{message}</p></div>
</body></html>
"""


# ---------------------------------------------------------------------------
# OS 标准路径
# ---------------------------------------------------------------------------

def app_data_dir() -> Path:
    """返回 OS 标准应用数据目录。"""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / APP_NAME


def app_log_dir() -> Path:
    """返回 OS 标准日志目录。"""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / APP_NAME
    elif sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME / "logs"
    else:
        return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP_NAME


# ---------------------------------------------------------------------------
# 环境配置与数据迁移
# ---------------------------------------------------------------------------

def _is_writable(path: Path) -> bool:
    """检查目录是否可写入。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        test_file = path / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        return True
    except Exception:  # noqa: BLE001
        return False


def _portable_data_dir() -> Path:
    """获取便携模式数据目录（可执行文件旁的 data/ 文件夹）。

    macOS .app bundle 内部只读，数据目录需放在 .app 外部。
    Windows 直接放在 .exe 旁。
    """
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        # macOS .app bundle: exe = .../App.app/Contents/MacOS/exe
        # 需要回到 .app 所在目录
        if sys.platform == "darwin" and exe.parent.name == "MacOS":
            return exe.parent.parent.parent.parent / "data"
        # Windows 或非 bundle 模式：可执行文件旁
        return exe.parent / "data"
    return ROOT / "data"


def configure_environment() -> Path:
    """配置数据/日志目录环境变量，返回数据目录。

    优先使用 OS 标准目录（~/Library/Application Support），
    若不可写则回退到可执行文件旁的 data/ 文件夹（便携模式）。
    首次运行自动迁移现有数据。
    """
    os_data = app_data_dir()
    os_log = app_log_dir()

    # 优先 OS 标准目录，不可写时回退到便携模式
    if _is_writable(os_data):
        data_dir = os_data
        log_dir = os_log if _is_writable(os_log) else _portable_data_dir()
    else:
        data_dir = _portable_data_dir()
        log_dir = _portable_data_dir()
        print(f"[配置] OS 数据目录不可写，回退到便携模式: {data_dir}")

    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    # 数据库路径（仅在未手动指定时设置）
    os.environ.setdefault("STUDIO_DB", str(data_dir / "studio.db"))
    # 日志文件路径
    os.environ.setdefault("STUDIO_LOG_FILE", str(log_dir / "studio.log"))
    # 标记为桌面端模式（不自动打开浏览器）
    os.environ["STUDIO_NO_BROWSER"] = "1"

    # 首次运行数据迁移：项目目录 → 数据目录
    _migrate_existing_data(data_dir)

    return data_dir


def _migrate_existing_data(data_dir: Path) -> None:
    """首次运行时将旧数据库迁移到数据目录。"""
    new_db = data_dir / "studio.db"
    if new_db.exists():
        return  # 新目录已有数据，不迁移

    # 查找旧数据库：源码目录或可执行文件旁
    candidates = []
    if not getattr(sys, "frozen", False):
        candidates.append(ROOT / "data" / "studio.db")
    else:
        exe = Path(sys.executable).resolve()
        if sys.platform == "darwin" and exe.parent.name == "MacOS":
            # .app bundle 旁
            candidates.append(exe.parent.parent.parent.parent / "data" / "studio.db")
        candidates.append(exe.parent / "data" / "studio.db")
        candidates.append(exe.parent.parent / "data" / "studio.db")

    old_db = None
    for c in candidates:
        if c.exists():
            old_db = c
            break
    if not old_db:
        return  # 旧目录没有数据，无需迁移

    try:
        shutil.copy2(old_db, new_db)
        for suffix in ("-wal", "-shm"):
            old_side = old_db.parent / (old_db.name + suffix)
            if old_side.exists():
                shutil.copy2(old_side, new_db.parent / (new_db.name + suffix))
        # 同步迁移 master key（数据库与密钥必须成对）
        old_key = old_db.parent / ".master.key"
        if old_key.exists():
            shutil.copy2(old_key, new_db.parent / ".master.key")
            try:
                os.chmod(new_db.parent / ".master.key", 0o600)
            except Exception:  # noqa: BLE001
                pass
        print(f"[迁移] 数据库已从 {old_db} 复制到 {new_db}")
    except Exception as exc:  # noqa: BLE001
        print(f"[迁移] 数据迁移失败: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 单实例锁定
# ---------------------------------------------------------------------------

def acquire_single_instance_lock() -> socket.socket | None:
    """尝试获取单实例锁。已有实例运行时返回 None。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        sock.listen(1)
        return sock
    except OSError:
        sock.close()
        return None


# ---------------------------------------------------------------------------
# 窗口状态持久化
# ---------------------------------------------------------------------------

def _window_state_path(data_dir: Path) -> Path:
    return data_dir / "window_state.json"


def load_window_state(data_dir: Path) -> dict:
    path = _window_state_path(data_dir)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_window_state(data_dir: Path, state: dict) -> None:
    path = _window_state_path(data_dir)
    try:
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 端口查找
# ---------------------------------------------------------------------------

def find_available_port(start: int = SERVER_START_PORT, max_tries: int = 20) -> int:
    """从 start 开始查找可用端口。"""
    for offset in range(max_tries):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"无法找到可用端口（尝试 {start}~{start + max_tries - 1}）")


# ---------------------------------------------------------------------------
# 服务器就绪检查
# ---------------------------------------------------------------------------

def wait_for_server(url: str, timeout: float = SERVER_START_TIMEOUT) -> bool:
    """轮询健康检查端点，等待服务器就绪。"""
    deadline = time.monotonic() + timeout
    health_url = url.rstrip("/") + "/api/v2/health"
    while time.monotonic() < deadline:
        try:
            req = urllib.request.urlopen(health_url, timeout=2)
            if req.status == 200:
                return True
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    return False


# ---------------------------------------------------------------------------
# 原生对话框（无 tkinter 时降级到 stderr）
# ---------------------------------------------------------------------------

def _show_dialog(method: str, title: str, message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        getattr(messagebox, method)(title, message)
        root.destroy()
    except Exception:  # noqa: BLE001
        print(f"[{title}] {message}", file=sys.stderr)


def show_error(title: str, message: str) -> None:
    _show_dialog("showerror", title, message)


def show_warning(title: str, message: str) -> None:
    _show_dialog("showwarning", title, message)


# ---------------------------------------------------------------------------
# macOS Dock 集成
# ---------------------------------------------------------------------------

def _ensure_webkit_dirs() -> None:
    """确保 macOS WebKit 数据目录存在（减少启动警告）。"""
    if sys.platform != "darwin":
        return
    webkit_base = Path.home() / "Library" / "WebKit" / "com.studio.wechat-ai"
    try:
        webkit_base.mkdir(parents=True, exist_ok=True)
        for subdir in ["WebsiteData", "Caches", "Cookies"]:
            (webkit_base / subdir).mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass  # 沙箱环境下可能无法创建，不影响核心功能


def _configure_macos_dock() -> None:
    """在 macOS 上将 Python 进程设为常规应用（Dock 图标 + 菜单栏）。"""
    if sys.platform != "darwin":
        return
    try:
        import AppKit  # type: ignore[import-not-found]
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)

        # 设置自定义 Dock 图标（优先 .app 内嵌图标，回退到项目 build_assets）
        icon_path = _find_macos_icon()
        if icon_path:
            try:
                data = AppKit.NSData.dataWithContentsOfFile_(str(icon_path))
                if data:
                    image = AppKit.NSImage.alloc().initWithData_(data)
                    if image:
                        image.setSize_((128, 128))
                        app.setApplicationIconImage_(image)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass  # 非 macOS 或缺少 pyobjc 时静默跳过


def _find_macos_icon() -> Path | None:
    """查找可用的 .icns 图标文件。"""
    # 1. .app bundle 内嵌图标（PyInstaller 打包后）
    exe = Path(sys.executable).resolve()
    for parent in [exe, *exe.parents]:
        icns = parent / "Contents" / "Resources" / "AppIcon.icns"
        if icns.exists():
            return icns
    # 2. 打包资源目录（_MEIPASS）
    if hasattr(sys, "_MEIPASS"):
        meipass_icon = Path(sys._MEIPASS) / "build_assets" / "AppIcon.icns"
        if meipass_icon.exists():
            return meipass_icon
    # 3. 项目目录 build_assets
    project_icon = ROOT / "build_assets" / "AppIcon.icns"
    if project_icon.exists():
        return project_icon
    # 4. 回退到 PNG
    project_png = ROOT / "build_assets" / "AppIcon.png"
    if project_png.exists():
        return project_png
    return None


# ---------------------------------------------------------------------------
# 优雅关闭
# ---------------------------------------------------------------------------

def shutdown_server(server: object) -> None:
    """安全停止服务器：先 shutdown() 再 server_close()，带超时保护。"""
    try:
        shutdown_thread = threading.Thread(target=server.shutdown, daemon=True)  # type: ignore[attr-defined]
        shutdown_thread.start()
        shutdown_thread.join(timeout=SERVER_SHUTDOWN_TIMEOUT)
        if shutdown_thread.is_alive():
            print("[关闭] 服务器关闭超时，强制退出", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass
    try:
        server.server_close()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def main() -> None:
    debug = "--debug" in sys.argv

    # 1. 检查 pywebview 依赖
    try:
        import webview
    except ImportError:
        show_error(APP_TITLE, "缺少 pywebview 依赖。\n\n请运行：\npip install pywebview")
        sys.exit(1)

    # 2. 配置环境（数据/日志目录 + 迁移）
    data_dir = configure_environment()

    # 3. 单实例检查
    lock_sock = acquire_single_instance_lock()
    if lock_sock is None:
        show_warning(APP_TITLE, "程序已在运行中，请勿重复启动。")
        sys.exit(0)

    # 4. 查找可用端口
    try:
        port = find_available_port()
    except RuntimeError as exc:
        show_error(APP_TITLE, str(exc))
        sys.exit(1)

    host = "127.0.0.1"
    url = f"http://{host}:{port}/"
    os.environ["STUDIO_PORT"] = str(port)

    # 5. 启动后端服务器（守护线程）
    from runtime_security import validate_runtime_security
    from server import VERSION, create_server

    validate_runtime_security(host)

    try:
        server = create_server(host, port)
    except Exception as exc:  # noqa: BLE001
        show_error(APP_TITLE, f"服务器初始化失败：\n{exc}")
        sys.exit(1)

    server_error: list[str] = []

    def _serve() -> None:
        try:
            server.serve_forever(poll_interval=0.2)
        except Exception as exc:  # noqa: BLE001
            server_error.append(str(exc))

    server_thread = threading.Thread(target=_serve, daemon=True, name="StudioServer")
    server_thread.start()

    # 6. 加载窗口尺寸
    saved = load_window_state(data_dir)
    win_w = max(1024, min(int(saved.get("width", 1440)), 2560))
    win_h = max(680, min(int(saved.get("height", 900)), 1600))

    # 7. macOS Dock 集成 + WebKit 目录
    _ensure_webkit_dirs()
    _configure_macos_dock()

    # 8. 创建窗口（先显示加载页）
    window = webview.create_window(
        title=f"{APP_TITLE} {VERSION}",
        html=LOADING_HTML,
        width=win_w,
        height=win_h,
        min_size=(1024, 680),
        resizable=True,
        text_select=True,
        confirm_close=False,
    )

    # 9. 后台线程：等待服务器就绪 → 导航到实际页面
    nav_stop = threading.Event()

    def _navigate_when_ready() -> None:
        if wait_for_server(url, timeout=SERVER_START_TIMEOUT):
            try:
                window.load_url(url)
            except Exception:  # noqa: BLE001
                pass
        else:
            msg = server_error[0] if server_error else "服务器启动超时"
            try:
                window.load_html(ERROR_HTML_TEMPLATE.format(message=msg))
            except Exception:  # noqa: BLE001
                show_error(APP_TITLE, f"启动失败：{msg}")

    nav_thread = threading.Thread(target=_navigate_when_ready, daemon=True, name="StudioNav")
    nav_thread.start()

    # 10. 后台线程：周期性保存窗口尺寸
    def _periodic_save() -> None:
        while not nav_stop.is_set():
            time.sleep(3)
            try:
                result = window.evaluate_js(
                    "JSON.stringify({w:window.outerWidth,h:window.outerHeight})"
                )
                if result and result != "null":
                    data = json.loads(result)
                    w, h = int(data.get("w", 0)), int(data.get("h", 0))
                    if w > 200 and h > 200:
                        save_window_state(data_dir, {"width": w, "height": h})
            except Exception:  # noqa: BLE001
                pass

    save_thread = threading.Thread(target=_periodic_save, daemon=True, name="StudioWinSave")
    save_thread.start()

    # 11. 信号处理（Ctrl+C / kill）
    def _signal_handler(signum: int, frame: object) -> None:
        nav_stop.set()
        shutdown_server(server)
        os._exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # 12. 启动 PyWebView（阻塞直到窗口关闭）
    print(f"{APP_TITLE} {VERSION} 桌面版已启动 → {url}")
    webview.start(debug=debug)

    # 13. 窗口关闭后清理
    nav_stop.set()
    shutdown_server(server)
    try:
        lock_sock.close()
    except Exception:  # noqa: BLE001
        pass
    print(f"{APP_TITLE} 已退出。")


if __name__ == "__main__":
    main()
