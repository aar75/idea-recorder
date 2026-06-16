#!/usr/bin/env python3
"""Rolling audio backup recorder.

Continuously keeps the last N seconds (default 60) of audio from an input
device in memory. Press Enter to dump that buffer to a WAV file — a safety
net for ideas played while the DAW wasn't recording.
"""

import argparse
import datetime
import queue
import sys
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf


class RingBuffer:
    """Fixed-length circular buffer of audio frames (frames x channels)."""

    def __init__(self, frames: int, channels: int):
        self.buf = np.zeros((frames, channels), dtype=np.float32)
        self.frames = frames
        self.write_pos = 0
        self.filled = 0
        self.lock = threading.Lock()

    def write(self, data: np.ndarray):
        n = len(data)
        if n >= self.frames:
            data = data[-self.frames:]
            n = self.frames
        with self.lock:
            end = self.write_pos + n
            if end <= self.frames:
                self.buf[self.write_pos:end] = data
            else:
                first = self.frames - self.write_pos
                self.buf[self.write_pos:] = data[:first]
                self.buf[:end - self.frames] = data[first:]
            self.write_pos = end % self.frames
            self.filled = min(self.filled + n, self.frames)

    def snapshot(self) -> np.ndarray:
        """Return buffered audio in chronological order."""
        with self.lock:
            if self.filled < self.frames:
                return self.buf[:self.write_pos].copy()
            return np.concatenate(
                (self.buf[self.write_pos:], self.buf[:self.write_pos])
            )

    def latest(self, n: int) -> np.ndarray:
        """Return the most recent n frames, chronological (frames x channels)."""
        with self.lock:
            n = min(n, self.filled)
            if n == 0:
                return np.zeros((0, self.buf.shape[1]), dtype=np.float32)
            start = (self.write_pos - n) % self.frames
            if start + n <= self.frames:
                return self.buf[start:start + n].copy()
            first = self.frames - start
            return np.concatenate((self.buf[start:], self.buf[:n - first]))

    def latest_mono(self, n: int) -> np.ndarray:
        """Most recent n frames mixed down to mono (1-D float32)."""
        block = self.latest(n)
        if len(block) == 0:
            return block.reshape(0)
        return block.mean(axis=1).astype(np.float32)


def pick_device(spec):
    """Resolve a device index or name substring to a device index."""
    if spec is None:
        return None
    try:
        return int(spec)
    except ValueError:
        pass
    matches = [
        i for i, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0 and spec.lower() in d["name"].lower()
    ]
    if not matches:
        sys.exit(f"No input device matching '{spec}'. Use --list-devices to see options.")
    if len(matches) > 1:
        names = ", ".join(f"[{i}] {sd.query_devices(i)['name']}" for i in matches)
        sys.exit(f"Multiple devices match '{spec}': {names}. Be more specific or use the index.")
    return matches[0]


def list_input_devices():
    default_in = sd.default.device[0]
    print("Input devices:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            marker = "*" if i == default_in else " "
            print(f" {marker}[{i}] {d['name']} "
                  f"({d['max_input_channels']} in, {int(d['default_samplerate'])} Hz)")
    print(" * = system default")


def main():
    parser = argparse.ArgumentParser(
        description="Keep a rolling backup of the last N seconds of audio. "
                    "Press Enter to save it, q+Enter to quit.")
    parser.add_argument("-d", "--device", metavar="DEV",
                        help="input device index or name substring (default: system default)")
    parser.add_argument("-s", "--seconds", type=float, default=60,
                        help="length of the rolling buffer in seconds (default: 60)")
    parser.add_argument("-c", "--channels", type=int,
                        help="number of input channels to capture (default: all on the device)")
    parser.add_argument("-r", "--samplerate", type=int,
                        help="sample rate in Hz (default: device default)")
    parser.add_argument("-o", "--output-dir", type=Path,
                        default=Path(__file__).resolve().parent / "captures",
                        help="where to save WAV files (default: ./captures)")
    parser.add_argument("--bit-depth", choices=["16", "24", "32f"], default="24",
                        help="WAV bit depth: 16, 24, or 32-bit float (default: 24)")
    parser.add_argument("--list-devices", action="store_true",
                        help="list input devices and exit")
    args = parser.parse_args()

    if args.list_devices:
        list_input_devices()
        return

    device = pick_device(args.device)
    info = sd.query_devices(device, "input")
    channels = args.channels or info["max_input_channels"]
    samplerate = args.samplerate or int(info["default_samplerate"])
    subtype = {"16": "PCM_16", "24": "PCM_24", "32f": "FLOAT"}[args.bit_depth]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ring = RingBuffer(int(args.seconds * samplerate), channels)
    errors = queue.Queue()

    def callback(indata, frames, time_info, status):
        if status:
            errors.put(str(status))
        ring.write(indata)

    print(f"Listening on: {info['name']}")
    print(f"  {channels} channel(s) @ {samplerate} Hz, "
          f"rolling buffer: {args.seconds:g}s, saving to: {args.output_dir}")
    print("Press Enter to save the last "
          f"{args.seconds:g} seconds. Type q + Enter to quit.\n")

    with sd.InputStream(device=device, channels=channels,
                        samplerate=samplerate, dtype="float32",
                        callback=callback):
        while True:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                print("\nStopped.")
                break
            if line.strip().lower() in ("q", "quit", "exit"):
                print("Stopped.")
                break

            audio = ring.snapshot()
            if len(audio) == 0:
                print("Buffer is empty — nothing to save yet.")
                continue
            stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            path = args.output_dir / f"idea_{stamp}.wav"
            sf.write(path, audio, samplerate, subtype=subtype)
            print(f"Saved {len(audio) / samplerate:.1f}s -> {path}")

            while not errors.empty():
                print(f"  (audio warning during capture: {errors.get()})")


if __name__ == "__main__":
    main()
