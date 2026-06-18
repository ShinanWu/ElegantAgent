"""pywebview 暴露给前端的桌面能力（目录选择、拖放路径等）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

AGENT_PI_CLIPBOARD = "application/x-cursor-agent-pi-attachments"


def enable_drop_path_capture() -> None:
    """让 pywebview 在系统拖放时记录完整路径（含文件夹）。"""
    try:
        from webview.dom import _dnd_state

        if _dnd_state["num_listeners"] <= 0:
            _dnd_state["num_listeners"] = 1
    except Exception:
        pass


class DesktopApi:
    def pick_folder(self, initial_dir: str = "") -> str:
        """打开系统文件夹选择对话框，返回所选路径或空字符串。"""
        try:
            import webview
        except ImportError:
            return ""

        windows = webview.windows
        if not windows:
            return ""

        directory = str(Path(initial_dir).expanduser()) if initial_dir else str(Path.home())
        if not Path(directory).is_dir():
            directory = str(Path.home())

        result = windows[0].create_file_dialog(
            webview.FOLDER_DIALOG,
            directory=directory,
        )
        if result and len(result) > 0:
            return str(result[0])
        return ""

    def consume_dropped_paths(self) -> list[dict[str, str]]:
        """读取并清空 pywebview 捕获的拖放路径（文件或文件夹）。"""
        try:
            from webview.dom import _dnd_state
        except ImportError:
            return []

        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for _name, raw_path in list(_dnd_state.get("paths", [])):
            path = Path(os.path.expanduser(str(raw_path))).resolve()
            key = str(path)
            if key in seen:
                continue
            if not path.exists():
                continue
            seen.add(key)
            kind = "directory" if path.is_dir() else "file"
            items.append(
                {
                    "name": path.name + ("/" if kind == "directory" else ""),
                    "path": key,
                    "kind": kind,
                }
            )
        _dnd_state["paths"] = []
        return items

    def read_clipboard(self) -> dict[str, str]:
        """读取系统剪贴板（供右键粘贴等无法使用 navigator.clipboard 的场景）。"""
        if sys.platform == "darwin":
            return self._read_clipboard_macos()
        return self._read_clipboard_fallback()

    def _read_clipboard_macos(self) -> dict[str, str]:
        try:
            from AppKit import NSPasteboard

            pb = NSPasteboard.generalPasteboard()
            types = pb.types() or []
            text = pb.stringForType_("public.utf8-plain-text") or pb.stringForType_("NSStringPboardType") or ""
            html = pb.stringForType_("public.html") or pb.stringForType_("NSHTMLPboardType") or ""
            custom = ""
            for uti in (AGENT_PI_CLIPBOARD,):
                if uti in types:
                    value = pb.stringForType_(uti)
                    if value:
                        custom = str(value)
                        break
            if not custom:
                for uti in types:
                    if "cursor-agent-pi" in str(uti).lower():
                        value = pb.stringForType_(uti)
                        if value:
                            custom = str(value)
                            break
            return {"text": text or "", "html": html or "", "custom": custom or ""}
        except Exception:
            return {"text": "", "html": "", "custom": ""}

    def _read_clipboard_fallback(self) -> dict[str, str]:
        try:
            import subprocess

            proc = subprocess.run(
                ["pbpaste"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            text = proc.stdout if proc.returncode == 0 else ""
            return {"text": text or "", "html": "", "custom": ""}
        except Exception:
            return {"text": "", "html": "", "custom": ""}

    def open_external_url(self, url: str) -> bool:
        """在系统默认浏览器中打开链接。"""
        target = str(url or "").strip()
        if not target.startswith(("http://", "https://")):
            return False
        try:
            import webbrowser

            return webbrowser.open(target, new=2)
        except Exception:
            return False

    def pick_paths(self, initial_dir: str = "") -> list[dict[str, str]]:
        """通过系统对话框选择多个文件（引用路径，不上传）。"""
        try:
            import webview
        except ImportError:
            return []

        windows = webview.windows
        if not windows:
            return []

        directory = str(Path(initial_dir).expanduser()) if initial_dir else str(Path.home())
        if not Path(directory).is_dir():
            directory = str(Path.home())

        result = windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            directory=directory,
            allow_multiple=True,
        )
        if not result:
            return []
        if isinstance(result, str):
            result = [result]

        items: list[dict[str, str]] = []
        for raw in result:
            path = Path(str(raw)).expanduser().resolve()
            if not path.is_file():
                continue
            items.append({"name": path.name, "path": str(path), "kind": "file"})
        return items
