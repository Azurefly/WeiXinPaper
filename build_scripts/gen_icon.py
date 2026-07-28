#!/usr/bin/env python3
"""生成公众号 AI Studio 应用图标（macOS .icns + Windows .ico + PNG）。

使用 PIL 创建品牌风格的 1024×1024 图标，然后转换为各平台格式。
- macOS: iconutil → .icns
- Windows: PIL → .ico（含多尺寸）
- 通用: .png
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUTPUT_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
ICON_SIZE = 1024


def find_cjk_font() -> str:
    """查找系统中的中文字体。"""
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return candidates[0]


def render_icon(size: int = ICON_SIZE) -> Image.Image:
    """渲染单个尺寸的图标。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    margin = int(size * 0.04)
    radius = int(size * 0.22)

    # 渐变背景
    bg = Image.new("RGB", (size, size), (15, 99, 104))
    for y in range(size):
        ratio = y / size
        r = int(15 + (8 - 15) * ratio)
        g = int(99 + (77 - 99) * ratio)
        b = int(104 + (82 - 104) * ratio)
        for x in range(size):
            xratio = x / size
            r2 = int(r + (8 - r) * xratio * 0.3)
            g2 = int(g + (77 - g) * xratio * 0.3)
            b2 = int(b + (82 - b) * xratio * 0.3)
            bg.putpixel((x, y), (r2, g2, b2))

    # 圆角遮罩
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=radius,
        fill=255,
    )
    img.paste(bg, (0, 0), mask)

    # 高光
    highlight = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    hl_draw = ImageDraw.Draw(highlight)
    hl_height = int(size * 0.35)
    for y in range(hl_height):
        alpha = int(40 * (1 - y / hl_height))
        hl_draw.line([(margin + radius, margin + y), (size - margin - radius, margin + y)],
                     fill=(255, 255, 255, alpha))
    img = Image.alpha_composite(img, highlight)

    # "公" 字
    draw = ImageDraw.Draw(img)
    font_path = find_cjk_font()
    font_size = int(size * 0.58)
    try:
        font = ImageFont.truetype(font_path, font_size)
    except Exception:
        font = ImageFont.load_default()

    text = "公"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (size - tw) / 2 - bbox[0]
    ty = (size - th) / 2 - bbox[1] - int(size * 0.02)

    # 阴影
    shadow_offset = max(2, int(size * 0.008))
    shadow_img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_img)
    shadow_draw.text((tx + shadow_offset, ty + shadow_offset), text,
                     font=font, fill=(0, 0, 0, 60))
    shadow_img = shadow_img.filter(ImageFilter.GaussianBlur(radius=max(2, int(size * 0.005))))
    img = Image.alpha_composite(img, shadow_img)

    draw = ImageDraw.Draw(img)
    draw.text((tx, ty), text, font=font, fill=(255, 255, 255, 255))

    return img


def build_macos_icon(output_dir: Path, master: Image.Image) -> Path | None:
    """生成 macOS .icns 图标。"""
    if sys.platform != "darwin":
        return None

    iconset_dir = output_dir / "AppIcon.iconset"
    if iconset_dir.exists():
        shutil.rmtree(iconset_dir)
    iconset_dir.mkdir(parents=True)

    sizes = [16, 32, 64, 128, 256, 512, 1024]
    for sz in sizes:
        resized = master.resize((sz, sz), Image.LANCZOS)
        resized.save(iconset_dir / f"icon_{sz}x{sz}.png")
        if sz <= 512:
            retina = master.resize((sz * 2, sz * 2), Image.LANCZOS)
            retina.save(iconset_dir / f"icon_{sz}x{sz}@2x.png")

    icns_path = output_dir / "AppIcon.icns"
    result = subprocess.run(
        ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(icns_path)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"[macOS] iconutil 失败: {result.stderr}", file=sys.stderr)
        return None

    print(f"[macOS] .icns 已生成: {icns_path}")
    return icns_path


def build_windows_icon(output_dir: Path, master: Image.Image) -> Path:
    """生成 Windows .ico 图标（含多尺寸）。"""
    ico_path = output_dir / "AppIcon.ico"
    sizes = [16, 32, 48, 64, 128, 256]
    images = [master.resize((sz, sz), Image.LANCZOS) for sz in sizes]
    images[0].save(ico_path, format="ICO", sizes=[(sz, sz) for sz in sizes], append_images=images[1:])
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

    if sys.platform == "darwin":
        build_macos_icon(out, master)

    build_windows_icon(out, master)

    print(f"\n所有图标已生成到: {out}")


if __name__ == "__main__":
    main()
