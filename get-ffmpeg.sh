#!/bin/zsh
# Refresh the vendored static ffmpeg for THIS Mac's architecture, written to
# vendor/ffmpeg-<arch>. These per-arch binaries are committed via Git LFS so
# normal builds stay fully local — you only need this script to *update* the
# bundled ffmpeg (run it once per arch and commit the result).
#
# Used to decode M4A/AAC (and other formats libsndfile can't open) in the file
# key/tempo analyzer; build-mac-app.sh copies the matching one into the app.
set -e
cd "${0:a:h}"
mkdir -p vendor

ARCH=$(uname -m)
case "$ARCH" in
  x86_64) URL="https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip" ;;
  arm64)  URL="https://www.osxexperts.net/ffmpeg711arm.zip" ;;
  *) echo "Unsupported arch: $ARCH"; exit 1 ;;
esac
OUT="vendor/ffmpeg-$ARCH"

echo "==> Downloading static ffmpeg for $ARCH…"
TMP=$(mktemp -d)
curl -sSL --max-time 300 -o "$TMP/ffmpeg.zip" "$URL"
unzip -o -q "$TMP/ffmpeg.zip" -d "$TMP/extract"
BIN=$(find "$TMP/extract" -name ffmpeg -type f -perm -u+x -o -name ffmpeg -type f | head -1)
if [[ -z "$BIN" ]]; then echo "ffmpeg binary not found in download"; exit 1; fi
cp "$BIN" "$OUT"
chmod +x "$OUT"
rm -rf "$TMP"

echo "==> $OUT ready:"
"./$OUT" -version 2>/dev/null | head -1
lipo -archs "$OUT" 2>/dev/null
echo "    Commit it (tracked via Git LFS) to update the bundled ffmpeg."
