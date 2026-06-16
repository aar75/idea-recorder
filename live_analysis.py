"""Lightweight live key + tempo detection on captured interface audio.

A numpy port of the session-prep tool's browser live-analyzer: spectral-flux /
autocorrelation tempo estimation and an FFT chroma + Krumhansl-Schmuckler key
estimate. Pure numpy (no librosa) so it stays cheap to run a few times a
second and small enough to bundle into the .app.
"""

import numpy as np

# Krumhansl-Schmuckler key profiles (same as session-prep/analysis.py).
KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
PITCH_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

_KS = [("major", KS_MAJOR - KS_MAJOR.mean()), ("minor", KS_MINOR - KS_MINOR.mean())]


def key_from_chroma(chroma):
    """Correlate a 12-bin chroma vector against all 24 keys, best wins."""
    v = chroma - chroma.mean()
    nv = float(np.linalg.norm(v)) + 1e-9
    best, second, best_name = -2.0, -2.0, "—"
    for mode, p in _KS:
        pn = float(np.linalg.norm(p)) + 1e-9
        for tonic in range(12):
            r = float(np.dot(v, np.roll(p, tonic)) / (nv * pn))
            if r > best:
                second, best, best_name = best, r, f"{PITCH_NAMES[tonic]} {mode}"
            elif r > second:
                second = r
    return {
        "key": best_name,
        "confidence": round(max(0.0, min(1.0, best)), 3),
        "margin": round(best - second, 3),
    }


class LiveAnalyzer:
    """Holds the short-term smoothing state between successive estimates."""

    def __init__(self):
        self.bpm_history = []
        self.chroma_smooth = np.zeros(12)
        self._key_bins = None        # cached (sr, pc_index, bin_index) for chroma

    # ---- tempo ----------------------------------------------------------
    def estimate_bpm(self, x, sr):
        hop, win = 512, 1024
        fps = sr / hop
        if len(x) < sr * 3:
            return None
        n_frames = (len(x) - win) // hop
        if n_frames < 64:
            return None

        w = np.hanning(win).astype(np.float32)
        idx = np.arange(win)[None, :] + hop * np.arange(n_frames)[:, None]
        spec = np.abs(np.fft.rfft(x[idx] * w, axis=1))[:, 1:512]   # bins 1..511

        flux = np.zeros(n_frames)
        flux[1:] = np.maximum(np.diff(spec, axis=0), 0.0).sum(axis=1)
        flux -= flux.mean()

        e0 = float(np.dot(flux, flux))
        if e0 <= 0:
            return None
        min_lag = max(2, int(60 / 210 * fps))
        max_lag = min(n_frames - 8, int(np.ceil(60 / 50 * fps)))
        if max_lag <= min_lag:
            return None

        ac = np.zeros(max_lag + 1)
        for lag in range(min_lag, max_lag + 1):
            s = float(np.dot(flux[:n_frames - lag], flux[lag:]))
            ac[lag] = s / (e0 * (n_frames - lag) / n_frames)
        best_lag = int(np.argmax(ac[min_lag:max_lag + 1])) + min_lag
        best_val = ac[best_lag]
        if best_val < 0.05:
            return None

        # prefer the faster octave when it is nearly as strong
        half = round(best_lag / 2)
        if half >= min_lag and ac[half] > 0.8 * best_val:
            best_lag = half

        lag = float(best_lag)
        if min_lag < best_lag < max_lag:
            a, b, c = ac[best_lag - 1], ac[best_lag], ac[best_lag + 1]
            denom = a - 2 * b + c
            if abs(denom) > 1e-9:
                lag = best_lag + 0.5 * (a - c) / denom

        bpm = 60 * fps / lag
        while bpm < 70:
            bpm *= 2
        while bpm >= 185:
            bpm /= 2

        self.bpm_history.append(bpm)
        if len(self.bpm_history) > 9:
            self.bpm_history.pop(0)
        ordered = sorted(self.bpm_history)
        median = float(ordered[len(ordered) // 2])
        spread = float(ordered[-1] - ordered[0])
        return {
            "bpm": round(median, 1),
            "locked": bool(len(self.bpm_history) >= 5 and spread < 4),
            "strength": round(float(best_val), 3),
        }

    # ---- key ------------------------------------------------------------
    def _key_index(self, sr):
        win = 8192
        if self._key_bins is None or self._key_bins[0] != sr:
            bins = np.arange(1, win // 2)
            freq = bins * sr / win
            mask = (freq >= 55) & (freq <= 2200)
            bins = bins[mask]
            midi = 69 + 12 * np.log2(freq[mask] / 440)
            pc = (np.round(midi).astype(int) % 12 + 12) % 12
            self._key_bins = (sr, pc, bins)
        return self._key_bins[1], self._key_bins[2]

    def estimate_key(self, x, sr):
        win, hop = 8192, 4096
        if len(x) < sr * 2:
            return None
        n_frames = (len(x) - win) // hop + 1
        if n_frames < 1:
            return None
        pc, bins = self._key_index(sr)
        w = np.hanning(win).astype(np.float32)

        chroma = np.zeros(12)
        for f in range(n_frames):
            seg = x[f * hop:f * hop + win] * w
            mag = np.abs(np.fft.rfft(seg))
            np.add.at(chroma, pc, mag[bins])

        norm = float(np.linalg.norm(chroma)) + 1e-9
        self.chroma_smooth = 0.75 * self.chroma_smooth + 0.25 * (chroma / norm)
        return key_from_chroma(self.chroma_smooth)
