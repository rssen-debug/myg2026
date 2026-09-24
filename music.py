"""Tiny numpy beat synthesizer: intro / rising / climax / outro / mysterious.

`mysterious` is not a beat bed. The other four are kick-and-hat driven, which
is what a highlight reel wants and what a mystery story does not: a kick every
0.48 s fights the narrator and makes the video feel busy. That style is a
continuous low drone with slow minor-key swells instead - no percussion at all,
so it can sit under speech for minutes without competing with it.
"""
import wave

import numpy as np

import config

SR = 44100

STYLES = {
    "intro":   {"bpm": 92,  "kick": 1.0, "hat": 0.25, "bass": 0.55, "pad": 0.5},
    "rising":  {"bpm": 110, "kick": 1.0, "hat": 0.5,  "bass": 0.7,  "pad": 0.4},
    "climax":  {"bpm": 126, "kick": 1.0, "hat": 0.8,  "bass": 0.9,  "pad": 0.3},
    "outro":   {"bpm": 92,  "kick": 0.6, "hat": 0.2,  "bass": 0.5,  "pad": 0.6},
}


def _kick(n):
    t = np.arange(n) / SR
    env = np.exp(-t * 14)
    return np.sin(2 * np.pi * (55 + 90 * np.exp(-t * 30)) * t) * env


def _hat(n):
    t = np.arange(n) / SR
    rng = np.random.default_rng(7)
    return rng.standard_normal(n) * np.exp(-t * 60) * 0.5


def _bass(n, freq):
    t = np.arange(n) / SR
    env = np.clip(1.0 - t / (n / SR), 0, 1) ** 0.5
    return (np.sin(2 * np.pi * freq * t) +
            0.4 * np.sin(2 * np.pi * freq * 2 * t)) * env * 0.5


def _pad(n, freq):
    t = np.arange(n) / SR
    ramp = min(1.0, n / SR / 2.0)
    fade = np.clip(t / max(0.2, ramp), 0, 1) * np.clip((n / SR - t) / 1.0, 0, 1)
    return (np.sin(2 * np.pi * freq * t) +
            np.sin(2 * np.pi * freq * 1.5 * t) * 0.6) * fade * 0.15


def _synthesize_mysterious(duration):
    """Continuous dark drone with slow swells. No percussion.

    Built for tension under narration: two detuned low sines beat slowly
    against each other (the 0.6 Hz difference is what makes it feel uneasy
    rather than static), a quiet fifth sits on top, the whole thing breathes at
    0.07 Hz, and every ~9 s a minor-key swell rises and decays. The result has
    no transients, so it never collides with a cut, a captioned word or the
    narrator - which is exactly why it can play for the whole video.
    """
    total = max(SR, int(duration * SR))
    t = np.arange(total) / SR
    out = np.zeros(total, dtype=np.float64)

    # --- drone: two sines a fraction apart -> slow beating, plus a fifth
    out += np.sin(2 * np.pi * 55.00 * t) * 0.30        # A1
    out += np.sin(2 * np.pi * 55.60 * t) * 0.26        # beats at ~0.6 Hz
    out += np.sin(2 * np.pi * 82.41 * t) * 0.09        # E2, a fifth up
    out *= 1.0 + 0.16 * np.sin(2 * np.pi * 0.07 * t)   # slow breathing

    # --- sparse minor swells (A minor: A, C, E, G)
    swell_notes = [110.0, 130.81, 98.0, 164.81]
    every = 9.0
    for i in range(max(0, int(duration / every))):
        pos = int(i * every * SR)
        seg = min(int((every + 4.0) * SR), total - pos)
        if seg <= 0:
            continue
        ts = np.arange(seg) / SR
        env = np.clip(ts / 3.5, 0, 1) * np.exp(-ts / 6.0)
        f = swell_notes[i % len(swell_notes)]
        out[pos:pos + seg] += (np.sin(2 * np.pi * f * ts)
                               + 0.45 * np.sin(2 * np.pi * f * 1.5 * ts)) \
            * env * 0.085

    # --- very sparse low pulse, 4 bars apart: keeps a pulse without clutter
    pulse = int(5.0 * SR)
    for i in range(max(0, int(duration / 14.0))):
        pos = int(i * 14.0 * SR)
        if pos + pulse > total:
            break
        tp = np.arange(pulse) / SR
        out[pos:pos + pulse] += np.sin(2 * np.pi * 41.2 * tp) \
            * np.exp(-tp * 1.6) * 0.11

    peak = np.max(np.abs(out)) or 1.0
    return out / peak * 0.8


def synthesize(duration, style="climax"):
    if style == "mysterious":
        return _synthesize_mysterious(duration)
    st = STYLES.get(style, STYLES["climax"])
    beat = 60.0 / st["bpm"]
    total = int(duration * SR) + SR
    out = np.zeros(total, dtype=np.float64)
    kick_n = int(0.4 * SR)
    hat_n = int(0.1 * SR)
    bass_n = int(beat * SR)
    notes = [55.0, 55.0, 65.4, 49.0]
    nb = max(1, int(duration / beat))
    # intensity ramp for 'rising'
    for i in range(nb):
        pos = int(i * beat * SR)
        amp = min(1.0, 0.4 + 0.6 * i / nb) if style == "rising" else 1.0
        if pos + kick_n < total and (i % 2 == 0 or st["kick"] >= 1.0):
            out[pos:pos + kick_n] += _kick(kick_n) * st["kick"] * amp
        if pos + hat_n < total:
            hp = pos + int(beat * SR / 2)
            if hp + hat_n < total:
                out[hp:hp + hat_n] += _hat(hat_n) * st["hat"] * amp
        if pos + bass_n < total:
            out[pos:pos + bass_n] += _bass(bass_n, notes[(i // 2) % 4]) * st["bass"] * amp
        if i % 8 == 0 and pos + int(8 * beat * SR) < total:
            seg = int(8 * beat * SR)
            out[pos:pos + seg] += _pad(seg, notes[(i // 8) % 4] * 2) * st["pad"]
    peak = np.max(np.abs(out)) or 1.0
    out = out / peak * 0.8
    return out[:int(duration * SR)]


def save_wav(path, data, sr=SR):
    pcm = (np.clip(data, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def bed_stats(path):
    """Measure a written music bed -> (attacks_per_min, seconds).

    This is how "is the music carrying the video or hitting it" gets a number.
    It works because the two styles are opposites: a kick-and-hat bed lands a
    hit every half second, a drone has no transients at all. Measured here:
    mysterious 0.0 attacks/min, climax 122, rising 108.

    A bass-variation figure was tried first and thrown away - it read 1.14 for
    the drone and 1.16 for the beat bed, so it could not tell them apart and
    would only have looked like evidence.
    """
    x, sr = _read(path)
    if x.size < sr // 10:
        return 0.0, 0.0
    dur = x.size / sr
    w = max(1, sr // 50)
    n = x.size // w
    e = np.sqrt((x[:n * w].reshape(n, w) ** 2).mean(axis=1))
    if e.max() <= 0:
        return 0.0, dur
    db = 20 * np.log10(e + 1e-9)
    thr = e.max() * 0.10
    attacks, last = 0, -9.0
    for i in range(3, n - 1):
        if e[i] > thr and db[i] - db[i - 2] > 6.0 and e[i] >= e[i + 1]:
            t = i * w / sr
            if t - last > 0.35:
                attacks += 1
                last = t
    return attacks / dur * 60.0, dur


def _read(path):
    """-> (float32 mono, sample rate) for a wav written by save_wav."""
    import wave
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
        width = w.getsampwidth()
    if width == 2:
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    else:
        x = np.frombuffer(raw, dtype=np.float32)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def make_music(path, duration, style="climax"):
    data = synthesize(max(5.0, duration), style)
    # fade out last 2.5s
    fade = min(int(2.5 * SR), len(data) - 1)
    data[-fade:] *= np.linspace(1, 0, fade)
    return save_wav(path, data)
