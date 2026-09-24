"""Edge-TTS with WordBoundary capture -> zero-drift karaoke timestamps."""
import asyncio
import os
import re
import subprocess

import edge_tts

import config

TICK = 1e7  # offsets are 100ns ticks


def _split_sentence(text, t0, t1):
    """Spread a sentence's span over its words, weighted by word length.

    Used when the service only reports sentence boundaries: a longer word takes
    longer to say, so length weighting tracks real speech far better than an
    even split. Not as good as true word boundaries, but it keeps caption
    timing close enough that no line drifts visibly.
    """
    parts = text.split()
    if not parts:
        return []
    weights = [max(1, len(re.sub(r"[^\w']", "", w))) for w in parts]
    total = float(sum(weights))
    out, acc = [], t0
    span = max(0.0, t1 - t0)
    for w, wt in zip(parts, weights):
        d = span * (wt / total)
        out.append((w, acc, acc + d))
        acc += d
    return out


async def _tts_with_words(text, out_mp3, voice, rate, boundary="WordBoundary"):
    words, sentences = [], []
    kwargs = {"rate": rate}
    if boundary:
        kwargs["boundary"] = boundary
    try:
        com = edge_tts.Communicate(text, voice, **kwargs)
    except TypeError:
        # pre-7.x edge-tts has no `boundary` parameter at all; it always emits
        # WordBoundary, so dropping the argument is the correct degradation
        com = edge_tts.Communicate(text, voice, rate=rate)
    with open(out_mp3, "wb") as f:
        async for chunk in com.stream():
            t = chunk["type"]
            if t == "audio":
                f.write(chunk["data"])
            elif t == "WordBoundary":
                t0 = chunk["offset"] / TICK
                words.append((chunk["text"], t0, t0 + chunk["duration"] / TICK))
            elif t == "SentenceBoundary":
                t0 = chunk["offset"] / TICK
                sentences.append((chunk["text"], t0,
                                  t0 + chunk["duration"] / TICK))
    if not words and sentences:
        for txt, a, b in sentences:
            words.extend(_split_sentence(txt, a, b))
    return words


_RESOLVED_VOICE = None


def _voice_ladder(preferred=None):
    ladder = []
    for name in [preferred or config.TTS_VOICE_EN, *getattr(config, "TTS_VOICE_FALLBACKS", [])]:
        if name and name not in ladder:
            ladder.append(name)
    return ladder


def tts_with_words(text, out_mp3, voice=None, rate=None):
    """-> [(word, t0, t1)] synced to out_mp3.

    Tries the documentary voice first. A dead voice name falls through the
    ladder instead of killing the run. The voice that actually spoke is
    remembered so later lines, and the word-rate calibration, match it.
    """
    global _RESOLVED_VOICE
    rate = rate or config.TTS_RATE
    ladder = [_RESOLVED_VOICE] if _RESOLVED_VOICE and not voice else _voice_ladder(voice)
    last = None
    for candidate in ladder:
        try:
            if os.path.exists(out_mp3):
                os.remove(out_mp3)
            words = asyncio.run(_tts_with_words(text, out_mp3, candidate, rate))
        except Exception as e:
            last = e
            print(f"  [tts] {candidate} failed ({type(e).__name__}) — next voice")
            continue
        if os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 800 and words:
            if candidate != _RESOLVED_VOICE:
                print(f"  [tts] voice {candidate}  rate {rate}")
            _RESOLVED_VOICE = candidate
            return words
        last = RuntimeError(f"{candidate} returned no words")
        print(f"  [tts] {candidate} had no word boundaries — next voice")
    if last:
        raise last
    raise RuntimeError("no TTS voice produced audio")


# ------------------------------------------------------------ calibration ---
CALIB_PATH = os.path.join(config.ASSETS_DIR, "tts_calibration.json")

# Fallback when nothing has been measured yet. GuyNeural at +8% measured
# 2.96 words/s. Andrew Multilingual at +4% is a touch slower; calibration
# replaces this on the first run. A short guess is safer than a long one:
# an over-long script fails the duration gate.
DEFAULT_WORDS_PER_SEC = 2.70

_CALIB_LINES = [
    "He lost everything in one night, and nobody saw it coming at all.",
    "This is the story of how a single decision undid years of quiet work.",
    "The internet did not believe a single word of it at first, and then it did.",
    "It started on a livestream that almost nobody watched, on a Tuesday.",
]


def calibrate(voice=None, rate=None, verbose=True):
    """Measure this machine's real speaking rate and cache it.

    Why measure instead of trusting a constant: the rate depends on the voice,
    the +N% setting, and the edge-tts/Edge backend version. It changed under
    this project once already. Measuring costs ~4 s of TTS and makes the word
    budget a fact about the voice actually being used.
    -> words per second (float).
    """
    import json
    voice = voice or config.TTS_VOICE_EN
    rate = rate or config.TTS_RATE
    tmp = os.path.join(config.ASSETS_DIR, "_calib.mp3")
    os.makedirs(os.path.dirname(CALIB_PATH), exist_ok=True)
    words = spent = 0
    try:
        for i, line in enumerate(_CALIB_LINES):
            out = tmp.replace(".mp3", f"{i}.mp3")
            tts_with_words(line, out, voice=voice, rate=rate)
            d = probe_duration(out)
            os.remove(out)
            if d > 0:
                words += len(line.split())
                spent += d
    except Exception as e:
        if verbose:
            print(f"  [tts] calibration failed ({type(e).__name__}) - "
                  f"keeping {DEFAULT_WORDS_PER_SEC} words/s")
        return DEFAULT_WORDS_PER_SEC
    if not spent:
        return DEFAULT_WORDS_PER_SEC
    wps = words / spent
    try:
        used = _RESOLVED_VOICE or voice
        with open(CALIB_PATH, "w", encoding="utf-8") as f:
            json.dump({"words_per_sec": round(wps, 4), "voice": used,
                       "rate": rate, "samples": len(_CALIB_LINES),
                       "words": words, "seconds": round(spent, 2)}, f, indent=1)
    except OSError:
        pass
    if verbose:
        print(f"  [tts] calibrated {voice} {rate}: {words} words in "
              f"{spent:.1f}s -> {wps:.2f} words/s")
    return wps


def words_per_sec(verbose=False, auto=True):
    """Calibrated rate for this machine, measuring once if never done.

    The calibration file is deliberately NOT committed: the rate belongs to a
    voice + rate + backend combination, so a value measured elsewhere would be
    a guess wearing a lab coat. Measuring costs about 4 seconds, once, and is
    then cached next to the other generated assets.
    """
    import json
    try:
        with open(CALIB_PATH, encoding="utf-8") as f:
            data = json.load(f)
        v = float(data.get("words_per_sec") or 0)
        if v > 0.5 and data.get("rate") == config.TTS_RATE and data.get("voice") in _voice_ladder():
            if verbose:
                print(f"  [tts] using calibrated {v:.2f} words/s "
                      f"({data.get('voice')} {data.get('rate')})")
            return v
    except (OSError, ValueError, TypeError):
        pass
    if auto:
        try:
            return calibrate()
        except Exception as e:
            print(f"  [tts] calibration unavailable ({type(e).__name__}) - "
                  f"using {DEFAULT_WORDS_PER_SEC} words/s")
    return DEFAULT_WORDS_PER_SEC


def probe_duration(path):
    """Duration in seconds, 0.0 if it cannot be measured (never None).
    Uses ffprobe when available and falls back to parsing ffmpeg's output,
    because imageio-ffmpeg ships ffmpeg WITHOUT ffprobe."""
    if config.FFPROBE:
        out = subprocess.run(
            [config.FFPROBE, "-v", "error", "-show_entries",
             "format=duration", "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True).stdout.strip()
        try:
            return float(out)
        except ValueError:
            pass
    try:
        err = subprocess.run([config.FFMPEG, "-hide_banner", "-i", path],
                             capture_output=True, text=True).stderr
        m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err)
        if m:
            return (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                    + float(m.group(3)))
    except Exception:
        pass
    return 0.0


def concat_audio(files, out_wav):
    """Concatenate mp3/wav segments into one 44.1k mono wav.

    Uses the concat FILTER, not the concat demuxer. The demuxer only works on
    files that already share codec and stream parameters, and these do not:
    the narration is 44.1 kHz wav while edge-tts hands back 24 kHz mono mp3.
    Pointed at mixed inputs the demuxer does not fail - it stops early, so
    appending a 4.9 s outro to a 6 s body produced 6.6 s of audio and the
    video's closing card played in silence with the subscribe line never
    spoken. The filter graph resamples per input, so the result is right.

    Length is verified afterwards: a silent truncation here is invisible
    everywhere else in the pipeline and has to fail loudly instead.
    """
    if not files:
        return out_wav[:0]
    args = [config.FFMPEG, "-y", "-v", "error"]
    for p in files:
        args += ["-i", p]
    fc = ("".join(f"[{i}:a]" for i in range(len(files)))
          + f"concat=n={len(files)}:v=0:a=1[cc];"
          + f"[cc]aformat=sample_fmts=flt:sample_rates={config.TTS_SAMPLE_RATE}"
            f":channel_layouts=mono[out]")
    args += ["-filter_complex", fc, "-map", "[out]", out_wav]
    subprocess.run(args, check=True, capture_output=True)

    want = sum(probe_duration(f) for f in files)
    got = probe_duration(out_wav)
    if want > 0.5 and got and got < want * 0.97:
        raise RuntimeError(
            f"concat_audio dropped audio: {got:.2f}s of {want:.2f}s from "
            f"{len(files)} inputs - refusing to return a truncated narration")
    return out_wav


def _main():
    """python tts.py              -> show the rate in use
       python tts.py --calibrate  -> measure it on this machine and cache"""
    import sys
    if "--calibrate" in sys.argv:
        wps = calibrate()
        print(f"  saved to {CALIB_PATH}")
        print(f"  -> {wps:.2f} words/s will be used for script length")
        return
    print(f"  voice     {config.TTS_VOICE_EN}  rate {config.TTS_RATE}")
    print(f"  words/s   {words_per_sec():.2f}")
    print(f"  source    {CALIB_PATH if os.path.exists(CALIB_PATH) else 'default (not yet calibrated)'}")
    print("  python tts.py --calibrate  to measure on this machine")


if __name__ == "__main__":
    _main()
