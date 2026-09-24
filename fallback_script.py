#!/usr/bin/env python3
"""Deterministic SunnyV2-shaped script generator - used when no LLM key exists.

This is the fallback for write_script(). The old one narrated its own section
titles ("The Rise and Fall of Vine - part 2. And that is when everything
changed.") and repeated six filler sentences, so a no-key run produced a
video that announced its own structure and said nothing. It was also the only
thing standing between the pipeline and a blank screen when a key expires,
which is exactly what happened today - so it is worth being decent.

What this produces instead: a hook (max 9 words, question or bold claim), a
narrative arc with a setup, an early-warning beat, a turn, an escalation, a
reveal and a payoff, then short searchable queries that name the subject.

It still cannot invent facts - that is the LLM's job and no template can do
it. What it can do is keep the shape, the tense and the pacing that make the
format work, and never speak about itself. Lines are chosen by rotating
through the pools so nothing repeats back to back, and the closer is kept for
the end.

    python fallback_script.py "The Rise and Fall of Vine" --words 120
"""
import argparse
import re

STOP = {"the", "a", "an", "of", "in", "on", "for", "and", "to", "inside",
        "how", "why", "what", "when", "untold", "story", "true", "real",
        "secret", "history", "rise", "fall", "dark", "side", "downfall"}

HOOKS = [
    "What if {short} was never what it looked like?",
    "{short} looks like a success story. It is not.",
    "Everyone got {short} wrong. Here is the proof.",
    "The real story of {short} never made the headlines.",
    "Nobody tells you how {short} really ended.",
]

BODY = [
    ("On paper, {short} had everything. The money, the audience, the momentum.", "context"),
    ("But the version everyone repeats is not the real one.", "context"),
    ("Go back a few years and none of this was supposed to happen.", "buildup"),
    ("The first cracks showed up early. Almost nobody noticed.", "buildup"),
    ("Then the numbers stopped adding up.", "shock"),
    ("That was the moment everything changed.", "shock"),
    ("The people closest to it saw the pattern first.", "context"),
    ("By the time the public caught on, the damage was done.", "payoff"),
    ("So the real question is not what happened. It is who knew.", "shock"),
    ("And the answer says more about the industry than about {short}.", "payoff"),
    ("Which brings us to the part nobody says out loud.", "buildup"),
    ("Because the same playbook is running right now.", "shock"),
    ("Find any comeback story and you find the same moves.", "context"),
    ("The ending is still being written.", "breather"),
    ("Nothing about {short} was an accident.", "payoff"),
    ("Every rule it broke was a rule somebody else wrote.", "context"),
    ("The silence afterwards told its own story.", "buildup"),
    ("And the money never really left. It just moved.", "shock"),
    ("That decision cost far more than anyone admits.", "payoff"),
    ("Which is exactly why this story keeps coming back.", "breather"),
    ("Because the people who lost never got their side told.", "buildup"),
    ("And that is the part that should worry you.", "shock"),
    ("The warning signs were there in plain sight.", "buildup"),
    ("Somebody signed off on every one of those calls.", "context"),
    ("By then, walking away was the expensive option.", "payoff"),
]

CLOSERS = [
    "And that is why {short} still matters.",
    "Which is the part nobody wants to say about {short}.",
    "And the next chapter is already being written.",
]

# Two-word search strings, on purpose: the relevance gate scores token
# overlap, so every extra word dilutes the score. Twenty-four of them means
# even a five-minute script does not ask YouTube the same thing twice in a row.
QUERIES = [
    "{short} interview", "{short} documentary", "{short} explained",
    "{short} story", "{short} behind the scenes", "{short} founder",
    "{short} ceo", "{short} rise", "{short} decline", "{short} collapse",
    "{short} news", "{short} timeline", "{short} funding", "{short} investors",
    "{short} launch", "{short} history", "{short} lawsuit", "{short} users",
    "{short} reaction", "{short} analysis", "{short} breakdown",
    "{short} report", "{short} archive", "{short} update",
]

# Acts two and three. A five-minute script needs ~65 body lines and one pool
# of 25 would repeat itself three times, which is audible. Pools A/B/C are
# ordered as acts, so indexing them in sequence gives a long script a shape
# and a short script only ever touches act one.
BODY_B = [
    ("That is where the story turns.", "buildup"),
    ("Because the plan only worked if nobody looked closely.", "context"),
    ("So they stopped explaining and started scaling.", "shock"),
    ("Growth covered everything. For a while.", "context"),
    ("The bigger it got, the harder it was to question.", "buildup"),
    ("Every quarter bought another year of silence.", "context"),
    ("And the people who asked questions were the ones who left.", "shock"),
    ("By then the numbers were the story.", "payoff"),
    ("Nobody inside wanted to be the one who said it out loud.", "buildup"),
    ("So the machine kept running on its own momentum.", "context"),
    ("Until momentum was all it had left.", "payoff"),
    ("The first outside investor saw it before the press did.", "context"),
    ("They pulled out quietly and told nobody why.", "shock"),
    ("That exit was the real warning.", "buildup"),
    ("A year later, everyone would pretend they saw it coming.", "context"),
    ("The founders knew. The staff suspected. The fans had no idea.", "shock"),
    ("That is how most collapses actually work.", "payoff"),
    ("Not one bad day. A long series of small yeses.", "context"),
    ("Somebody approved every single one.", "buildup"),
    ("And the approvals were always defensible at the time.", "context"),
]

BODY_C = [
    ("Then the pause. Then the explanation nobody believed.", "shock"),
    ("The official version arrived before the facts did.", "context"),
    ("By the time the details surfaced, the audience had moved on.", "breather"),
    ("That is the part the industry would rather forget.", "context"),
    ("Because the same thing is running somewhere else right now.", "shock"),
    ("Different logo. Same sequence.", "shock"),
    ("And the people who get hurt are never the ones holding the bag.", "payoff"),
    ("They are the ones who believed the story first.", "payoff"),
    ("Which is why the archive still matters.", "context"),
    ("The footage is the only thing nobody edited afterwards.", "buildup"),
    ("You can watch it build. You can watch it break.", "context"),
    ("And once you see the pattern, you cannot unsee it.", "shock"),
    ("Every boom carries the shape of its own ending.", "payoff"),
    ("That is not cynicism. That is the record.", "context"),
    ("So the question was never whether it would end.", "buildup"),
    ("It was who would be standing when it did.", "payoff"),
    ("And that part was decided a long time ago.", "shock"),
    ("Which is why this story keeps getting retold.", "context"),
    ("Not for the scandal. For the lesson.", "breather"),
    ("And the lesson is always more expensive in hindsight.", "payoff"),
]


def subject_of(topic):
    """-> (short subject used in the narration, query subject).

    Strips the boilerplate that the pipeline itself adds to titles, then keeps
    the distinctive first content word: 'The Rise and Fall of Vine' -> 'Vine',
    "Inside MrBeast's Money Machine" -> 'MrBeast'.
    """
    t = re.sub(r"^(the\s+)?(untold\s+story\s+of\s+|inside\s+|how\s+|why\s+)",
               "", (topic or "").strip(), flags=re.I)
    words = re.findall(r"[A-Za-z0-9'&.-]+", t)
    keep = [w for w in words if w.lower() not in STOP and len(w) > 2]
    if not keep:
        keep = words or ["this"]
    # strip("\'s") removes every leading/trailing quote and "s" character, so
    # "Theranos" became "Therano". Only a real possessive should be dropped.
    short = keep[0].strip(".,")
    if short.lower().endswith("\'s"):
        short = short[:-2]
    if short.lower() in ("mrbeast", "mrbeasts"):
        short = short.rstrip("s")
    # An acronym taken bare from the title reads as a typo: "What if CEO was
    # never what it looked like?" So when the title used a plural or an
    # expanded form of it, keep the form the title actually used.
    if short.isupper() and len(short) <= 4:
        for w in re.findall(r"[A-Za-z0-9'&.-]+", topic or ""):
            if w.lower().startswith(short.lower()) and len(w) > len(short):
                short = w
                break
    return short[0].upper() + short[1:], " ".join(keep[:3]).lower()


def _pluralise(text, plural):
    """Fix the verbs and pronouns when the subject turned out to be plural.

    Every template in this file is written for a singular subject ("What if
    Vine was never what it looked like?"). A topic like "How CEOs Talk When the
    Camera Is On" yields the plural subject "CEOs", and rendering the templates
    unchanged produces "What if CEOs was never what it looked like?" - which
    reads as broken English in the very first line of the video. Rewriting the
    handful of verbs/pronouns the templates use is enough, and it is checked by
    printing a sample line for a plural and a singular topic.
    """
    if not plural:
        return text
    for a, b in ((" was ", " were "), (" it was ", " they were "),
                 (" it ", " they "), (" it's ", " they're "),
                 (" it.", " they."), (" its ", " their "),
                 (" was.", " were."), (" that was ", " that were ")):
        text = text.replace(a, b)
    return text.strip()


def build_lines(topic, words_target):
    """-> [{'text','intent','query'}] shaped exactly like the LLM's output."""
    short, qsub = subject_of(topic)
    # "CEOs" vs "Vine": the templates need the verbs to agree
    # Only an all-caps acronym with a trailing s is treated as plural: "CEOs",
    # "NFTs", "EVs". A proper noun that merely ends in s - Theranos, Vine,
    # MrBeast - is singular, and guessing from the suffix alone turned
    # "Theranos" into a plural and stripped it to "Therano".
    plural = bool(re.fullmatch(r"[A-Z]{2,5}s", short))
    n = max(9, int(round(words_target / 9.5)))
    hook = HOOKS[len(topic) % len(HOOKS)].format(short=short)

    lines = [{"text": _pluralise(hook, plural), "intent": "shock",
              "query": f"{qsub} documentary"}]
    body_n = n - 2                       # hook + closer take the ends
    all_body = BODY + BODY_B + BODY_C
    for i in range(body_n):
        tpl, intent = all_body[i % len(all_body)]
        lines.append({"text": _pluralise(tpl.format(short=short), plural),
                      "intent": intent,
                      "query": QUERIES[i % len(QUERIES)].format(short=qsub)})
    # the closer must not repeat the line before it
    lines.append({"text": _pluralise(
                      CLOSERS[len(topic) % len(CLOSERS)].format(short=short),
                      plural),
                  "intent": "payoff", "query": f"{qsub} story"})
    return lines


def words_per_line(lines):
    return sum(len(l["text"].split()) for l in lines)


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("topic")
    ap.add_argument("--words", type=int, default=120,
                    help="spoken word budget (~140 wpm is a typical read)")
    a = ap.parse_args()
    lines = build_lines(a.topic, a.words)
    for i, l in enumerate(lines, 1):
        print(f"  {i:2d}. [{l['intent']:8s}] {l['text']}")
        print(f"      query: {l['query']}")
    print(f"\n  {len(lines)} lines, {words_per_line(lines)} words")


if __name__ == "__main__":
    _main()
