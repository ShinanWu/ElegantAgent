from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .json_store import load_json_object, save_json_object
from .paths import config_file
from .secret_store import load_cursor_api_key, save_cursor_api_key


def _restrict_config_file(path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


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
    raw = load_json_object(path, label="应用配置")
    legacy_key = str(raw.get("api_key", "")).strip()
    api_key = load_cursor_api_key() or legacy_key

    # 旧版本把 API Key 写在 config.json。迁移成功后立即移除明文。
    if legacy_key and save_cursor_api_key(legacy_key):
        raw.pop("api_key", None)
        save_json_object(path, raw)
        _restrict_config_file(path)

    try:
        return AppConfig(
            api_key=api_key,
            default_cwd=str(raw.get("default_cwd", "")).strip(),
            default_model=str(raw.get("default_model", "composer-2.5")).strip() or "composer-2.5",
            # 桌面应用没有远程访问场景，固定回环地址，避免旧配置意外暴露服务。
            host="127.0.0.1",
            port=int(raw.get("port", 3847)),
        )
    except (TypeError, ValueError):
        return AppConfig(api_key=api_key)


def save_config(config: AppConfig) -> None:
    path = config_file()
    payload = asdict(config)
    secret = str(payload.pop("api_key", "") or "").strip()
    if secret and not save_cursor_api_key(secret):
        # 非 macOS 开发环境或钥匙串不可用时的最小权限回退。
        payload["api_key"] = secret
    save_json_object(path, payload)
    _restrict_config_file(path)
