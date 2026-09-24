"""Beat building + SunnyV2 pacing rules + decide_effect."""
import random
from dataclasses import dataclass

import config
from speaker import rms_z

HEAVY_FX = {"shake", "glitch"}


@dataclass
class Beat:
    t: float
    dur: float
    line: str = ""
    intent: str = "buildup"
    effect: str = None
    punchword: str = ""
    clip_id: str = ""
    src_t0: float = 0.0


def find_punchwords(text):
    return [w.strip(".,!?\"'()").lower() for w in text.split()
            if w.strip(".,!?\"'()").lower() in config.PUNCH_WORDS]


def decide_effect(t, index, rms_z_val, punchword, since_last_fx,
                  since_heavy_fx, intent="buildup"):
    """SunnyV2 rules: 0-3s hook = hardest cut + zoom; beat every 2-4s;
    max ~1 heavy fx per 8-10s; never let the screen sit still >4s."""
    if t < config.HOOK_MAX_SEC:
        return "zoom_punch" if index == 0 else "fast_cut"
    w = (punchword or "").lower().strip(".,!?")
    if w in config.PUNCH_WORDS and rms_z_val > 1.0:
        return "zoom_punch"
    if intent == "shock" and rms_z_val > 1.2 and since_heavy_fx > 8.0:
        return "shake"
    if rms_z_val > 2.0 and since_heavy_fx > 8.0:
        return "shake"
    if rms_z_val > 1.5:
        return "flash"
    if since_last_fx > config.MAX_STILL_SEC:
        return random.choice(["zoom_punch", "flash", "slide"])
    return None


def build_beats(total_dur, lines_timed, rms, narr_words=None):
    """lines_timed: [(text, t0, t1, intent)]. Returns list[Beat] covering the
    narration with cuts every 2.2-3.6s, effects chosen from RMS + punch words."""
    beats = []
    t, idx, last_fx, last_heavy = 0.0, 0, -9.0, -9.0
    random.seed(config.RANDOM_SEED)  # deterministic after planning
    while t < total_dur - 0.4:
        # first cut lands at ~3s (hook), then 2.2-3.6s
        dur = min(config.HOOK_MAX_SEC, total_dur - t) if idx == 0 else \
            min(random.uniform(config.CUT_MIN_SEC, config.CUT_MAX_SEC), total_dur - t)
        line_text, intent = "", "buildup"
        for text, l0, l1, li in lines_timed:
            if l0 <= t < l1:
                line_text, intent = text, li
                break
        z = rms_z(rms, t, t + dur)
        punch = ""
        if narr_words:
            # words that START inside this beat - not words spanning its end
            spoken = " ".join(w for w, a, b in narr_words if t <= a < t + dur)
            hits = find_punchwords(spoken) or find_punchwords(line_text)
            punch = hits[0] if hits else ""
        fx = decide_effect(t, idx, z, punch, t - last_fx, t - last_heavy, intent)
        if fx:
            last_fx = t
            if fx in HEAVY_FX:
                last_heavy = t
        beats.append(Beat(t=t, dur=dur, line=line_text, intent=intent,
                          effect=fx, punchword=punch))
        t += dur
        idx += 1
    return beats
