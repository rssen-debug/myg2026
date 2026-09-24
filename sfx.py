"""sfx.py — synthetic cinematic SFX + a placement bed. No downloads, no
licensing questions, fully reproducible from math. Writes plain 16-bit WAVs.

BUG THIS AVOIDS
---------------
The common naive way to synthesise a sweep ("riser") is:

    f = f0 + (f1 - f0) * (t / T)
    y = sin(2*pi*f*t)                    # <-- WRONG

That does not sweep from f0 to f1. The instantaneous frequency of sin(2*pi*f*t)
is d/dt(f*t) = f + t*df/dt, so a sweep to f1 actually reaches f1 + (f1-f0): it
overshoots to roughly double the intended top frequency and sounds like a
broken siren. The correct construction integrates the frequency to get phase:

    phase = 2*pi * cumsum(f) / sr
    y = sin(phase)                       # <-- correct

Everything here is peak-normalised to PEAK_DBFS with short fades so the mix
never clips and there are no click artefacts at the file edges.

Run:  python sfx.py            -> assets/sfx/*.wav
"""
import os
import wave

import numpy as np

import config

SR = 44100
SFX_DIR = os.path.join(config.ASSETS_DIR, "sfx")
PEAK_DBFS = -6.0                      # headroom for the final mix


def _t(dur):
    return np.linspace(0.0, dur, int(SR * dur), endpoint=False)


def _chirp(f0, f1, dur, curve=1.0):
    """Correct frequency sweep: integrate frequency to get phase."""
    t = _t(dur)
    shape = (t / dur) ** curve
    f = f0 + (f1 - f0) * shape
    phase = 2.0 * np.pi * np.cumsum(f) / SR
    return t, phase


def _fade(y, ms=8.0):
    n = max(1, int(SR * ms / 1000.0))
    if len(y) < 2 * n:
        return y
    ramp = np.linspace(0.0, 1.0, n)
    y = y.copy()
    y[:n] *= ramp
    y[-n:] *= ramp[::-1]
    return y


def _normalise(y, peak_dbfs=PEAK_DBFS):
    peak = float(np.max(np.abs(y))) + 1e-12
    target = 10.0 ** (peak_dbfs / 20.0)
    return y * (target / peak)


def _write(path, y):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    y = _normalise(_fade(y))
    data = np.clip(y, -1.0, 1.0)
    pcm = (data * 32767.0).astype(np.int16)
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return path


# ------------------------------------------------------------------ sounds --
def braaam(dur=2.6):
    """Deep trailer BRAAAM: sub + overtones with slow swell and slight detune
    beating so it does not sound like a static sine."""
    t = _t(dur)
    f0 = np.linspace(1.0, 0.94, len(t))          # tiny downward drift
    y = (np.sin(2 * np.pi * 55.0 * f0 * t) * 0.60
         + np.sin(2 * np.pi * 110.0 * t) * 0.26
         + np.sin(2 * np.pi * 27.5 * t) * 0.48
         + np.sin(2 * np.pi * 55.7 * t) * 0.18)   # detune -> beating
    env = (1.0 - np.exp(-t * 9.0)) * np.exp(-t * 0.75)
    return y * env


def riser(dur=8.0, f0=120.0, f1=1400.0):
    """Correct exponential-ish sweep with a rising noise bed on top."""
    t, phase = _chirp(f0, f1, dur, curve=2.0)
    tone = np.sin(phase) * 0.7 + np.sin(2 * phase) * 0.16
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(len(t))
    # cheap one-pole low-pass so the noise rises with the sweep
    for _ in range(2):
        noise = np.convolve(noise, np.ones(8) / 8.0, mode="same")
    env = (t / dur) ** 1.6
    return tone * env + noise * env * 0.22


def sub_drop(dur=1.6, f0=170.0, f1=28.0):
    """Pitch-diving sub for reveals and hard cuts."""
    t, phase = _chirp(f0, f1, dur, curve=0.55)
    y = np.sin(phase) * np.exp(-t * 1.6)
    y += np.sin(2 * phase) * 0.12 * np.exp(-t * 3.0)
    return y


def hit(dur=0.7):
    """Impact: click transient + low thump + short noise burst."""
    t = _t(dur)
    rng = np.random.default_rng(11)
    thump = np.sin(2 * np.pi * 82.0 * t) * np.exp(-t * 13.0)
    click = rng.standard_normal(len(t)) * np.exp(-t * 90.0) * 0.55
    body = np.sin(2 * np.pi * 190.0 * t) * np.exp(-t * 26.0) * 0.25
    return thump + click + body


def whoosh(dur=1.0):
    """Filtered noise swell for cuts and transitions."""
    t = _t(dur)
    rng = np.random.default_rng(23)
    n = rng.standard_normal(len(t))
    for _ in range(2):
        n = np.convolve(n, np.ones(6) / 6.0, mode="same")
    env = np.exp(-((t - dur * 0.42) ** 2) / (dur * 0.11) ** 2)
    _, phase = _chirp(300.0, 900.0, dur)          # integrated phase, not f*t
    sweep = np.sin(phase) * 0.15
    return (n * 0.85 + sweep) * env


def glitch_tick(dur=0.35):
    """Digital stutter for UI / text reveals."""
    t = _t(dur)
    gate = (np.mod(t, 0.11) < 0.03).astype(float)
    return np.sin(2 * np.pi * 1400.0 * t) * gate * 0.8


def ui_click(dur=0.18):
    t = _t(dur)
    return (np.sin(2 * np.pi * 1800.0 * t) * np.exp(-t * 60.0)
            + np.sin(2 * np.pi * 900.0 * t) * np.exp(-t * 30.0) * 0.4)


BUILDERS = {
    "braaam": braaam,
    "riser": riser,
    "sub_drop": sub_drop,
    "hit": hit,
    "whoosh": whoosh,
    "glitch_tick": glitch_tick,
    "ui_click": ui_click,
}


def build_all(outdir=None):
    """Generate every SFX. Returns {name: path}."""
    outdir = outdir or SFX_DIR
    out = {}
    for name, fn in BUILDERS.items():
        out[name] = _write(os.path.join(outdir, f"{name}.wav"), fn())
    return out


# -------------------------------------------------------------------- bed ---
def _read_wav(path):
    with wave.open(path, "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
        sr = w.getframerate()
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, sr


def bed_stats(path):
    """Measure the impacts actually present in a built bed.

    This exists because the pling problem could not be measured where it was
    heard. Counting transients in the finished mix is hopeless: the narrator
    produces ~130 onsets per minute all by himself, so a drop from 15 impacts
    to 6 disappears into the noise. The bed contains no speech, so here the
    count is exact.

    Counts things that LAND, measured by attack (a 6 dB rise inside 40 ms),
    which is what the ear reacts to. Two consequences worth knowing: a riser
    counts as zero, because an 8 s crescendo has no attack, and two hits 0.3 s
    apart count as one, because that is how they are heard. Verified against
    click tracks of known length (15/15, 6/6, 3/3, 2/2).

    -> (n_impacts, mean_gap_seconds, min_gap_seconds)
    """
    x, sr = _read_wav(path)
    if x.size < sr // 10:
        return 0, 0.0, 0.0
    w = max(1, sr // 50)                      # 20 ms frames
    n = x.size // w
    e = np.sqrt((x[:n * w].reshape(n, w) ** 2).mean(axis=1))
    if e.max() <= 0:
        return 0, 0.0, 0.0
    # Detection is by attack, not by local maximum. A local-max test counts
    # the texture of a sustained sound: a riser is a smooth 8 s crescendo with
    # no attacks whatsoever, and it registered 7-10 "hits" because the RMS of
    # noise-based material wobbles a few percent from frame to frame. An
    # attack is a fast RISE, so that is what is measured - if the level climbs
    # 6 dB within 40 ms and it is above the floor, something landed.
    thr = e.max() * 0.10
    db = 20 * np.log10(e + 1e-9)
    hits = []
    for i in range(3, n - 1):
        if e[i] <= thr:
            continue
        rise = db[i] - db[i - 2]              # 40 ms window
        if rise < 6.0:
            continue
        if e[i] < e[i + 1]:                   # let the attack develop first
            continue
        t = i * w / sr
        if not hits or t - hits[-1] > 0.35:
            hits.append(t)
    if len(hits) < 2:
        return len(hits), 0.0, 0.0
    gaps = [b - a for a, b in zip(hits, hits[1:])]
    return len(hits), sum(gaps) / len(gaps), min(gaps)


def build_bed(events, total, out_wav, sfx_dir=None):
    """Mix a set of SFX hits into one bed track.

    events: [(time_seconds, name, gain_db)] - name must exist in BUILDERS.
    Returns the path. The bed is peak-normalised so it sits under narration.
    """
    sfx_dir = sfx_dir or SFX_DIR
    sr = SR
    bed = np.zeros(int(sr * (total + 0.5)), dtype=np.float32)
    for t0, name, gain_db in events:
        if name not in BUILDERS:
            continue
        p = os.path.join(sfx_dir, f"{name}.wav")
        if not os.path.exists(p):
            continue
        y, ysr = _read_wav(p)
        if ysr != sr:                     # naive resample, good enough for SFX
            idx = (np.arange(int(len(y) * sr / ysr)) * ysr / sr).astype(int)
            y = y[np.clip(idx, 0, len(y) - 1)]
        i = int(max(0.0, t0) * sr)
        if i >= len(bed):
            continue
        seg = min(len(y), len(bed) - i)
        bed[i:i + seg] += y[:seg] * (10.0 ** (gain_db / 20.0))
    peak = float(np.max(np.abs(bed))) + 1e-12
    if peak > 0.707:
        bed *= 0.707 / peak
    os.makedirs(os.path.dirname(os.path.abspath(out_wav)), exist_ok=True)
    with wave.open(out_wav, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(bed, -1, 1) * 32767).astype(np.int16).tobytes())
    return out_wav


def hit_events_for(beats, outro_at, bpm=0, snap_tol=0.08):
    """Convenience: derive SFX events from the story beats.
    Returns [(t, name, gain_db)] ready for build_bed().

    bpm: the tempo of the music bed actually used for this render. When given,
    impacts are snapped onto that beat grid (within snap_tol) so the sound
    design hits musically instead of merely simultaneously. Pass 0 to disable.

    The number of impacts is capped by config.SFX_MIN_GAP and
    config.SFX_MAX_PER_VIDEO. Sound design is punctuation: one hit every six
    seconds lands, one every two seconds is noise.
    """
    import cinema

    snapped = [0]

    def on_beat(t):
        if not bpm:
            return t
        s = cinema.beat(t, bpm, tol=snap_tol)
        if abs(s - t) > 1e-6:
            snapped[0] += 1
        return s

    # ---- candidate impacts -------------------------------------------------
    # Each candidate carries a priority so the cap below keeps the BEST
    # moments rather than the first ones it happens to see.
    cands = []          # (t, name, gain_db, priority)
    for i, b in enumerate(beats):
        if b.effect == "shake":
            cands.append((b.t, "hit", -11.0, 3))          # heaviest first
        elif b.effect == "zoom_punch":
            cands.append((b.t, "hit", -12.5, 2))
        if b.effect in ("flash", "glitch"):
            cands.append((b.t, "glitch_tick", -18.0, 1))
        if b.intent == "payoff" and i > 0:
            cands.append((max(0.0, b.t - 2.4), "riser", -19.0, 2))

    # ---- cap: this is what stops the every-two-seconds pinging ------------
    # A hit every ~6 s reads as punctuation. The first version fired on every
    # zoom_punch, which is the most common effect in the edit, so a 48 s short
    # got 15 impacts - it sounded like a broken notification, not a soundtrack.
    budget = min(config.SFX_MAX_PER_VIDEO,
                 max(2, int(outro_at / max(config.SFX_MIN_GAP, 0.1))))
    cands.sort(key=lambda c: (-c[3], c[0]))     # priority desc, then time asc
    kept = []
    for t, name, gain, _pri in cands:
        if len(kept) >= budget:
            break
        if any(abs(t - kt) < config.SFX_MIN_GAP for kt, _n, _g in kept):
            continue
        kept.append((t, name, gain))
    kept.sort(key=lambda k: k[0])               # back into timeline order

    events = [(max(0.0, on_beat(t) - 0.04), n, g) for t, n, g in kept]
    # the outro sting is structural: it marks the subscribe card, so it is not
    # part of the budget and never competes with a hit for a slot
    events.append((max(0.0, on_beat(outro_at) - 0.3), "sub_drop", -14.0))
    events.append((max(0.0, on_beat(outro_at)), "braaam", -11.0))
    hit_events_for.snapped = snapped[0]
    hit_events_for.total = len(events)
    hit_events_for.dropped = max(0, len(cands) - len(kept))
    return events


def main():
    paths = build_all()
    for name, p in paths.items():
        y, sr = _read_wav(p)
        peak_db = 20 * np.log10(max(float(np.max(np.abs(y))), 1e-9))
        print(f"  [ok] {name:12s} {len(y)/sr:5.2f}s  peak {peak_db:6.2f} dBFS")
    print(f"\nSFX written to {SFX_DIR}")


if __name__ == "__main__":
    main()
