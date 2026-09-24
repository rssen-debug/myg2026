"""Cerebras (or any configured LLM) writes the on-screen copy.

It may only use claims and the person's actual words. If the key is missing
or the call fails, the caller keeps the template. A missing key never
kills a run.
"""
import re

import llm
import packaging


def _words(text):
    return re.findall(r"[A-Za-z0-9']+", text or "")


def _same_words(chunks, quote):
    """The model is not allowed to put words in his mouth."""
    allowed = [w.lower() for w in _words(quote)]
    got = []
    for chunk in chunks:
        got.extend(w.lower() for w in _words(chunk))
    return got == allowed[:len(got)] and len(got) >= max(4, len(allowed) // 2)


def pick_quote(topic, windows):
    """windows: [{quote, start, end, title, channel, score}]. -> index or None."""
    if not windows or not llm.have_key():
        return None
    sample = [{"i": i, "quote": w.get("quote", "")[:180], "title": (w.get("title") or "")[:80]}
              for i, w in enumerate(windows[:8])]
    data = llm.chat_json([
        {"role": "system", "content":
         "You pick the one spoken line that should open a YouTube short. "
         "Prefer a complete thought, a claim, a number, or an emotional turn. "
         "Reject jokes about tools, ums, and half sentences if anything better exists. "
         "Respond ONLY with JSON {\"i\": <index>, \"why\": \"...\"}."},
        {"role": "user", "content": f"Subject: {topic}\n{sample}"},
    ], temperature=0.2, max_tokens=300)
    if not data or "i" not in data:
        return None
    try:
        i = int(data["i"])
    except (TypeError, ValueError):
        return None
    if 0 <= i < len(windows):
        print(f"  [llm] quote pick {i}: {data.get('why', '')[:80]}")
        return i
    return None


def polish(topic, claims, quotes):
    """-> {hook, title, chunks} or {} if the model is unavailable or unusable."""
    if not llm.have_key():
        print("  [llm] no key — template hook")
        return {}
    quote = (quotes or [{}])[0].get("quote") or ""
    claim_lines = [c.get("claim") for c in (claims or [])[:5] if c.get("claim")]
    data = llm.chat_json([
        {"role": "system", "content":
         "You write the on-screen text for a SunnyV2 video. "
         "Respond ONLY with JSON: {\"hook\": str, \"title\": str, \"chunks\": [str]}. "
         "hook is max 9 words, a question, payable by the quote or a claim. "
         "title names the subject and is under 70 characters. "
         "chunks split the quote into 2-4 word groups IN ORDER, using ONLY words "
         "from the quote. Do not invent numbers, names, or words."},
        {"role": "user", "content":
         f"Subject: {topic}\nClaims:\n- " + "\n- ".join(claim_lines[:4])
         + f"\nSpoken quote:\n{quote[:400]}"},
    ], temperature=0.4, max_tokens=500)
    if not data:
        print("  [llm] polish failed — keeping template")
        return {}
    out = {}
    hook = packaging.fit_hook(data.get("hook") or "", topic)
    ok, reasons = packaging.verify_title(data.get("title") or "", topic)
    title = data.get("title") if ok else ""
    if title and len(title) <= 100:
        out["title"] = title.strip()
    if hook and len(hook.split()) <= 9:
        out["hook"] = hook
    chunks = data.get("chunks") or []
    if isinstance(chunks, list) and quote and _same_words(chunks, quote):
        out["chunks"] = [c.strip() for c in chunks if str(c).strip()]
    else:
        print("  [llm] caption chunks rejected — they were not the spoken words")
    if out.get("hook"):
        print(f"  [llm] hook: {out['hook']}")
    if out.get("title"):
        print(f"  [llm] title: {out['title']}")
    return out
