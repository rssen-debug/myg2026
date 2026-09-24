"""Source acquisition: search -> relevance gate -> download -> quality gate
-> transcript verify -> pHash dedupe -> relevance-based assignment.

Three gates (Fable 5 design):
  gate 1: metadata relevance (embedding cosine / token overlap) >= RELEVANCE_MIN
  gate 2: quality probe (resolution/fps/face_ratio/letterbox/shot score)
  gate 3: optional transcript entity verify (VERIFY_TRANSCRIPT, CPU-heavy)
"""
import hashlib
import json
import shutil
import os
import re
import subprocess

import config
from vision import engine, score_shot

try:
    import yt_dlp
    from yt_dlp.utils import download_range_func
except ImportError:      # yt-dlp missing -> url mode still works with local files
    yt_dlp = None
    download_range_func = None

# Order matters: clients are tried in sequence and the first one that can
# serve a URL wins. Measured against the same real video, they differ wildly:
#   tv_embedded, android_producer -> 1080p available
#   android_vr, android           -> 360p only
#   web, ios, mweb, web_safari    -> fail outright ("Requested format is not
#                                    available" / "page needs to be reloaded")
# The previous list was ["web", "tv", "android"]: it spent its first two
# attempts on clients that raise, then silently took the worst quality on
# offer. That - not any IP-level block - was why every source came back 360p.
_CLIENTS = ["tv_embedded", "android_producer", "android_vr", "android",
            "web", "tv"]

# None = let yt-dlp choose, which is a different code path from naming a
# client: it runs its own default list plus whatever fallbacks the release
# ships. This matters more than any individual entry in _CLIENTS above,
# because YouTube retires clients without warning and the failures do not look
# like failures - "Requested format is not available" is what a dead client
# looks like when it is still accepted by the extractor. Measured on
# 2026-09-23: every named client above failed on a video that yt-dlp's own
# default downloaded without complaint, so the default is tried FIRST and the
# named clients are the fallback.
_DEFAULT_CLIENT = "__default__"

# The client that last succeeded, tried first. Client support shifts between
# yt-dlp releases, so probing costs ~3 s per attempt and a run makes dozens.
_LAST_GOOD = []


def _client_order():
    """Default first, then whatever worked last, then the quality-ordered rest."""
    rest = [_DEFAULT_CLIENT] + [c for c in _LAST_GOOD if c != _DEFAULT_CLIENT]
    rest += [c for c in _CLIENTS if c not in rest]
    return rest


def _extractor_args(client):
    """-> yt-dlp options for a client choice, empty for the default path."""
    if client == _DEFAULT_CLIENT:
        return {}
    return {"extractor_args": {"youtube": {"player_client": [client]}}}


def _print_client(client):
    return "yt-dlp default clients" if client == _DEFAULT_CLIENT else client


def _remember_client(client):
    if client in _LAST_GOOD:
        return
    _LAST_GOOD.insert(0, client)
    del _LAST_GOOD[2:]

_emb_model = None
_emb_failed = False


def _embedder():
    """Lazy sentence-transformers loader; None -> token-overlap fallback."""
    global _emb_model, _emb_failed
    if _emb_failed:
        return None
    if _emb_model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _emb_model = SentenceTransformer(
                "paraphrase-multilingual-MiniLM-L12-v2")
            print("  [sources] embeddings loaded (MiniLM)")
        except ImportError:
            _emb_failed = True
            print("  [sources] sentence-transformers missing -> "
                  "token-overlap relevance fallback")
    return _emb_model


# Number words expanded to a canonical digit form so "$1,000,000" and
# "1 million" compare equal. Without this, the query "mrbeast 1 million"
# scored 0.00 against the title "Spending $1,000,000 In 24 Hours" and threw
# away exactly the footage it was asking for - and the '1' was being dropped
# by the >2-character filter as well, so the number matched nothing at all.
_NUM_WORDS = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6,
              "b": 1e9, "bn": 1e9, "billion": 1e9}


def _norm_text(s):
    """Lowercase, strip currency, and canonicalise numbers."""
    s = re.sub(r"[$€£]", "", (s or "").lower())
    s = re.sub(r"(?<=\d),(?=\d{3}\b)", "", s)          # 1,000,000 -> 1000000

    def _mult(m):
        try:
            return str(int(float(m.group(1)) * _NUM_WORDS[m.group(2)]))
        except (ValueError, KeyError):
            return m.group(0)
    return re.sub(r"\b(\d+(?:\.\d+)?)\s*(k|thousand|m|mn|million|b|bn|billion)\b",
                  _mult, s)


def _tokens(s):
    """Content tokens. Digits of any length count; words need 3+ chars so
    'ad', 'in', 'of' do not create accidental matches.

    Possessives are folded ("MrBeast's" -> "mrbeast"). Without this a query
    that names the subject in the possessive - "Inside MrBeast's Money
    Machine" - produced the token "mrbeast's", which matches no channel and no
    title, so the channel floor in relevance_score() never fired and every
    MrBeast clip was rejected with relevance=0.00. The best footage for the
    video was being thrown away by one apostrophe.
    """
    toks = set()
    for w in re.findall(r"[a-z0-9']+", _norm_text(s)):
        w = w.replace("\u2019", "'").strip("'")
        if w.endswith("'s"):
            w = w[:-2]
        if len(w) > 2 or w.isdigit():
            toks.add(w)
    return toks


def relevance_score(query, title, description="", channel=""):
    """0..1 similarity between a script line/query and candidate metadata.
    Embedding cosine when available, normalized token overlap otherwise.

    The channel is part of the candidate text on purpose: for b-roll the
    question is "is this footage of the right subject", and a video called
    "Spending $1,000,000 In 24 Hours" uploaded by MrBeast is a perfect hit for
    the query "mrbeast 1 million" even though the title shares almost nothing
    with it. Scoring the title alone rejected the best clips in the pool.
    """
    if not query:
        return 0.0
    txt = f"{title} {channel or ''} {(description or '')[:300]}"
    emb = _embedder()
    if emb is not None:
        try:
            from sentence_transformers import util
            sim = float(util.cos_sim(emb.encode(query[:400]),
                                     emb.encode(txt[:600]))[0][0])
            return max(0.0, min(1.0, (sim + 1) / 2))   # -1..1 -> 0..1
        except Exception:
            pass
    qtok = _tokens(query)
    if not qtok:
        return 0.0
    overlap = len(qtok & _tokens(txt)) / len(qtok)
    bonus = 0.15 if _norm_text(query).strip() in _norm_text(txt) else 0.0
    score = overlap + bonus
    # A channel-name match means the clip is BY the subject the query is about.
    # For b-roll that is the strongest relevance signal there is: a video called
    # "Spending $1,000,000 In 24 Hours" from the MrBeast channel is the right
    # footage for "mrbeast million challenge" even though only 2 of the query's
    # 3 words appear anywhere in it. Without this floor the gate kept throwing
    # away exactly the clips it should have wanted.
    if channel and (qtok & _tokens(channel)):
        score = max(score, 0.65)
    return min(1.0, score)


def _ydl(extra=None):
    opts = {"quiet": True, "noplaylist": True, "no_warnings": True,
            "socket_timeout": 25, "retries": 3}
    if extra:
        opts.update(extra)
    return yt_dlp.YoutubeDL(opts)


def search_videos(query, n=6):
    """-> [{'url','title'}] via ytsearch. Multi-client fallback."""
    if yt_dlp is None:
        print("  [sources] yt-dlp not installed")
        return []
    for client in _client_order():
        try:
            with _ydl({"extract_flat": True,
                       **_extractor_args(client)}) as ydl:
                info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
            out = [{"url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
                    "title": e.get("title", ""),
                    "channel": e.get("channel") or e.get("uploader") or "",
                    "description": e.get("description") or ""}
                   for e in (info.get("entries") or []) if e]
            if out:
                _remember_client(client)
                return out
        except Exception as e:
            print(f"  [sources] search client '{client}' failed: {type(e).__name__}")
    return []


def _party_score(row, topic):
    """Own channel before a reaction upload. Used only to sort search hits."""
    tokens = [t for t in re.findall(r"[a-z0-9]+", (topic or "").lower()) if len(t) > 2]
    title = (row.get("title") or "").lower()
    channel = (row.get("channel") or "").lower()
    score = sum(2 for t in tokens if t in channel) + sum(1 for t in tokens if t in title)
    if any(b in title for b in config.BAD_TITLE_WORDS):
        score -= 4
    return score


def title_ok(title):
    t = title.lower()
    return not any(b in t for b in config.BAD_TITLE_WORDS)


def probe_url(url):
    """Cheap metadata lookup WITHOUT downloading.

    Why this exists: the quality gate (height/fps/duration) used to run AFTER
    the download, so a 360p two-hour podcast was fetched in full - 443 MB, 35 s
    of bandwidth - and only then rejected. Metadata is available for free, so
    the gate belongs before the transfer, next to the relevance gate.

    -> {'height','fps','duration','title'} or None.
    """
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "socket_timeout": 30}
    for client in _client_order():
        try:
            p = dict(opts)
            p.update(_extractor_args(client))
            with yt_dlp.YoutubeDL(p) as ydl:
                info = ydl.extract_info(url, download=False)
            if not info:
                continue
            _remember_client(client)
            fmts = [f for f in (info.get("formats") or [])
                    if (f.get("vcodec") or "none") != "none"]
            best_h = max((f.get("height") or 0) for f in fmts) if fmts else 0
            fps = 0.0
            for f in fmts:
                if (f.get("height") or 0) >= best_h and f.get("fps"):
                    fps = float(f["fps"])
                    break
            return {"height": int(info.get("height") or best_h or 0),
                    "fps": fps,
                    "duration": float(info.get("duration") or 0),
                    "title": str(info.get("title") or "")}
        except Exception:
            continue
    return None


def url_fails_quality_gate(info, title="", min_height=None):
    """-> (rejected: bool, reason: str). Uses only metadata, costs no download.
    min_height overrides config.MIN_SOURCE_HEIGHT (used by the relaxed retry)."""
    min_h = config.MIN_SOURCE_HEIGHT if min_height is None else min_height
    if not info:
        return False, ""                 # unknown -> let the download decide
    h = info.get("height") or 0
    dur = info.get("duration") or 0
    if min_h and h and h < min_h:
        return True, f"only {h}p (need {min_h}p)"
    if dur and dur < 45:
        return True, f"too short ({dur:.0f}s)"
    if dur and dur > config.MAX_SOURCE_DURATION:
        # not fatal: we only download a section, so long VODs are fine as long
        # as we slice them
        return False, f"long VOD ({dur / 60:.0f} min) - will slice a section"
    return False, ""


def _cache_key(url, start, sec):
    return hashlib.sha1(f"{url}|{start}|{sec}".encode()).hexdigest()[:16]


def _cache_lookup(url, start, sec):
    p = os.path.join(config.CLIP_CACHE_DIR, _cache_key(url, start, sec) + ".mp4")
    return p if os.path.exists(p) and os.path.getsize(p) > 50_000 else None


def _cache_store(url, start, sec, path):
    try:
        os.makedirs(config.CLIP_CACHE_DIR, exist_ok=True)
        dst = os.path.join(config.CLIP_CACHE_DIR,
                           _cache_key(url, start, sec) + ".mp4")
        if not os.path.exists(dst):
            shutil.copy2(path, dst)
    except Exception as e:
        print(f"  [sources] cache write skipped: {type(e).__name__}")


def download(url, out_dir, tag, section=None, info=None, min_height=None):
    """Download a SECTION of the best >=720p <=1080p mp4.

    Two things this deliberately does NOT do:
      * fetch the whole VOD. A two-hour podcast is 400+ MB and we need 6-8
        seconds of it; download_ranges slices the section we will actually use.
      * accept a format below MIN_SOURCE_HEIGHT. The height floor lives in the
        format string, so a 360p-only source fails selection instead of
        silently downloading something the quality gate will reject anyway.

    section: (start_sec, end_sec) or None. Defaults to config.SECTION_SECONDS
    from an offset that avoids intros.
    -> path or None.
    """
    h = min_height or config.MIN_SOURCE_HEIGHT
    sec = int(section[1] - section[0]) if section else config.SECTION_SECONDS
    start = int(section[0]) if section else config.SECTION_START

    cached = _cache_lookup(url, start, sec)
    if cached:
        print(f"  [sources] cached section reused: {os.path.basename(cached)}")
        return cached

    out_tmpl = os.path.join(out_dir, f"{tag}.%(ext)s")
    params = {
        "outtmpl": out_tmpl,
        # avc1/h264 FIRST. Slicing a ranged download goes through ffmpeg, and
        # the AV1 and VP9 variants that YouTube serves over HLS fail every
        # time ("ffmpeg exited with code 8"). Those failures are also not
        # clean: a failed merge can leave an audio-only file behind, so the
        # avc1 preference is about the download actually working, not just
        # about codec fashion. The trailing fallbacks stay height-constrained -
        # an unconstrained "b" would let a 360p file masquerade as a result.
        "format": (f"bv*[height>={h}][height<=1080][ext=mp4][vcodec^=avc1]+ba[ext=m4a]/"
                   f"bv*[height>={h}][height<=1080][ext=mp4][vcodec^=avc1]+ba/"
                   f"bv*[height>={h}][height<=1080][ext=mp4]+ba[ext=m4a]/"
                   f"bv*[height>={h}][height<=1080]+ba/"
                   f"b[height>={h}][height<=1080]"),
        "merge_output_format": "mp4",
        "noplaylist": True, "quiet": True, "no_warnings": True,
        "noprogress": True, "retries": 2, "socket_timeout": 30,
        "download_ranges": download_range_func(None, [(start, start + sec)]),
        "force_keyframes_at_cuts": True,
        # yt-dlp slices a ranged download with ffmpeg and looks for a binary
        # literally named "ffmpeg" on PATH. Ours is the imageio-ffmpeg build
        # (ffmpeg-linux-x86_64-v7.0.2), so without this hint partial downloads
        # die with "ffmpeg is not installed" and every clip fails.
        "ffmpeg_location": config.FFMPEG,
    }
    for client in _client_order():
        try:
            p = dict(params)
            p.update(_extractor_args(client))
            with yt_dlp.YoutubeDL(p) as ydl:
                info_dl = ydl.extract_info(url, download=True)
            _remember_client(client)
            path = ydl.prepare_filename(info_dl)
            path = os.path.splitext(path)[0] + ".mp4"
            # ranged downloads sometimes land on a different extension
            cands = [path] + [os.path.splitext(path)[0] + e
                              for e in (".mkv", ".webm", ".mp4")]
            for cand in cands:
                if not (os.path.exists(cand) and os.path.getsize(cand) > 50_000):
                    continue
                # A file size proves nothing here: a failed video merge leaves
                # a perfectly plausible audio-only mp4 behind, which then
                # reaches the quality gate as "height 0" and looks like a
                # scoring bug instead of a download bug. Confirm there is
                # actually a video stream before handing it on.
                m = ffprobe_meta(cand)
                if (m.get("height") or 0) < 1:
                    print(f"  [sources] client '{client}' produced no video "
                          f"stream ({os.path.getsize(cand) / 1e6:.1f} MB, "
                          f"audio only) - trying next client")
                    try:
                        os.remove(cand)
                    except OSError:
                        pass
                    break            # this client's formats are unusable here
                _cache_store(url, start, sec, cand)
                return cand
        except Exception as e:
            # the exception TYPE hides everything useful: a bot check, a
            # retired client and a dead format all raise DownloadError
            msg = str(e).strip().splitlines()[-1][:110] if str(e).strip() else ""
            print(f"  [sources] download client '{_print_client(client)}' "
                  f"failed: {type(e).__name__}: {msg}")
    return None


def _meta_from_ffmpeg(path):
    """Parse `ffmpeg -i` stderr when ffprobe is unavailable (e.g. a pip-only
    install of imageio-ffmpeg, which ships ffmpeg but not ffprobe).
    Returns the same dict shape as the ffprobe path."""
    try:
        err = subprocess.run([config.FFMPEG, "-hide_banner", "-i", path],
                             capture_output=True, text=True,
                             timeout=60).stderr
    except Exception:
        return {}
    dur = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    w = h = 0
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    fps = 0.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", err)
    if m:
        fps = float(m.group(1))
    return {"height": h, "width": w, "fps": fps, "duration": dur,
            "has_audio": "Audio:" in err}


def ffprobe_meta(path):
    """Video metadata. Uses ffprobe when present, otherwise falls back to
    parsing ffmpeg. Always returns the same keys; never raises."""
    if config.FFPROBE:
        try:
            out = subprocess.run(
                [config.FFPROBE, "-v", "error", "-print_format", "json",
                 "-show_format", "-show_streams", path],
                capture_output=True, text=True, timeout=60).stdout
            meta = json.loads(out)
            v = next((s for s in meta.get("streams", [])
                      if s.get("codec_type") == "video"), {})
            fps = 0.0
            rr = v.get("r_frame_rate") or "0/1"
            try:
                num, den = rr.split("/")
                if float(den):
                    fps = float(num) / float(den)
            except (ValueError, ZeroDivisionError):
                fps = 0.0
            got = {"height": int(v.get("height") or 0),
                   "width": int(v.get("width") or 0),
                   "fps": fps,
                   "duration": float(meta.get("format", {}).get("duration") or 0),
                   "has_audio": any(s.get("codec_type") == "audio"
                                    for s in meta.get("streams", []))}
            if got["width"]:
                return got
        except Exception:
            pass
    return _meta_from_ffmpeg(path)


def probe_local(path, samples=10):
    """Probe a downloaded clip: face_ratio, letterbox, shot score."""
    import cv2
    meta = ffprobe_meta(path)
    if not meta.get("height"):
        return None
    eng = engine()
    face_hits, lb_hits, got = 0, 0, 0
    cap = cv2.VideoCapture(path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    dur = meta.get("duration") or 0
    for k in range(samples):
        frac = (k + 0.5) / samples
        if total > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * frac))
        else:
            # some containers report 0 frames -> seek by time instead
            cap.set(cv2.CAP_PROP_POS_MSEC, dur * frac * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        got += 1
        h = frame.shape[0]
        small = cv2.resize(frame, (320, max(2, int(320 * h / frame.shape[1]))))
        if eng.detect_faces(small):
            face_hits += 1
        band = max(2, int(h * 0.05))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if gray[:band].mean() < 12 and gray[-band:].mean() < 12:
            lb_hits += 1
    cap.release()
    if got == 0:
        return None
    meta["face_ratio"] = face_hits / got
    meta["letterbox"] = (lb_hits / got) >= 0.8
    meta["shot_score"] = score_shot(path)
    return meta


def score_source(meta, title="", min_height=None):
    """Quality/face score. min_height MUST be the same floor the download used.

    The bug this signature fixes: when every source is below the 720p floor the
    downloader relaxes the floor and fetches the clip anyway, but scoring kept
    comparing against the hard 720p default and returned 0.0 - so the relaxed
    clip was downloaded (bandwidth spent) and then thrown away, and the pool
    stayed empty. One run, one effective floor.
    """
    if meta is None:
        return 0.0
    if not title_ok(title):
        return 0.0
    min_h = config.MIN_SOURCE_HEIGHT if min_height is None else min_height
    fps = meta.get("fps", 0)
    if (min_h and meta["height"] < min_h) or (fps and fps < config.MIN_SOURCE_FPS):
        return 0.0
    s = 1.0
    s += 0.5 if meta["height"] >= 1080 else 0.0
    s += meta.get("face_ratio", 0) * 1.0
    s -= 0.5 if meta.get("letterbox") else 0.0
    s -= 0.6 if meta.get("shot_score", 0) < config.SHOT_SCORE_MIN else 0.0
    if meta.get("duration", 0) > 1800:
        s -= 0.4
    return s


def verify_by_transcript(path, query, max_sec=45):
    """Gate 3: do the queried entities actually appear in the clip's audio?
    Off unless config.VERIFY_TRANSCRIPT (45s of CPU-ASR per candidate)."""
    if not config.VERIFY_TRANSCRIPT or not query:
        return True
    ents = [t for t in re.findall(r"[A-Za-z0-9']{4,}", query)]
    if not ents:
        return True
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return True
    wav = path + ".verify.wav"
    try:
        subprocess.run([config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                        "-t", str(max_sec), "-i", path,
                        "-ar", "16000", "-ac", "1", wav],
                       check=True, capture_output=True, timeout=120)
        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        segs, _ = model.transcribe(wav)
        text = " ".join(s.text for s in segs).lower()
    except Exception:
        return True
    finally:
        if os.path.exists(wav):
            os.remove(wav)
    hits = sum(1 for e in ents if e.lower() in text)
    return hits >= 1


def phash_frames(path):
    """Perceptual hashes of sampled frames for dedupe. [] if imagehash missing."""
    try:
        import imagehash
        from PIL import Image
    except ImportError:
        return []
    hashes = []
    from vision import sample_frames
    for f in sample_frames(path, 5):
        hashes.append(str(imagehash.phash(
            Image.fromarray(f[..., ::-1]).resize((32, 32)))))
    return hashes


def _is_phash_dup(hashes, seen_hashes):
    """Hamming distance <= PHASH_MAX_DIST counts as duplicate (re-encodes)."""
    if not hashes or not seen_hashes:
        return False
    try:
        import imagehash
        for h in hashes:
            hh = imagehash.hex_to_hash(h)
            for s in seen_hashes:
                if hh - imagehash.hex_to_hash(s) <= config.PHASH_MAX_DIST:
                    return True
    except Exception:
        return any(h in seen_hashes for h in hashes)
    return False


def acquire_clip_pool(queries, work_dir, urls=None, max_downloads=None,
                      topic_query=None):
    """Search/download/gate clips for a list of queries (+ optional pasted urls).

    topic_query: the story subject (title/topic). Tried first as a safety net so
    the pool can never end up empty just because every per-line visual query
    missed - an empty pool means a slideshow, and that is a rejected video.
    -> [{'id','path','title','meta','phash','queries'}]"""
    max_downloads = max_downloads or config.MAX_DOWNLOADS_PER_RUN
    pool, seen_hash, used_urls = [], [], set()
    relaxed_candidates = []

    def consider(url, title, tag, query=None, info=None, min_height=None,
                 channel="", description=""):
        if len(pool) >= max_downloads or url in used_urls:
            return
        # gate 1a: relevance BEFORE downloading
        rel = relevance_score(query or title, title, description, channel)
        if query and rel < config.RELEVANCE_MIN:
            print(f"  [sources] REJECTED gate1 relevance={rel:.2f}: "
                  f"{title[:55]!r} [{channel[:18]}] vs query {query[:40]!r}")
            return
        # gate 1b: quality METADATA before downloading. Checking height here
        # instead of after the transfer is the difference between a few MB and
        # hundreds of MB spent on a clip that gets thrown away.
        if info is None and yt_dlp is not None:
            info = probe_url(url)
        bad, why = url_fails_quality_gate(info, title, min_height=min_height)
        if bad:
            print(f"  [sources] REJECTED pre-download (metadata): {title[:50]!r}"
                  f" - {why}")
            if min_height is None and info:
                relaxed_candidates.append((url, title, tag, query, info,
                                           channel, description))
            return
        if why:
            print(f"  [sources] note: {title[:44]!r} - {why}")
        used_urls.add(url)
        eff_floor = config.MIN_SOURCE_HEIGHT if min_height is None else min_height
        path = download(url, work_dir, tag, info=info, min_height=eff_floor)
        if not path:
            print(f"  [sources] download failed: {title[:60]}")
            return
        # gate 2: quality + faces
        meta = probe_local(path)
        sc = score_source(meta, title, min_height=eff_floor)
        print(f"  [sources] {title[:55]!r}: rel={rel:.2f} score={sc:.2f} "
              f"faces={meta.get('face_ratio', 0) if meta else 0:.2f} "
              f"h={meta.get('height') if meta else 0}"
              f"{' (floor relaxed)' if eff_floor != config.MIN_SOURCE_HEIGHT else ''}")
        if sc < config.SOURCE_SCORE_MIN:
            os.remove(path)
            print("    -> REJECTED gate2 (quality/face score)")
            return
        # gate 3: transcript entity verify (optional, CPU-heavy)
        if not verify_by_transcript(path, query or ""):
            os.remove(path)
            print("    -> REJECTED gate3 (entities not in transcript)")
            return
        hs = phash_frames(path)
        if _is_phash_dup(hs, seen_hash):
            os.remove(path)
            print("    -> REJECTED (pHash duplicate, re-encode detected)")
            return
        seen_hash.extend(hs)
        pool.append({"id": f"clip{len(pool):02d}", "path": path, "title": title,
                     "meta": meta, "phash": hs,
                     "queries": [query] if query else []})

    for u in (urls or []):
        consider(u, "pasted-url", f"pasted{len(pool)}")

    # Safety net: whatever else happens, try one query built from the subject
    # itself. Per-line queries are written by the LLM and can all miss (a
    # visual description rarely matches a real video title), and the failure
    # mode is severe - an empty pool means no footage and a slideshow, which
    # MANDATE explicitly rejects. The subject query is the one that is
    # guaranteed to be about this story.
    subject = topic_query or ""
    if subject:
        queries = [subject] + [q for q in queries if q != subject]
    for q in queries:
        if len(pool) >= max_downloads:
            break
        if any(q in c.get("queries", []) for c in pool):
            continue
        results = [r for r in search_videos(q, 6) if title_ok(r["title"])]
        # first-party channel before a reaction upload of the same name
        if topic_query:
            results.sort(key=lambda r: _party_score(r, topic_query), reverse=True)
        for r in results[:3]:
            before = len(pool)
            consider(r["url"], r["title"], f"q{len(used_urls):02d}", query=q,
                     channel=r.get("channel", ""),
                     description=r.get("description", ""))
            if len(pool) > before:
                break

    # Nothing survived the quality floor -> relax it once, loudly, rather than
    # ship a video with no real footage in it.
    if not pool and relaxed_candidates:
        floor = config.MIN_SOURCE_HEIGHT_FALLBACK
        print(f"  [sources] WARNING: no clip met the {config.MIN_SOURCE_HEIGHT}p "
              f"floor. Retrying {len(relaxed_candidates)} candidate(s) at best "
              f"available quality. Footage will be visibly softer than the "
              f"1080p target - usually a sign that YouTube is only offering "
              f"low-res formats to this IP.")
        for (url, title, tag, query, info, channel,
             description) in relaxed_candidates[:max_downloads]:
            if len(pool) >= max_downloads:
                break
            consider(url, title, tag, query=query, info=info, min_height=floor,
                     channel=channel, description=description)
    return pool


def assign_clip(line_text, pool, used_ids, use_count=None):
    """Relevance-based assignment with reuse penalty (no clip winning 20 beats
    in a row). use_count: {clip_id: times_used_so_far}."""
    use_count = use_count or {}
    best, best_score = None, -1e9
    for c in pool:
        sc = relevance_score(line_text, c["title"])
        sc += 0.3 if c["id"] not in used_ids else 0.0
        sc -= 0.4 * use_count.get(c["id"], 0)
        sc += 0.3 * c["meta"].get("face_ratio", 0)
        if sc > best_score:
            best_score, best = sc, c
    if best is None and pool:
        best = pool[0]
    return best
