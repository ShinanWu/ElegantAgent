from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from .paths import config_file


@dataclass
class AppConfig:
    api_key: str = ""
    default_cwd: str = ""
    default_model: str = "composer-2.5"
    host: str = "127.0.0.1"
    port: int = 3847

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key.strip())

    def public_view(self) -> dict[str, Any]:
        return {
            "configured": self.is_configured,
            "defaultCwd": self.default_cwd,
            "defaultModel": self.default_model,
            "host": self.host,
            "port": self.port,
            "hasApiKey": self.is_configured,
        }


def load_config() -> AppConfig:
    path = config_file()
    if not path.is_file():
        return AppConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return AppConfig(
            api_key=str(raw.get("api_key", "")).strip(),
            default_cwd=str(raw.get("default_cwd", "")).strip(),
            default_model=str(raw.get("default_model", "composer-2.5")).strip() or "composer-2.5",
            host=str(raw.get("host", "127.0.0.1")).strip() or "127.0.0.1",
            port=int(raw.get("port", 3847)),
        )
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return AppConfig()


def save_config(config: AppConfig) -> None:
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(config), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass
