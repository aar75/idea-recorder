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


def note_from_freq(freq, a4=440.0):
    """Nearest equal-tempered note for a frequency, plus its cents offset.

    Cents are signed against A=440: negative = flat of the note, positive =
    sharp. ``target_hz`` is the exact 440-tuned pitch of that note, so a tuner
    can show how far the played note sits from where it should land.
    """
    midi = 69 + 12 * np.log2(freq / a4)
    nearest = int(round(midi))
    return {
        "note": PITCH_NAMES[nearest % 12],
        "octave": nearest // 12 - 1,
        "cents": round(float((midi - nearest) * 100.0), 1),
        "target_hz": round(float(a4 * 2 ** ((nearest - 69) / 12.0)), 2),
    }


def detect_pitch(x, sr, fmin=40.0, fmax=1500.0):
    """Estimate the fundamental of a (mostly monophonic) signal — tuner duty.

    Normalized autocorrelation with parabolic interpolation on the lag peak.
    Returns ``{"freq", "clarity"}`` or ``None`` when the input is silent or not
    clearly pitched. An octave slip wouldn't change the cents reading, so the
    simple ACF is plenty accurate for a tuner; we still prefer the earliest
    strong lag to keep the displayed octave honest.
    """
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    if n < 1024:
        return None
    x = x - x.mean()
    if float(np.sqrt(np.mean(x * x))) < 1e-4:    # effectively silent
        return None

    tau_min = max(2, int(sr / fmax))
    tau_max = min(n - 1, int(sr / fmin) + 1)
    if tau_max <= tau_min + 2:
        return None

    xw = x * np.hanning(n)
    fft_size = 1 << int(np.ceil(np.log2(2 * n)))
    spec = np.fft.rfft(xw, fft_size)
    acf = np.fft.irfft(spec * np.conj(spec), fft_size)[:tau_max + 2]
    r0 = float(acf[0])
    if r0 <= 0:
        return None
    nacf = acf / r0

    seg = nacf[tau_min:tau_max + 1]
    peak_val = float(seg.max())
    if peak_val < 0.5:                           # not convincingly periodic
        return None

    # Earliest local max within 85% of the strongest peak: avoids locking onto
    # a sub-octave (a multiple of the true period) that reads a fifth/octave low.
    thresh = 0.85 * peak_val
    k = None
    for i in range(1, len(seg) - 1):
        if seg[i] >= thresh and seg[i] >= seg[i - 1] and seg[i] >= seg[i + 1]:
            k = i + tau_min
            break
    if k is None:
        k = int(np.argmax(seg)) + tau_min

    a, b, c = nacf[k - 1], nacf[k], nacf[k + 1]
    denom = a - 2 * b + c
    tau = k + 0.5 * (a - c) / denom if abs(denom) > 1e-12 else float(k)
    freq = sr / tau
    if not (fmin <= freq <= fmax):
        return None
    return {"freq": float(freq), "clarity": round(peak_val, 3)}


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


# Same ordering key_from_chroma walks: index 0..11 = major tonics, 12..23 = minor.
KEY_NAMES_24 = [f"{n} major" for n in PITCH_NAMES] + [f"{n} minor" for n in PITCH_NAMES]


def key_correlations(chroma):
    """Correlation of a 12-bin chroma against all 24 keys as a length-24 array.

    Indexed to match KEY_NAMES_24 (0..11 major, 12..23 minor).
    """
    v = chroma - chroma.mean()
    nv = float(np.linalg.norm(v)) + 1e-9
    out = np.empty(24)
    for m, (_, p) in enumerate(_KS):
        pn = float(np.linalg.norm(p)) + 1e-9
        for tonic in range(12):
            out[m * 12 + tonic] = float(np.dot(v, np.roll(p, tonic)) / (nv * pn))
    return out


def _mode_filter(labels, k):
    """Majority-vote smoothing over a k-wide window — kills single-frame blips."""
    if k <= 1 or len(labels) < k:
        return labels
    half = k // 2
    out = list(labels)
    for i in range(len(labels)):
        window = labels[max(0, i - half):i + half + 1]
        out[i] = max(set(window), key=window.count)
    return out


def _enforce_min_run(labels, min_steps):
    """Dissolve any run shorter than min_steps into its longer neighbour.

    Repeated until every surviving segment is long enough — this is what keeps a
    brief borrowed chord or a noisy patch from registering as a key change.
    """
    labels = list(labels)
    while True:
        runs, i = [], 0
        while i < len(labels):
            j = i
            while j + 1 < len(labels) and labels[j + 1] == labels[i]:
                j += 1
            runs.append((labels[i], i, j))   # label, start, end (inclusive)
            i = j + 1
        if len(runs) <= 1:
            return labels
        short = min(runs, key=lambda r: r[2] - r[1])
        if (short[2] - short[1] + 1) >= min_steps:
            return labels
        idx = runs.index(short)
        prev_r = runs[idx - 1] if idx > 0 else None
        next_r = runs[idx + 1] if idx < len(runs) - 1 else None
        if prev_r and next_r:
            target = prev_r if (prev_r[2] - prev_r[1]) >= (next_r[2] - next_r[1]) else next_r
        else:
            target = prev_r or next_r
        for p in range(short[1], short[2] + 1):
            labels[p] = target[0]


class LiveAnalyzer:
    """Holds the short-term smoothing state between successive estimates."""

    def __init__(self):
        self.bpm_history = []
        self.chroma_smooth = np.zeros(12)
        self._key_bins = None        # cached (sr, pc_index, bin_index) for chroma
        self.tuning_smooth = 0j      # smoothed complex resultant for cents-vs-440

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

    # ---- key map over time ---------------------------------------------
    def chromagram(self, x, sr, win=8192, hop=4096):
        """Per-frame 12-bin chroma for the whole signal; returns (frames, dt)."""
        if len(x) < win:
            return np.zeros((0, 12)), hop / sr
        pc, bins = self._key_index(sr)
        w = np.hanning(win).astype(np.float32)
        n_frames = (len(x) - win) // hop + 1
        chroma = np.zeros((n_frames, 12))
        for f in range(n_frames):
            seg = x[f * hop:f * hop + win] * w
            mag = np.abs(np.fft.rfft(seg))
            row = np.zeros(12)
            np.add.at(row, pc, mag[bins])
            chroma[f] = row
        return chroma, hop / sr

    def key_map(self, x, sr, win_s=6.0, hop_s=1.0, min_seg_s=10.0, switch_margin=0.05):
        """Segment a file into stable key regions and flag obvious changes.

        Estimates the key in a sliding window, but biases every step toward the
        whole-file (overall) key by ``switch_margin`` and dissolves any region
        shorter than ``min_seg_s`` — so only sustained, clearly-different keys
        register as a change, not passing chords or noisy passages.
        """
        chroma, dt = self.chromagram(x, sr)
        n = len(chroma)
        duration = len(x) / sr
        if n == 0:
            return None

        total = chroma.sum(axis=0)
        overall = key_from_chroma(total)
        overall_idx = int(np.argmax(key_correlations(total)))

        win_f = max(1, int(round(win_s / dt)))
        step_f = max(1, int(round(hop_s / dt)))

        half = win_f // 2
        times, labels, scores = [], [], []
        for center in range(0, n, step_f):
            a, b = max(0, center - half), min(n, center + half)   # window centred on t
            seg = chroma[a:b].sum(axis=0)
            corr = key_correlations(seg)
            biased = corr.copy()
            biased[overall_idx] += switch_margin     # favour the overall key
            lbl = int(np.argmax(biased))
            times.append(center * dt)
            labels.append(lbl)
            scores.append(float(corr[lbl]))

        one_segment = {
            "overall": overall["key"],
            "duration": round(duration, 1),
            "segments": [{"start": 0.0, "end": round(duration, 1),
                          "key": overall["key"], "confidence": overall["confidence"]}],
            "changes": [],
        }
        if len(labels) <= 1:
            return one_segment

        labels = _mode_filter(labels, 3)
        labels = _enforce_min_run(labels, max(1, int(round(min_seg_s / hop_s))))

        segments, i = [], 0
        while i < len(labels):
            j = i
            while j + 1 < len(labels) and labels[j + 1] == labels[i]:
                j += 1
            end_t = times[j + 1] if j + 1 < len(times) else duration
            run_scores = scores[i:j + 1]
            segments.append({
                "start": round(times[i], 1),
                "end": round(end_t, 1),
                "key": KEY_NAMES_24[labels[i]],
                "confidence": round(max(0.0, min(1.0, sum(run_scores) / len(run_scores))), 3),
            })
            i = j + 1

        return {
            "overall": overall["key"],
            "duration": round(duration, 1),
            "segments": segments,
            "changes": [s["start"] for s in segments[1:]],
        }

    # ---- tuning (cents vs A=440) ----------------------------------------
    def estimate_tuning(self, x, sr):
        """How far the audio's tuning sits from A=440, in cents.

        Finds parabolically-interpolated spectral peaks across the pitched
        range, measures each peak's deviation from the nearest equal-tempered
        semitone, and takes a magnitude-weighted *circular* mean so peaks
        sitting near the ±50-cent wrap don't cancel each other out. The result
        is the overall tuning offset of the material relative to a 440 Hz
        tuner: positive = sharp (pitch the beat down by that many cents to line
        up with 440), negative = flat. ``hz`` is the equivalent A reference, so
        a +12¢ read means the beat is tuned to A≈443.1.
        """
        win, hop = 8192, 4096
        if len(x) < sr * 2:
            return None
        n_frames = (len(x) - win) // hop + 1
        if n_frames < 1:
            return None
        w = np.hanning(win).astype(np.float32)
        lo = max(1, int(80 * win / sr))            # ~80 Hz, below most pitched content
        hi = min(win // 2 - 1, int(2000 * win / sr))  # ~2 kHz, above which partials blur
        if hi <= lo + 1:
            return None

        acc, weight = 0j, 0.0
        for f in range(n_frames):
            seg = x[f * hop:f * hop + win] * w
            mag = np.abs(np.fft.rfft(seg))
            band = mag[lo:hi + 1]
            # strict local maxima inside the band, mapped back to mag indices
            peaks = np.where((band[1:-1] > band[:-2]) &
                             (band[1:-1] >= band[2:]))[0] + 1 + lo
            if len(peaks) == 0:
                continue
            thresh = float(mag[peaks].max()) * 0.1   # ignore peaks 20 dB below the top
            for k in peaks:
                m = float(mag[k])
                if m < thresh:
                    continue
                y0, y1, y2 = float(mag[k - 1]), float(mag[k]), float(mag[k + 1])
                denom = y0 - 2 * y1 + y2
                delta = 0.5 * (y0 - y2) / denom if abs(denom) > 1e-9 else 0.0
                delta = max(-0.5, min(0.5, delta))   # parabolic fit, refined bin offset
                freq = (k + delta) * sr / win
                if freq <= 0:
                    continue
                midi = 69 + 12 * np.log2(freq / 440.0)
                dev = midi - round(midi)             # semitone offset, [-0.5, 0.5]
                acc += m * np.exp(2j * np.pi * dev)
                weight += m
        if weight <= 0:
            return None

        # smooth the complex resultant across calls for a steadier readout
        self.tuning_smooth = 0.7 * self.tuning_smooth + 0.3 * (acc / weight)
        zs = self.tuning_smooth
        cents = float(np.angle(zs) / (2 * np.pi) * 100.0)
        conf = float(min(1.0, abs(zs)))              # peak concentration, 0..1
        return {
            "cents": round(cents, 1),
            "hz": round(440.0 * 2 ** (cents / 1200.0), 1),
            "confidence": round(conf, 3),
        }
