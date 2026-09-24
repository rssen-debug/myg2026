"""Research + factcheck, ported from sunnyv2youtube.

The old pipeline wrote the script before any source existed, then asked the
model not to invent numbers. That is a prompt, not a gate. This module gathers
claims first and rewrites any narration number or name that is not in them.

kind: FACT, CLAIM, ALLEGATION, OPINION, RUMOR
tier: 1 official/interview, 2 major reference, 3 news, 4 forum, 5 random.
Tier 4-5 may suggest a lead. They may not appear as proof in the narration.
"""
import html
import json
import os
import re

import requests

UA = {"User-Agent": "sunny-pipeline/4 (research; contact: local)"}
NUM_RE = re.compile(
    r"\$?\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?\s?(?:million|billion|thousand|%|percent)|(?<!\w)\d{4}(?!\w)",
    re.I)
NAME_RE = re.compile(r"\b[A-Z][a-zA-Z']{2,}(?:\s[A-Z][a-zA-Z']{2,}){0,2}\b")
STOP = {
    "the", "a", "an", "and", "or", "but", "what", "if", "this", "that", "then",
    "here", "there", "everyone", "nobody", "someone", "youtube", "wikipedia",
    "almost", "also", "after", "before", "because", "which", "while", "where",
    "when", "go", "so", "by", "on", "in", "find", "every", "nothing", "somebody",
    "he", "she", "they", "his", "her", "their", "it", "its",
}


def _get(url, params=None, timeout=18):
    r = requests.get(url, params=params, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r


def _clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _tokens(topic):
    return [t for t in re.findall(r"[a-z0-9]+", (topic or "").lower()) if len(t) > 2 and t not in STOP]


def numbers_in(text):
    return [n.strip() for n in NUM_RE.findall(text or "")]


def _norm_num(n):
    return re.sub(r"\s+", "", (n or "").lower())


def wikipedia(topic):
    """-> (title, extract, url) or ('','','')."""
    try:
        search = _get("https://en.wikipedia.org/w/api.php", {
            "action": "query", "list": "search", "srsearch": topic,
            "srlimit": 3, "format": "json",
        }).json()
        hits = (search.get("query") or {}).get("search") or []
        if not hits:
            return "", "", ""
        title = hits[0]["title"]
        summary = _get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{requests.utils.quote(title)}").json()
        extract = _clean(summary.get("extract") or "")
        url = ((summary.get("content_urls") or {}).get("desktop") or {}).get("page") or ""
        return summary.get("title") or title, extract, url
    except Exception as e:
        print(f"  [research] wikipedia skipped: {type(e).__name__}")
        return "", "", ""


def news(topic, limit=6):
    url = "https://news.google.com/rss/search"
    try:
        raw = _get(url, {"q": topic, "hl": "en-US", "gl": "US", "ceid": "US:en"}).text
    except Exception as e:
        print(f"  [research] news skipped: {type(e).__name__}")
        return []
    out = []
    for m in re.finditer(r"<item>(.*?)</item>", raw, re.S):
        body = m.group(1)
        title = re.search(r"<title>(.*?)</title>", body, re.S)
        link = re.search(r"<link>(.*?)</link>", body, re.S)
        if not title:
            continue
        out.append({
            "title": _clean(title.group(1)),
            "url": _clean(link.group(1)) if link else "",
        })
        if len(out) >= limit:
            break
    return out


def _claim(text, source, url, kind, tier):
    text = _clean(text)
    if len(text) < 12:
        return None
    return {
        "claim": text[:280],
        "source": source,
        "url": url,
        "kind": kind,
        "tier": tier,
        "numbers": numbers_in(text),
    }


def gather(topic):
    """Claims the narration is allowed to stand on. Never raises."""
    claims = []
    title, extract, url = wikipedia(topic)
    if extract:
        for sent in re.split(r"(?<=[.!?])\s+", extract):
            c = _claim(sent, f"Wikipedia: {title}", url, "FACT", 2)
            if c:
                claims.append(c)
            if len(claims) >= 6:
                break
        print(f"  [research] wikipedia: {title} ({len(claims)} sentences)")
    else:
        print("  [research] wikipedia: no page")
    for item in news(topic):
        # A headline is a lead, not a measured fact.
        c = _claim(item["title"], "Google News", item.get("url", ""), "CLAIM", 3)
        if c:
            claims.append(c)
    print(f"  [research] {len(claims)} claims (tier<=3 usable as proof)")
    return claims


def save(path, claims):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(claims, f, indent=2, ensure_ascii=False)


def _allowed_blob(claims):
    parts = []
    for c in claims:
        if c.get("tier", 5) <= 3 and c.get("kind") != "RUMOR":
            parts.append(c.get("claim") or "")
    return " ".join(parts).lower()


def _allowed_numbers(claims):
    out = set()
    for c in claims:
        if c.get("tier", 5) <= 3 and c.get("kind") in ("FACT", "CLAIM"):
            for n in c.get("numbers") or numbers_in(c.get("claim") or ""):
                out.add(_norm_num(n))
    return out


def _allowed_names(claims, topic):
    names = {topic.lower()}
    blob = _allowed_blob(claims)
    for m in NAME_RE.finditer(blob.title() if False else " ".join(
            c.get("claim") or "" for c in claims if c.get("tier", 5) <= 3)):
        names.add(m.group(0).lower())
    for t in _tokens(topic):
        names.add(t)
    return names


def factcheck(lines, claims, topic):
    """Drop narration that states a number or a name the claims do not support.

    Returns (lines, report). A line that fails is replaced with a number-free
    sentence, or with an allowed claim if one fits. The hook length is preserved
    by not injecting a long claim into line 0.
    """
    allowed_n = _allowed_numbers(claims)
    allowed_names = _allowed_names(claims, topic)
    safe = "The public record is thinner than the headline."
    backed = next((c["claim"] for c in claims
                   if c.get("kind") == "FACT" and c.get("tier", 5) <= 2
                   and not numbers_in(c["claim"])), safe)
    report = {"rewritten": [], "kept_numbers": sorted(allowed_n)}
    out = []
    for i, ln in enumerate(lines):
        text = (ln.get("text") or "").strip()
        bad_n = [n for n in numbers_in(text) if _norm_num(n) not in allowed_n]
        bad_names = []
        for m in NAME_RE.finditer(text):
            name = m.group(0)
            if name.lower() in STOP or name.lower() in allowed_names:
                continue
            if any(t in name.lower() for t in _tokens(topic)):
                continue
            # a single capitalised word at the start of a sentence is English,
            # not a person. "Almost nobody noticed." was being treated as a name.
            if " " not in name:
                prev = text[max(0, m.start() - 2):m.start()]
                if m.start() < 2 or prev.endswith(". ") or prev.endswith("! ") or prev.endswith("? "):
                    continue
            bad_names.append(name)
        if bad_n or bad_names:
            replacement = safe if i == 0 or len(backed.split()) > 18 else backed
            if i == 0:
                replacement = "What does the record actually show?"
            new = dict(ln)
            new["text"] = replacement
            new["factcheck"] = "rewritten"
            out.append(new)
            report["rewritten"].append({
                "i": i, "bad_numbers": bad_n, "bad_names": bad_names,
                "was": text[:140], "now": replacement,
            })
            print(f"  [factcheck] line {i} rewritten "
                  f"(numbers={bad_n or '-'} names={bad_names or '-'})")
        else:
            out.append(ln)
    if not report["rewritten"]:
        print("  [factcheck] PASS - every number and name is in the claims")
    return out, report
