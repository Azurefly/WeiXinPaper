"""程序化封面图片生成器。

当用户未手动上传封面时，工作流会调用此模块根据文章标题自动生成一张
带渐变背景和标题文字的 PNG 封面图，避免发布到公众号时因缺少封面而被拒绝。
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import textwrap
from typing import Sequence

# 封面尺寸：900×383 像素（微信公众号推荐比例 2.35:1）
COVER_WIDTH = 900
COVER_HEIGHT = 383

_FONT_CANDIDATES = (
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)

# 渐变色板：每组 (起始色, 结束色)，均使用 RGB 元组
_PALETTES: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = [
    ((66, 99, 235), (123, 67, 223)),    # 蓝紫
    ((30, 144, 255), (0, 191, 255)),     # 天蓝
    ((16, 185, 129), (5, 150, 105)),     # 翠绿
    ((245, 158, 11), (220, 38, 38)),     # 暖橙红
    ((139, 92, 246), (217, 70, 239)),    # 紫粉
    ((59, 130, 246), (14, 165, 233)),    # 海蓝
    ((236, 72, 153), (185, 28, 122)),    # 玫红
    ((20, 184, 166), (13, 148, 136)),    # 青绿
]


def _resolve_font(size: int):
    """加载系统中第一个可用的中文字体，找不到时回退到默认字体。"""
    from PIL import ImageFont

    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def _pick_palette(seed: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """根据字符串哈希选取一组渐变色板，使同一标题始终生成相同配色。"""
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    index = int(digest[:8], 16) % len(_PALETTES)
    return _PALETTES[index]


def _draw_gradient(draw, width: int, height: int, start: tuple[int, int, int], end: tuple[int, int, int]) -> None:
    """垂直线性渐变填充。"""
    for y in range(height):
        ratio = y / max(height - 1, 1)
        r = int(start[0] + (end[0] - start[0]) * ratio)
        g = int(start[1] + (end[1] - start[1]) * ratio)
        b = int(start[2] + (end[2] - start[2]) * ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b))


def _wrap_title(title: str, max_chars_per_line: int) -> list[str]:
    """将标题按最大字符数折行，支持中英文混合。"""
    title = title.strip()
    if not title:
        return ["未命名文章"]
    lines: list[str] = []
    for segment in title.split("\n"):
        if not segment.strip():
            continue
        wrapped = textwrap.wrap(segment, width=max_chars_per_line, break_long_words=True)
        lines.extend(wrapped if wrapped else [segment])
    return lines[:3]  # 最多 3 行


def _draw_centered_text(draw, lines: Sequence[str], font, width: int, height: int) -> None:
    """在图片中央绘制多行文字，带半透明阴影增强可读性。"""
    from PIL import ImageDraw

    line_height = font.size + 8
    total_height = len(lines) * line_height
    start_y = (height - total_height) // 2

    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        text_width = bbox[2] - bbox[0]
        x = (width - text_width) // 2
        y = start_y + i * line_height
        # 阴影
        draw.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0, 120))
        # 正文（白色）
        draw.text((x, y), line, font=font, fill=(255, 255, 255))


def generate_cover_data_url(title: str, subtitle: str = "") -> str:
    """生成 PNG 格式的封面 data URL。

    Args:
        title: 文章标题，会显示在封面中央。
        subtitle: 可选副标题/摘要，显示在标题下方（小字）。

    Returns:
        ``data:image/png;base64,...`` 格式的字符串，可直接存入 coverDataUrl 字段。
    """
    from PIL import Image, ImageDraw

    seed = title or "untitled"
    start_color, end_color = _pick_palette(seed)

    img = Image.new("RGB", (COVER_WIDTH, COVER_HEIGHT), start_color)
    draw = ImageDraw.Draw(img)

    _draw_gradient(draw, COVER_WIDTH, COVER_HEIGHT, start_color, end_color)

    # 装饰：左上角和右下角半透明圆
    for offset, radius, alpha in [(0, 180, 30), (COVER_WIDTH - 160, 160, 25)]:
        overlay = Image.new("RGBA", (COVER_WIDTH, COVER_HEIGHT), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        od.ellipse(
            [-40 + offset, -40, 120 + offset, 120],
            fill=(255, 255, 255, alpha),
        )
        img.paste(overlay, (0, 0), overlay)

    # 标题字体大小根据标题长度自适应
    clean_title = re.sub(r"^#+\s*", "", title or "").strip()
    title_len = len(clean_title)
    if title_len <= 12:
        font_size = 52
        max_chars = 14
    elif title_len <= 24:
        font_size = 42
        max_chars = 18
    else:
        font_size = 34
        max_chars = 24

    title_font = _resolve_font(font_size)
    title_lines = _wrap_title(clean_title, max_chars)
    _draw_centered_text(draw, title_lines, title_font, COVER_WIDTH, COVER_HEIGHT)

    # 副标题
    if subtitle:
        sub_clean = subtitle.strip()[:60]
        sub_font = _resolve_font(max(18, font_size // 2))
        sub_lines = _wrap_title(sub_clean, max_chars + 6)[:1]
        line_height = font_size + 8
        total_title_height = len(title_lines) * line_height
        sub_y = (COVER_HEIGHT - total_title_height) // 2 + total_title_height + 12
        for line in sub_lines:
            bbox = draw.textbbox((0, 0), line, font=sub_font)
            tw = bbox[2] - bbox[0]
            x = (COVER_WIDTH - tw) // 2
            draw.text((x + 1, sub_y + 1), line, font=sub_font, fill=(0, 0, 0, 100))
            draw.text((x, sub_y), line, font=sub_font, fill=(255, 255, 255, 220))

    # 输出为 PNG data URL
    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    raw = buffer.getvalue()

    # 控制大小在 2MB 以内
    if len(raw) > 2_000_000:
        # 降低质量重试
        buffer = io.BytesIO()
        img.save(buffer, format="PNG", optimize=False)
        raw = buffer.getvalue()

    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:image/png;base64,{encoded}"
