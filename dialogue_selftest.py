#!/usr/bin/env python3
"""Self-test for the SHOW/TELL dialogue structure. No network, no LLM.

Builds a complete alternating video from a local file treated as footage, and
then measures the result instead of describing it:

  1. analyze the footage (speech spans + transcript)
  2. plan SHOW/TELL segments around real narration lines
  3. render every segment with its own audio (footage audio vs. TTS line)
  4. concat, add the music bed, verify
  5. assert the properties the structure exists for:
       - SHOW segments contain the footage's audio and NOT the narration
       - TELL segments contain the narration and NOT the footage's audio
       - the two caption tracks never overlap in time
       - the video's audio covers the whole video

Point it at any video with speech in it:

    python dialogue_selftest.py footage.mp4
"""
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

import captions
import config
import dialogue
import music
import render
import tts

LINES = [
    "It started as a joke between three friends.",
    "Nineteen months later, a hundred million people were watching.",
    "Then, in one week, it was gone.",
]


def _speak(lines, work):
    """-> ([(text,t0,t1,intent)], [mp3], [(word,t0,t1)], total) real TTS path."""
    audios, timed, all_words, offset = [], [], [], 0.0
    for i, text in enumerate(lines):
        mp3 = os.path.join(work, f"line{i:03d}.mp3")
        words = tts.tts_with_words(text, mp3)
        dur = tts.probe_duration(mp3) or 2.0
        timed.append((text, offset, offset + dur, "context"))
        for w, a, b in words:
            all_words.append((w, offset + a, offset + b))
        offset += dur
        audios.append(mp3)
    return timed, audios, all_words, offset


def _dur(path):
    return float(subprocess.run(
        [config.FFPROBE, "-v", "error", "-show_entries", "stream=duration",
         "-select_streams", "a:0", "-of", "csv=p=0", path],
        capture_output=True, text=True).stdout.strip().split(",")[0] or 0.0)


def main(argv):
    if len(argv) != 1 or not os.path.exists(argv[0]):
        print(__doc__)
        return 2
    footage = argv[0]
    work = tempfile.mkdtemp(prefix="dlgtest_")
    print(f"  footage: {footage}")
    print(f"  work:    {work}")

    info = dialogue.analyze_clip(footage, work, asr=True,
                                model=config.SOURCE_ASR_MODEL)
    print(f"  analysis: {info['dur']:.1f}s, {len(info['spans'])} speech spans, "
          f"{len(info['words'])} transcript words")
    assert info["spans"], "no speech found in the footage"
    assert info["words"], "no transcript produced"

    lines, audios, all_words, tts_dur = _speak(LINES, work)
    print(f"  narration: {len(lines)} lines, {tts_dur:.1f}s total")

    clips = [{"path": footage, "id": "test", "dur": info["dur"],
              "spans": info["spans"], "words": info["words"]}]
    outro_mp3 = os.path.join(work, "outro.mp3")
    outro_words = tts.tts_with_words(config.OUTRO_TEXT, outro_mp3)
    segs = dialogue.build_dialogue(clips, lines, audios, target_dur=30.0,
                                   outro_dur=tts.probe_duration(outro_mp3) or 5.0,
                                   work_dir=work, mode="short",
                                   all_words=all_words, outro_audio=outro_mp3,
                                   outro_words=outro_words)
    print(f"  plan: {sum(1 for s in segs if s['kind'] == 'show')} SHOW, "
          f"{sum(1 for s in segs if s['kind'] == 'tell')} TELL, "
          f"{dialogue.total_duration(segs):.1f}s total")
    for s in segs:
        print(f"    {s['kind']:5s} {s['t']:6.2f} +{s['dur']:5.2f}  "
              f"{s['text'][:46]}")

    # ---- render every segment with its own audio -------------------------
    fmt = config.FORMATS["short"]
    seg_files = []
    for i, s in enumerate(segs):
        out = os.path.join(work, f"seg{i:03d}.mp4")
        if s["kind"] == "outro":
            ass = os.path.join(work, "outro.ass")
            with open(ass, "w", encoding="utf-8") as f:
                f.write(captions.static_card_ass(
                    [config.OUTRO_CARD_LINE1, config.OUTRO_CARD_LINE2],
                    fmt["w"], fmt["h"]))
            render.render_outro_card(out, fmt, "outro.ass", cwd=work,
                                     seconds=s["dur"])
        elif s["kind"] == "tell":
            render.render_segment(s["clip"], s["src_in"], s["dur"], out, fmt,
                                  fmt["w"] / 2, effect=None, has_audio=False)
        else:
            render.render_segment(s["clip"], s["src_in"], s["dur"], out, fmt,
                                  fmt["w"] / 2, effect="fast_cut",
                                  has_audio=False)
        seg_files.append(out)
        print(f"  rendered {i + 1}/{len(segs)} ({s['kind']})")

    concat = os.path.join(work, "concat.mp4")
    render.concat_segments(seg_files, concat, work)
    track_wav, track_report = dialogue.build_track(
        segs, os.path.join(work, "dialogue.wav"), work)
    print("  dialogue track (one row per segment):")
    for kind, t, dur, rms in track_report:
        print(f"    {kind:5s} {t:6.2f} +{dur:5.2f}  rms {rms:.4f}")
    music_wav = os.path.join(work, "music.wav")
    music.make_music(music_wav, dialogue.total_duration(segs) + 2,
                     config.MUSIC_STYLE)
    final = os.path.join(work, "final.mp4")
    render.mix_dialogue(concat, track_wav, music_wav, final)

    # ---- measure ---------------------------------------------------------
    print("\n  == dialogue verification ==")
    report = dialogue.verify(segs, final, work)

    print("  == structural checks ==")
    fails = []
    v_dur = render.probe_duration(final)
    a_dur = _dur(final)
    print(f"    video {v_dur:.2f}s / audio {a_dur:.2f}s")
    if a_dur < v_dur - 0.35:
        fails.append(f"audio ends {v_dur - a_dur:.2f}s before the video")

    tts_w, src_w = dialogue.caption_plan(segs)
    print(f"    caption tracks: {len(tts_w)} narrator words, "
          f"{len(src_w)} footage words")
    both = [w for w in tts_w for v in src_w
            if not (w[2] <= v[1] or v[2] <= w[1])]
    if both:
        fails.append(f"{len(both)} caption overlaps between the two speakers")
    else:
        print("    the two caption tracks never overlap")
    if not src_w:
        fails.append("the footage track has no captions")

    if not report["ok"]:
        fails.append("segment audio verification failed")
    for f in fails:
        print(f"    FAIL: {f}")
    print(f"\n  SELF-TEST: {'PASS' if not fails else 'FAIL'}  ({final})")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
