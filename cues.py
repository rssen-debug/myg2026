"""cues.py — the Director's visual cues turned into real overlays.

WHY THIS EXISTS
---------------
gfx.py can draw a lower third, a tweet card, a stat slam. But knowing WHEN to
draw them is the hard part: something has to decide "here a person is
introduced" or "this sentence is a claim that needs a receipt". Without that
decision the whole gfx library is dead code and the pipeline can only do glow
titles and one stat card.

So the Director (LLM) tags each script line with a cue, this module validates
that tag (LLMs emit creative JSON), draws the asset, and hands the renderer a
list of overlays with segment-local timings.

Cue schema (what the Director is asked to produce):
    {"type": "person",
     "name": "...", "sub": "..."}
    {"type": "social", "kind": "tweet|discord|youtube|phone", ...fields...}
    {"type": "stat", "big": "1M+", "label": "VIEWS IN HOURS"}
    {"type": "timeline", "items": [["Aug 8", "the stream"], ...]}
    {"type": "chart", "data": [["before", 3], ["after", 41]]}

Everything here degrades: an unusable cue is dropped, a render failure skips
that cue, and the run continues. Graphics are never allowed to kill a render.
"""
import os
import re

import config
import gfx

# How much of the frame width each asset occupies, where it sits, and how long
# it stays. Keyed by _label(), i.e. per social flavour rather than one generic
# "social" entry - a phone chat is narrow, a tweet card is wide.
# pos: 'center' | 'lower_safe' (sits above the caption zone) | 'fill'
LAYOUT = {
    "person":   dict(frac=0.86, pos="lower_safe", dur=2.6),
    "tweet":    dict(frac=0.78, pos="center",     dur=2.8),
    "discord":  dict(frac=0.76, pos="center",     dur=2.8),
    "youtube":  dict(frac=0.80, pos="center",     dur=2.8),
    "phone":    dict(frac=0.52, pos="center",     dur=2.6),
    "stat":     dict(frac=0.52, pos="center",     dur=1.8),
    "timeline": dict(frac=0.84, pos="center",     dur=2.8),
    "chart":    dict(frac=0.56, pos="center",     dur=2.8),
}
DEFAULT_LAYOUT = dict(frac=0.60, pos="center", dur=2.4)

SOCIAL_KINDS = ("tweet", "discord", "youtube", "phone")


# ------------------------------------------------------------- validation --
def _s(v, limit=140):
    return str(v).strip()[:limit] if v is not None else ""


def _clean_lines(v, max_items=6):
    """Accept [[a, b], ...] or [{"x":..,"y":..}, ...] from the LLM."""
    out = []
    if not isinstance(v, list):
        return out
    for item in v[:max_items]:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            a, b = _s(item[0], 40), _s(item[1], 60)
        elif isinstance(item, dict):
            a = _s(item.get("date") or item.get("label") or item.get("x"), 40)
            b = _s(item.get("text") or item.get("value") or item.get("y"), 60)
        else:
            continue
        if a and b:
            out.append((a, b))
    return out


def norm_cue(raw):
    """Validate a Director cue. Returns a normalised dict, or None."""
    if not isinstance(raw, dict):
        return None
    t = _s(raw.get("type"), 20).lower()
    if not t:
        return None

    if t == "person":
        name = _s(raw.get("name"), 28)
        if not name:
            return None
        return {"type": "person", "name": name.upper(), "sub": _s(raw.get("sub"), 64)}

    if t == "social":
        kind = _s(raw.get("kind"), 12).lower() or "tweet"
        if kind not in SOCIAL_KINDS:
            kind = "tweet"
        if kind == "tweet":
            body = _s(raw.get("body"), 220)
            if not body:
                return None
            return {"type": "social", "kind": "tweet",
                    "name": _s(raw.get("name"), 28) or "user",
                    "handle": _s(raw.get("handle"), 24) or "@user",
                    "body": body, "meta": _s(raw.get("meta"), 48),
                    "likes": _s(raw.get("likes"), 12) or "12K"}
        if kind == "discord":
            body = _s(raw.get("body"), 220)
            if not body:
                return None
            return {"type": "social", "kind": "discord",
                    "channel": _s(raw.get("channel"), 24) or "#general",
                    "name": _s(raw.get("name"), 28) or "user",
                    "body": body, "time": _s(raw.get("time"), 24) or "Today"}
        if kind == "youtube":
            title = _s(raw.get("title"), 70)
            if not title:
                return None
            return {"type": "social", "kind": "youtube", "title": title,
                    "channel": _s(raw.get("channel"), 32) or "channel",
                    "views": _s(raw.get("views"), 18) or "1.2M views",
                    "age": _s(raw.get("age"), 18) or "3 days ago"}
        # phone
        pairs = []
        for item in (raw.get("lines") or [])[:5]:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                side = "me" if str(item[0]).lower() in ("me", "self", "right") else "them"
                txt = _s(item[1], 120)
                if txt:
                    pairs.append((side, txt))
        if not pairs:
            return None
        return {"type": "social", "kind": "phone",
                "contact": _s(raw.get("contact"), 28) or "unknown",
                "lines": pairs}

    if t == "stat":
        big = _s(raw.get("big"), 14)
        if not big:
            return None
        return {"type": "stat", "big": big.upper(),
                "label": _s(raw.get("label"), 34).upper() or "FROM THE STORY"}

    if t == "timeline":
        items = _clean_lines(raw.get("items"))
        if len(items) < 2:
            return None
        return {"type": "timeline", "items": items[:5]}

    if t == "chart":
        data = _clean_lines(raw.get("data"))
        vals = []
        for label, value in data:
            try:
                vals.append((label, float(re.sub(r"[^0-9.\-]", "", value) or 0)))
            except ValueError:
                continue
        if len(vals) < 2 or max(v for _, v in vals) <= 0:
            return None
        return {"type": "chart",
                "data": [(lab, int(v) if float(v).is_integer() else round(v, 1))
                         for lab, v in vals]}

    return None


# ---------------------------------------------------------------- drawing --
def _label(cue):
    """Layout key for this cue. Socials share one entry per flavour, so a tweet
    card and a Discord card can be positioned and timed independently."""
    if cue["type"] == "social":
        return cue["kind"]
    return cue["type"]


def render_cue(cue, idx, outdir):
    """Draw one cue's PNG. Returns the path, or None if it cannot be drawn."""
    name = f"cue_{idx:02d}_{cue['type']}.png"
    try:
        t = cue["type"]
        if t == "person":
            return gfx.lower_third(name, cue["name"], cue["sub"], outdir=outdir)
        if t == "stat":
            return gfx.stat_card(name, cue["big"], cue["label"], outdir=outdir)
        if t == "timeline":
            return gfx.timeline_card(name, cue["items"], outdir=outdir)
        if t == "chart":
            return gfx.bar_chart(name, cue["data"], outdir=outdir)
        if t == "social":
            k = cue["kind"]
            if k == "tweet":
                return gfx.tweet_ui(name, cue["name"], cue["handle"], cue["body"],
                                    cue["meta"], cue["likes"], outdir=outdir)
            if k == "discord":
                return gfx.discord_ui(name, cue["channel"], cue["name"],
                                      cue["body"], cue["time"], outdir=outdir)
            if k == "youtube":
                return gfx.youtube_ui(name, cue["title"], cue["channel"],
                                      cue["views"], cue["age"], outdir=outdir)
            if k == "phone":
                return gfx.phone_chat(name, cue["contact"], cue["lines"],
                                      outdir=outdir)
    except Exception as e:
        print(f"  [cues] could not draw {cue.get('type')}: {e}")
    return None


def render_cue_safe(cue, idx, outdir):
    """Public entry point. Kept separate so the draw path is swappable without
    touching callers, and so one broken cue can never kill a render."""
    return render_cue(cue, idx, outdir)


# ----------------------------------------------------------- selection -----
_NUM = re.compile(r"(\$?\d[\d,.]*\s?(?:million|billion|trillion|k\b|m\b|%|percent|times|x\b))",
                  re.I)


def synth_fallbacks(lines):
    """MANDATE wants at least one stat and one social card per video. If the
    Director did not supply them, derive the cheapest honest version from the
    script itself (a real number, never invented)."""
    out = []
    for i, ln in enumerate(lines):
        m = _NUM.search(ln.get("text", ""))
        if m:
            out.append((i, {"type": "stat",
                            "big": m.group(1).upper().replace("  ", " ").strip()[:14],
                            "label": "FROM THE STORY"}))
            break
    return out


def plan(lines, max_cues=None):
    """Turn the Director's cues into a validated, ordered, capped plan.
    Returns [(line_index, cue, png_path)] - png paths are filled in later."""
    max_cues = max_cues or config.CUE_MAX_PER_VIDEO
    found = []
    for i, ln in enumerate(lines):
        cue = norm_cue(ln.get("cue")) if ln.get("cue") else None
        if cue:
            found.append((i, cue))

    # MANDATE minimums: make sure a stat exists, and ideally a social receipt
    types = {c["type"] for _, c in found}
    if "stat" not in types:
        found.extend(synth_fallbacks(lines))

    # prefer the social card early (receipts land hardest before the payoff)
    def rank(item):
        i, c = item
        prio = {"social": 0, "stat": 1, "person": 2, "timeline": 3, "chart": 4}
        return (prio.get(c["type"], 9), i)

    if len(found) > max_cues:
        keep = sorted(sorted(found, key=rank)[:max_cues])
        dropped = len(found) - len(keep)
        found = keep
        print(f"  [cues] capped at {max_cues} ({dropped} dropped to avoid clutter)")
    return sorted(found)


def attach(plan_items, lines_timed, beats, work_dir):
    """Draw every cue and map it onto the beat that starts its line.

    Returns {beat_index: [overlay, ...]} where each overlay is
    {"png", "scale", "pos", "t_in", "t_out"} in SEGMENT-LOCAL time, which is
    what render.render_segment expects."""
    by_text = {}
    for text, t0, t1, intent in lines_timed:
        by_text.setdefault(" ".join(text.split()), (t0, t1))

    attached, drawn = {}, 0
    for idx, (line_i, cue) in enumerate(plan_items):
        png = render_cue_safe(cue, idx, work_dir)
        if not png:
            continue
        drawn += 1
        lay = LAYOUT.get(_label(cue), DEFAULT_LAYOUT)
        if line_i >= len(lines_timed):
            continue
        target = " ".join(lines_timed[line_i][0].split())
        beat_i = None
        for bi, b in enumerate(beats):
            if " ".join(b.line.split()) == target:
                beat_i = bi
                break
        if beat_i is None:
            continue
        t_in = 0.35                      # let the cut land before the graphic
        t_out = min(beats[beat_i].dur + 0.4, t_in + lay["dur"])
        attached.setdefault(beat_i, []).append(
            {"png": png, "scale": lay["frac"], "pos": lay["pos"],
             "t_in": t_in, "t_out": t_out})
    return attached, drawn


# ---------------------------------------------------------- atmosphere -----
def atmosphere(beats, fmt, work_dir):
    """Frame-level mood overlays that are not tied to a script line:
      * flare on shock/payoff beats  (the big visual punctuation)
      * light leak on breathers      (lets the eye rest)
      * HUD frame on heavy effects   (tension)
    Returns {beat_index: [overlay, ...]}, same shape as attach()."""
    W, H = fmt["w"], fmt["h"]
    made = {}
    try:
        made["flare"] = gfx.anamorphic_flare("atm_flare.png", W, H, outdir=work_dir)
    except Exception as e:
        print(f"  [cues] flare skipped: {e}")
    try:
        made["leak"] = gfx.light_leak("atm_leak.png", W, H, outdir=work_dir)
    except Exception as e:
        print(f"  [cues] light leak skipped: {e}")
    try:
        made["hud"] = gfx.hud_frame("atm_hud.png", W, H, outdir=work_dir)
    except Exception as e:
        print(f"  [cues] hud skipped: {e}")

    out, last_heavy = {}, -99
    for i, b in enumerate(beats):
        if "flare" in made and b.intent in ("shock", "payoff") and i > 1:
            out.setdefault(i, []).append(
                {"png": made["flare"], "scale": 1.0, "pos": "fill",
                 "t_in": 0.0, "t_out": min(1.2, b.dur)})
        if "leak" in made and b.intent == "breather" and i % 3 == 0:
            out.setdefault(i, []).append(
                {"png": made["leak"], "scale": 1.0, "pos": "fill",
                 "t_in": 0.0, "t_out": min(2.0, b.dur)})
        if "hud" in made and b.effect in ("shake", "glitch") and i - last_heavy >= 3:
            last_heavy = i
            out.setdefault(i, []).append(
                {"png": made["hud"], "scale": 1.0, "pos": "fill",
                 "t_in": 0.0, "t_out": b.dur})
    return out


def merge(*maps):
    """Combine several {beat_index: [overlay]} maps."""
    out = {}
    for m in maps:
        for k, v in m.items():
            out.setdefault(k, []).extend(v)
    return out
