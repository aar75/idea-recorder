#!/usr/bin/env python3
"""Standalone always-on-top UI for Idea Recorder (pywebview).

Boots the same local server as the web dashboard, then opens a frameless,
always-on-top native window — the capture strip by default (`--widget` for the
floating widget). The full dashboard (settings, captures, tuner, file analyzer)
opens from the gear / green-dot button via the JS bridge.

Camera (getUserMedia) inside WKWebView needs three things:
  1. NSCameraUsageDescription in the .app Info.plist (set by the build / spec) —
     without it macOS SIGABRTs the process the instant it touches the camera.
  2. The WKUIDelegate media-capture permission granted — done by subclassing
     pywebview's delegate (a dynamically-added method can't marshal the block).
  3. WebKit's (private) media prefs enabled so navigator.mediaDevices exists.
The page is served from http://127.0.0.1, which counts as a secure context.
"""
import argparse
import threading
from http.server import ThreadingHTTPServer

import webview

import app as ir_app

DEFAULT_PORT = 8766


def _patch_cocoa_for_camera():
    """macOS only: make getUserMedia work inside pywebview's WKWebView."""
    from webview.platforms import cocoa

    # (2) Grant the media-capture permission. Defined on a *subclass* so pyobjc
    # uses WebKit's metadata for the decisionHandler block.
    class MediaDelegate(cocoa.BrowserView.BrowserDelegate):
        def webView_requestMediaCapturePermissionForOrigin_initiatedByFrame_type_decisionHandler_(
                self, webView, origin, frame, type_, handler):
            handler(1)  # WKPermissionDecisionGrant

    cocoa.BrowserView.BrowserDelegate = MediaDelegate

    # (3) Enable the WebKit media prefs before each window's first load.
    _orig_first_show = cocoa.BrowserView.first_show

    def first_show(self):
        try:
            prefs = self.webview.configuration().preferences()
            # Only the keys that expose the real getUserMedia pipeline. NOT
            # mockCaptureDevicesEnabled — that swaps in WebKit's synthetic test
            # camera and hides the user's actual devices.
            for key in ("mediaDevicesEnabled", "mediaStreamEnabled"):
                try:
                    prefs.setValue_forKey_(True, key)
                except Exception:
                    pass
        except Exception:
            pass
        return _orig_first_show(self)

    cocoa.BrowserView.first_show = first_show

    # Re-show the strip when the user clicks the Dock icon, so hiding the whole
    # strip from settings can never leave the app stuck with nothing on screen.
    class IRAppDelegate(cocoa.BrowserView.AppDelegate):   # unique ObjC class name
        def applicationShouldHandleReopen_hasVisibleWindows_(self, app, flag):
            for w in webview.windows:
                if w.title == "Idea Recorder":
                    try:
                        w.show()
                    except Exception:
                        pass
            return True

    cocoa.BrowserView.AppDelegate = IRAppDelegate


def start_server(port):
    """Run the existing app.py request handler in a background thread."""
    ir_app.load_config()
    ir_app.CAPTURES.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), ir_app.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def screen_size():
    """Main-screen pixel size, so the strip can span the top of the display."""
    try:
        from AppKit import NSScreen
        f = NSScreen.mainScreen().frame()
        return int(f.size.width), int(f.size.height)
    except Exception:
        return 1440, 900


class Bridge:
    """JS API exposed to the strip / widget windows."""

    def __init__(self, port):
        self.port = port

    def open_dashboard(self):
        for w in webview.windows:
            if w.title == "The Dashboard":
                return
        # Open well below the always-on-top strip so the window's title bar (and
        # its close button) isn't hidden underneath it. ?panel=strip puts the
        # dashboard in settings-only mode: it does NOT grab its own camera (the
        # strip is the sole live capture), and shows an in-page Close button.
        sw, sh = screen_size()
        webview.create_window("The Dashboard",
                              url=f"http://127.0.0.1:{self.port}/?panel=strip",
                              width=720, height=min(900, sh - 200),
                              x=max(0, (sw - 720) // 2), y=160,
                              min_size=(560, 600), js_api=self)

    def close_dashboard(self):
        for w in webview.windows:
            if w.title == "The Dashboard":
                w.destroy()
                return

    def set_strip_visible(self, visible):
        # Hide/show the entire strip window (toggled from settings). The page keeps
        # running while hidden, so capture continues; the Dock icon brings it back.
        for w in webview.windows:
            if w.title == "Idea Recorder":
                try:
                    w.show() if visible else w.hide()
                except Exception:
                    pass
                return

    def resize_strip(self, height):
        # Grow/shrink the always-on-top strip to show more camera, keeping it
        # pinned full-width at the top of the screen.
        sw, _ = screen_size()
        for w in webview.windows:
            if w.title == "Idea Recorder":
                try:
                    w.resize(sw, int(height))
                    w.move(0, 0)
                except Exception:
                    pass
                return

    def close_window(self):
        # Quit from the widget's red dot: tear down the primary window.
        if webview.windows:
            webview.windows[0].destroy()


def main():
    parser = argparse.ArgumentParser(description="Idea Recorder standalone UI")
    parser.add_argument("-p", "--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--widget", action="store_true",
                        help="open the floating widget instead of the strip")
    parser.add_argument("--dashboard", action="store_true",
                        help="open the full web dashboard window")
    args = parser.parse_args()

    start_server(args.port)
    bridge = Bridge(args.port)
    base = f"http://127.0.0.1:{args.port}"
    sw, sh = screen_size()

    if args.dashboard:
        webview.create_window("The Dashboard", url=base + "/",
                              width=720, height=920, min_size=(560, 600))
    elif args.widget:
        webview.create_window("Idea Recorder", url=base + "/widget",
                              width=340, height=360, x=max(0, sw - 364), y=44,
                              frameless=True, easy_drag=True, on_top=True,
                              resizable=False, js_api=bridge)
    else:
        # The strip is OFF by default: create it hidden so capture is wired up,
        # then open the settings menu on startup. The user clicks the strip on
        # from there ("Show the on-screen strip"), which calls set_strip_visible.
        webview.create_window("Idea Recorder", url=base + "/strip",
                              width=sw, height=116, x=0, y=0,
                              frameless=True, easy_drag=True, on_top=True,
                              resizable=False, js_api=bridge, hidden=True)
        webview.create_window("The Dashboard",
                              url=base + "/?panel=strip",
                              width=720, height=min(900, sh - 200),
                              x=max(0, (sw - 720) // 2), y=160,
                              min_size=(560, 600), js_api=bridge)

    _patch_cocoa_for_camera()
    webview.start()


if __name__ == "__main__":
    main()
