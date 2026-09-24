"""Burn the HTML/Three.js cards onto a finished documentary.

Shorts already render this layer for the whole cut. A documentary is longer,
so only the card windows are rendered in Chromium and overlaid. Captions stay
the ASS track locked to the narration. If Chromium fails, the caller keeps
the video it already has.
"""
import json
import os
import subprocess

import config
import sources


def _run_gfx(timeline, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    tl = os.path.join(out_dir, "timeline.json")
    with open(tl, "w", encoding="utf-8") as f:
        json.dump(timeline, f)
    subprocess.run(
        ["node", os.path.join(config.ROOT, "gfx", "domgfx.mjs"),
         "--timeline", tl, "--out", out_dir],
        check=True, cwd=config.ROOT,
    )


def _card(topic, packet, w, h, fps, dur, kind, stat=None):
    hook = (packet.get("hook") or topic or "").rstrip("?").upper()
    channel = ""
    quotes = packet.get("quotes") or []
    if quotes:
        channel = (quotes[0].get("channel") or "")[:32]
    if channel.lower().replace(" ", "") == (topic or "").lower().replace(" ", ""):
        channel = ""
    tl = {
        "w": w, "h": h, "fps": fps, "duration": dur,
        "hook": hook if kind == "hook" else "",
        "kicker": "THE FILE" if kind == "hook" else "",
        "hookIn": 0, "hookOut": dur if kind == "hook" else 0,
        "person": topic if kind == "lower" else "",
        "personSub": channel or "ON CAMERA",
        "lowerIn": 0 if kind == "lower" else 99,
        "stat": stat if kind == "stat" else None,
        "captions": [],
        # corner marks on the slam cards only — a persistent frame on a
        # 10-minute doc fights the footage
        "brackets": kind != "lower",
    }
    return tl


def _stat(packet):
    for c in packet.get("claims") or []:
        if c.get("kind") == "FACT" and c.get("numbers") and c.get("tier", 5) <= 2:
            raw = c["numbers"][0].upper().replace(" MILLION", "M").replace(" BILLION", "B")
            label = "SUBSCRIBERS" if "subscriber" in (c.get("claim") or "").lower() else "ON THE RECORD"
            return {"big": raw[:10], "label": label, "t": 0, "dur": 2.0}
    return None


def burn(video, packet, topic, fmt):
    """Overlay hook, stat slam and lower third. Replaces video on success."""
    dur = float(sources.ffprobe_meta(video).get("duration") or 0)
    if dur < 2:
        print("  [html] video too short for cards")
        return video
    w, h, fps = fmt["w"], fmt["h"], config.OUTPUT_FPS
    work = os.path.join(os.path.dirname(video), "_htmlcards")
    hook_dur = min(2.6, max(1.2, dur * 0.28))
    stat = _stat(packet)
    stat_at = min(3.2, max(hook_dur + 0.3, dur * 0.4))
    print(f"  [html] cards on {os.path.basename(video)} ({dur:.1f}s)")

    _run_gfx(_card(topic, packet, w, h, fps, hook_dur, "hook"), os.path.join(work, "hook"))
    _run_gfx(_card(topic, packet, w, h, fps, 0.7, "lower"), os.path.join(work, "lower"))
    inputs = [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
              "-i", video,
              "-framerate", str(fps), "-i", os.path.join(work, "hook", "%04d.png")]
    parts = ["[1:v]setpts=PTS+0.15/TB,format=rgba[h]"]
    next_idx = 2
    use_stat = bool(stat) and stat_at + 2.1 < dur
    if use_stat:
        _run_gfx(_card(topic, packet, w, h, fps, 2.0, "stat", stat), os.path.join(work, "stat"))
        inputs += ["-framerate", str(fps), "-i", os.path.join(work, "stat", "%04d.png")]
        parts.append(f"[{next_idx}:v]setpts=PTS+{stat_at:.2f}/TB,format=rgba[s]")
        next_idx += 1
    inputs += ["-framerate", str(fps), "-i", os.path.join(work, "lower", "%04d.png")]
    parts.append(
        f"[{next_idx}:v]tpad=stop_mode=clone:stop_duration={dur:.2f},"
        f"setpts=PTS+0.4/TB,format=rgba[l]"
    )
    # intro only. A lower third that stays up covers the karaoke for the
    # whole documentary. It yields to the stat slam and does not return.
    lower_end = stat_at if use_stat else min(4.2, max(1.6, dur - 0.4))
    lower_enable = f"between(t,0.4,{lower_end:.2f})"
    parts.append(f"[0:v][l]overlay=0:0:enable='{lower_enable}':eof_action=pass[vL]")
    parts.append(
        f"[vL][h]overlay=0:0:enable='between(t,0.15,{0.15 + hook_dur:.2f})':eof_action=pass[vH]"
    )
    prev = "vH"
    if use_stat:
        parts.append(
            f"[vH][s]overlay=0:0:enable='between(t,{stat_at:.2f},{stat_at + 2.0:.2f})':eof_action=pass[vS]"
        )
        prev = "vS"
    out = video + ".cards.mp4"
    inputs += ["-filter_complex", ";".join(parts),
               "-map", f"[{prev}]", "-map", "0:a?",
               "-c:v", "libx264", "-preset", config.ENCODE_PRESET,
               "-crf", str(config.ENCODE_CRF),
               "-c:a", "copy", "-shortest", out]
    subprocess.run(inputs, check=True)
    os.replace(out, video)
    print(f"  [html] burned into {os.path.basename(video)}")
    return video
