#!/bin/zsh
# Build a standalone "Idea Recorder.app" for THIS Mac's architecture.
#
#   - Run it on an Apple Silicon Mac  -> arm64 build  (dist/Idea-Recorder-AppleSilicon.zip)
#   - Run it on an Intel Mac          -> x86_64 build (dist/Idea-Recorder-Intel.zip)
#
# PyInstaller can only build for the architecture it runs on, because it
# bundles the actual compiled libraries (numpy, PortAudio, libsndfile). So to
# get the Apple Silicon version, copy this whole folder to an M-series Mac and
# run:   ./build-mac-app.sh
#
# Requirements on the build machine: Python 3 and an internet connection.

set -e
cd "${0:a:h}"

ARCH=$(uname -m)
case "$ARCH" in
  arm64)  LABEL="AppleSilicon" ;;
  x86_64) LABEL="Intel" ;;
  *)      LABEL="$ARCH" ;;
esac

echo "==> Building Idea Recorder for $ARCH ($LABEL)"

# Bundle the matching ffmpeg from the per-arch binaries committed in vendor/
# (via Git LFS) — no network needed, so the build stays fully local. The spec
# bundles vendor/ffmpeg, so stage the right arch there.
SRC="vendor/ffmpeg-$ARCH"
if [[ -f "$SRC" ]] && lipo -archs "$SRC" 2>/dev/null | grep -q "$ARCH"; then
  echo "    Using vendored ffmpeg ($ARCH)."
  cp "$SRC" vendor/ffmpeg
  chmod +x vendor/ffmpeg
elif [[ -f "$SRC" ]]; then
  # File exists but isn't a real binary — almost always an un-pulled Git LFS
  # pointer. Tell the user how to fix it rather than bundling a broken ffmpeg.
  echo "!!  $SRC is a Git LFS pointer, not the binary." >&2
  echo "    Run 'git lfs install && git lfs pull' to fetch it, then rebuild." >&2
  echo "    Building without M4A/AAC support for now." >&2
  rm -f vendor/ffmpeg
else
  echo "    No vendored ffmpeg for $ARCH — building without M4A/AAC support."
  echo "    (Run ./get-ffmpeg.sh to add it to vendor/, then rebuild.)"
  rm -f vendor/ffmpeg
fi

VENV=".build-venv"
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "    Creating build environment…"
  python3 -m venv "$VENV"
fi
echo "    Installing dependencies…"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r requirements.txt pyinstaller

echo "    Running PyInstaller…"
rm -rf "build/$LABEL" "dist/app-$LABEL"
"$VENV/bin/pyinstaller" --noconfirm \
  --distpath "dist/app-$LABEL" \
  --workpath "build/$LABEL" \
  idea_recorder.spec

# Fail loudly if PyInstaller somehow produced a binary for the wrong arch —
# the whole point of running this on an M-series Mac is an arm64 app.
BIN="dist/app-$LABEL/Idea Recorder.app/Contents/MacOS/Idea Recorder"
BUILT_ARCH=$(lipo -archs "$BIN" 2>/dev/null || true)
echo "    Built binary architecture: ${BUILT_ARCH:-unknown}"
if [[ "$BUILT_ARCH" != *"$ARCH"* ]]; then
  echo "!!  Built binary ($BUILT_ARCH) does not match this Mac ($ARCH); aborting." >&2
  exit 1
fi

echo "    Zipping the app bundle…"
( cd "dist/app-$LABEL" \
  && ditto -c -k --sequesterRsrc --keepParent "Idea Recorder.app" "../Idea-Recorder-$LABEL.zip" )

echo
echo "==> Done."
echo "    App:  dist/app-$LABEL/Idea Recorder.app  ($BUILT_ARCH)"
echo "    Zip:  dist/Idea-Recorder-$LABEL.zip"
