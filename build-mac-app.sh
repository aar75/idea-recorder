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

# Make sure the vendored ffmpeg matches THIS machine's architecture. Copying the
# project folder between an Intel and an Apple Silicon Mac leaves a wrong-arch
# binary behind, which would bundle a non-runnable ffmpeg into the app (M4A/AAC
# decode silently breaks). Fetch the right one when it's missing or mismatched.
if [[ -x get-ffmpeg.sh ]]; then
  FF_ARCH=$(lipo -archs vendor/ffmpeg 2>/dev/null || true)
  if [[ ! -f vendor/ffmpeg ]]; then
    echo "    No vendored ffmpeg — fetching for $ARCH…"
    ./get-ffmpeg.sh || echo "    (ffmpeg fetch failed; app will still build without M4A/AAC support)"
  elif [[ "$FF_ARCH" != *"$ARCH"* ]]; then
    echo "    Vendored ffmpeg is '$FF_ARCH', need '$ARCH' — refetching…"
    rm -f vendor/ffmpeg
    ./get-ffmpeg.sh || echo "    (ffmpeg fetch failed; app will still build without M4A/AAC support)"
  else
    echo "    Vendored ffmpeg matches $ARCH."
  fi
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
