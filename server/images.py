from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_EDGE = 1920
MAX_BYTES = 1_200_000


def prepare_image_for_sdk(path: Path) -> Path:
    """过大的图片会让 local agent 长时间停在 RUNNING。缩到可发送的尺寸后再读入 SDK。"""
    if not path.is_file():
        return path
    size = path.stat().st_size
    width, height = _image_size(path)
    if size <= MAX_BYTES and (width == 0 or max(width, height) <= MAX_EDGE):
        return path
    prepared = _downscale(path)
    if prepared is None:
        logger.warning("图片缩小失败，使用原图: %s (%s bytes, %sx%s)", path, size, width, height)
        return path
    logger.info(
        "已缩小图片 %s → %s (%s bytes → %s bytes)",
        path.name,
        prepared.name,
        size,
        prepared.stat().st_size,
    )
    return prepared


def _image_size(path: Path) -> tuple[int, int]:
    try:
        result = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0, 0
    width = height = 0
    for line in result.stdout.splitlines():
        if "pixelWidth:" in line:
            width = _parse_int(line)
        elif "pixelHeight:" in line:
            height = _parse_int(line)
    return width, height


def _parse_int(line: str) -> int:
    try:
        return int(line.rsplit(":", 1)[-1].strip())
    except ValueError:
        return 0


def _downscale(path: Path) -> Path | None:
    suffix = path.suffix.lower() if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"
    try:
        handle = tempfile.NamedTemporaryFile(prefix="elegant-img-", suffix=suffix, delete=False)
        dest = Path(handle.name)
        handle.close()
        result = subprocess.run(
            [
                "sips",
                "-Z",
                str(MAX_EDGE),
                "-s",
                "format",
                "jpeg" if suffix in {".jpg", ".jpeg"} else suffix.lstrip("."),
                str(path),
                "--out",
                str(dest),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        return None
    return dest
