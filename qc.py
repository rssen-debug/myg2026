"""QC gates: concrete failures fed back into the pipeline (max 2 retries)."""
import subprocess

import config
from sources import ffprobe_meta
from tts import probe_duration


def script_qc_text(lines, words_target):
    """PRE-TTS gate (word-based). Single source of truth for script QC;
    orchestrator calls this before spending time on TTS."""
    fails = []
    if not lines:
        return ["script is empty"]
    hook_words = len(lines[0]["text"].split())
    if hook_words > config.HOOK_MAX_WORDS:
        fails.append(f"hook is {hook_words} words (max {config.HOOK_MAX_WORDS}); "
                     "it must be a question or a bold claim")
    total = sum(len(l["text"].split()) for l in lines)
    if total < words_target * 0.55:
        fails.append(f"only {total} words, need ~{words_target}")
    if total > words_target * 1.4:
        fails.append(f"{total} words is too many, need ~{words_target}")
    intents = [l.get("intent") for l in lines]
    if intents.count("shock") + intents.count("payoff") < max(1, len(lines) // 6):
        fails.append("too few shock/payoff beats - add open loops")
    return fails


def script_qc(lines_timed, target_dur):
    """POST-TTS gate (real seconds from synthesized audio).
    lines_timed: [(text, t0, t1, intent)]"""
    fails = []
    if not lines_timed:
        return ["script is empty"]
    first = lines_timed[0]
    hook_words = len(first[0].split())
    hook_len = first[2] - first[1]
    if hook_words > config.HOOK_MAX_WORDS or hook_len > config.HOOK_MAX_SEC + 1.5:
        fails.append(f"hook too long ({hook_words} words / {hook_len:.1f}s) - "
                     f"max {config.HOOK_MAX_WORDS} words, needs a question or bold claim")
    total = lines_timed[-1][2]
    if total < target_dur * 0.5:
        fails.append(f"script too short: {total:.0f}s vs target {target_dur}s - expand sections")
    if total > target_dur * 1.35:
        fails.append(f"script too long: {total:.0f}s vs target {target_dur}s - trim it")
    intents = [li for _, _, _, li in lines_timed]
    if intents.count("shock") + intents.count("payoff") < max(1, len(lines_timed) // 6):
        fails.append("too few shock/payoff moments - add open loops and reveals")
    return fails


def timeline_qc(beats, covered_ratio):
    fails = []
    if covered_ratio < config.MIN_COVERAGE:
        fails.append(f"coverage {covered_ratio:.0%} < {config.MIN_COVERAGE:.0%}")
    heavy = [b for b in beats if b.effect in ("shake", "glitch")]
    per10 = len(heavy) / max(beats[-1].t + beats[-1].dur, 1) * 10 if beats else 0
    if per10 > config.MAX_HEAVY_FX_PER_10S + 0.5:
        fails.append(f"effect spam: {per10:.1f} heavy fx / 10s")
    if beats and not beats[0].effect:
        fails.append("no hook effect on beat 0")
    return fails


def final_qc(mp4, target_dur):
    fails = []
    meta = ffprobe_meta(mp4)
    dur = probe_duration(mp4)
    if dur <= 1.0:
        return ["output missing or corrupt"]
    if abs(dur - target_dur) > target_dur * 0.25:
        fails.append(f"length {dur:.0f}s vs target {target_dur}s")
    if not meta.get("has_audio"):
        fails.append("no audio stream")
    # loudness check (info only -> fail only on silence)
    try:
        r = subprocess.run(
            [config.FFMPEG, "-hide_banner", "-i", mp4, "-af",
             "loudnorm=print_format=json", "-f", "null", "-"],
            capture_output=True, text=True, timeout=600)
        import json as _json, re
        m = re.search(r"\{.*\}", r.stderr, re.S)
        if m:
            loud = _json.loads(m.group(0))
            if float(loud.get("input_i", -99)) < -45:
                fails.append("audio nearly silent (< -45 LUFS)")
    except Exception:
        pass
    return fails
