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

echo "    Zipping the app bundle…"
( cd "dist/app-$LABEL" \
  && ditto -c -k --sequesterRsrc --keepParent "Idea Recorder.app" "../Idea-Recorder-$LABEL.zip" )

echo
echo "==> Done."
echo "    App:  dist/app-$LABEL/Idea Recorder.app"
echo "    Zip:  dist/Idea-Recorder-$LABEL.zip"
echo
echo "    Confirm the architecture with:"
echo "    lipo -archs 'dist/app-$LABEL/Idea Recorder.app/Contents/MacOS/Idea Recorder'"
