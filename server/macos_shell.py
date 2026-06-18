"""macOS 原生应用行为：关窗隐藏、Dock 恢复、Cmd+Q / Dock 退出。"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from webview.window import Window

from .ws_hub import post_shell_visible

logger = logging.getLogger(__name__)


def _on_main_thread(fn: Any) -> None:
    try:
        from Foundation import NSThread
        from PyObjCTools import AppHelper

        if NSThread.isMainThread():
            fn()
        else:
            AppHelper.callAfter(fn)
    except Exception:
        fn()


def _install_cocoa_hooks(shell: "MacAppShell") -> None:
    import AppKit
    import Foundation
    import objc
    from webview.platforms import cocoa

    OriginalWindowDelegate = cocoa.BrowserView.WindowDelegate

    class PatchedWindowDelegate(OriginalWindowDelegate):  # type: ignore[misc, valid-type]
        def windowShouldClose_(self, window: Any) -> bool:
            inst = cocoa.BrowserView.get_instance("window", window)
            if inst is None:
                return Foundation.YES
            if shell.force_quit:
                return Foundation.YES
            shell._hide_cocoa_sync()
            return Foundation.NO

    class AppDelegateBridge(Foundation.NSObject):  # type: ignore[misc, valid-type]
        shell: MacAppShell

        def initWithShell_(self, shell: MacAppShell) -> AppDelegateBridge:
            self = objc.super(AppDelegateBridge, self).init()
            if self is None:
                return None
            self.shell = shell
            return self

        def applicationShouldTerminate_(self, app: Any) -> int:
            # 标准 macOS 退出：直接允许 terminate，不走 pywebview should_close（会死锁）
            self.shell.force_quit = True
            logger.info("应用即将退出")
            return Foundation.YES

        def applicationShouldHandleReopen_hasVisibleWindows_(self, app: Any, flag: bool) -> bool:
            logger.info("Dock 点击，恢复窗口")
            self.shell._show_cocoa_sync()
            return True

        def applicationSupportsSecureRestorableState_(self, app: Any) -> bool:
            return Foundation.YES

    cocoa.BrowserView.WindowDelegate = PatchedWindowDelegate

    bridge = AppDelegateBridge.alloc().initWithShell_(shell)
    bridge.retain()
    shell._delegate_bridge = bridge
    cocoa.BrowserView._shared_app_delegate = bridge
    AppKit.NSApplication.sharedApplication().setDelegate_(bridge)

    shell._patched = True
    logger.info("已安装 macOS 原生窗口钩子")


class MacAppShell:
    """协调 pywebview 在 macOS 上的窗口生命周期。"""

    def __init__(self) -> None:
        self.main_window: Window | None = None
        self.app_url: str = ""
        self.force_quit = False
        self._patched = False
        self._delegate_bridge: Any = None

    def attach_window(self, window: Window, url: str = "") -> None:
        self.main_window = window
        self.app_url = url

    def install_delegate_hooks(self) -> None:
        if self._patched or sys.platform != "darwin":
            return
        try:
            _install_cocoa_hooks(self)
        except Exception:
            logger.exception("安装 macOS 钩子失败")

    def _ensure_app_delegate(self) -> None:
        if sys.platform != "darwin" or self._delegate_bridge is None:
            return
        try:
            import AppKit
            from webview.platforms import cocoa

            cocoa.BrowserView._shared_app_delegate = self._delegate_bridge
            AppKit.NSApplication.sharedApplication().setDelegate_(self._delegate_bridge)
        except Exception:
            logger.exception("刷新 AppDelegate 失败")

    def _rebind_window_delegates(self) -> None:
        """确保每个 NSWindow 使用 PatchedWindowDelegate 实例。"""
        try:
            from webview.platforms import cocoa

            for inst in cocoa.BrowserView.instances.values():
                delegate = cocoa.BrowserView.WindowDelegate.alloc().init()
                inst._windowDelegate = delegate
                inst.window.setDelegate_(delegate)
        except Exception:
            logger.exception("绑定窗口 Delegate 失败")

    def finalize_hooks(self) -> None:
        """GUI 启动完成后在主线程调用。"""
        self.install_delegate_hooks()
        self._ensure_app_delegate()
        self._rebind_window_delegates()
        self._enable_clipboard_access()

    def _enable_clipboard_access(self) -> None:
        """允许 WKWebView 通过 JS 访问剪贴板（右键粘贴 / navigator.clipboard）。"""
        if sys.platform != "darwin":
            return
        try:
            from webview.platforms import cocoa

            for inst in cocoa.BrowserView.instances.values():
                config = inst.webview.configuration()
                prefs = config.preferences()
                prefs.setValue_forKey_(True, "javaScriptCanAccessClipboard")
                try:
                    prefs.setValue_forKey_(True, "DOMPasteAllowed")
                except KeyError:
                    pass
            logger.info("已启用 WebKit 剪贴板访问")
        except Exception:
            logger.exception("启用 WebKit 剪贴板访问失败")

    def show_main_window(self) -> None:
        if sys.platform != "darwin":
            if self.main_window is not None:
                self.main_window.show()
            post_shell_visible(True)
            return
        _on_main_thread(self._show_cocoa_sync)

    def hide_main_window(self) -> None:
        if sys.platform != "darwin":
            if self.main_window is not None:
                self.main_window.hide()
            return
        _on_main_thread(self._hide_cocoa_sync)

    def _hide_cocoa_sync(self) -> None:
        """主线程同步隐藏（标准 macOS：关窗 = 隐藏应用）。"""
        try:
            import AppKit
            from webview.platforms import cocoa

            for inst in cocoa.BrowserView.instances.values():
                inst.window.orderOut_(None)
            AppKit.NSApplication.sharedApplication().hide_(None)
            logger.info("窗口已隐藏")
            post_shell_visible(False)
        except Exception:
            logger.exception("隐藏窗口失败")

    def _show_cocoa_sync(self) -> None:
        """主线程同步显示（仅 Cocoa API，避免与 uvicorn 争锁）。"""
        try:
            import AppKit
            import Foundation
            from webview.platforms import cocoa

            if not cocoa.BrowserView.instances:
                logger.warning("没有可恢复的窗口")
                return

            app = AppKit.NSApplication.sharedApplication()
            app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
            app.unhide_(None)
            app.activateIgnoringOtherApps_(Foundation.YES)

            for inst in cocoa.BrowserView.instances.values():
                inst.window.setLevel_(AppKit.NSNormalWindowLevel)
                inst.window.makeKeyAndOrderFront_(None)

            logger.info("窗口已恢复")
            post_shell_visible(True)
        except Exception:
            logger.exception("恢复窗口失败")

    def quit_application(self) -> None:
        self.force_quit = True
        try:
            import AppKit

            AppKit.NSApplication.sharedApplication().terminate_(None)
        except Exception:
            logger.exception("退出应用失败")
