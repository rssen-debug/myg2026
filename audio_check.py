#!/usr/bin/env python3
"""Measure a finished render's mix instead of trusting your memory of it.

The first version of this pipeline sounded wrong in ways that were obvious to
a listener and invisible to the log: MrBeast was inaudible, the narrator and
the footage talked over each other, and a pling fired every two seconds. None
of that shows up in an exit code, so this script turns each complaint into a
number you can compare between two files.

    python audio_check.py output/short_x.mp4 [output/short_y.mp4]

Reports per file:
  loudness      integrated LUFS / true peak / LRA (target -14, TP <= -1.0)
  onset density onsets per minute in 60-250 Hz, where an impact lives.
                CAVEAT: narration is speech, so this floor is ~130/min and the
                number cannot resolve a drop from 15 impacts to 6. For the SFX
                question run --bed on work/sfx.wav (or any bed built by
                sfx.build_bed) - no speech in there, so the count is exact
  bass variance spread of the 30-120 Hz band. Printed without a verdict: it
                measured 1.14 on a drone and 1.16 on a beat bed, so it cannot
                separate them. Compare two files, or read the run log, which
                reports attacks per minute for the bed (drone 0, bed ~120)
  speech band   energy 300-3000 Hz, where the narrator and the footage both
                live. Comparing two files shows whether the footage got
                louder, but it cannot separate two voices in one file - use
                the isolated duck test in the notes for that
"""
import os
import re
import subprocess
import sys
import tempfile

import numpy as np

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
SR = 22050


def _decode(path, sr=SR):
    """-> mono float32 waveform + sample rate, via ffmpeg pipe."""
    p = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-ac", "1",
                        "-ar", str(sr), "-f", "f32le", "-"],
                       capture_output=True)
    if p.returncode != 0 or not p.stdout:
        return None, sr
    return np.frombuffer(p.stdout, dtype=np.float32), sr


def _band(x, sr, lo, hi):
    """Cheap block-FFT band-pass. Not a real filter - good enough for levels.

    Non-overlapping blocks with the window applied on the way in and out, so
    the result is Hann^2 weighted. That tapers each block, which costs a
    little level accuracy and costs nothing for what this is used for
    (comparing two files with the same method).
    """
    if x.size < 1024:
        return x
    n = 1 << 14
    freqs = np.fft.rfftfreq(n, 1 / sr)
    mask = (freqs >= lo) & (freqs <= hi)
    if not mask.any():
        return np.zeros_like(x)
    win = np.hanning(n).astype(np.float32)
    out = np.zeros(x.size, dtype=np.float32)
    for i in range(0, x.size, n):
        seg = x[i:i + n]
        if seg.size < n:
            seg = np.pad(seg, (0, n - seg.size))
        spec = np.fft.rfft(seg * win)
        spec[~mask] = 0
        blk = (np.fft.irfft(spec, n).astype(np.float32) * win)[:x.size - i]
        out[i:i + blk.size] = blk
    return out


def _envelope(x, sr, ms=20):
    w = max(1, int(sr * ms / 1000))
    n = x.size // w
    if n == 0:
        return np.zeros(1, dtype=np.float32)
    return np.sqrt((x[:n * w].reshape(n, w) ** 2).mean(axis=1))


def _bursts(env, sr, ms=20, rise=3.0, floor=1e-4, min_gap_ms=120):
    """Count sharp rises in an envelope = percussive hits.

    A burst is an envelope frame that is `rise` times the local floor and then
    decays. Smooth sustained material (a drone, a pad, speech) produces almost
    none; a click track produces one per click.
    """
    if env.size < 3:
        return 0, []
    t = np.arange(env.size) * ms / 1000.0
    hits, last = [], -9.0
    for i in range(1, env.size - 1):
        local = np.median(env[max(0, i - 12):i + 1]) + floor
        if (env[i] > 2.5 * local and env[i] > env[i - 1]
                and env[i] >= env[i + 1] and t[i] - last >= min_gap_ms / 1000.0):
            hits.append(float(t[i]))
            last = t[i]
    return len(hits), hits


def _loudness(path):
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", path, "-af",
                        "loudnorm=print_format=summary", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    out = {}
    for key, pat in (("lufs", r"Input Integrated:\s*([-\d.]+)"),
                     ("tp", r"Input True Peak:\s*([-\d.]+)"),
                     ("lra", r"Input LRA:\s*([-\d.]+)")):
        m = re.search(pat, r)
        out[key] = float(m.group(1)) if m else None
    return out


def measure(path):
    x, sr = _decode(path)
    if x is None or x.size == 0:
        return None
    dur = x.size / sr
    loud = _loudness(path)

    total, _ = _bursts(_envelope(_band(x, sr, 2000, 6000), sr), sr)

    bass_env = _envelope(_band(x, sr, 30, 120), sr)
    cv = float(bass_env.std() / (bass_env.mean() + 1e-9)) if bass_env.size else 0.0

    sp = _band(x, sr, 300, 3000)
    speech_db = 20 * np.log10(np.sqrt((sp ** 2).mean()) + 1e-9)

    return {"path": os.path.basename(path), "dur": dur, "clicks": total,
            "clip_rate": total / dur * 60 if dur else 0.0, "bass_cv": cv,
            "speech_db": float(speech_db), "loud": loud}


def bed_report(path):
    """Exact impact measurement for a bed with no speech in it."""
    x, sr = _decode(path)
    if x is None or x.size == 0:
        print(f"  {path}: could not decode")
        return
    dur = x.size / sr
    w = max(1, sr // 50)
    n = x.size // w
    e = np.sqrt((x[:n * w].reshape(n, w) ** 2).mean(axis=1))
    thr = (e.max() if e.size else 0) * 0.02
    hits = []
    for i in range(1, n - 1):
        if e[i] > thr and e[i] >= e[i - 1] and e[i] > e[i + 1]:
            t = i * w / sr
            if not hits or t - hits[-1] > 0.25:
                hits.append(t)
    gaps = [b - a for a, b in zip(hits, hits[1:])]
    rate = len(hits) / dur * 60 if dur else 0.0
    print(f"  {os.path.basename(path)[:52]}")
    print(f"    length       {dur:6.1f} s")
    print(f"    impacts      {len(hits):4d}  ({rate:4.1f}/min)")
    if gaps:
        print(f"    spacing      mean {sum(gaps)/len(gaps):4.1f}s  "
              f"closest {min(gaps):4.1f}s")
    print("    " + ("exact count - a bed has no speech to hide in" if len(hits)
                    else "no impacts found"))
    print()


def report(m):
    l = m["loud"]
    print(f"  {m['path'][:52]}")
    print(f"    length       {m['dur']:6.1f} s")
    if l["lufs"] is not None:
        warn = "" if l["lufs"] >= -16 else "   <-- quiet, target -14"
        print(f"    loudness     {l['lufs']:6.1f} LUFS{warn}")
    if l["tp"] is not None:
        warn = "   <-- hot, keep <= -1.0 for YouTube" if l["tp"] > -1.0 else ""
        print(f"    true peak    {l['tp']:6.1f} dBTP{warn}")
    if l["lra"] is not None:
        print(f"    range        {l['lra']:6.1f} LU")
    print(f"    onsets 60-250 Hz {m['clicks']:4d}  ({m['clip_rate']:4.1f}/min)"
          "   (includes narration - use --bed for the SFX count)")
    # No interpretation here on purpose. Calibrating this on isolated beds gave
    # 1.14 for the drone against 1.16 for the kick-and-hat bed, so it cannot
    # tell the two apart and any label it printed would be made up. The number
    # is only useful next to another file measured the same way. To actually
    # measure which kind of bed is in the video, use music.bed_stats() or the
    # run log, which prints attacks per minute (drone 0, beat bed ~120).
    print(f"    bass 30-120 Hz variance {m['bass_cv']:.3f}"
          "   (no verdict - compare two files, or see the run log for the bed)")
    print(f"    speech band  {m['speech_db']:6.1f} dB")
    print()


def main(argv):
    if "--bed" in argv:
        argv = [a for a in argv if a != "--bed"]
        if not argv:
            print("usage: python audio_check.py --bed <sfx_bed.wav>")
            return 2
        for f in argv:
            if os.path.exists(f):
                bed_report(f)
            else:
                print(f"  {f}: not found")
        return 0
    if not argv:
        print(__doc__)
        print("usage: python audio_check.py <video.mp4> [<video.mp4> ...]")
        return 2
    rows = []
    for f in argv:
        if not os.path.exists(f):
            print(f"  {f}: not found")
            continue
        m = measure(f)
        if m is None:
            print(f"  {f}: could not decode")
            continue
        rows.append(m)
    for m in rows:
        report(m)
    if len(rows) > 1:
        a, b = rows[0], rows[-1]
        print(f"  {a['path'][:24]} -> {b['path'][:24]}")
        print(f"    bursts/min {a['clip_rate']:5.1f} -> {b['clip_rate']:5.1f}"
              f"   ({b['clip_rate']-a['clip_rate']:+.1f})")
        print(f"    speech band {a['speech_db']:.1f} -> {b['speech_db']:.1f} dB"
              f"   ({b['speech_db']-a['speech_db']:+.1f} dB)")
        print(f"    bass steadiness {a['bass_cv']:.3f} -> {b['bass_cv']:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
