from __future__ import annotations

import json
import os
import sys
from pathlib import Path


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
        base = Path.home() / "Library" / "Application Support" / "CursorAgentPi"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home())) / "CursorAgentPi"
    else:
        base = Path.home() / ".config" / "cursor-agent-pi"
    base.mkdir(parents=True, exist_ok=True)
    return base


def config_file() -> Path:
    return config_dir() / "config.json"


def agents_file() -> Path:
    return config_dir() / "agents.json"


def discussions_file() -> Path:
    return config_dir() / "discussions.json"


def log_file() -> Path:
    return config_dir() / "app.log"


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
