"""Topic scout: Kick popular, Reddit, Google News.

Story potential, not just who is live. Used when the topic is "auto".
A scout hit is a subject plus an angle, never a 200-character headline used
as a YouTube search.
"""
import html
import json
import re

import requests

UA = {"User-Agent": "sunny-pipeline/4 (scout; contact: local)"}
KICK = "https://kick.com/api/v2/livestreams?sort=viewer_count&direction=desc&per_page=20"
NEWS = "https://news.google.com/rss/search"
HOT = (
    ("banned", 9), ("ban", 8), ("beef", 8), ("drama", 7), ("arrest", 9),
    ("exposed", 8), ("leak", 7), ("quit", 7), ("response", 6), ("record", 6),
    ("fight", 7), ("apology", 6), ("scam", 8), ("collab", 5), ("returns", 5),
)


def _get(url, params=None, timeout=16):
    r = requests.get(url, params=params, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r


def _kick():
    try:
        data = _get(KICK).json()
    except Exception as e:
        print(f"  [scout] kick skipped: {type(e).__name__}")
        return []
    rows = data.get("data", data if isinstance(data, list) else [])
    out = []
    for row in rows[:20]:
        ch = row.get("channel") or {}
        user = ch.get("user") or {}
        name = user.get("username") or ch.get("slug") or ""
        title = (row.get("session_title") or "").strip()
        if name and title:
            out.append({
                "topic": name,
                "angle": title[:80],
                "source": "kick",
                "signal": int(row.get("viewer_count") or 0),
                "url": f"https://kick.com/{ch.get('slug') or name}",
            })
    return out


def _reddit(sub="LivestreamFail"):
    url = f"https://www.reddit.com/r/{sub}/hot.json"
    try:
        data = _get(url, {"limit": 15, "raw_json": 1}).json()
    except Exception as e:
        print(f"  [scout] reddit skipped: {type(e).__name__}")
        return []
    out = []
    for child in (data.get("data") or {}).get("children") or []:
        d = child.get("data") or {}
        title = (d.get("title") or "").strip()
        if not title or d.get("stickied"):
            continue
        topic = _name_from_title(title)
        out.append({
            "topic": topic or title[:40],
            "angle": title[:90],
            "source": "reddit",
            "signal": int(d.get("score") or 0),
            "url": "https://www.reddit.com" + (d.get("permalink") or ""),
        })
    return out


def _news():
    try:
        raw = _get(NEWS, {"q": "streamer OR youtuber", "hl": "en-US", "gl": "US", "ceid": "US:en"}).text
    except Exception as e:
        print(f"  [scout] news skipped: {type(e).__name__}")
        return []
    out = []
    for m in re.finditer(r"<item>(.*?)</item>", raw, re.S):
        title_m = re.search(r"<title>(.*?)</title>", m.group(1), re.S)
        if not title_m:
            continue
        title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", title_m.group(1)))).strip()
        topic = _name_from_title(title)
        if not topic:
            continue
        out.append({
            "topic": topic,
            "angle": title[:90],
            "source": "news",
            "signal": 20,
            "url": "",
        })
        if len(out) >= 8:
            break
    return out


def _name_from_title(title):
    m = re.match(r"([A-Z][A-Za-z0-9]+(?:\s[A-Z][A-Za-z0-9]+)?)", title.strip())
    if m and m.group(1).lower() not in {"the", "why", "how", "this", "what"}:
        return m.group(1)
    return ""


def _score(row):
    text = f"{row.get('topic','')} {row.get('angle','')}".lower()
    s = 0
    for word, weight in HOT:
        if word in text:
            s += weight
    if row["source"] == "kick":
        s += min(row.get("signal") or 0, 40000) / 8000
    elif row["source"] == "reddit":
        s += min(row.get("signal") or 0, 4000) / 400
    else:
        s += 2
    # a subject we can search is worth more than a sentence
    if 2 <= len(row.get("topic") or "") <= 28:
        s += 2
    return round(s, 2)


def scout(limit=5):
    rows = _kick() + _reddit() + _news()
    for row in rows:
        row["score"] = _score(row)
    rows.sort(key=lambda r: r["score"], reverse=True)
    # one row per subject
    seen, out = set(), []
    for row in rows:
        key = row["topic"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= limit:
            break
    for i, row in enumerate(out, 1):
        print(f"  [scout] {i}. {row['score']:5.1f}  {row['topic']}  ({row['source']})  {row['angle'][:60]}")
    return out
