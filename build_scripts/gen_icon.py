#!/usr/bin/env python3
"""生成公众号 AI Studio 应用图标（macOS .icns + Windows .ico + PNG）。

使用 build_assets/AppIconSource.png 透明母版转换为各平台格式。
- macOS: Pillow → .icns
- Windows: PIL → .ico（含多尺寸）
- 通用: .png
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

OUTPUT_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
ICON_SIZE = 1024
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = PROJECT_ROOT / "build_assets" / "AppIconSource.png"


def render_icon(
    size: int = ICON_SIZE,
    source_path: Path = DEFAULT_SOURCE,
) -> Image.Image:
    """从透明母版生成正方形 RGBA 图标，并保留边缘安全区。"""
    if not source_path.is_file():
        raise FileNotFoundError(f"缺少图标母版: {source_path}")

    with Image.open(source_path) as source:
        source = source.convert("RGBA")
        if source.width != source.height:
            raise ValueError(f"图标母版必须为正方形，当前为 {source.width}×{source.height}")
        alpha = source.getchannel("A")
        alpha_min, alpha_max = alpha.getextrema()
        if alpha_min != 0 or alpha_max != 255:
            raise ValueError("图标母版必须同时包含透明边缘和不透明主体")
        return source.resize((size, size), Image.Resampling.LANCZOS)


def build_macos_icon(output_dir: Path, master: Image.Image) -> Path:
    """生成 macOS .icns 图标。"""
    icns_path = output_dir / "AppIcon.icns"
    # 当前 macOS iconutil 对部分由 Pillow 输出的 RGBA PNG iconset 会误报
    # Invalid Iconset；Pillow 可直接写入包含多分辨率资源的标准 ICNS。
    master.save(icns_path, format="ICNS")
    with Image.open(icns_path) as icon:
        sizes = set(icon.info.get("sizes", []))
    if (512, 512, 2) not in sizes:
        raise ValueError(f"ICNS 缺少 1024×1024 表示层: {sorted(sizes)}")

    print(f"[macOS] .icns 已生成: {icns_path}")
    return icns_path


def build_windows_icon(output_dir: Path, master: Image.Image) -> Path:
    """生成 Windows .ico 图标（含多尺寸）。"""
    ico_path = output_dir / "AppIcon.ico"
    sizes = [16, 32, 48, 64, 128, 256]
    master.save(ico_path, format="ICO", sizes=[(sz, sz) for sz in sizes])
    print(f"[Windows] .ico 已生成: {ico_path}")
    return ico_path


def build_png(output_dir: Path, master: Image.Image) -> Path:
    """生成 1024×1024 PNG 图标。"""
    png_path = output_dir / "AppIcon.png"
    master.save(png_path)
    print(f"[通用] .png 已生成: {png_path}")
    return png_path


def main() -> None:
    out = Path(OUTPUT_DIR)
    out.mkdir(parents=True, exist_ok=True)

    master = render_icon(1024)
    build_png(out, master)

    build_macos_icon(out, master)
    build_windows_icon(out, master)

    print(f"\n所有图标已生成到: {out}")


if __name__ == "__main__":
    main()
