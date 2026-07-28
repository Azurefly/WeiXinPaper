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
    pathex=["..", "../backend"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
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

# macOS 图标
icon_path = "../build_assets/AppIcon.icns" if sys.platform == "darwin" else "../build_assets/AppIcon.ico"

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
    icon=icon_path if Path(icon_path).exists() else None,
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
        icon=icon_path if Path(icon_path).exists() else None,
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
