#!/usr/bin/env python3
"""Web UI for the rolling idea recorder.

Runs a local server (default http://127.0.0.1:8766) with start/stop controls,
a live input meter, one-click saving of the rolling buffer, and playback of
saved captures. Audio capture happens in this process via sounddevice; the
browser is only the control surface.
"""

import argparse
import datetime
import io
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

from idea_recorder import RingBuffer
from live_analysis import LiveAnalyzer, detect_pitch, note_from_freq

FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    # Inside a PyInstaller .app: bundled files live in the temp _MEIPASS dir,
    # which is read-only, so captures must go to a user-writable location.
    RES = Path(sys._MEIPASS)
    CAPTURES = Path.home() / "Music" / "Idea Recorder"
else:
    RES = Path(__file__).resolve().parent
    CAPTURES = RES / "captures"
STATIC = RES / "static"
# Capture files we'll serve/delete: the hi-fi WAV plus an optional companion
# video recorded in the browser and uploaded on save.
SAFE_NAME = re.compile(r"^[\w.-]+\.(wav|webm|mp4)$")
VIDEO_TYPES = {".webm": "video/webm", ".mp4": "video/mp4"}
CONFIG = Path.home() / ".idea_recorder.json"


def capture_ctype(name):
    """Content-Type for a capture file, by extension."""
    return VIDEO_TYPES.get(Path(name).suffix.lower(), "audio/wav")


def load_config():
    """Restore a previously chosen captures folder, if any."""
    global CAPTURES
    try:
        saved = json.loads(CONFIG.read_text()).get("captures_dir")
        if saved:
            CAPTURES = Path(saved)
    except Exception:
        pass


def set_captures_dir(path):
    """Point captures at a new folder, creating it and persisting the choice."""
    global CAPTURES
    p = Path(path).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    CAPTURES = p
    try:
        CONFIG.write_text(json.dumps({"captures_dir": str(CAPTURES)}))
    except Exception:
        pass
    return CAPTURES


def key_token(name):
    """'C# minor' -> 'Cs_min' for use in a filename ('#' isn't URL-safe)."""
    parts = name.split(" ")
    if len(parts) != 2:
        return ""
    tonic, mode = parts
    return tonic.replace("#", "s") + "_" + ("maj" if mode == "major" else "min")


def find_ffmpeg():
    """Locate an ffmpeg binary: bundled with the app, on PATH, or in Homebrew.

    Finder-launched apps get a minimal PATH that misses Homebrew, so the
    common install locations are checked explicitly.
    """
    src_vendor = Path(__file__).resolve().parent / "vendor"
    candidates = [
        RES / "ffmpeg",                       # bundled inside the .app
        src_vendor / "ffmpeg",                # generated working copy next to source
        src_vendor / f"ffmpeg-{platform.machine()}",  # committed per-arch binary (Git LFS)
    ]
    on_path = shutil.which("ffmpeg")
    if on_path:
        candidates.append(Path(on_path))
    candidates += [Path("/opt/homebrew/bin/ffmpeg"), Path("/usr/local/bin/ffmpeg")]
    for c in candidates:
        if c and c.exists() and os.access(c, os.X_OK):
            return str(c)
    return None


def _decode_with_ffmpeg(data):
    """Fallback decode (anything ffmpeg reads: M4A/AAC, WMA, odd WAVs, …).

    Returns (mono float32 @ 44.1k, sr). M4A/MP4 need a seekable input, so the
    upload is written to a temp file rather than piped on stdin.
    """
    ff = find_ffmpeg()
    if not ff:
        raise ValueError("this format needs ffmpeg, which isn't installed")
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        out = subprocess.run(
            [ff, "-v", "error", "-i", path, "-f", "f32le", "-ac", "1", "-ar", "44100", "pipe:1"],
            capture_output=True)
        if out.returncode != 0 or not out.stdout:
            raise ValueError("could not decode this file")
        return np.frombuffer(out.stdout, dtype="<f4").astype(np.float32), 44100
    finally:
        os.unlink(path)


def build_companion_video(segments, ext, wav_path, channels):
    """Assemble the browser's rolling video buffer into one capture file.

    `segments` are self-contained clips recorded back-to-back in the browser
    (rotating the recorder keeps each one valid, unlike slicing a single
    stream — which is why a naive buffer played back as ~1 second). They're
    concatenated with ffmpeg's concat demuxer, which recomputes timestamps
    into a single correctly-timed clip. When `channels` is non-empty and a WAV
    is given, those channels are downmixed to stereo and muxed in as the audio
    track. Returns the finished file's bytes.
    """
    ff = find_ffmpeg()
    if not ff:
        raise ValueError("Saving video needs ffmpeg, which wasn't found.")
    tmp = Path(tempfile.mkdtemp(prefix="ir_vid_"))
    try:
        seg_paths = []
        for i, blob in enumerate(segments):
            p = tmp / f"seg{i:04d}.{ext}"
            p.write_bytes(blob)
            seg_paths.append(p)

        # 1) Concatenate the segments into one valid, correctly-timed clip.
        concat = tmp / f"concat.{ext}"
        if len(seg_paths) == 1:
            concat = seg_paths[0]
        else:
            listing = tmp / "list.txt"
            listing.write_text("".join(f"file '{p.name}'\n" for p in seg_paths))
            run = subprocess.run(
                [ff, "-v", "error", "-f", "concat", "-safe", "0",
                 "-i", str(listing), "-c", "copy", str(concat)],
                cwd=str(tmp), capture_output=True)
            if run.returncode != 0 or not concat.exists():
                raise ValueError("Couldn't assemble the video segments.")

        # 2) Optionally fold the chosen WAV channels in as a stereo audio track.
        if channels and wav_path:
            n = len(channels)
            expr = "+".join(f"{1.0 / n:.6f}*c{c}" for c in channels)
            # A look-ahead limiter caps the bundled audio so it can't clip — a hot
            # channel sitting near 0 dBFS would otherwise push the lossy encoder
            # over full scale (AAC inter-sample peaks run ~1 dB hot). A 0.8 ceiling
            # (~-1.9 dBFS) leaves enough headroom for both AAC and Opus to stay
            # under 0 after encoding; level=0 keeps it from re-normalising back up.
            afilter = (f"[1:a]pan=stereo|c0={expr}|c1={expr},"
                       f"alimiter=limit=0.8:level=0[a]")
            acodec = (["-c:a", "libopus", "-b:a", "128k"] if ext == "webm"
                      else ["-c:a", "aac", "-b:a", "192k"])
            muxed = tmp / f"out.{ext}"
            run = subprocess.run(
                [ff, "-v", "error", "-i", str(concat), "-i", wav_path,
                 "-filter_complex", afilter,
                 "-map", "0:v:0", "-map", "[a]",
                 "-c:v", "copy", *acodec, "-shortest", str(muxed)],
                capture_output=True)
            if run.returncode == 0 and muxed.exists():
                return muxed.read_bytes()
            # Muxing failed (e.g. codec) — keep the silent video rather than lose it.
        return concat.read_bytes()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def analyze_audio_file(data):
    """Decode an uploaded audio file and return a quick key + tempo estimate."""
    try:
        audio, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
        mono = audio.mean(axis=1).astype(np.float32)
        channels = int(audio.shape[1])
    except Exception:
        mono, sr = _decode_with_ffmpeg(data)   # M4A/AAC and anything libsndfile can't open
        channels = 1
    duration = len(mono) / sr if sr else 0
    if len(mono) < sr * 3:
        raise ValueError("clip is shorter than 3 seconds")
    # Analyse up to the first two minutes — plenty for a stable read, and
    # keeps a long track from blocking the request.
    seg = mono[:int(sr * 120)]
    analyzer = LiveAnalyzer()
    # The key map covers the whole song (capped at 10 min to bound compute) so
    # later modulations aren't missed; the headline key/tempo use the first 2 min.
    return {
        "duration": round(duration, 1),
        "samplerate": int(sr),
        "channels": channels,
        "bpm": analyzer.estimate_bpm(seg, sr),
        "key": analyzer.estimate_key(seg, sr),
        "tuning": analyzer.estimate_tuning(seg, sr),
        "key_map": analyzer.key_map(mono[:int(sr * 600)], sr),
        "tempo_map": analyzer.tempo_map(mono[:int(sr * 600)], sr),
    }


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.stream = None
        self.ring = None
        self.peak = 0.0
        self.peaks = []          # per-channel peak amplitudes
        self.device_name = None
        self.samplerate = None
        self.channels = None
        self.seconds = None
        self.last_status = ""
        self.analysis = {}       # latest live key/tempo readout
        self._analysis_stop = None
        # continuous "record everything" mode
        self.writer = None
        self.write_queue = None
        self.writer_thread = None
        self.record_path = None
        self.record_frames = 0

    def start(self, device, seconds, channels):
        with self.lock:
            self._stop_locked()
            info = sd.query_devices(device, "input")
            samplerate = int(info["default_samplerate"])
            channels = channels or info["max_input_channels"]
            ring = RingBuffer(int(seconds * samplerate), channels)

            def callback(indata, frames, time_info, status):
                if status:
                    self.last_status = str(status)
                ring.write(indata)
                q = self.write_queue
                if q is not None:
                    q.put(indata.copy())   # hand disk I/O to the writer thread
                if frames:
                    per_channel = np.max(np.abs(indata), axis=0)
                    self.peaks = per_channel.tolist()
                    self.peak = float(per_channel.max())

            stream = sd.InputStream(device=device, channels=channels,
                                    samplerate=samplerate, dtype="float32",
                                    callback=callback)
            stream.start()
            self.stream = stream
            self.ring = ring
            self.device_name = info["name"]
            self.samplerate = samplerate
            self.channels = channels
            self.seconds = seconds
            self.peak = 0.0
            self.peaks = [0.0] * channels
            self.last_status = ""
            self.analysis = {}

            # Live key/tempo runs in its own thread off the rolling buffer so
            # the heavier FFT work never touches the audio callback.
            stop_evt = threading.Event()
            self._analysis_stop = stop_evt
            threading.Thread(target=self._analysis_loop,
                             args=(ring, samplerate, stop_evt),
                             daemon=True).start()

    def _analysis_loop(self, ring, samplerate, stop_evt):
        analyzer = LiveAnalyzer()
        while not stop_evt.wait(1.2):
            try:
                mono = ring.latest_mono(int(samplerate * 8))
                if len(mono) < samplerate * 3:
                    continue
                bpm = analyzer.estimate_bpm(mono, samplerate)
                key = analyzer.estimate_key(mono[-int(samplerate * 4):], samplerate)
                tuning = analyzer.estimate_tuning(mono[-int(samplerate * 4):], samplerate)
                self.analysis = {"bpm": bpm, "key": key, "tuning": tuning}
            except Exception as e:
                self.analysis = {"error": str(e)}

    def _analysis_tokens(self):
        """Filename tokens for the dominant detected key + tempo, if confident."""
        a = self.analysis or {}
        parts = []
        key = a.get("key") or {}
        if key.get("key") and key["key"] != "—" and key.get("confidence", 0) >= 0.5:
            tok = key_token(key["key"])
            if tok:
                parts.append(tok)
        bpm = a.get("bpm") or {}
        if bpm.get("bpm"):
            parts.append(f"{int(round(bpm['bpm']))}bpm")
        return parts

    # ---- continuous record mode ----------------------------------------
    def start_recording(self):
        with self.lock:
            if self.stream is None:
                raise RuntimeError("Start listening before recording.")
            if self.writer is not None:
                return
            CAPTURES.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            self.record_path = CAPTURES / f"rec_{stamp}.wav"
            writer = sf.SoundFile(str(self.record_path), mode="w",
                                  samplerate=self.samplerate,
                                  channels=self.channels, subtype="PCM_24")
            q = queue.Queue()
            self.writer = writer
            self.record_frames = 0
            self.writer_thread = threading.Thread(
                target=self._writer_loop, args=(writer, q), daemon=True)
            self.writer_thread.start()
            self.write_queue = q   # set last so the callback only queues once ready

    def _writer_loop(self, writer, q):
        while True:
            block = q.get()
            if block is None:
                break
            writer.write(block)
            self.record_frames += len(block)
        writer.close()

    def stop_recording(self):
        with self.lock:
            return self._finalize_recording_locked()

    def _finalize_recording_locked(self):
        if self.writer is None:
            return None
        q, self.write_queue = self.write_queue, None   # stop callback queuing
        q.put(None)                                    # flush sentinel
        self.writer_thread.join(timeout=5)
        self.writer = self.writer_thread = None
        path = self.record_path
        self.record_path = None
        duration = self.record_frames / self.samplerate if self.samplerate else 0
        tokens = self._analysis_tokens()
        if tokens:
            final = path.with_name(f"{path.stem}_{'_'.join(tokens)}.wav")
            try:
                path.rename(final)
                path = final
            except OSError:
                pass
        return path.name, duration

    def _stop_locked(self):
        self._finalize_recording_locked()
        if self._analysis_stop is not None:
            self._analysis_stop.set()
            self._analysis_stop = None
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self.peak = 0.0
        self.peaks = []
        self.analysis = {}

    def stop(self):
        with self.lock:
            self._stop_locked()

    def save(self):
        with self.lock:
            if self.ring is None:
                raise RuntimeError("Not recording — nothing to save.")
            audio = self.ring.snapshot()
            samplerate = self.samplerate
            tokens = self._analysis_tokens()
        if len(audio) == 0:
            raise RuntimeError("Buffer is empty — nothing to save yet.")
        CAPTURES.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        suffix = ("_" + "_".join(tokens)) if tokens else ""
        path = CAPTURES / f"idea_{stamp}{suffix}.wav"
        sf.write(path, audio, samplerate, subtype="PCM_24")
        return path.name, len(audio) / samplerate

    def status(self):
        with self.lock:
            running = self.stream is not None
            filled = self.ring.filled / self.samplerate if running else 0
            recording = self.writer is not None
            rec_secs = (self.record_frames / self.samplerate
                        if recording and self.samplerate else 0)
            return {
                "running": running,
                "device": self.device_name if running else None,
                "samplerate": self.samplerate if running else None,
                "channels": self.channels if running else None,
                "seconds": self.seconds if running else None,
                "filled": round(filled, 1),
                "peak": self.peak if running else 0.0,
                "peaks": self.peaks if running else [],
                "analysis": self.analysis if running else {},
                "recording": recording,
                "record_seconds": round(rec_secs, 1),
                "warning": self.last_status or None,
            }


class Tuner:
    """Standalone chromatic tuner on one chosen input channel.

    Runs its own short-buffer InputStream (independent of the recorder) so the
    user can tune a single interface input — a guitar, bass, or vocal — and see
    the nearest note plus its cents offset from A=440 in real time.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.stream = None
        self.ring = None
        self.device_name = None
        self.samplerate = None
        self.channel = 0
        self.max_channels = 0
        self.level = 0.0
        self.reading = None
        self._midi_ema = None     # smoothed pitch (in MIDI) for a steady needle
        self._miss = 0            # consecutive no-pitch reads, to clear on silence
        self._stop = None

    def start(self, device, channel):
        with self.lock:
            self._stop_locked()
            info = sd.query_devices(device, "input")
            sr = int(info["default_samplerate"])
            maxch = int(info["max_input_channels"])
            ch = max(0, min(int(channel or 0), maxch - 1))
            ring = RingBuffer(int(sr * 0.5), 1)   # half a second is plenty to tune

            def callback(indata, frames, time_info, status):
                col = indata[:, ch] if indata.shape[1] > ch else indata[:, 0]
                ring.write(col.reshape(-1, 1))
                if frames:
                    self.level = float(np.max(np.abs(col)))

            stream = sd.InputStream(device=device, channels=maxch,
                                    samplerate=sr, dtype="float32",
                                    callback=callback)
            stream.start()
            self.stream = stream
            self.ring = ring
            self.device_name = info["name"]
            self.samplerate = sr
            self.channel = ch
            self.max_channels = maxch
            self.level = 0.0
            self.reading = None
            self._midi_ema = None
            self._miss = 0

            stop_evt = threading.Event()
            self._stop = stop_evt
            threading.Thread(target=self._loop, args=(ring, sr, stop_evt),
                             daemon=True).start()

    def _loop(self, ring, sr, stop_evt):
        while not stop_evt.wait(0.08):           # ~12 Hz, snappy for tuning
            try:
                mono = ring.latest_mono(int(sr * 0.12))
                p = detect_pitch(mono, sr)
                if p is None:
                    self._miss += 1
                    if self._miss > 6:           # ~0.5s quiet → clear the readout
                        self._midi_ema = None
                        self.reading = None
                    continue
                self._miss = 0
                midi = 69 + 12 * np.log2(p["freq"] / 440.0)
                if self._midi_ema is None or abs(midi - self._midi_ema) > 0.6:
                    self._midi_ema = midi         # snap when a new note is played
                else:
                    self._midi_ema = 0.6 * self._midi_ema + 0.4 * midi
                freq = 440.0 * 2 ** ((self._midi_ema - 69) / 12.0)
                r = note_from_freq(freq)
                r["freq"] = round(float(freq), 2)
                r["clarity"] = p["clarity"]
                self.reading = r
            except Exception as e:
                self.reading = {"error": str(e)}

    def _stop_locked(self):
        if self._stop is not None:
            self._stop.set()
            self._stop = None
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self.level = 0.0
        self.reading = None

    def stop(self):
        with self.lock:
            self._stop_locked()

    def status(self):
        with self.lock:
            running = self.stream is not None
            return {
                "running": running,
                "device": self.device_name if running else None,
                "channel": self.channel if running else None,
                "max_channels": self.max_channels if running else None,
                "samplerate": self.samplerate if running else None,
                "level": self.level if running else 0.0,
                "reading": self.reading if running else None,
            }


recorder = Recorder()
tuner = Tuner()


def list_devices():
    default_in = sd.default.device[0]
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            out.append({
                "index": i,
                "name": d["name"],
                "channels": d["max_input_channels"],
                "samplerate": int(d["default_samplerate"]),
                "default": i == default_in,
            })
    return out


def list_captures():
    out = []
    for p in sorted(CAPTURES.glob("*.wav"), key=lambda p: p.stat().st_mtime,
                    reverse=True):
        try:
            info = sf.info(str(p))
            duration = round(info.duration, 1)
            channels = info.channels
        except RuntimeError:
            duration, channels = None, None
        # Pair the WAV with a companion video, if one was saved alongside it.
        video = None
        for ext in (".webm", ".mp4"):
            vp = p.with_suffix(ext)
            if vp.exists():
                video = vp.name
                break
        out.append({
            "name": p.name,
            "duration": duration,
            "channels": channels,
            "size": p.stat().st_size,
            "mtime": int(p.stat().st_mtime),
            "video": video,
        })
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._serve_file(STATIC / "index.html", "text/html")
        elif path == "/api/devices":
            # Re-scan for hot-plugged interfaces, but a PortAudio re-init
            # would kill a live stream, so only do it while everything's stopped.
            if recorder.stream is None and tuner.stream is None:
                sd._terminate()
                sd._initialize()
            self._json(list_devices())
        elif path == "/api/status":
            self._json(recorder.status())
        elif path == "/api/tuner/status":
            self._json(tuner.status())
        elif path == "/api/info":
            self._json({"captures_dir": str(CAPTURES)})
        elif path == "/api/captures":
            self._json(list_captures())
        elif path.startswith("/captures/"):
            name = path[len("/captures/"):]
            if not SAFE_NAME.match(name):
                self._json({"error": "bad filename"}, 400)
                return
            self._serve_file(CAPTURES / name, capture_ctype(name))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            if self.path == "/api/start":
                body = self._read_body()
                recorder.start(device=int(body["device"]),
                               seconds=float(body.get("seconds") or 60),
                               channels=int(body["channels"])
                               if body.get("channels") else None)
                self._json(recorder.status())
            elif self.path == "/api/stop":
                recorder.stop()
                self._json(recorder.status())
            elif self.path == "/api/tuner/start":
                body = self._read_body()
                tuner.start(device=int(body["device"]),
                            channel=int(body.get("channel") or 0))
                self._json(tuner.status())
            elif self.path == "/api/tuner/stop":
                tuner.stop()
                self._json(tuner.status())
            elif self.path == "/api/save":
                name, duration = recorder.save()
                self._json({"saved": name, "duration": round(duration, 1)})
            elif self.path == "/api/record/start":
                recorder.start_recording()
                self._json(recorder.status())
            elif self.path == "/api/record/stop":
                result = recorder.stop_recording()
                if result is None:
                    self._json({"error": "Not recording."}, 400)
                else:
                    name, duration = result
                    self._json({"saved": name, "duration": round(duration, 1)})
            elif self.path == "/api/analyze-file":
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    self._json({"error": "No file uploaded."}, 400)
                    return
                if length > 300 * 1024 * 1024:
                    self._json({"error": "File too large (max 300 MB)."}, 400)
                    return
                data = self.rfile.read(length)
                try:
                    result = analyze_audio_file(data)
                except ValueError as e:
                    self._json({"error": str(e).capitalize() + "."}, 400)
                    return
                except Exception:
                    self._json({"error": "Couldn't decode this file. Try "
                                "WAV, AIFF, FLAC, MP3, M4A, AAC, or OGG."}, 400)
                    return
                result["filename"] = self.headers.get("X-Filename", "")
                self._json(result)
            elif self.path == "/api/save-video":
                # Store a browser-recorded video buffer next to the WAV that was
                # just saved, sharing its stem so the two are paired by name.
                # The browser sends the rolling buffer as a run of self-contained
                # segments (X-Seg-Sizes); we concat them into one valid clip and
                # optionally mux in the chosen channels of the WAV's audio.
                audio = self.headers.get("X-Audio-Name", "")
                ext = "." + (self.headers.get("X-Video-Ext") or "webm").lower()
                if not SAFE_NAME.match(audio) or not audio.endswith(".wav"):
                    self._json({"error": "bad audio name"}, 400)
                    return
                if ext not in VIDEO_TYPES:
                    self._json({"error": "unsupported video type"}, 400)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    self._json({"error": "No video uploaded."}, 400)
                    return
                if length > 2 * 1024 * 1024 * 1024:
                    self._json({"error": "Video too large (max 2 GB)."}, 400)
                    return
                data = self.rfile.read(length)
                # Split the body back into the individual segment blobs.
                try:
                    sizes = [int(x) for x in
                             (self.headers.get("X-Seg-Sizes") or "").split(",") if x.strip()]
                except ValueError:
                    sizes = []
                if sizes and sum(sizes) == len(data):
                    segs, off = [], 0
                    for s in sizes:
                        segs.append(data[off:off + s])
                        off += s
                else:
                    segs = [data]   # single-blob fallback
                # Channels of the WAV to fold into the video's audio track.
                try:
                    channels = [int(x) for x in
                                (self.headers.get("X-Audio-Channels") or "").split(",") if x.strip()]
                except ValueError:
                    channels = []
                wav_path = CAPTURES / audio
                if channels and wav_path.exists():
                    try:
                        chn = sf.info(str(wav_path)).channels
                        channels = [c for c in channels if 0 <= c < chn]
                    except Exception:
                        channels = []
                CAPTURES.mkdir(parents=True, exist_ok=True)
                out = CAPTURES / (Path(audio).stem + ext)
                try:
                    out.write_bytes(build_companion_video(
                        segs, ext.lstrip("."),
                        str(wav_path) if wav_path.exists() else None, channels))
                except ValueError as e:
                    self._json({"error": str(e)}, 400)
                    return
                self._json({"saved": out.name, "channels": channels})
            elif self.path == "/api/set-folder":
                # Use a typed path if given, else pop the native macOS chooser.
                path = self._read_body().get("path")
                if not path:
                    script = ('POSIX path of (choose folder with prompt '
                              '"Choose where The Dashboard saves captures")')
                    out = subprocess.run(["osascript", "-e", script],
                                         capture_output=True, text=True, timeout=300)
                    if out.returncode != 0:        # user cancelled
                        self._json({"cancelled": True})
                        return
                    path = out.stdout.strip()
                set_captures_dir(path)
                self._json({"captures_dir": str(CAPTURES)})
            elif self.path == "/api/delete":
                name = self._read_body().get("name", "")
                if not SAFE_NAME.match(name):
                    self._json({"error": "bad filename"}, 400)
                    return
                (CAPTURES / name).unlink(missing_ok=True)
                # Drop the companion video too when deleting the WAV.
                if name.endswith(".wav"):
                    for ext in (".webm", ".mp4"):
                        (CAPTURES / name).with_suffix(ext).unlink(missing_ok=True)
                self._json({"deleted": name})
            elif self.path == "/api/reveal":
                # Open the captures folder in Finder, or reveal one file in it.
                name = self._read_body().get("name", "")
                if name:
                    if not SAFE_NAME.match(name):
                        self._json({"error": "bad filename"}, 400)
                        return
                    subprocess.run(["open", "-R", str(CAPTURES / name)], check=False)
                else:
                    subprocess.run(["open", str(CAPTURES)], check=False)
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 400)

    def _serve_file(self, path: Path, ctype: str):
        if not path.is_file():
            self._json({"error": "not found"}, 404)
            return
        data = path.read_bytes()
        # Minimal Range support so <audio> seeking works in Safari
        range_header = self.headers.get("Range")
        if range_header:
            m = re.match(r"bytes=(\d*)-(\d*)", range_header)
            start = int(m.group(1) or 0)
            end = int(m.group(2) or len(data) - 1)
            end = min(end, len(data) - 1)
            chunk = data[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes {start}-{end}/{len(data)}")
        else:
            chunk = data
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)


def main():
    parser = argparse.ArgumentParser(description="Idea recorder web UI")
    parser.add_argument("-p", "--port", type=int, default=8766)
    parser.add_argument("--open", action="store_true",
                        help="open the UI in a browser once the server is up")
    args = parser.parse_args()
    load_config()
    CAPTURES.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"The Dashboard UI: {url}")

    # As a bundled .app there's no terminal to open the browser, so do it here.
    if args.open or FROZEN:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        recorder.stop()
        tuner.stop()


if __name__ == "__main__":
    main()
