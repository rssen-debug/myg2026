#!/usr/bin/env python3
"""e2e_test.py — runs the REAL pipeline end to end without touching the network.

What is mocked:  LLM (offline script with cues), TTS (synthesised speech-like
                 wavs + word timings), source search/download (local clips).
What is REAL:    render.py, cues.py, gfx.py, pacing.py, qc.py, verify.py,
                 the ffmpeg calls, the concat, the mix, the LUT pass.

This is the test that proves the pipeline produces a playable file. Run it
after any change that touches rendering:

    python e2e_test.py            # short format
    python e2e_test.py --doc      # 16:9 document mode
"""
import os
import shutil
import subprocess
import sys
import types
import wave

import numpy as np

import config
import render

OUT = os.environ.get("E2E_DIR", "/tmp/e2e_run")
SR = 44100


# ----------------------------------------------------------------- helpers --
def log(msg):
    print(f"  {msg}")


def _synth_face_png(path, size=420, shift=0):
    """A crude but detectable face: YuNet finds this. Needed because testsrc2
    has no faces, which would leave the face-tracked crop and the thumbnail
    portrait permanently untested."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (size, size), (86, 74, 66))
    dr = ImageDraw.Draw(im)
    cx = size // 2 + shift
    dr.ellipse([cx - 130, 70, cx + 130, size - 60], fill=(232, 190, 158))
    dr.ellipse([cx - 62, 150, cx - 22, 195], fill=(255, 255, 255))
    dr.ellipse([cx + 22, 150, cx + 62, 195], fill=(255, 255, 255))
    dr.ellipse([cx - 48, 162, cx - 34, 180], fill=(38, 30, 26))
    dr.ellipse([cx + 34, 162, cx + 48, 180], fill=(38, 30, 26))
    dr.polygon([(cx, 195), (cx - 14, 232), (cx + 14, 232)], fill=(212, 168, 138))
    dr.arc([cx - 60, 250, cx + 60, 312], 0, 180, fill=(120, 62, 58), width=10)
    im.save(path)
    return path


def make_source_clip(path, seconds=30, w=1280, h=720, seed=1):
    """A moving clip WITH a detectable face, so the face-tracked crop and the
    thumbnail portrait are exercised instead of silently skipped."""
    if os.path.exists(path):
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    face = _synth_face_png(f"{os.path.dirname(path)}/face{seed}.png",
                           shift=(seed - 1) * 40)
    subprocess.run([
        config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=30:duration={seconds}",
        "-loop", "1", "-t", str(seconds), "-i", face,
        "-f", "lavfi", "-i", f"sine=frequency={180 + seed * 60}:duration={seconds}",
        "-filter_complex",
        "[1:v]scale=300:-1[face];[0:v][face]overlay="
        "x='W/2-150+%d+80*sin(t/2)':y=H/2-160[v]" % (seed * 30),
        "-map", "[v]", "-map", "2:a", "-shortest",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
        "-c:a", "aac", path], check=True)
    return path


def fake_tts(text, path, voice=None, rate=None, seconds=None):
    """Write a spoken-length wav with word timings that line up with the audio.
    Returns [(word, start, end)] exactly like edge-tts would."""
    words = text.split()
    if not words:
        words = ["."]
    per_word = 0.40   # ~2.5 words/sec, like real edge-tts
    dur = seconds or max(1.0, len(words) * per_word)
    n = int(SR * dur)
    t = np.arange(n) / SR
    # speech-like: a few formant-ish tones gated at word rate
    gate = (np.sin(2 * np.pi * (len(words) / dur / 2) * t) > -0.25).astype(float)
    y = (np.sin(2 * np.pi * 165 * t) * 0.5
         + np.sin(2 * np.pi * 320 * t) * 0.25
         + np.sin(2 * np.pi * 780 * t) * 0.12) * gate
    y *= 0.25 + 0.75 * np.exp(-((t - dur / 2) ** 2) / (dur / 1.4) ** 2)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(y, -1, 1) * 32000).astype(np.int16).tobytes())
    step = dur / len(words)
    return [(w_, i * step, (i + 1) * step) for i, w_ in enumerate(words)]


# Set by main() from the active format. The mocked writer uses it to emit a
# script of the right LENGTH for the mode - a fixed 13-line stub makes every
# doc run fail the duration gate for reasons that have nothing to do with the
# pipeline being tested.
WORDS_TARGET = 100


def fake_llm_json(messages, schema=None, **kw):
    """Offline Director + writer. Detects which call it is by the prompt."""
    joined = " ".join(str(m.get("content", "")) for m in messages)
    last = str(messages[-1].get("content", ""))
    if "sections" in last and "Brief" not in last:
        return {"title": "The $4.2 Million Mistake",
                "angle": "one decision undid years of work",
                "hook": "He lost everything in one night.",
                "sections": [{"title": f"part {i + 1}",
                              "intent": ["shock", "context", "buildup",
                                         "payoff", "breather"][i % 5]}
                             for i in range(6)]}
    if "lines" in last:
        # a realistic ~110-word short script so the duration/QC gates are
        # exercised at full length instead of failing on a stub
        body = [
            ("He lost everything in one night.", "shock", "interview room lights",
             {"type": "stat", "big": "$4.2M", "label": "GONE IN ONE NIGHT"}),
            ("This is Drake, the biggest rapper alive.", "context", "rapper on stage",
             {"type": "person", "name": "Drake", "sub": "THE BIGGEST RAPPER ALIVE"}),
            ("The internet did not believe a single word of it.", "buildup",
             "phone scrolling social media",
             {"type": "social", "kind": "tweet", "name": "ryan",
              "handle": "@scubaryan_", "body": "he actually did it and I was not ready",
              "meta": "9:12 PM", "likes": "4.1M"}),
            ("It started on August eighth with a livestream nobody watched.",
             "context", "streamer setup room",
             {"type": "timeline", "items": [["Aug 8", "the stream"],
                                            ["Aug 22", "the castle"]]}),
            ("Then the numbers came in and they were brutal.", "payoff",
             "crowd reaction cheering",
             {"type": "chart", "data": [["before", 3], ["after", 41]]}),
            ("Nobody saw the ending coming.", "breather", "city skyline night", None),
            ("And that is what makes this story so strange.", "buildup",
             "quiet street rain", None),
            ("He had spent years building something nobody could touch.",
             "buildup", "office at night", None),
            ("One decision undid almost all of it.", "shock", "empty parking lot",
             None),
            ("The people closest to him said nothing at first.", "context",
             "group of friends talking", None),
            ("Then the receipts started appearing online.", "buildup",
             "laptop screen at night", None),
            ("Every hour brought a worse headline than the last.", "buildup",
             "newspaper printing press", None),
            ("By morning the whole thing was over.", "payoff",
             "sunrise over city", None),
        ]
        # scale to the mode: repeat the beats (dropping their cues after the
        # first pass so an hour-long test does not draw hundreds of graphics)
        # until the word target is met
        out, total, seen = [], 0, set()
        scalers = ["the story behind it", "what happened next", "the fallout",
                   "who knew about it", "the money trail", "the silence"]
        round_i = 0
        while total < WORDS_TARGET:
            for t, i, q, c in body:
                if total >= WORDS_TARGET:
                    break
                if round_i:
                    t = f"{t} {scalers[(round_i - 1) % len(scalers)]}"
                    c = None if (t, i) in seen else c
                    seen.add((t, i))
                else:
                    c = c if t not in seen else None
                    seen.add((t, None))
                out.append({"text": t, "intent": i, "query": q, "cue": c})
                total += len(t.split())
            round_i += 1
        # stop on the word target instead of overshooting by a whole beat: at a
        # reduced test duration one extra line is ~6% of the video, which would
        # fail the duration gate for reasons the pipeline has no control over
        while len(out) > 3 and total - len(out[-1]["text"].split()) >= WORDS_TARGET - 3:
            total -= len(out[-1]["text"].split())
            out.pop()
        return {"lines": out}
    # generic JSON probe (llm.self_test)
    return {"status": "ok", "n": 3}


def fake_clip_for(line_text, pool, used_ids, use_count=None):
    """Stand-in for assign_clip: returns a clip DICT (like the real one),
    round-robining the local pool and respecting the reuse penalty."""
    use_count = use_count or {}
    if not pool:
        return None
    best = min(pool, key=lambda c: (use_count.get(c["id"], 0), c["id"]))
    return best


# -------------------------------------------------------------------- run ---
def main():
    global WORDS_TARGET
    mode = "doc5" if "--doc" in sys.argv else "short"
    # --minutes N renders the doc path at a reduced duration. The 16:9 code
    # path, the pacing and the duration gate are identical; only the number of
    # beats changes, which makes a full-length doc run testable in minutes
    # instead of ~20.
    if "--minutes" in sys.argv:
        m = float(sys.argv[sys.argv.index("--minutes") + 1])
        config.FORMATS[mode]["duration"] = int(m * 60)
    fmt = config.FORMATS[mode]
    WORDS_TARGET = int(fmt["duration"] * 2.4)
    print("=" * 62)
    print(f"  E2E TEST  mode={mode}  {fmt['w']}x{fmt['h']}  "
          f"target={fmt['duration']}s  words={WORDS_TARGET}")
    print("=" * 62)

    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT, exist_ok=True)

    print("\n[1] building stand-in source clips")
    clips = []
    for i in range(3):
        p = make_source_clip(f"{OUT}/clips/clip{i}.mp4", seconds=40, seed=i)
        clips.append({"id": f"c{i}", "path": p, "score": 2.0, "title": "test clip",
                      "meta": {"face_ratio": 0.6, "has_audio": True}})
        log(f"{os.path.basename(p)} ({os.path.getsize(p)//1024} KB)")

    config.SFX_ENABLED = True   # exercise the full audio path
    print("\n[2] patching network-facing pieces")
    import llm as LLM
    import orchestrator as O
    import tts as TTS
    LLM.chat_json = fake_llm_json
    LLM.have_key = lambda: True
    LLM.chat = lambda *a, **k: "LLM OK"
    TTS.tts_with_words = fake_tts
    O.plan_brief = lambda topic, niche, mode, minutes: fake_llm_json(
        [{"role": "user", "content": "sections"}])
    O.write_script = lambda brief, mode, minutes, wt, feedback=None: \
        fake_llm_json([{"role": "user", "content": "lines"}])["lines"]

    def fake_acquire(topic, query, work_dir, used_ids, **kw):
        # hand the real pipeline real files
        for c in clips:
            if c["id"] not in used_ids:
                return c
        return None
    O.sources.acquire_clip_pool = lambda *a, **k: list(clips)
    O.sources.assign_clip = fake_clip_for
    O.sources.search_videos = lambda *a, **k: []   # real name; blocks all network
    log("LLM, TTS and source acquisition mocked; render is REAL")

    print("\n[3] running run_autopilot\n")
    final = O.run_autopilot(mode, "The $4.2 Million Mistake", niche="drama")

    print("\n[4] verifying output")
    if not final or not os.path.exists(final):
        print("  FAILED: no output file")
        return 1
    size = os.path.getsize(final) // 1024
    dur = render.probe_duration(final)
    log(f"file: {final}")
    log(f"{size} KB, {dur:.1f}s")

    import verify
    ok, fails, warns = verify.verify_build(final, fmt["duration"] +
                                           config.OUTRO_SECONDS, quiet=False)
    print()
    print("=" * 62)
    print(f"  E2E RESULT: {'PASS' if ok else 'REJECT'}")
    if fails:
        for f in fails:
            print(f"    {f}")
    print("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
