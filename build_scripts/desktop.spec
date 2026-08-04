# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 — 公众号 AI Studio 桌面端

用法:
    cd 项目根目录
    pyinstaller build_scripts/desktop.spec --noconfirm

输出:
    dist/公众号 AI Studio.app  (macOS)
    dist/公众号 AI Studio/     (Windows)
"""

import sys
from pathlib import Path

block_cipher = None
PROJECT_ROOT = Path(SPEC).resolve().parent.parent

# 数据文件：(源路径, 目标路径)
# spec 文件在 build_scripts/ 目录下，源路径需 ../
datas = [
    ("../web", "web"),                    # 前端静态资源
    ("../backend", "backend"),            # 后端 Python 模块
    ("../build_assets/AppIcon.icns", "build_assets"),  # macOS 图标
    ("../build_assets/AppIcon.png", "build_assets"),   # 通用图标
    ("../build_assets/AppIcon.ico", "build_assets"),   # Windows 图标
]

# 隐式导入（PyInstaller 无法自动检测的模块）
hiddenimports = [
    # desktop.py 会在运行时把 backend/ 加入 sys.path，再动态 import server。
    # PyInstaller 无法沿这条动态路径分析依赖，因此必须把后端入口全部纳入
    # 模块图；只把 backend/ 当 datas 复制会漏掉 urllib.robotparser 等依赖。
    "ai_engine",
    "auth_password",
    "auth_session",
    "content_security",
    "cover_generator",
    "data_transfer",
    "db",
    "logger_config",
    "runtime_security",
    "secrets_store",
    "secure_http",
    "server",
    "source_fetcher",
    "test_mode",
    "wechat_api",
    "workflow",
    "webview",
    "webview.platforms.edgechromium",
    "webview.platforms.cocoa",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageFont",
    "PIL.ImageFilter",
    # 标准库 C 扩展
    "sqlite3",
    "ssl",
    "ctypes",
    "ctypes.wintypes",
    "hashlib",
    "hmac",
    "secrets",
    "zlib",
    "ssl",
    "_ssl",
    "_socket",
    "_hashlib",
    "_ctypes",
    # 标准库纯 Python
    "http.client",
    "http.server",
    "urllib.parse",
    "urllib.request",
    "urllib.error",
    "urllib.robotparser",
    "html.parser",
    "json",
    "uuid",
    "base64",
    "mimetypes",
    "argparse",
    "logging",
    "logging.handlers",
    "concurrent.futures",
    "dataclasses",
    "ipaddress",
    "textwrap",
    "contextlib",
    "enum",
    "typing",
    "pathlib",
    "shutil",
    "signal",
    "socket",
    "threading",
    "time",
    "os",
    "sys",
    "re",
    "collections",
]

# macOS 专用隐式导入
if sys.platform == "darwin":
    hiddenimports += [
        "AppKit",
        "WebKit",
        "Foundation",
        "Quartz",
        "UniformTypeIdentifiers",
        "Security",
    ]

# Windows 专用隐式导入
if sys.platform == "win32":
    hiddenimports += [
        "webview.platforms.winforms",
        "webview.platforms.mshtml",
        "clr",
        "System",
        "System.Windows.Forms",
        "System.Drawing",
    ]

a = Analysis(
    ["../desktop.py"],
    pathex=[str(PROJECT_ROOT), str(PROJECT_ROOT / "backend")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    # 优先使用项目 hook。本项目的 backend/workflow.py 与 PyPI 上的
    # workflow 包同名，不覆盖第三方 hook 会错误查找不存在的包元数据。
    hookspath=[str(PROJECT_ROOT / "build_scripts" / "hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",       # 不需要 tkinter
        "unittest",
        "pydoc",
        "doctest",
        "test",
        "tests",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# 图标路径必须按 spec 文件定位。普通 Path("../build_assets/...") 会按构建
# 命令的 cwd 解析并误判为不存在，导致 PyInstaller 静默回退默认图标。
icon_name = "AppIcon.icns" if sys.platform == "darwin" else "AppIcon.ico"
icon_path = str(PROJECT_ROOT / "build_assets" / icon_name)
if not Path(icon_path).is_file():
    raise FileNotFoundError(f"缺少应用图标: {icon_path}")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="公众号 AI Studio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="公众号 AI Studio",
)

# macOS: 包装为 .app bundle
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="公众号 AI Studio.app",
        icon=icon_path,
        bundle_identifier="com.studio.wechat-ai",
        info_plist={
            "CFBundleName": "公众号 AI Studio",
            "CFBundleDisplayName": "公众号 AI Studio",
            "CFBundleVersion": "2.1.3",
            "CFBundleShortVersionString": "2.1.3",
            "CFBundlePackageType": "APPL",
            "LSMinimumSystemVersion": "10.13",
            "NSHighResolutionCapable": True,
            "LSUIElement": False,
            "NSAppTransportSecurity": {
                "NSAllowsLocalNetworking": True,
            },
        },
    )
