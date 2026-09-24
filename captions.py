r"""SunnyV2-style karaoke ASS captions (word-level \k timing)."""

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {resx}
PlayResY: {resy}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sunny,Arial Black,{size},&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{bord},0,2,60,60,240,1
Style: SunnyHook,Arial Black,{sizehook},&H0000FFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{bord},0,2,60,60,240,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ts(s):
    s = max(0.0, float(s))
    h = int(s // 3600)
    m = int(s % 3600 // 60)
    sec = s % 60
    return f"{h}:{m:02d}:{sec:05.2f}"


def static_card_ass(text_lines, resx, resy, fontsize=None):
    """Centered static text (used for the outro card)."""
    size = fontsize or max(72, int(resy * 0.07))
    header = HEADER.format(resx=resx, resy=resy, size=size,
                           sizehook=size, bord=max(4, size // 12))
    body = "\\N".join(
        f"{{\\k10}}{t}" for t in text_lines)
    ev = f"Dialogue: 0,0:00:00.00,9:59:59.99,SunnyHook,,0,0,0,,{body}\n"
    return header + ev


# =====================================================================
# KINETIC CAPTIONS v2 (ported from sunnyv2youtube/scripts/make_captions.py)
# Archivo/Arial Black, keyword-highlight (accent red), rotation jitter,
# pop-in scale 62->100 over 70 ms, groups of <=3 words / <=16 chars.
# =====================================================================
KINETIC_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {resx}
PlayResY: {resy}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Pop,Arial Black,{size},&H00FFFFFF,&H0000FFFF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,1,{bord},2,2,60,60,{marginv},1
Style: PopBig,Arial Black,{sizebig},&H00FFFFFF,&H0000FFFF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,1,{bord},2,2,60,60,{marginv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

RED = r"{\c&H2E22E8&}"     # accent red (232,34,46) in BGR
YELLOW = r"{\c&H00FFFF&}"
WHITE = r"{\c&HFFFFFF&}"


# Style for the footage's own speech. Deliberately a different colour from the
# narrator's captions: when the person in the clip talks, the caption is yellow,
# when the narrator talks it is white. Both are on screen at different times by
# construction (see dialogue.py), and the colour is how a viewer can tell which
# voice they are reading without being told.
def ass_dialogue(tts_words, src_words, resx, resy, keywords=None, hook_until=0.0):
    """One .ass containing BOTH caption tracks.

    tts_words: [(word,t0,t1)] for the narrator - white, punch words in red.
    src_words: [(word,t0,t1)] for the person in the footage - yellow.

    Built by taking the kinetic header up to its [Events] section and adding
    one style, rather than by editing the finished text: string surgery on the
    header silently dropped the [Events] marker the first time, which produces
    a file that some players accept and others play with no captions at all.
    The single burn pass then handles the whole video.
    """
    keywords = {k.lower() for k in (keywords or set())}
    size = max(22, int(resy * 0.062))
    qsize = max(18, int(size * 0.86))
    full = KINETIC_HEADER.format(
        resx=resx, resy=resy, size=size, sizebig=int(size * 1.22),
        bord=max(4, size // 12), marginv=int(resy * 0.16))
    styles_part, _, _ = full.partition("[Events]")
    quote_style = (
        f"Style: Quote,Arial Black,{qsize},&H0000F0FF,&H00FFFFFF,&H00000000,"
        f"&HA0000000,-1,0,0,0,100,100,0,0,1,{max(4, qsize // 12)},2,2,60,60,"
        f"{int(resy * 0.16)},1\n")
    events_head = ("\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL,"
                   " MarginR, MarginV, Effect, Text\n")
    body = (_events_for(tts_words, keywords, True, hook_until)
            + _events_for(src_words, keywords, False, 0.0))
    lines = [l for l in body.split("\n") if l.startswith("Dialogue:")]
    lines.sort(key=lambda l: l.split(",")[1])       # time order, readable
    return styles_part + quote_style + events_head + "\n".join(lines) + "\n"


def _events_for(words, keywords, is_tts, hook_until, group_max=3, group_chars=16):
    """Kinetic caption events for one speaker. Style differs per speaker."""
    out = []
    group, idx = [], 0
    style_name = None

    def flush():
        nonlocal idx
        if not group:
            return
        start, end = group[0][1], group[-1][2]
        fx = (r"{\fscx62\fscy62\fr%.1f" % (((idx * 7) % 25) / 10 - 1.2)) + \
             r"\t(0,70,\fscx100\fscy100)\fad(35,45)}"
        parts = []
        for w, _a, _b in group:
            if is_tts and _clean(w) in keywords:
                parts.append(f"{RED}{w}{WHITE}")
            else:
                parts.append(w)
        if is_tts:
            style = "PopBig" if start < hook_until else "Pop"
        else:
            style = "Quote"
        out.append((start, end, style, fx + " ".join(parts)))
        idx += 1

    for w, a, b in words:
        group.append((w, a, b))
        glen = sum(len(x[0]) + 1 for x in group)
        if len(group) >= group_max or glen >= group_chars:
            flush()
            group = []
    flush()
    return "".join(f"Dialogue: 0,{ts(a)},{ts(b)},{st},,0,0,0,,{txt}\n"
                   for a, b, st, txt in out)


def _clean(w):
    return w.strip('.,!?"()[]\'').lower()


def ass_kinetic(words, resx, resy, keywords=None, hook_until=0.0,
                group_max=3, group_chars=16):
    """words: [(word, t0, t1)] from TTS word-boundaries. keywords: set of punch
    words highlighted in accent red. Returns full kinetic .ass content.
    Groups use real per-word timestamps, so timing stays zero-drift."""
    keywords = {k.lower() for k in (keywords or set())}
    size = max(22, int(resy * 0.062))
    header = KINETIC_HEADER.format(
        resx=resx, resy=resy, size=size, sizebig=int(size * 1.22),
        bord=max(4, size // 12), marginv=int(resy * 0.16))

    events, group, glen = [], [], 0
    idx = 0

    def flush():
        nonlocal idx
        if not group:
            return
        start, end = group[0][1], group[-1][2]
        style = "PopBig" if start < hook_until else "Pop"
        # deterministic rotation jitter (hash() is salted across runs)
        j = ((idx * 7) % 25) / 10 - 1.2
        fx = (r"{\fscx62\fscy62\fr%.1f" % j) + \
             r"\t(0,70,\fscx100\fscy100)\fad(35,45)}"
        parts = []
        for w, a, b in group:
            if _clean(w) in keywords:
                parts.append(f"{RED}{w}{WHITE}")
            else:
                parts.append(w)
        events.append((start, end, style, fx + " ".join(parts)))
        group.clear()
        glen = 0
        idx += 1

    for w, t0, t1 in words:
        group.append((w, t0, t1))
        glen += len(w) + 1
        if (len(group) >= group_max or glen >= group_chars
                or w.rstrip().endswith((".", "!", "?"))):
            flush()
    flush()

    lines = [f"Dialogue: 0,{ts(a)},{ts(b)},{st},,0,0,0,,{txt}"
             for a, b, st, txt in events]
    return header + "\n".join(lines) + "\n"
