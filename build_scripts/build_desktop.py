#!/usr/bin/env python3
"""构建公众号 AI Studio 桌面端独立应用。

使用 PyInstaller 将 Python 运行时、依赖和资源打包为独立应用，
无需目标机器安装 Python 或任何依赖。

支持两个平台：
- macOS: 生成 .app 应用包（含内嵌 Python、自定义图标、ad-hoc 签名）
- Windows: 生成独立目录（含 .exe、_internal/ 依赖目录、图标）

用法:
    python build_scripts/build_desktop.py          # 自动检测平台
    python build_scripts/build_desktop.py --macos   # 仅 macOS
    python build_scripts/build_desktop.py --windows # 仅 Windows
    python build_scripts/build_desktop.py --clean   # 清理后构建
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "公众号 AI Studio"
SPEC_FILE = PROJECT_ROOT / "build_scripts" / "desktop.spec"
BUILD_ASSETS = PROJECT_ROOT / "build_assets"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"


# ---------------------------------------------------------------------------
# 前置检查
# ---------------------------------------------------------------------------

def check_prerequisites() -> None:
    """检查构建前置条件。"""
    print("=== 检查构建环境 ===")

    # Python 版本
    major, minor = sys.version_info[:2]
    if major < 3 or minor < 9:
        print(f"✗ 需要 Python 3.9+，当前 {major}.{minor}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ Python {major}.{minor}")

    # PyInstaller
    try:
        import PyInstaller  # noqa: F401
        print(f"✓ PyInstaller {PyInstaller.__version__}")
    except ImportError:
        print("✗ 缺少 PyInstaller，正在安装...", file=sys.stderr)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
        print("✓ PyInstaller 已安装")

    # pywebview
    try:
        import webview  # noqa: F401
        print("✓ pywebview")
    except ImportError:
        print("✗ 缺少 pywebview，正在安装...", file=sys.stderr)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pywebview"])
        print("✓ pywebview 已安装")

    # PIL
    try:
        import PIL  # noqa: F401
        print("✓ Pillow")
    except ImportError:
        print("✗ 缺少 Pillow，正在安装...", file=sys.stderr)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "Pillow"])
        print("✓ Pillow 已安装")

    # spec 文件
    if not SPEC_FILE.exists():
        print(f"✗ 缺少 spec 文件: {SPEC_FILE}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ spec 文件: {SPEC_FILE.name}")

    print()


def ensure_icons() -> None:
    """确保图标文件存在，不存在则生成。"""
    icns = BUILD_ASSETS / "AppIcon.icns"
    ico = BUILD_ASSETS / "AppIcon.ico"
    png = BUILD_ASSETS / "AppIcon.png"

    if icns.exists() and ico.exists() and png.exists():
        print("✓ 图标文件已存在")
        return

    print("=== 生成图标 ===")
    gen_icon = PROJECT_ROOT / "build_scripts" / "gen_icon.py"
    if not gen_icon.exists():
        print(f"✗ 缺少图标生成脚本: {gen_icon}", file=sys.stderr)
        sys.exit(1)

    BUILD_ASSETS.mkdir(parents=True, exist_ok=True)
    subprocess.check_call([sys.executable, str(gen_icon), str(BUILD_ASSETS)])
    print("✓ 图标生成完成")
    print()


# ---------------------------------------------------------------------------
# macOS 构建
# ---------------------------------------------------------------------------

def build_macos(clean: bool = False) -> None:
    """构建 macOS .app 独立应用包。"""
    print("=== 构建 macOS .app ===")

    app_bundle = DIST_DIR / f"{APP_NAME}.app"

    # 清理
    if clean:
        print("清理旧构建...")
        if (PROJECT_ROOT / "build" / "desktop").exists():
            shutil.rmtree(PROJECT_ROOT / "build" / "desktop")
        if app_bundle.exists():
            shutil.rmtree(app_bundle)
        if (DIST_DIR / APP_NAME).exists():
            shutil.rmtree(DIST_DIR / APP_NAME)

    # 运行 PyInstaller
    print("运行 PyInstaller...")
    env = os.environ.copy()
    # 清理可能干扰的环境变量
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
        env.pop(key, None)
    # 使用本地缓存目录避免权限问题
    env["PYINSTALLER_CONFIG_DIR"] = str(PROJECT_ROOT / ".pyinstaller_config")

    subprocess.check_call(
        [sys.executable, "-m", "PyInstaller", str(SPEC_FILE), "--noconfirm"],
        cwd=str(PROJECT_ROOT),
        env=env,
    )

    if not app_bundle.exists():
        print(f"✗ 构建失败：{app_bundle} 不存在", file=sys.stderr)
        sys.exit(1)

    # 复制到项目根目录
    root_app = PROJECT_ROOT / f"{APP_NAME}.app"
    if root_app.exists():
        shutil.rmtree(root_app)
    # macOS framework 依赖符号链接维持标准 bundle 结构。copytree 默认会解引用
    # 符号链接，导致 Python.framework/Versions/Current 变成实体目录，最终使
    # codesign 报 "bundle format is ambiguous"。
    shutil.copytree(app_bundle, root_app, symlinks=True)
    print(f"✓ 已复制到项目根目录: {root_app}")

    # 清理缓存目录（避免 codesign 失败）
    print("清理缓存目录...")
    for pattern in [".pytest_cache", "__pycache__", ".gitignore", "CACHEDIR.TAG"]:
        for item in root_app.rglob(pattern):
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            elif item.is_file():
                item.unlink(missing_ok=True)

    # 分步 Ad-hoc 签名（--deep 在新版 macOS 有兼容问题）
    print("Ad-hoc 签名...")
    # 1. 签名所有动态库
    for dylib in root_app.rglob("*.dylib"):
        subprocess.check_call(
            ["codesign", "--force", "--sign", "-", str(dylib)],
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
    # 2. 签名所有 .so 文件
    for so in root_app.rglob("*.so"):
        subprocess.check_call(
            ["codesign", "--force", "--sign", "-", str(so)],
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
    # 3. 重新签名嵌套 Python framework
    python_framework = root_app / "Contents" / "Frameworks" / "Python.framework"
    if python_framework.exists():
        subprocess.check_call(
            ["codesign", "--force", "--sign", "-", str(python_framework)],
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
    # 4. 签名主可执行文件
    exe = root_app / "Contents" / "MacOS" / APP_NAME
    subprocess.check_call(
        ["codesign", "--force", "--sign", "-", str(exe)],
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    # 5. 签名整个 .app bundle
    subprocess.check_call(
        ["codesign", "--force", "--sign", "-", str(root_app)],
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )

    # 去除隔离属性
    print("去除隔离属性...")
    subprocess.check_call(["xattr", "-cr", str(root_app)], stderr=subprocess.DEVNULL)

    # 严格验证所有嵌套代码。签名失败属于发布阻断项，不能降级为警告。
    result = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(root_app)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        print(f"✗ 签名验证失败：\n{detail}", file=sys.stderr)
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )
    print("✓ 签名验证通过（deep + strict）")

    # 显示大小
    size = sum(f.stat().st_size for f in root_app.rglob("*") if f.is_file())
    print(f"\n✓ 构建完成！")
    print(f"  位置: {root_app}")
    print(f"  大小: {size / 1024 / 1024:.1f} MB")
    print(f"  双击 {APP_NAME}.app 即可启动")
    print(f"\n  分发到其他 Mac：")
    print(f"  1. 复制 .app 到目标机器")
    print(f"  2. 首次打开时右键 → 打开（绕过 Gatekeeper）")
    print(f"  3. 或在终端运行: xattr -cr '{APP_NAME}.app'")


# ---------------------------------------------------------------------------
# Windows 构建
# ---------------------------------------------------------------------------

def build_windows(clean: bool = False) -> None:
    """构建 Windows 独立应用目录。"""
    print("=== 构建 Windows 独立应用 ===")

    win_dir = DIST_DIR / APP_NAME

    # 清理
    if clean:
        print("清理旧构建...")
        if (PROJECT_ROOT / "build" / "desktop").exists():
            shutil.rmtree(PROJECT_ROOT / "build" / "desktop")
        if win_dir.exists():
            shutil.rmtree(win_dir)

    # 运行 PyInstaller
    print("运行 PyInstaller...")
    env = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
        env.pop(key, None)

    subprocess.check_call(
        [sys.executable, "-m", "PyInstaller", str(SPEC_FILE), "--noconfirm"],
        cwd=str(PROJECT_ROOT),
        env=env,
    )

    if not win_dir.exists():
        print(f"✗ 构建失败：{win_dir} 不存在", file=sys.stderr)
        sys.exit(1)
    exe = win_dir / f"{APP_NAME}.exe"
    internal_dir = win_dir / "_internal"
    if not exe.is_file():
        print(f"✗ 构建失败：缺少主程序 {exe.name}", file=sys.stderr)
        sys.exit(1)
    if not internal_dir.is_dir():
        print("✗ 构建失败：缺少 _internal 依赖目录", file=sys.stderr)
        sys.exit(1)

    # 创建快捷方式脚本
    shortcut_ps1 = win_dir / "创建桌面快捷方式.ps1"
    shortcut_ps1.write_text(
        f'$WshShell = New-Object -comObject WScript.Shell\n'
        f'$Shortcut = $WshShell.CreateShortcut("$env:USERPROFILE\\Desktop\\{APP_NAME}.lnk")\n'
        f'$Shortcut.TargetPath = "$PSScriptRoot\\{APP_NAME}.exe"\n'
        f'$Shortcut.IconLocation = "$PSScriptRoot\\AppIcon.ico,0"\n'
        f'$Shortcut.WorkingDirectory = "$PSScriptRoot"\n'
        f'$Shortcut.Description = "{APP_NAME} 桌面端"\n'
        f'$Shortcut.Save()\n'
        f'Write-Host "桌面快捷方式已创建"\n',
        encoding="utf-8",
    )

    # 复制图标到输出目录
    ico = BUILD_ASSETS / "AppIcon.ico"
    if ico.exists():
        shutil.copy2(ico, win_dir / "AppIcon.ico")

    # 显示大小
    size = sum(f.stat().st_size for f in win_dir.rglob("*") if f.is_file())
    print(f"\n✓ 构建完成！")
    print(f"  位置: {win_dir}")
    print(f"  大小: {size / 1024 / 1024:.1f} MB")
    print(f"  双击 {APP_NAME}.exe 即可启动")
    print(f"\n  分发到其他 Windows：")
    print(f"  1. 将整个目录打包为 zip")
    print(f"  2. 在目标机器解压")
    print(f"  3. 双击 {APP_NAME}.exe 启动")
    print(f"  4. 运行 '创建桌面快捷方式.ps1' 可创建桌面快捷方式")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> None:
    args = sys.argv[1:]
    target = "auto"
    clean = False

    if "--macos" in args or "-m" in args:
        target = "macos"
    elif "--windows" in args or "-w" in args:
        target = "windows"
    if "--clean" in args or "-c" in args:
        clean = True

    check_prerequisites()
    ensure_icons()

    if target in ("auto", "macos") and sys.platform == "darwin":
        build_macos(clean=clean)
    elif target in ("auto", "windows") and sys.platform == "win32":
        build_windows(clean=clean)
    elif target == "macos":
        print("macOS 构建仅在 macOS 上可用", file=sys.stderr)
        sys.exit(1)
    elif target == "windows":
        print("Windows 构建仅在 Windows 上可用", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"不支持的操作系统: {sys.platform}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
