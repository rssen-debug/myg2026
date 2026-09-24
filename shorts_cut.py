"""Shorts without TTS.

The person talks. Their words are captions, inside the safe area. Hook, stat
and lower third are HTML + a Three.js accent, rendered in headless Chromium
and laid over the footage. Narration is a documentary tool, not a short.
"""
import json
import os
import re
import subprocess
from datetime import datetime

import config
import music
import render
import sources
import vision


def _dur(path):
    meta = sources.ffprobe_meta(path)
    return float(meta.get("duration") or 0)


def _chunks(text, n=3):
    words = re.findall(r"[A-Za-z0-9']+", text or "")
    out = []
    for i in range(0, len(words), n):
        bit = " ".join(words[i:i + n])
        if bit:
            out.append(bit)
    return out or [" "]


def _stat(claims):
    for c in claims or []:
        if c.get("kind") == "FACT" and c.get("numbers") and c.get("tier", 5) <= 2:
            raw = c["numbers"][0]
            big = raw.upper().replace(" MILLION", "M").replace(" BILLION", "B")
            label = "ON THE RECORD"
            claim = (c.get("claim") or "").lower()
            if "subscriber" in claim:
                label = "SUBSCRIBERS"
            elif "view" in claim:
                label = "VIEWS"
            return {"big": big[:10], "label": label}
    return None


def _timeline(packet, topic, dur, w, h, fps):
    quote = ""
    quotes = packet.get("quotes") or []
    chunks = []
    if quotes:
        quote = quotes[0].get("quote") or ""
        chunks = [c for c in (quotes[0].get("chunks") or []) if c]
    if not chunks:
        chunks = _chunks(quote, 3)
    # leave the first 0.35s for the hook to land, then their words
    span = max(0.4, (dur - 0.5) / max(len(chunks), 1))
    caps = []
    t = 0.35
    for bit in chunks:
        if t >= dur - 0.15:
            break
        caps.append({"t": round(t, 3), "dur": round(min(span, dur - t), 3), "text": bit})
        t += span
    stat = _stat(packet.get("claims"))
    if stat:
        stat["t"] = min(3.0, dur * 0.42)
        stat["dur"] = min(2.1, max(1.2, dur * 0.28))
    hook = (packet.get("hook") or topic or "").rstrip("?").upper()
    channel = ""
    if quotes:
        channel = (quotes[0].get("channel") or "")[:32]
    if channel.lower().replace(" ", "") == (topic or "").lower().replace(" ", ""):
        channel = ""
    return {
        "w": w, "h": h, "fps": fps, "duration": dur,
        "hook": hook,
        "kicker": "HE SAID THIS",
        "hookIn": 0.12,
        "hookOut": min(2.8, dur * 0.4),
        "person": topic,
        "personSub": channel or "ON CAMERA",
        "lowerIn": 0.4,
        "stat": stat,
        "captions": caps,
        "brackets": True,
    }


def _overlay(timeline, work):
    gfx_dir = os.path.join(work, "htmlgfx")
    os.makedirs(gfx_dir, exist_ok=True)
    tl_path = os.path.join(work, "gfx_timeline.json")
    with open(tl_path, "w", encoding="utf-8") as f:
        json.dump(timeline, f)
    script = os.path.join(config.ROOT, "gfx", "domgfx.mjs")
    subprocess.run(
        ["node", script, "--timeline", tl_path, "--out", gfx_dir],
        check=True, cwd=config.ROOT,
    )
    return gfx_dir


def _picture(clip, dur, fmt, work):
    out = os.path.join(work, "short_picture.mp4")
    try:
        _fps, _W, _H, path = vision.compute_crop_path(clip, t_start=0, t_end=max(dur, 0.5))
        cx = vision.cx_at(path, dur / 2)
    except Exception as e:
        print(f"  [short] face track skipped ({type(e).__name__}) — center crop")
        meta = sources.ffprobe_meta(clip)
        cx = (meta.get("width") or 1080) / 2
    render.render_segment(clip, 0.0, dur, out, fmt, cx, has_audio=True)
    return out


def _composite(picture, gfx_dir, fps, out):
    subprocess.run([
        config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", picture,
        "-framerate", str(fps), "-i", os.path.join(gfx_dir, "%04d.png"),
        "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto,format=yuv420p",
        "-c:v", "libx264", "-preset", config.ENCODE_PRESET, "-crf", str(config.ENCODE_CRF),
        "-c:a", "aac", "-b:a", "160k", "-shortest", out,
    ], check=True)


def _bed(voiced, dur, out):
    bed = voiced + ".bed.wav"
    try:
        music.make_music(bed, dur + 0.5, "mysterious")
    except Exception as e:
        print(f"  [short] music skipped: {e}")
        os.replace(voiced, out)
        return
    # their voice stays in front. the bed is under it, not a narrator.
    subprocess.run([
        config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", voiced, "-i", bed,
        "-filter_complex",
        "[1:a]volume=0.10[m];[0:a][m]amix=inputs=2:duration=first:dropout_transition=0,"
        "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", out,
    ], check=True)


def build(packet, fmt, work, topic):
    """-> final mp4 path, or None if there is no footage to cut."""
    clip = None
    for q in packet.get("quotes") or []:
        if q.get("path") and os.path.exists(q["path"]):
            clip = q["path"]
            break
    if not clip:
        print("  [short] no quote clip — cannot cut a no-TTS short")
        return None
    dur = _dur(clip)
    if dur < 1.5:
        print(f"  [short] quote clip too short ({dur:.2f}s)")
        return None
    # a short is the quote, not a padded narration. cap so a long section
    # does not become a documentary by accident.
    dur = min(dur, 16.0)
    fps = config.OUTPUT_FPS
    print(f"  [short] {dur:.2f}s of their voice, no narrator  {fmt['w']}x{fmt['h']}")
    picture = _picture(clip, dur, fmt, work)
    timeline = _timeline(packet, topic, dur, fmt["w"], fmt["h"], fps)
    with open(os.path.join(work, "gfx_timeline.json"), "w", encoding="utf-8") as f:
        json.dump(timeline, f, indent=1)
    print("  [short] rendering HTML + Three.js overlay ...")
    gfx_dir = _overlay(timeline, work)
    voiced = os.path.join(work, "short_voiced.mp4")
    _composite(picture, gfx_dir, fps, voiced)
    os.makedirs(config.OUT_DIR, exist_ok=True)
    slug = "".join(ch for ch in (packet.get("title") or topic)[:30]
                   if ch.isalnum() or ch == " ").strip().replace(" ", "_")
    final = os.path.join(
        config.OUT_DIR,
        f"short_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{slug}.mp4")
    _bed(voiced, dur, final)
    print(f"  [short] {final}")
    return final
