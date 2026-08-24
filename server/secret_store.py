from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SERVICE_NAME = "com.shinanwu.yoya"
CURSOR_API_KEY_ACCOUNT = "cursor-api-key"


def load_cursor_api_key() -> str:
    try:
        import keyring

        return str(keyring.get_password(SERVICE_NAME, CURSOR_API_KEY_ACCOUNT) or "").strip()
    except Exception:
        logger.warning("无法从系统钥匙串读取 Cursor API Key", exc_info=True)
        return ""


def save_cursor_api_key(value: str) -> bool:
    secret = str(value or "").strip()
    if not secret:
        return False
    try:
        import keyring

        keyring.set_password(SERVICE_NAME, CURSOR_API_KEY_ACCOUNT, secret)
        return True
    except Exception:
        logger.warning("无法将 Cursor API Key 写入系统钥匙串", exc_info=True)
        return False
