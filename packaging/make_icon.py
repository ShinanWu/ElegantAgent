#!/usr/bin/env python3
"""生成 macOS 原生风格的 AppIcon.icns（简洁渐变 + π 符号）。"""

from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "AppIcon.icns"
ICONSET = ROOT / "AppIcon.iconset"
CANVAS = 1024

# 与应用 UI 一致的深蓝配色
BG_TOP = (36, 59, 107)       # #243b6b
BG_BOTTOM = (18, 24, 41)     # #121829
ACCENT = (110, 168, 219)     # #6ea8db
GLYPH = (235, 242, 255)


def _lerp(a: int, b: int, t: float) -> int:
    return int(a + (b - a) * t)


def _render_master(size: int = CANVAS):
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        t = y / max(size - 1, 1)
        row = (
            _lerp(BG_TOP[0], BG_BOTTOM[0], t),
            _lerp(BG_TOP[1], BG_BOTTOM[1], t),
            _lerp(BG_TOP[2], BG_BOTTOM[2], t),
        )
        for x in range(size):
            px[x, y] = row

    draw = ImageDraw.Draw(img)

    # 顶部柔和高光（类似系统图标质感）
    highlight = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    hdraw = ImageDraw.Draw(highlight)
    cx, cy = size // 2, int(size * 0.28)
    r = int(size * 0.55)
    for i in range(r, 0, -1):
        alpha = int(28 * (1 - i / r) ** 1.6)
        hdraw.ellipse((cx - i, cy - i, cx + i, cy + i), fill=(255, 255, 255, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), highlight).convert("RGB")
    draw = ImageDraw.Draw(img)

    # 中心 π
    font = None
    for path, sz in (
        ("/System/Library/Fonts/SFNS.ttf", int(size * 0.46)),
        ("/System/Library/Fonts/SFNSRounded.ttf", int(size * 0.46)),
        ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", int(size * 0.50)),
        ("/Library/Fonts/Arial Unicode.ttf", int(size * 0.50)),
    ):
        try:
            font = ImageFont.truetype(path, sz)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    glyph = "π"
    bbox = draw.textbbox((0, 0), glyph, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (size - tw) // 2 - bbox[0]
    ty = (size - th) // 2 - bbox[1] - int(size * 0.02)
    draw.text((tx, ty), glyph, fill=GLYPH, font=font)

    # 底部 accent 弧线（极简装饰）
    arc_y = int(size * 0.78)
    arc_w = int(size * 0.36)
    draw.arc(
        (size // 2 - arc_w, arc_y - arc_w // 3, size // 2 + arc_w, arc_y + arc_w // 2),
        start=200,
        end=340,
        fill=ACCENT,
        width=max(2, size // 128),
    )

    return img


def _save_png(img, path: Path) -> None:
    img.save(path, format="PNG", compress_level=1, optimize=False)


def _resize_sharp(master, size: int):
    from PIL import Image, ImageFilter

    out = master.resize((size, size), Image.Resampling.LANCZOS)
    if size <= 128:
        out = out.filter(ImageFilter.UnsharpMask(radius=0.4, percent=120, threshold=2))
    return out


def main() -> None:
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        print("Pillow 未安装，跳过图标生成")
        return

    master = _render_master(CANVAS)
    _save_png(master, ROOT / "AppIcon.png")

    if ICONSET.exists():
        shutil.rmtree(ICONSET)
    ICONSET.mkdir()

    mapping = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    for size, name in mapping:
        _save_png(_resize_sharp(master, size), ICONSET / name)

    subprocess.run(
        ["iconutil", "-c", "icns", str(ICONSET), "-o", str(OUT)],
        check=True,
    )
    print(f"已生成 {OUT}")


if __name__ == "__main__":
    main()
