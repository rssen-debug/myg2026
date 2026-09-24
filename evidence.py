"""Wires research, quotes, stills, hook/title and scout into one packet.

Every step degrades: a dead source prints a line and the render continues.
The packet is what the Director is allowed to know.
"""
import json
import os

import config
import packaging
import quotes
import research
import scout
import stills


def prepare(topic, work, mode="short"):
    packet = {
        "topic": topic,
        "scout": None,
        "claims": [],
        "quotes": [],
        "hook": "",
        "hooks": [],
        "title": "",
        "titles": [],
        "stills": [],
        "factcheck": {},
    }
    if (topic or "").strip().lower() in ("auto", "scout"):
        print("\n== SCOUT (Kick / Reddit / News) ==")
        picks = scout.scout(limit=5)
        if picks:
            packet["scout"] = picks[0]
            topic = picks[0]["topic"]
            packet["topic"] = topic
            print(f"  [scout] using {topic!r} — {picks[0]['angle']}")
        else:
            topic = "MrBeast"
            packet["topic"] = topic
            print("  [scout] nothing usable, falling back to MrBeast")

    print("\n== RESEARCH (claims before script) ==")
    packet["claims"] = research.gather(topic)
    try:
        research.save(os.path.join(work, "claims.json"), packet["claims"])
    except OSError:
        pass

    print("\n== QUOTES (their words, before the script) ==")
    try:
        packet["quotes"] = quotes.discover(topic, os.path.join(work, "quotes"), n=1 if mode == "short" else 2)
    except Exception as e:
        print(f"  [quotes] skipped: {type(e).__name__}: {e}")
        packet["quotes"] = []

    print("\n== HOOK + TITLE ==")
    hook, hooks = packaging.choose_hook(topic, packet["claims"], packet["quotes"])
    title, titles = packaging.choose_title(topic, packet["claims"], packet["quotes"])
    packet["hook"] = hook
    packet["hooks"] = hooks
    packet["title"] = title
    packet["titles"] = titles
    try:
        import creative
        polished = creative.polish(topic, packet["claims"], packet["quotes"])
    except Exception as e:
        print(f"  [llm] polish skipped: {type(e).__name__}: {e}")
        polished = {}
    if polished.get("hook"):
        packet["hook"] = polished["hook"]
    if polished.get("title"):
        packet["title"] = polished["title"]
    if polished.get("chunks") and packet["quotes"]:
        packet["quotes"][0]["chunks"] = polished["chunks"]
    print(f"  hook:  {packet['hook']}")
    print(f"  title: {packet['title']}")

    print("\n== STILLS (title must name the subject) ==")
    try:
        packet["stills"] = stills.fetch(topic, os.path.join(work, "stills"), n=3)
    except Exception as e:
        print(f"  [stills] skipped: {type(e).__name__}: {e}")
        packet["stills"] = []
    config.EVIDENCE_STILLS = packet["stills"]
    return packet


def apply_brief(brief, packet):
    if packet.get("hook"):
        brief["hook"] = packet["hook"]
    if packet.get("title"):
        brief["title"] = packet["title"]
    brief["evidence"] = {
        "claims": [c.get("claim") for c in (packet.get("claims") or [])[:6]],
        "quotes": [q.get("quote") for q in (packet.get("quotes") or [])[:2]],
    }
    return brief


def evidence_block(brief):
    ev = brief.get("evidence") or {}
    claims = ev.get("claims") or []
    spoken = ev.get("quotes") or []
    if not claims and not spoken:
        return ""
    lines = ["EVIDENCE RULES - these are the only facts you may state:"]
    for c in claims[:5]:
        lines.append(f"  - {c}")
    if spoken:
        lines.append("SPOKEN QUOTE (do not put the quote in the narration; set it up):")
        lines.append(f"  - {spoken[0][:180]}")
        lines.append("One line's query must be the subject's name plus 'interview'.")
    lines.append("If a number is not in EVIDENCE, do not say it. No invented names.")
    if brief.get("hook"):
        lines.append(f"Line 1 MUST be exactly: {brief['hook']}")
    return "\n".join(lines) + "\n"


def trim_to_budget(lines, words_target):
    """The fallback writer ignores a short preview budget. A 80-word read
    leaves no time for the quote, so cut after the payable hook and the
    first evidence line rather than speeding the voice into mush.
    """
    if not lines or not words_target:
        return lines
    kept, total = [], 0
    cap = max(18, int(words_target * 1.05))
    for ln in lines:
        n = len((ln.get("text") or "").split())
        if kept and total + n > cap:
            break
        kept.append(ln)
        total += n
    return kept or lines[:2]


def stamp_lines(lines, packet, topic):
    """Force the payable hook onto line 1 and hang real cues on the script."""
    if not lines:
        return lines
    if packet.get("hook"):
        lines[0]["text"] = packet["hook"]
        lines[0]["intent"] = "shock"
    claims = packet.get("claims") or []
    fact = next((c for c in claims if c.get("kind") == "FACT" and c.get("numbers")), None)
    if fact and len(lines) > 1 and not lines[1].get("cue"):
        num = fact["numbers"][0]
        lines[1]["cue"] = {
            "type": "stat",
            "big": num[:12],
            "label": "ON THE RECORD",
        }
    quotes_ = packet.get("quotes") or []
    if quotes_ and len(lines) > 2 and not lines[2].get("cue"):
        q = quotes_[0]
        lines[2]["query"] = q.get("query") or f"{topic} interview"
        lines[2]["cue"] = {
            "type": "social",
            "kind": "youtube",
            "title": (q.get("title") or topic)[:70],
            "channel": (q.get("channel") or topic)[:32],
            "views": "source clip",
            "age": "quoted",
        }
    if len(lines) > 3 and not lines[3].get("cue"):
        lines[3]["cue"] = {"type": "person", "name": topic[:28], "sub": "the subject"}
    return lines


def prepend_quote_clips(pool, packet):
    """Quote sections bypass the 720p gate. They are the receipt."""
    extra = []
    for i, q in enumerate(packet.get("quotes") or []):
        path = q.get("path")
        if not path or not os.path.exists(path):
            continue
        extra.append({
            "id": f"quote{i:02d}",
            "path": path,
            "title": q.get("title") or "quote",
            "meta": {"face_ratio": 0.4, "height": 0, "has_audio": True},
            "phash": [],
            "queries": [q.get("query") or ""],
            "quote": True,
            "spans": q.get("spans") or [],
            "words": q.get("words") or [],
        })
    if extra:
        print(f"  [quotes] {len(extra)} quote clip(s) placed first in the pool")
    return extra + pool


def write_sidecar(final_path, packet):
    side = os.path.splitext(final_path)[0] + ".evidence.json"
    slim = {
        "topic": packet.get("topic"),
        "hook": packet.get("hook"),
        "title": packet.get("title"),
        "titles": packet.get("titles"),
        "scout": packet.get("scout"),
        "claims": packet.get("claims"),
        "quotes": [{k: v for k, v in q.items() if k != "words"} for q in (packet.get("quotes") or [])],
        "stills": [os.path.basename(p) for p in (packet.get("stills") or [])],
        "factcheck": packet.get("factcheck"),
    }
    try:
        with open(side, "w", encoding="utf-8") as f:
            json.dump(slim, f, indent=2, ensure_ascii=False)
        print(f"  [evidence] {side}")
    except OSError as e:
        print(f"  [evidence] sidecar skipped: {e}")
    return side
