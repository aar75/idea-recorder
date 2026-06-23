#!/bin/zsh
# Double-click launcher for Idea Recorder.
# Finder runs this in Terminal. It starts the local server, opens the
# browser, and keeps running until you close the window or press Ctrl+C.

cd "${0:a:h}" || exit 1

PORT="${PORT:-8766}"   # override with: PORT=8770 open "Idea Recorder.command"
PY=".venv/bin/python"

# Self-heal: build the venv the first time, or if it's missing.
if [[ ! -x "$PY" ]]; then
  echo "First run — setting up (this happens once)…"
  if ! command -v python3 >/dev/null 2>&1; then
    echo
    echo "  Python 3 isn't installed. Get it from https://www.python.org/downloads/"
    echo "  then double-click this file again."
    echo
    read "?Press Return to close."
    exit 1
  fi
  python3 -m venv .venv || { read "?Setup failed. Press Return to close."; exit 1; }
  "$PY" -m pip install --quiet --upgrade pip
  if [[ -f requirements.txt ]]; then
    "$PY" -m pip install --quiet -r requirements.txt \
      || { read "?Could not install dependencies. Press Return to close."; exit 1; }
  else
    "$PY" -m pip install --quiet sounddevice soundfile numpy \
      || { read "?Could not install dependencies. Press Return to close."; exit 1; }
  fi
  echo "Setup done."
  echo
fi

# ffmpeg powers M4A/AAC decoding in the file analyzer. The repo ships a vendored
# static binary per arch (via Git LFS), so normally nothing is downloaded. Only
# if it's genuinely missing — e.g. cloned without `git lfs pull`, which leaves a
# tiny pointer stub — fall back to fetching one (best-effort, online once).
ARCH=$(uname -m)
have_ffmpeg=0
for f in vendor/ffmpeg "vendor/ffmpeg-$ARCH"; do
  # A real binary is tens of MB; a Git LFS pointer stub is a few hundred bytes.
  [[ -f "$f" && $(wc -c < "$f") -gt 1000000 ]] && have_ffmpeg=1
done
if [[ "$have_ffmpeg" -eq 0 && -f "get-ffmpeg.sh" ]]; then
  echo "Fetching ffmpeg for M4A/AAC support (one-time, optional)…"
  zsh get-ffmpeg.sh >/dev/null 2>&1 || echo "  (skipped — M4A/AAC analysis won't be available)"
fi

# Open the browser once the server is actually accepting connections.
( for i in {1..40}; do
    if curl -s -o /dev/null "http://127.0.0.1:$PORT/"; then
      open "http://127.0.0.1:$PORT/"
      break
    fi
    sleep 0.25
  done ) &

echo "Idea Recorder is running."
echo "If the browser doesn't open, go to: http://127.0.0.1:$PORT/"
echo "Leave this window open while recording. Press Ctrl+C or close it to quit."
echo

exec "$PY" app.py --port "$PORT"
