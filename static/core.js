/* Idea Recorder — shared front-end core.
   Used by the web dashboard, the always-on-top strip, and the floating widget.
   No load-time side effects: every surface imports this and wires its own DOM.

   The camera buffer pipeline (the intricate part) lives here as a factory so the
   three surfaces share one implementation. Audio capture, metering, and live
   key/tempo all happen server-side and are read via /api/status. */
(function (global) {
  "use strict";

  const IR = {};

  // ---- tiny helpers -------------------------------------------------------
  IR.$ = (id) => document.getElementById(id);

  IR.api = async function (path, body) {
    const res = await fetch(path, body !== undefined
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
      : undefined);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  };

  // Finds a .ir-toast (or #toast) element; falls back to console.
  IR.toast = function (msg, isError) {
    const t = document.querySelector(".ir-toast") || IR.$("toast");
    if (!t) { (isError ? console.error : console.log)(msg); return; }
    t.textContent = msg;
    t.className = (t.classList.contains("ir-toast") ? "ir-toast" : "toast") +
      " show" + (isError ? " error" : "");
    clearTimeout(t._timer);
    t._timer = setTimeout(() => t.classList.remove("show"), 2600);
  };

  IR.fmtClock = function (s) {
    s = Math.floor(s);
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  };

  // amplitude (0..1) -> meter fill percent on a -60..0 dBFS scale
  IR.dbToPct = (v) => Math.max(0, Math.min(100, (20 * Math.log10(v || 1e-7) + 60) / 60 * 100));

  IR.peakDb = function (overall, peaks) {
    const v = (overall ?? Math.max(0, ...(peaks || []))) || 1e-7;
    const db = 20 * Math.log10(v);
    return db <= -90 ? "–∞ dB" : db.toFixed(1) + " dB";
  };

  // Drive a set of meter inner-bars from per-channel peaks, with decay.
  // dim is "width" (horizontal) or "height" (vertical).
  IR.renderMeters = function (innerEls, peaks, smooth, dim) {
    for (let i = 0; i < innerEls.length && i < peaks.length; i++) {
      smooth[i] = Math.max(peaks[i], (smooth[i] || 0) * 0.82);
      innerEls[i].style[dim] = IR.dbToPct(smooth[i]) + "%";
    }
  };

  // ---- live key / tempo / tuning formatting (rendering is per-surface) ----
  IR.formatLive = function (analysis) {
    const out = {};
    if (!analysis) return out;
    const k = analysis.key, b = analysis.bpm, t = analysis.tuning;
    if (k && k.key && k.key !== "—") {
      out.key = k.key;
      out.keyConf = Math.round((k.confidence || 0) * 100);
      out.keyLocked = (k.confidence || 0) >= 0.6;
    }
    if (b && b.bpm) {
      out.bpm = b.bpm.toFixed(1);
      out.bpmLocked = !!b.locked;
    }
    if (t && typeof t.cents === "number") {
      const c = Math.round(t.cents);
      out.cents = c;
      out.centsText = (c > 0 ? "+" : c < 0 ? "−" : "") + Math.abs(c);
      out.centsHz = t.hz;
      out.centsLocked = (t.confidence || 0) >= 0.5;
    }
    return out;
  };

  // pitch-class color used by the key map (matches the handoff palette feel)
  IR.PC_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
  IR.keyColor = function (name) {
    const [tonic, mode] = (name || "").split(" ");
    const h = Math.max(0, IR.PC_NAMES.indexOf(tonic)) * 30;
    return mode === "minor" ? `hsl(${h} 42% 46%)` : `hsl(${h} 68% 52%)`;
  };

  // =========================================================================
  // Camera buffer engine
  //
  // The rolling video buffer is recorded as a run of short, self-contained
  // segments (the recorder is rotated every SEG_SECONDS). Each segment is a
  // complete, valid clip, so on save we ship the recent ones and ffmpeg concats
  // them server-side into one correctly-timed file — slicing a single
  // MediaRecorder stream instead produced the old ~1-second clip.
  //
  // opts: { previewEl, getBufferSeconds, onContinuous(bool), onState() }
  // =========================================================================
  IR.createCamera = function (opts) {
    opts = opts || {};
    const SEG_SECONDS = 3;
    let stream = null, mime = "";
    let segments = [];                 // [{blob, end}]
    let curRec = null, curChunks = [], segTimer = null;
    let continuous = false, continuousStart = 0;

    const getBuffer = () => (opts.getBufferSeconds ? opts.getBufferSeconds() : 60);

    function pickVideoMime() {
      if (!window.MediaRecorder) return "";
      const cands = ["video/webm;codecs=vp9", "video/webm;codecs=vp8",
                     "video/webm", "video/mp4"];
      for (const m of cands) {
        try { if (MediaRecorder.isTypeSupported(m)) return m; } catch (e) {}
      }
      return "";
    }

    function startSegment() {
      if (!stream) return;
      curChunks = [];
      let rec;
      try { rec = new MediaRecorder(stream, { mimeType: mime, videoBitsPerSecond: 4e6 }); }
      catch (e) { return; }
      curRec = rec;
      rec.ondataavailable = (e) => { if (e.data && e.data.size) curChunks.push(e.data); };
      rec.onstop = () => {
        if (curChunks.length) {
          segments.push({ blob: new Blob(curChunks, { type: mime.split(";")[0] }),
                          end: performance.now() / 1000 });
          pruneSegments();
        }
        if (stream && curRec === rec) startSegment();   // roll into the next
      };
      rec.start();
    }

    function pruneSegments() {
      if (continuous) return;          // keep the whole take while recording
      const cutoff = performance.now() / 1000 - (getBuffer() + SEG_SECONDS * 2);
      while (segments.length > 1 && segments[0].end < cutoff) segments.shift();
    }

    function rotateSegment() {
      if (curRec && curRec.state === "recording") curRec.stop();   // onstop chains the next
    }

    // Finalize the in-progress segment so a save captures the freshest footage.
    function flush() {
      return new Promise((resolve) => {
        const rec = curRec;
        if (!rec || rec.state !== "recording") return resolve();
        const chain = rec.onstop;
        rec.onstop = () => { chain(); resolve(); };
        rec.stop();
        setTimeout(resolve, 600);      // safety net
      });
    }

    // The segment blobs covering the window we're about to save.
    function collectSegments(cont) {
      let segs;
      if (cont) {
        segs = segments.filter((s) => s.end >= continuousStart - SEG_SECONDS);
      } else {
        const cutoff = performance.now() / 1000 - (getBuffer() + SEG_SECONDS);
        segs = segments.filter((s) => s.end >= cutoff);
      }
      if (!segs.length && segments.length) segs = [segments[segments.length - 1]];
      return segs.map((s) => s.blob);
    }

    async function enable(deviceId) {
      mime = pickVideoMime();
      if (!mime) throw new Error("This browser can't record video.");
      const video = { width: { ideal: 1280 }, height: { ideal: 720 } };
      if (deviceId) video.deviceId = { exact: deviceId };
      stream = await navigator.mediaDevices.getUserMedia({ audio: false, video });
      segments = []; curChunks = []; continuous = false;
      if (opts.previewEl) opts.previewEl.srcObject = stream;
      startSegment();
      segTimer = setInterval(rotateSegment, SEG_SECONDS * 1000);
      stream.getVideoTracks()[0].addEventListener("ended", () => {
        disable();
        if (opts.onEnded) opts.onEnded();   // let the strip auto-reacquire
      });
      return stream;
    }

    function disable() {
      if (segTimer) { clearInterval(segTimer); segTimer = null; }
      const rec = curRec;
      curRec = null;                   // null first so onstop won't chain a new segment
      if (rec && rec.state !== "inactive") { try { rec.stop(); } catch (e) {} }
      if (stream) stream.getTracks().forEach((t) => t.stop());
      stream = null; segments = []; curChunks = []; continuous = false;
      if (opts.previewEl) opts.previewEl.srcObject = null;
      if (opts.onContinuous) opts.onContinuous(false);
      if (opts.onState) opts.onState();
    }

    async function uploadVideo(audioName, blobs, channels) {
      if (!blobs || !blobs.length) return;
      const sizes = blobs.map((b) => b.size).join(",");
      const res = await fetch("/api/save-video", {
        method: "POST",
        headers: {
          "X-Audio-Name": audioName,
          "X-Video-Ext": videoExt(),
          "X-Seg-Sizes": sizes,
          "X-Audio-Channels": (channels || []).join(","),
        },
        body: new Blob(blobs, { type: mime.split(";")[0] }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.error || "video save failed");
      }
    }

    const videoExt = () => (mime.includes("mp4") ? "mp4" : "webm");

    // seconds of footage currently held in the rolling buffer
    function bufferSpan() {
      if (!segments.length) return 0;
      const span = performance.now() / 1000 - segments[0].end + SEG_SECONDS;
      return Math.min(Math.round(span), getBuffer());
    }

    return {
      pickVideoMime, videoExt,
      isActive: () => !!stream,
      isContinuous: () => continuous,
      startContinuous() { continuous = true; continuousStart = performance.now() / 1000;
        if (opts.onContinuous) opts.onContinuous(true); },
      stopContinuous() { continuous = false; if (opts.onContinuous) opts.onContinuous(false); },
      continuousElapsed: () => performance.now() / 1000 - continuousStart,
      enable, disable, flush, collectSegments, uploadVideo, bufferSpan,
    };
  };

  // =========================================================================
  // Hyperspace background
  //
  // A lightweight warp-starfield drawn on a <canvas>, shown on the strip and the
  // widget whenever there's no live camera feed (no camera connected, or the
  // camera is held by another window). Stars streak outward from the centre. The
  // rAF loop only runs while active — setActive(false) stops it — so it costs
  // nothing once the camera takes over.
  // =========================================================================
  IR.createHyperspace = function (canvas) {
    const ctx = canvas.getContext("2d");
    let raf = 0, stars = [], w = 0, h = 0, cx = 0, cy = 0;
    const N = 280, SPEED = 0.015;

    function resize() {
      w = canvas.width = canvas.clientWidth || 1;
      h = canvas.height = canvas.clientHeight || 1;
      cx = w / 2; cy = h / 2;
    }
    function seed() {
      stars = [];
      for (let i = 0; i < N; i++)
        stars.push({ x: Math.random() * 2 - 1, y: Math.random() * 2 - 1, z: Math.random() });
    }
    function frame() {
      // Pick up strip/widget resizes without restarting the loop.
      if (canvas.clientWidth !== w || canvas.clientHeight !== h) resize();
      ctx.fillStyle = "#04060b";
      ctx.fillRect(0, 0, w, h);
      const spread = Math.max(w, h);
      for (const s of stars) {
        const pz = s.z;
        s.z -= SPEED;
        if (s.z <= 0.02) {            // recycle a star that reached the viewer
          s.x = Math.random() * 2 - 1; s.y = Math.random() * 2 - 1; s.z = 1;
          continue;                   // skip drawing so it doesn't streak across
        }
        const k = 1 / s.z - 1, pk = 1 / pz - 1;     // projected offset, near vs prev
        const x = cx + s.x * spread * k,  y = cy + s.y * spread * k;
        const px = cx + s.x * spread * pk, py = cy + s.y * spread * pk;
        const a = Math.min(1, (1 - s.z) * 1.5);
        ctx.strokeStyle = "rgba(190,232,255," + a + ")";
        ctx.lineWidth = Math.max(0.5, (1 - s.z) * 2.4);
        ctx.beginPath(); ctx.moveTo(px, py); ctx.lineTo(x, y); ctx.stroke();
      }
      raf = requestAnimationFrame(frame);
    }
    return {
      setActive(on) {
        if (on) {
          if (raf) return;            // already warping
          resize();
          if (!stars.length) seed();
          raf = requestAnimationFrame(frame);
        } else if (raf) {
          cancelAnimationFrame(raf); raf = 0;
        }
      },
    };
  };

  global.IR = IR;
})(window);
