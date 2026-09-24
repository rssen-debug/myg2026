"""verify.py — mechanical QA gate per MANDATE.md (ported from
sunnyv2youtube/scripts/verify_build.py). REJECTS slideshow-style output.

Use as function:  ok, fails, warns = verify.verify_build("out.mp4", target_dur=45)
Use as CLI:       python verify.py out.mp4 [target_duration_s]

Measured checks:
  A. streams (h264+aac), min resolution, duration vs target (±5 %)
  B. motion: median frame-activity must exceed threshold (no static slideshows),
     dead windows flagged
  C. audio: not silent, >=3 distinct loudness peaks, no clipping risk
"""
import os
import re
import subprocess
import sys

import numpy as np

import config


def _sh(args):
    return subprocess.run(args, capture_output=True)


def duration_of(f):
    out = subprocess.run([config.FFMPEG, "-i", f], capture_output=True,
                         text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", out)
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def audio_samples(f, sr=8000):
    out = _sh([config.FFMPEG, "-v", "error", "-i", f, "-map", "0:a", "-ac", "1",
               "-ar", str(sr), "-f", "s16le", "-"]).stdout
    return np.frombuffer(out, dtype=np.int16).astype(np.float32) / 32768


def audio_rms_series(f, win=2.0):
    """Long-window RMS: for 'is there any sound at all' and level checks."""
    x = audio_samples(f)
    n = int(8000 * win)
    if len(x) < n:
        return np.array([0.0])
    return np.array([float(np.sqrt(np.mean(x[i:i + n] ** 2)) + 1e-9)
                     for i in range(0, len(x) - n, n)])


def audio_peak_count(f, win=0.25, ratio=1.15):
    """Count audible hits (SFX impacts, clip audio, VO accents).

    The window matters: a 0.7 s impact averaged over a 2 s window raises that
    window's RMS by only ~16 %, which sits right at the threshold - so a coarse
    window reports ZERO peaks on a video that is full of them and the gate
    rejects good output. Measured on real renders: 2.0 s -> 0 peaks, 0.5 s ->
    31, 0.25 s -> 93. We use a short window to match the intent ("are there
    distinct hits?") and require the hit to also beat an absolute floor so
    near-silence cannot manufacture peaks.
    """
    x = audio_samples(f)
    n = max(1, int(8000 * win))
    if len(x) < 3 * n:
        return 0
    r = np.array([float(np.sqrt(np.mean(x[i:i + n] ** 2)))
                  for i in range(0, len(x) - n, n)])
    med = float(np.median(r)) + 1e-9
    peaks = 0
    for i in range(2, len(r) - 2):
        neigh = (r[i - 2] + r[i - 1] + r[i + 1] + r[i + 2]) / 4.0
        if r[i] > ratio * neigh and r[i] > 0.03 and r[i] > med:
            peaks += 1
    return peaks


def frame_activity(f, fps=5):
    """mean |diff| of downscaled grayscale per 2 s window => motion metric."""
    out = _sh([config.FFMPEG, "-v", "error", "-i", f, "-vf", f"fps={fps},scale=64:36",
               "-f", "rawvideo", "-pix_fmt", "gray", "-"]).stdout
    fr = 64 * 36
    n = len(out) // fr
    if n < 3:
        return np.array([0.0])
    x = np.frombuffer(out[:n * fr], dtype=np.uint8).reshape(n, fr).astype(np.float32)
    d = np.abs(np.diff(x, axis=0)).mean(axis=1)
    w = max(1, fps * 2)
    if len(d) <= w:
        return np.array([float(d.mean())])
    return np.array([float(d[i:i + w].mean() + 1e-6) for i in range(0, len(d) - w, w)])


def verify_build(f, target=None, min_activity=0.25, min_peaks=3, quiet=False):
    """-> (ok, fails, warns). Exits nothing; caller decides."""
    fails, warns = [], []
    if not os.path.exists(f):
        return False, ["file missing: " + f], []
    dur = duration_of(f)

    # A. streams + duration
    info = subprocess.run([config.FFMPEG, "-i", f], capture_output=True, text=True).stderr
    ok_v = "h264" in info
    ok_a = "aac" in info or "mp4a" in info
    if not ok_v or not ok_a:
        fails.append(f"A: streams (h264={ok_v}, aac={ok_a})")
    if "320x" in info or "240x" in info:
        fails.append("A: resolution too small")
    if target and abs(dur - target) / target > 0.05:
        fails.append(f"A: duration {dur:.0f}s vs target {target:.0f}s (>5%)")

    # B. motion
    act = frame_activity(f)
    med = float(np.median(act))
    dead = [i * 2 for i, a in enumerate(act) if a < med * 0.18]
    if med < min_activity:
        fails.append(f"B: near-static video (activity {med:.3f} < {min_activity})"
                     f" => slideshow!")
    elif len(dead) > max(2, len(act) // 6):
        warns.append(f"B: {len(dead)} dead windows (low motion): {dead[:6]}")

    # C. audio
    rms = audio_rms_series(f)
    if float(np.median(rms)) < 0.01:
        fails.append("C: near-silent audio")
    peaks = audio_peak_count(f)
    if peaks < min_peaks:
        fails.append(f"C: too few audio peaks/clip hits ({peaks} < {min_peaks})")
    if float(rms.max()) > 0.97:
        fails.append("C: clipping risk (RMS window >0.97)")

    if not quiet:
        print("\n--- VERIFY MEASUREMENTS ---")
        print(f"file: {f} | duration: {dur:.1f}s")
        print(f"activity median: {med:.3f} (req > {min_activity})")
        print(f"audio peaks: {peaks} (req >= {min_peaks})")
        print(f"audio RMS median: {float(np.median(rms)):.3f}")
        for w in warns:
            print("WARN:", w)
        if fails:
            for x in fails:
                print("REJECT:", x)
        else:
            print("PASS (all measurable mandate rules satisfied)")
    return len(fails) == 0, fails, warns


def main():
    f = sys.argv[1] if len(sys.argv) > 1 else None
    if not f:
        print("usage: python verify.py <video.mp4> [target_duration_s]")
        sys.exit(2)
    target = float(sys.argv[2]) if len(sys.argv) > 2 else None
    ok, fails, _ = verify_build(f, target)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
