# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Idea Recorder macOS app.
# Build:  pyinstaller --noconfirm idea_recorder.spec
import os
from PyInstaller.utils.hooks import collect_all

# Pull in the compiled audio libraries (PortAudio via sounddevice,
# libsndfile via soundfile), the pywebview backend, and their data files /
# hidden imports.
datas, binaries, hiddenimports = [], [], []
for pkg in ("sounddevice", "soundfile", "webview"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# pywebview's macOS backend loads these dynamically.
hiddenimports += [
    "webview.platforms.cocoa", "objc", "Foundation", "AppKit", "WebKit",
    "Quartz", "Security", "PyObjCTools", "PyObjCTools.AppHelper",
]

# The web UI's static files ship inside the bundle.
datas += [("static", "static")]

# Bundle a vendored ffmpeg (for M4A/AAC and other formats libsndfile can't
# read) when present. Run ./get-ffmpeg.sh first to fetch it for this arch.
if os.path.exists("vendor/ffmpeg"):
    binaries += [("vendor/ffmpeg", ".")]

a = Analysis(
    # desktop.py is the entry: it boots the local server and opens the native
    # always-on-top strip. It imports app.py, so the dashboard ships too.
    ["desktop.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + ["numpy"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PyInstaller"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Idea Recorder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed app — no terminal
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Idea Recorder",
)

app = BUNDLE(
    coll,
    name="Idea Recorder.app",
    icon=None,
    bundle_identifier="com.arjunrao.idearecorder",
    info_plist={
        "CFBundleName": "Idea Recorder",
        "CFBundleDisplayName": "Idea Recorder",
        "CFBundleShortVersionString": "1.6.0",
        "CFBundleVersion": "1.6.0",
        # Built on Apple Silicon → arm64 app; on Intel → x86_64. Both run on
        # macOS 11+; arm64 binaries are required (not just preferred) on M-series.
        "LSMinimumSystemVersion": "11.0",
        "NSMicrophoneUsageDescription":
            "Idea Recorder listens to your audio interface to keep a rolling "
            "backup of anything you play.",
        # Required: without it macOS aborts the app the instant the strip's
        # camera buffer touches the webcam (getUserMedia inside WKWebView).
        "NSCameraUsageDescription":
            "Idea Recorder rolls a companion video buffer alongside your audio "
            "so a matching video can be saved with each capture.",
        "NSHighResolutionCapable": True,
        # Shows in the Dock with a menu bar so it can be quit with Cmd-Q.
        "LSUIElement": False,
    },
)
