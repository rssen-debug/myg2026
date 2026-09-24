"""Hook engine + title verifier, ported from sunnyv2youtube.

The hook has to be payable by a real claim or a real quote, and it has to
fit the existing 9-word gate. The title has to name the subject, stay under
100 characters, and not be empty clickbait.
"""
import re

CLICKBAIT = (
    "you won't believe", "you wont believe", "gone wrong", "shocking!!!",
    "exposed!!!", "must watch", "number one",
)
TYPES = ("curiosity", "conflict", "mystery", "investigation")


def _short(topic):
    topic = re.sub(r"\s+", " ", (topic or "").strip())
    return topic[:28] or "this"


def _words(text):
    return re.findall(r"[A-Za-z0-9']+", text or "")


def fit_hook(text, topic):
    words = _words(text)
    if not words:
        words = _words(f"What does the record say about {_short(topic)}?")
    if len(words) > 9:
        words = words[:9]
    hook = " ".join(words)
    if not hook.endswith("?"):
        hook = hook.rstrip(".") + "?"
    return hook


def choose_hook(topic, claims, quotes):
    short = _short(topic)
    cands = []
    if quotes:
        cands.append((f"What did {short} actually say", "quote"))
    fact = next((c for c in claims if c.get("kind") == "FACT" and c.get("tier", 5) <= 2), None)
    if fact:
        cands.append((f"What does the record say about {short}", "claim"))
    cands.append((f"Is the {short} story missing a receipt", "curiosity"))
    cands.append((f"Who is telling the {short} story", "conflict"))
    hook = fit_hook(cands[0][0], topic)
    return hook, [{"text": fit_hook(t, topic), "tied_to": why} for t, why in cands]


def verify_title(title, topic, seen=()):
    reasons = []
    title = re.sub(r"\s+", " ", (title or "")).strip()
    if not title:
        reasons.append("empty")
    if len(title) > 100:
        reasons.append("over 100 characters")
    tokens = [t for t in re.findall(r"[a-z0-9]+", (topic or "").lower()) if len(t) > 2]
    if tokens and not any(t in title.lower() for t in tokens):
        reasons.append("does not name the subject")
    low = title.lower()
    if any(b in low for b in CLICKBAIT):
        reasons.append("empty clickbait")
    if low in {s.lower() for s in seen}:
        reasons.append("repeats a previous title")
    return (not reasons), reasons


def choose_title(topic, claims, quotes, seen=()):
    short = _short(topic)
    quote_bit = "what he actually said" if quotes else "what the record shows"
    cands = [
        ("curiosity", f"{short}: {quote_bit}"),
        ("conflict", f"{short}, and the version that does not match"),
        ("mystery", f"The {short} detail nobody put on screen"),
        ("investigation", f"{short}, checked against the record"),
    ]
    report = []
    chosen = None
    for kind, title in cands:
        ok, reasons = verify_title(title, topic, seen)
        report.append({"type": kind, "title": title, "ok": ok, "reasons": reasons})
        if ok and chosen is None:
            chosen = title
    if chosen is None:
        chosen = f"{short}: what the record shows"
    return chosen, report
