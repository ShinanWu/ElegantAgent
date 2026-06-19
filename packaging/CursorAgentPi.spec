# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — 生成 macOS .app"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

spec_dir = Path(SPEC).resolve().parent
project_root = spec_dir.parent if spec_dir.name == "packaging" else spec_dir
cursor_datas, cursor_binaries, cursor_hidden = collect_all("cursor_sdk")

block_cipher = None

a = Analysis(
    [str(project_root / "launcher.py")],
    pathex=[str(project_root)],
    binaries=cursor_binaries,
    datas=[
        (str(project_root / "public"), "public"),
        *cursor_datas,
    ],
    hiddenimports=[
        "server",
        "server.app",
        "server.agent_manager",
        "server.bridge_env",
        "server.stream_format",
        "server.gui",
        "server.macos_shell",
        "server.shutdown",
        "webview",
        "server.config",
        "server.paths",
        "server.runtime",
        "server.agents",
        "server.agent_workspace",
        "server.prompt_builder",
        "server.discussions",
        "server.discussion_manager",
        "server.desktop_api",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        *cursor_hidden,
        *collect_submodules("uvicorn"),
        *collect_submodules("fastapi"),
        *collect_submodules("starlette"),
        "multipart",
        "python_multipart",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(project_root / "packaging" / "runtime_hook.py")],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CursorAgentPi",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CursorAgentPi",
)

app = BUNDLE(
    coll,
    name="尤雅.app",
    icon=str(project_root / "packaging" / "AppIcon.icns")
    if (project_root / "packaging" / "AppIcon.icns").exists()
    else None,
    bundle_identifier="com.cursor.agent.pi",
    info_plist={
        "CFBundleName": "尤雅",
        "CFBundleDisplayName": "尤雅",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        # PyInstaller 默认会写成 "AppIcon.icns"（带扩展名），导致系统找不到图标
        "CFBundleIconFile": "AppIcon",
        "CFBundleIconName": "AppIcon",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
    },
)
