#!/bin/zsh
# Fetch a static ffmpeg binary for THIS Mac's architecture into ./vendor/ffmpeg.
# Used to decode M4A/AAC (and other formats libsndfile can't open) in the file
# key/tempo analyzer, and bundled into the standalone .app by build-mac-app.sh.
#
# Re-run this on each build machine (Intel and Apple Silicon) before building.
set -e
cd "${0:a:h}"
mkdir -p vendor

ARCH=$(uname -m)
case "$ARCH" in
  x86_64) URL="https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip" ;;
  arm64)  URL="https://www.osxexperts.net/ffmpeg711arm.zip" ;;
  *) echo "Unsupported arch: $ARCH"; exit 1 ;;
esac

echo "==> Downloading static ffmpeg for $ARCH…"
TMP=$(mktemp -d)
curl -sSL --max-time 300 -o "$TMP/ffmpeg.zip" "$URL"
unzip -o -q "$TMP/ffmpeg.zip" -d "$TMP/extract"
BIN=$(find "$TMP/extract" -name ffmpeg -type f -perm -u+x -o -name ffmpeg -type f | head -1)
if [[ -z "$BIN" ]]; then echo "ffmpeg binary not found in download"; exit 1; fi
cp "$BIN" vendor/ffmpeg
chmod +x vendor/ffmpeg
rm -rf "$TMP"

echo "==> vendor/ffmpeg ready:"
./vendor/ffmpeg -version 2>/dev/null | head -1
lipo -archs vendor/ffmpeg 2>/dev/null
