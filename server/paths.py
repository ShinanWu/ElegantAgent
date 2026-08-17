from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

_LOGGING_CONFIGURED = False
APP_DIR_NAME = "yoya"


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def app_root() -> Path:
    if is_frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


def resource_root() -> Path:
    return app_root() / "public"


def config_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home())) / APP_DIR_NAME
    else:
        base = Path.home() / ".config" / APP_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def config_file() -> Path:
    return config_dir() / "config.json"


def agents_file() -> Path:
    return config_dir() / "agents.json"


def discussions_file() -> Path:
    return config_dir() / "discussions.json"


def combined_summaries_file() -> Path:
    return config_dir() / "combined_summaries.json"


def log_file() -> Path:
    return config_dir() / "app.log"


def configure_logging() -> None:
    """同时写控制台与 app.log。打包版 console=False 时文件日志是唯一排障途径。"""
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    _LOGGING_CONFIGURED = True

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    has_stream = any(
        isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)
        for handler in root.handlers
    )
    if not has_stream:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)

    try:
        path = log_file()
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
        logging.getLogger(__name__).info("日志写入 %s", path)
    except OSError:
        logging.getLogger(__name__).warning("无法创建日志文件 %s", log_file())


def load_dotenv_if_present() -> None:
    env_path = app_root() / ".env"
    if env_path.is_file():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_path)
        except ImportError:
            pass


def env_or_config(key: str, default: str = "") -> str:
    value = os.environ.get(key, "").strip()
    if value:
        return value
    cfg = config_dir() / "config.json"
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            mapped = {
                "CURSOR_API_KEY": "api_key",
                "DEFAULT_CWD": "default_cwd",
                "DEFAULT_MODEL": "default_model",
                "PORT": "port",
                "HOST": "host",
            }.get(key)
            if mapped and data.get(mapped) is not None:
                return str(data[mapped]).strip()
        except (json.JSONDecodeError, OSError):
            pass
    return default
