"""Quote-first, ported from videofinal.

Pick a 4-8s window of the person's own words from subtitles BEFORE the script
is written, preferring the subject's own channel over a reaction or a
compilation. The narration is then written around that quote. The clip itself
is a short section download, not the VOD.
"""
import os
import re
import subprocess
import sys

import config
import sources

TAG_RE = re.compile(r"<[^>]+>")
WORD_RE = re.compile(r"[A-Za-z0-9']+")
FILLER = {"um", "uh", "like", "subscribe", "guys", "gonna", "wanna", "yeah", "okay", "ok"}


def party_score(title, channel, topic):
    tokens = [t for t in re.findall(r"[a-z0-9]+", (topic or "").lower()) if len(t) > 2]
    title_l = (title or "").lower()
    ch = (channel or "").lower()
    score = 0.0
    score += sum(2.0 for t in tokens if t in ch)
    score += sum(1.0 for t in tokens if t in title_l)
    if any(b in title_l for b in config.BAD_TITLE_WORDS):
        score -= 4.0
    if "official" in ch or "interview" in title_l:
        score += 0.5
    return score


def _ts(value):
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    try:
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        if len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
    except ValueError:
        return None
    return None


def parse_vtt(text):
    """-> [(start, end, words)]"""
    cues = []
    blocks = re.split(r"\n\s*\n", text.replace("\r", ""))
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines or lines[0].startswith("WEBVTT") or lines[0].startswith("NOTE"):
            continue
        timing = next((ln for ln in lines if "-->" in ln), None)
        if not timing:
            continue
        a, b = [p.strip().split()[0] for p in timing.split("-->")]
        start, end = _ts(a), _ts(b)
        if start is None or end is None or end <= start:
            continue
        spoken = " ".join(ln for ln in lines if "-->" not in ln and not ln.startswith("Kind:") and not ln.isdigit())
        spoken = TAG_RE.sub("", spoken)
        spoken = re.sub(r"\s+", " ", spoken).strip()
        words = WORD_RE.findall(spoken)
        if words:
            cues.append((start, end, words))
    return cues


def _dedupe_cues(cues):
    """YouTube auto-subs repeat the previous line inside the next cue."""
    kept = []
    for start, end, words in cues:
        text = " ".join(words)
        if kept:
            prev = " ".join(kept[-1][2])
            if text.lower() in prev.lower() or prev.lower() in text.lower():
                # rolling caption: keep the longer one, same start
                if len(words) > len(kept[-1][2]):
                    kept[-1] = (kept[-1][0], end, words)
                else:
                    kept[-1] = (kept[-1][0], max(kept[-1][1], end), kept[-1][2])
                continue
        kept.append((start, end, words))
    return kept


def _windows(cues):
    """Slide subtitle cues into 4-8s spoken moments."""
    cues = _dedupe_cues(cues)
    out = []
    for i, (start, _end, _words) in enumerate(cues):
        buf, end = [], start
        for j in range(i, min(len(cues), i + 6)):
            if j > i and cues[j][0] < end - 0.35:
                continue
            if cues[j][0] - start > 8.0:
                break
            buf.extend(cues[j][2])
            end = cues[j][1]
            dur = end - start
            if 4.0 <= dur <= 8.0 and 8 <= len(buf) <= 28:
                text = " ".join(buf)
                interesting = sum(1 for w in buf if w.lower() not in FILLER)
                score = interesting
                if text[:1].isupper():
                    score += 1
                out.append({
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "quote": text[:220],
                    "score": score,
                })
                break
    # unique by start
    seen, uniq = set(), []
    for w in sorted(out, key=lambda x: x["score"], reverse=True):
        key = int(w["start"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(w)
    return uniq


def _subs(url, work):
    os.makedirs(work, exist_ok=True)
    out = os.path.join(work, "subs")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--skip-download", "--write-auto-sub", "--write-sub",
        "--sub-langs", "en.*", "--sub-format", "vtt",
        "--extractor-args", "youtube:player_client=android,web",
        "-o", out, url,
    ]
    try:
        subprocess.run(cmd, timeout=40, capture_output=True, text=True)
    except Exception as e:
        print(f"  [quotes] subs failed: {type(e).__name__}")
        return ""
    for dirpath, _dirs, files in os.walk(work):
        for name in files:
            if name.endswith(".vtt"):
                try:
                    return open(os.path.join(dirpath, name), encoding="utf-8", errors="replace").read()
                except OSError:
                    continue
    return ""


def discover(topic, work, n=2):
    """-> [{quote, start, end, url, title, channel, party, path?, spans?, words?}]"""
    if not topic:
        return []
    rows = []
    for query in (f"{topic} interview", topic):
        for row in sources.search_videos(query, 5):
            row = dict(row)
            row["party"] = party_score(row.get("title"), row.get("channel"), topic)
            row["query"] = query
            rows.append(row)
    rows.sort(key=lambda r: r["party"], reverse=True)
    seen, ranked = set(), []
    for row in rows:
        if row["url"] in seen or not sources.title_ok(row.get("title") or ""):
            continue
        seen.add(row["url"])
        ranked.append(row)
    picked = []
    sub_dir = os.path.join(work, "quote_subs")
    for row in ranked[:4]:
        if len(picked) >= n:
            break
        print(f"  [quotes] reading subs party={row['party']:.1f} {row.get('title','')[:60]!r}")
        text = _subs(row["url"], os.path.join(sub_dir, str(len(picked))))
        windows = _windows(parse_vtt(text)) if text else []
        if not windows:
            print("  [quotes] no 4-8s spoken window")
            continue
        import creative
        choice = creative.pick_quote(topic, windows)
        best = windows[choice] if choice is not None else windows[0]
        item = {
            "quote": best["quote"],
            "start": best["start"],
            "end": best["end"],
            "url": row["url"],
            "title": row.get("title") or "",
            "channel": row.get("channel") or "",
            "party": row["party"],
            "query": row.get("query") or topic,
        }
        path = _download(item, work)
        if path:
            item["path"] = path
            rel0 = 0.15
            rel1 = max(1.6, item["end"] - item["start"])
            item["spans"] = [[round(rel0, 2), round(rel1, 2)]]
            # subtitle words, shifted into the downloaded section
            words = []
            cursor = 0.2
            for w in item["quote"].split():
                words.append([w, round(cursor, 2), round(cursor + 0.28, 2)])
                cursor += 0.30
            item["words"] = words
            picked.append(item)
            print(f"  [quotes] kept {rel1:.1f}s from {item['channel'][:24]!r}: {item['quote'][:80]!r}")
        else:
            print("  [quotes] section download failed, quote kept as text only")
            picked.append(item)
    return picked


def _download(item, work):
    start = max(0.0, float(item["start"]) - 0.2)
    end = float(item["end"]) + 0.4
    # a quote window is short on purpose; do not apply the 720p floor
    try:
        return sources.download(
            item["url"], work, f"quote{int(start)}",
            section=(start, end), min_height=0)
    except Exception as e:
        print(f"  [quotes] download error: {type(e).__name__}: {e}")
        return None
