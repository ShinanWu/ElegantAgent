from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    """读取 JSON 对象；损坏或结构错误时让应用以空数据继续启动。"""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("顶层必须是 JSON 对象")
        return raw
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as err:
        logger.error("读取 %s 失败，忽略损坏数据 path=%s error=%s", label, path, err)
        return {}


def save_json_object(path: Path, payload: dict[str, Any]) -> None:
    """同目录临时文件 + replace，避免中断写入留下半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
