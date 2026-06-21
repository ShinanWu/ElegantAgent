#!/usr/bin/env python3
"""从 AppIconSource.png 生成 macOS AppIcon.icns、顶栏 logo 与各尺寸 PNG。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
PUBLIC = PROJECT_ROOT / "public"
SOURCE = ROOT / "AppIconSource.png"
OUT = ROOT / "AppIcon.icns"
ICONSET = ROOT / "AppIcon.iconset"
CANVAS = 1024
BG = (255, 255, 255)
TRIM_THRESHOLD = 248
TRIM_PAD_RATIO = 0.015
CANVAS_PADDING_RATIO = 0.035
HEADER_LOGO_SIZES = (80, 160)  # 顶栏 40px CSS，@1x / @2x
HERO_LOGO_SIZE = 384  # README 展示用高清图


def _trim_content(img):
    from PIL import Image

    rgba = img.convert("RGBA")
    w, h = rgba.size
    px = rgba.load()
    min_x, min_y, max_x, max_y = w, h, 0, 0
    found = False
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a < 16:
                continue
            if r >= TRIM_THRESHOLD and g >= TRIM_THRESHOLD and b >= TRIM_THRESHOLD:
                continue
            found = True
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x)
            max_y = max(max_y, y)
    if not found:
        return rgba

    cw = max_x - min_x + 1
    ch = max_y - min_y + 1
    pad = max(6, int(max(cw, ch) * TRIM_PAD_RATIO))
    return rgba.crop(
        (
            max(0, min_x - pad),
            max(0, min_y - pad),
            min(w, max_x + pad + 1),
            min(h, max_y + pad + 1),
        )
    )


def _fit_on_square(img, size: int, padding_ratio: float = CANVAS_PADDING_RATIO):
    from PIL import Image

    canvas = Image.new("RGBA", (size, size), (*BG, 255))
    padding = max(4, int(size * padding_ratio))
    max_w = size - padding * 2
    max_h = size - padding * 2
    sw, sh = img.size
    scale = min(max_w / sw, max_h / sh)
    nw = max(1, int(sw * scale))
    nh = max(1, int(sh * scale))
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    x = (size - nw) // 2
    y = (size - nh) // 2
    canvas.paste(resized, (x, y), resized)
    return canvas


def _compose_master(trimmed, size: int = CANVAS):
    return _fit_on_square(trimmed, size).convert("RGB")


def _save_png(img, path: Path) -> None:
    img.save(path, format="PNG", compress_level=1, optimize=False)


def _resize_icon(master, size: int):
    from PIL import Image

    return master.resize((size, size), Image.Resampling.LANCZOS)


def _export_header_logos(trimmed) -> None:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    one_x, two_x = HEADER_LOGO_SIZES
    _save_png(_fit_on_square(trimmed, one_x), PUBLIC / "app-logo.png")
    _save_png(_fit_on_square(trimmed, two_x), PUBLIC / "app-logo@2x.png")
    _save_png(_fit_on_square(trimmed, HERO_LOGO_SIZE), PUBLIC / "app-logo-hero.png")


def main() -> None:
    try:
        from PIL import Image
    except ImportError:
        print("Pillow 未安装，跳过图标生成")
        return

    if not SOURCE.is_file():
        raise FileNotFoundError(f"缺少图标源文件: {SOURCE}")

    trimmed = _trim_content(Image.open(SOURCE))
    print(f"源图 {Image.open(SOURCE).size} → 裁切后 {trimmed.size}")

    master = _compose_master(trimmed, CANVAS)
    _save_png(master, ROOT / "AppIcon.png")
    _export_header_logos(trimmed)

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
        _save_png(_resize_icon(master, size), ICONSET / name)

    subprocess.run(
        ["iconutil", "-c", "icns", str(ICONSET), "-o", str(OUT)],
        check=True,
    )
    print(f"已生成 {OUT}")
    print(f"已生成顶栏 logo: {PUBLIC / 'app-logo.png'} ({HEADER_LOGO_SIZES[0]}px)")


if __name__ == "__main__":
    main()
