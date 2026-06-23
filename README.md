# Idea Recorder

A safety net for musical ideas. It listens to your audio interface and keeps
the **last 60 seconds** of audio in memory at all times. When you play
something worth keeping and the DAW wasn't recording, press **Enter** — the
last minute is written to a WAV file in `captures/`.

Nothing is written to disk until you ask, so it can run all day without
filling your drive.

**The web UI also gives you:**
- A **per-channel level meter** — one bar per input, so a 16/32/64-channel
  interface shows every input separately (a single "IN" bar for a mono source).
- **Live key & tempo detection** on whatever's coming in, updated a few times a
  second (ported from the Session Prep analyzer; runs server-side on the
  captured interface audio).
- **Tuning vs 440 readout** — measures how many cents the incoming material sits
  from A=440 (and the equivalent A reference, e.g. A≈443.1), so you know exactly
  how far to pitch a beat to line up with a 440 tuner before tracking over it.
- A **standalone live tuner** — pick any one input channel of your interface and
  get a chromatic tuner: nearest note, cents sharp/flat, and a needle against a
  ±50¢ scale, all referenced to A=440. Runs on its own audio stream, independent
  of the recorder.
- A **drag-and-drop file analyzer** ("Key & tempo of a file") — drop any
  WAV/AIFF/FLAC/MP3/OGG and get its key and tempo in a second, using the same
  engine. M4A/AAC (and other formats libsndfile can't open) decode via a
  bundled/auto-fetched ffmpeg fallback — see below.
- A **key map** for uploaded files — a colored timeline showing where the key
  changes, with timestamps. It biases toward the song's overall key and ignores
  brief, unsustained shifts, so passing chords and noisy passages don't show up
  as false changes — only clear, lasting modulations do.
- A **tempo map** alongside it — the same colored-timeline treatment for BPM
  over time. It folds half-/double-time jitter into the overall tempo and merges
  near-identical regions, so only sustained, clearly different tempos register.
- **Continuous record mode** — alongside the rolling buffer, hit *Record
  continuously* to stream everything straight to disk until you stop. Disk
  writes run on a separate thread so the audio path never glitches.
- **Dominant key + tempo in the filename** — when a confident reading is
  available at save time, files are named like
  `idea_2026-06-13_14-22-05_Cs_min_122bpm.wav` (`#` becomes `s`). When the
  detector isn't confident, the tokens are simply omitted.
- A **"Saving to" bar** showing the captures folder with **Change…** (native
  macOS folder picker, remembered across launches) and **Open** buttons, plus
  per-capture **drag-to-Finder/DAW**, **download**, and **reveal in Finder**.

Saved files are standard 24-bit WAVs. A multichannel capture is one WAV with
all inputs preserved as separate channels (drag it into the DAW and split to
mono tracks).

## Easiest: double-click to launch

Double-click **`Idea Recorder.command`** in Finder. It starts the tool and
opens your browser automatically. The first launch sets itself up (takes a
few seconds); after that it's instant. Leave the Terminal window open while
recording — closing it quits the tool.

## Share it with a friend

A ready-to-send bundle is built at **`dist/Idea-Recorder-mac.zip`**. Send that
one file. Your friend unzips it, double-clicks **`Idea Recorder.command`**
inside, and the launcher sets everything up on first run.

- Works on any Mac — Intel or Apple Silicon (it builds itself on their
  machine, so it isn't tied to your hardware).
- Needs Python 3 and an internet connection the *first* time only; the
  launcher downloads a few small audio libraries, then runs offline after.
- macOS blocks `.command` files downloaded from the internet on first open —
  they **right-click → Open** once (then *Open* in the dialog), and it's
  trusted afterward.

To rebuild the bundle after changing the app:

```sh
cd ~/Coding/idea-recorder
cp app.py idea_recorder.py live_analysis.py "dist/Idea Recorder/"
cp static/index.html "dist/Idea Recorder/static/"
cd dist && rm -f Idea-Recorder-mac.zip \
  && zip -r -X "Idea-Recorder-mac.zip" "Idea Recorder" -x "*.DS_Store"
```

## Standalone Mac app (no Python needed)

For friends who shouldn't have to touch a terminal at all, there's a fully
self-contained **`Idea Recorder.app`** — Python and every dependency are
bundled inside, so they just double-click.

### Collaborators on Apple Silicon — just download it

You don't need to build anything. GitHub builds a native **Apple Silicon**
(`arm64`) app for you on every release:

1. Go to the repo's **Actions** tab → **Build macOS app (Apple Silicon)** and
   click **Run workflow** (or push a `v*` tag to also attach it to a Release).
2. When it finishes, download **`Idea-Recorder-AppleSilicon.zip`** — from the
   run's **Artifacts**, or from the **Releases** page for a tagged build.
3. Unzip it. The app is downloaded from the internet, so macOS quarantines it on
   first open. Either **right-click → Open** once (then *Open* in the dialog),
   or clear the quarantine flag in Terminal:

   ```sh
   xattr -dr com.apple.quarantine "Idea Recorder.app"
   ```

That's the whole flow — no Python, no Xcode, no `pip`.

### Building it yourself

PyInstaller can only build for the architecture it runs on, so each Mac chip
needs its own build:

```sh
git lfs install && git lfs pull   # one-time: fetch the vendored ffmpeg binaries
./build-mac-app.sh
```

A static ffmpeg (for M4A/AAC support in the file analyzer) is **committed in the
repo** as `vendor/ffmpeg-x86_64` and `vendor/ffmpeg-arm64` via [Git LFS](https://git-lfs.com),
so the build is fully local — nothing is downloaded from a third-party site. The
script copies the matching arch into the app and aborts if the finished binary
doesn't match the build machine. (Clone without LFS and you'll just get pointer
files — `git lfs pull` fixes it; the app still builds without ffmpeg, it just
won't read M4A/AAC.) To refresh ffmpeg, run `./get-ffmpeg.sh` on each arch and
commit the updated `vendor/ffmpeg-<arch>`.

- Run it on an **Intel** Mac  → `dist/Idea-Recorder-Intel.zip` (x86_64)
- Run it on an **Apple Silicon** Mac → `dist/Idea-Recorder-AppleSilicon.zip` (arm64)

To produce the Apple Silicon build, copy this folder to an M-series Mac and run
the same script there. Each app is ad-hoc signed, so on first open a downloaded
copy needs **right-click → Open** once. Standalone-app captures are saved to
**`~/Music/Idea Recorder/`**.

## Web UI (from the terminal)

```sh
cd ~/Coding/idea-recorder
./ui            # then open http://127.0.0.1:8766
```

Pick your interface, choose a buffer length, hit **Start listening**. The
level meter confirms audio is coming in. Click the big red button (or press
**Space**) to save the most recent buffer; captures appear below with inline
playback and delete. Recording happens in the Python server, so the browser
tab can be closed and reopened without interrupting the buffer.

## Command line

```sh
cd ~/Coding/idea-recorder

# See which inputs are available (plug in your interface first)
./record --list-devices

# Run on the system default input
./record

# Run on your interface, by name substring or index
./record --device "Scarlett"
./record --device 4
```

While it's running:

- **Enter** — save the last 60 seconds to `captures/idea_<date>_<time>.wav`
- **q + Enter** (or Ctrl+C) — quit

You can press Enter repeatedly; each press saves a new snapshot of the most
recent minute.

## Options

| Flag | Default | What it does |
|---|---|---|
| `-d`, `--device` | system default | Input device index or name substring |
| `-s`, `--seconds` | `60` | Length of the rolling buffer |
| `-c`, `--channels` | all on the device | How many input channels to capture |
| `-r`, `--samplerate` | device default | Sample rate in Hz |
| `-o`, `--output-dir` | `./captures` | Where WAV files go |
| `--bit-depth` | `24` | `16`, `24`, or `32f` (32-bit float) |

Examples:

```sh
# 2-minute buffer on inputs 1–2 of the interface, saved to your Music folder
./record -d "Scarlett" -c 2 -s 120 -o ~/Music/IdeaBackups
```

Multi-channel captures are saved as one multi-channel WAV, so every input on
the interface is preserved separately — drag it into the DAW and split it
there if needed.

## Notes

- The first time it runs, macOS will ask for microphone permission for your
  terminal app. Allow it, or every capture will be silence.
- Memory use is modest: a 60 s stereo buffer at 48 kHz is about 23 MB. A
  64-channel buffer at 48 kHz is about 740 MB, so for big interfaces limit
  `--channels` to the inputs you actually use.
- Setup was done with a venv; to recreate it:
  `python3 -m venv .venv && .venv/bin/pip install sounddevice soundfile numpy`
