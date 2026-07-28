#!/usr/bin/env python3
"""构建公众号 AI Studio 桌面端应用包。

支持两个平台：
- macOS: 生成 .app 应用包（含 Info.plist、自定义 .icns 图标、launcher 脚本）
- Windows: 生成带快捷方式图标的启动目录（含 .ico 图标、VBS 启动器）

用法:
    python3 build_scripts/build_desktop.py          # 自动检测平台
    python3 build_scripts/build_desktop.py --macos   # 仅 macOS
    python3 build_scripts/build_desktop.py --windows # 仅 Windows
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "公众号 AI Studio"
APP_BUNDLE = PROJECT_ROOT / f"{APP_NAME}.app"

# ---------------------------------------------------------------------------
# macOS .app 构建
# ---------------------------------------------------------------------------

INFO_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>公众号 AI Studio</string>
    <key>CFBundleDisplayName</key>
    <string>公众号 AI Studio</string>
    <key>CFBundleIdentifier</key>
    <string>com.studio.wechat-ai</string>
    <key>CFBundleVersion</key>
    <string>2.1.3</string>
    <key>CFBundleShortVersionString</key>
    <string>2.1.3</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleSignature</key>
    <string>WAIS</string>
    <key>CFBundleExecutable</key>
    <string>launcher</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.13</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSUIElement</key>
    <false/>
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsLocalNetworking</key>
        <true/>
    </dict>
</dict>
</plist>
"""

LAUNCHER_SCRIPT = """\
#!/bin/bash
# 公众号 AI Studio — macOS 应用启动器
set -eu

# 清理可能干扰 Python 的环境变量
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP VIRTUAL_ENV 2>/dev/null || true

# 定位项目根目录
APP_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"

# 查找可用的 Python 3（优先有 pywebview 的）
PYTHON=""
for candidate in \\
    "/opt/homebrew/bin/python3" \\
    "/usr/local/bin/python3" \\
    "/usr/bin/python3" \\
    "$(command -v python3 2>/dev/null)"; do
    if [ -x "$candidate" ]; then
        version=$("$candidate" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo "0.0")
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 9 ] 2>/dev/null; then
            if "$candidate" -c "import webview" 2>/dev/null; then
                PYTHON="$candidate"
                break
            fi
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    for candidate in "/opt/homebrew/bin/python3" "/usr/local/bin/python3" "/usr/bin/python3"; do
        if [ -x "$candidate" ]; then
            version=$("$candidate" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo "0.0")
            major=$(echo "$version" | cut -d. -f1)
            minor=$(echo "$version" | cut -d. -f2)
            if [ "$major" -ge 3 ] && [ "$minor" -ge 9 ] 2>/dev/null; then
                PYTHON="$candidate"
                break
            fi
        fi
    done
fi

if [ -z "$PYTHON" ]; then
    osascript -e 'display dialog "未找到 Python 3.9+，请先安装 Python。" with title "公众号 AI Studio" buttons {"确定"} default button 1 with icon stop' 2>/dev/null || true
    exit 1
fi

if ! "$PYTHON" -c "import webview" 2>/dev/null; then
    "$PYTHON" -m pip install pywebview --break-system-packages 2>/dev/null || true
    if ! "$PYTHON" -c "import webview" 2>/dev/null; then
        osascript -e 'display dialog "缺少 pywebview 依赖，请在终端运行：\\n\\n'"$PYTHON"' -m pip install pywebview --break-system-packages" with title "公众号 AI Studio" buttons {"确定"} default button 1 with icon stop' 2>/dev/null || true
        exit 1
    fi
fi

cd "$APP_ROOT"
exec "$PYTHON" desktop.py "$@"
"""

PKG_INFO = "APPLWAIS"


def build_macos() -> None:
    """构建 macOS .app 应用包。"""
    contents = APP_BUNDLE / "Contents"
    macos_dir = contents / "MacOS"
    resources_dir = contents / "Resources"

    if APP_BUNDLE.exists():
        shutil.rmtree(APP_BUNDLE)

    macos_dir.mkdir(parents=True, exist_ok=True)
    resources_dir.mkdir(parents=True, exist_ok=True)

    (contents / "Info.plist").write_text(INFO_PLIST, encoding="utf-8")
    (contents / "PkgInfo").write_text(PKG_INFO, encoding="utf-8")

    launcher_path = macos_dir / "launcher"
    launcher_path.write_text(LAUNCHER_SCRIPT, encoding="utf-8")
    launcher_path.chmod(
        launcher_path.stat().st_mode
        | stat.S_IRUSR | stat.S_IXUSR
        | stat.S_IRGRP | stat.S_IXGRP
        | stat.S_IROTH | stat.S_IXOTH
    )

    # 复制图标
    icon_icns = PROJECT_ROOT / "build_assets" / "AppIcon.icns"
    icon_png = PROJECT_ROOT / "build_assets" / "AppIcon.png"
    if icon_icns.exists():
        shutil.copy2(icon_icns, resources_dir / "AppIcon.icns")
    if icon_png.exists():
        shutil.copy2(icon_png, resources_dir / "AppIcon.png")

    print(f"[macOS] 应用包已创建: {APP_BUNDLE}")
    print(f"  双击 {APP_NAME}.app 即可启动")


# ---------------------------------------------------------------------------
# Windows 启动目录构建
# ---------------------------------------------------------------------------

# VBS 启动器 — 无控制台窗口启动 Python
WIN_VBS_LAUNCHER = """\
' 公众号 AI Studio — Windows 无控制台启动器
Set objShell = CreateObject("WScript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")

' 定位项目根目录（VBS 所在目录的上级）
strRoot = objFSO.GetParentFolderName(objFSO.GetParentFolderName(WScript.ScriptFullName))

' 查找 Python
Dim pythonExe
pythonExe = ""

' 检查 py launcher
If objFSO.FileExists("C:\\Windows\\py.exe") Then
    Dim wshExec
    On Error Resume Next
    Dim q
    q = Chr(34)
    Set wshExec = objShell.Exec("py -3 -c " & q & "import sys; print(sys.executable)" & q)
    If Err.Number = 0 Then
        Dim output
        output = ""
        Do While Not wshExec.StdOut.AtEndOfStream
            output = wshExec.StdOut.ReadAll()
        Loop
        output = Trim(output)
        If objFSO.FileExists(output) Then
            pythonExe = output
        End If
    End If
    On Error GoTo 0
End If

' 回退到常见路径
If pythonExe = "" Then
    Dim candidates
    candidates = Array( _
        "python", _
        "python3", _
        "%LOCALAPPDATA%\\Programs\\Python\\Python313\\python.exe", _
        "%LOCALAPPDATA%\\Programs\\Python\\Python312\\python.exe", _
        "%LOCALAPPDATA%\\Programs\\Python\\Python311\\python.exe", _
        "%LOCALAPPDATA%\\Programs\\Python\\Python310\\python.exe", _
        "C:\\Python313\\python.exe", _
        "C:\\Python312\\python.exe", _
        "C:\\Python311\\python.exe", _
        "C:\\Python310\\python.exe" _
    )
    Dim i
    For i = 0 To UBound(candidates)
        Dim candidate
        candidate = objShell.ExpandEnvironmentStrings(candidates(i))
        If objFSO.FileExists(candidate) Then
            pythonExe = candidate
            Exit For
        End If
    Next
End If

If pythonExe = "" Then
    MsgBox "未找到 Python 3.9+，请先安装 Python。", vbCritical, "公众号 AI Studio"
    WScript.Quit 1
End If

' 检查 pywebview
On Error Resume Next
Dim checkResult
checkResult = objShell.Exec(q & pythonExe & q & " -c " & q & "import webview" & q)
checkResult.StdOut.ReadAll()
If Err.Number <> 0 Then
    On Error GoTo 0
    ' 尝试自动安装
    objShell.Run q & pythonExe & q & " -m pip install pywebview", 0, True
End If
On Error GoTo 0

' 启动桌面端（无控制台窗口）
objShell.CurrentDirectory = strRoot
objShell.Run q & pythonExe & q & " " & q & strRoot & "\\desktop.py" & q, 0, False
"""

WIN_README = """\
公众号 AI Studio — Windows 桌面端
================================

启动方式：
  双击 "启动桌面端.vbs" 即可启动

首次运行需要：
  1. 安装 Python 3.9+（https://python.org）
  2. 安装依赖：pip install pywebview Pillow

如需创建带图标的桌面快捷方式：
  右键 "启动桌面端.vbs" → 发送到 → 桌面快捷方式
  然后右键快捷方式 → 属性 → 更改图标 → 选择 AppIcon.ico
"""


def build_windows() -> None:
    """构建 Windows 启动目录。"""
    win_dir = PROJECT_ROOT / f"{APP_NAME} (Windows)"
    if win_dir.exists():
        shutil.rmtree(win_dir)
    win_dir.mkdir(parents=True)

    # VBS 启动器
    (win_dir / "启动桌面端.vbs").write_text(WIN_VBS_LAUNCHER, encoding="utf-8")

    # 图标文件
    icon_ico = PROJECT_ROOT / "build_assets" / "AppIcon.ico"
    icon_png = PROJECT_ROOT / "build_assets" / "AppIcon.png"
    if icon_ico.exists():
        shutil.copy2(icon_ico, win_dir / "AppIcon.ico")
    if icon_png.exists():
        shutil.copy2(icon_png, win_dir / "AppIcon.png")

    # 说明文件
    (win_dir / "README.txt").write_text(WIN_README, encoding="utf-8")

    # 创建快捷方式脚本（需要 PowerShell）
    shortcut_ps1 = """\
$WshShell = New-Object -comObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("$env:USERPROFILE\\Desktop\\公众号 AI Studio.lnk")
$Shortcut.TargetPath = "$PSScriptRoot\\启动桌面端.vbs"
$Shortcut.IconLocation = "$PSScriptRoot\\AppIcon.ico,0"
$Shortcut.WorkingDirectory = "$PSScriptRoot"
$Shortcut.Description = "公众号 AI Studio 桌面端"
$Shortcut.Save()
Write-Host "桌面快捷方式已创建"
"""
    (win_dir / "创建桌面快捷方式.ps1").write_text(shortcut_ps1, encoding="utf-8")

    print(f"[Windows] 启动目录已创建: {win_dir}")
    print(f"  双击 '启动桌面端.vbs' 即可启动")
    print(f"  运行 '创建桌面快捷方式.ps1' 可创建带图标的桌面快捷方式")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> None:
    args = sys.argv[1:]
    target = "auto"
    if "--macos" in args or "-m" in args:
        target = "macos"
    elif "--windows" in args or "-w" in args:
        target = "windows"

    if target in ("auto", "macos") and sys.platform == "darwin":
        build_macos()
    elif target in ("auto", "windows") and sys.platform == "win32":
        build_windows()
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
