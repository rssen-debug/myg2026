"""Name-locked stills, ported from new2226.

A Commons photo is only used when the file title actually contains the
subject. Searching "N3on" must not come back as a neon sign. These stills are
the evidence panel when a beat has no footage, not a replacement for it.
"""
import io
import os
import re

import requests
from PIL import Image

API = "https://commons.wikimedia.org/w/api.php"
UA = {"User-Agent": "sunny-pipeline/4 (stills; contact: local)"}
STOP = {"the", "a", "an", "of", "and", "story", "true", "real"}


def _tokens(subject):
    return [t for t in re.findall(r"[a-z0-9]+", (subject or "").lower())
            if len(t) > 2 and t not in STOP]


def _title_has_subject(title, tokens):
    low = (title or "").lower().replace("_", " ")
    if not tokens:
        return False
    # every significant token must be in the file title, so a partial
    # collision ("neon") cannot stand in for a person
    return all(t in low for t in tokens)


def fetch(subject, out_dir, n=4):
    """-> [png path]. Empty list on any failure. Never raises."""
    tokens = _tokens(subject)
    if not tokens:
        return []
    os.makedirs(out_dir, exist_ok=True)
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": "filetype:bitmap " + " ".join(tokens),
        "gsrnamespace": 6, "gsrlimit": max(8, n * 3),
        "prop": "imageinfo", "iiprop": "url", "iiurlwidth": 640,
    }
    try:
        r = requests.get(API, params=params, headers=UA, timeout=20)
        r.raise_for_status()
        pages = ((r.json().get("query") or {}).get("pages") or {})
    except Exception as e:
        print(f"  [stills] search failed: {type(e).__name__}")
        return []
    pages = sorted(pages.values(), key=lambda p: p.get("index", 99))
    saved = []
    for page in pages:
        title = page.get("title") or ""
        if not _title_has_subject(title, tokens):
            print(f"  [stills] REJECT stand-in: {title[:70]!r}")
            continue
        ii = (page.get("imageinfo") or [{}])[0]
        url = ii.get("thumburl") or ii.get("url")
        if not url:
            continue
        try:
            img = requests.get(url, headers=UA, timeout=20)
            img.raise_for_status()
            im = Image.open(io.BytesIO(img.content)).convert("RGB")
        except Exception as e:
            print(f"  [stills] download failed: {type(e).__name__}")
            continue
        path = os.path.join(out_dir, f"still_{len(saved):02d}.jpg")
        im.save(path, "JPEG", quality=85)
        saved.append(path)
        print(f"  [stills] kept {os.path.basename(path)} <- {title[:70]}")
        if len(saved) >= n:
            break
    if not saved:
        print(f"  [stills] no title-verified photo for {subject!r}")
    return saved
