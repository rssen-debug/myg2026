#!/usr/bin/env python3
"""Alternating SHOW/TELL timeline: the person talks, then the narrator explains.

The problem this module exists to solve: the pipeline used to lay one continuous
narration track over the whole video and lower the footage underneath it. That
is a voiceover, not a dialogue - the person in the clip is never actually
speaking to the viewer, the narrator just talks through them. Worse, both are
audible at once, which is the one thing that must never happen here.

What it builds instead is an explicit list of non-overlapping time segments:

    SHOW  the person talks, original audio, captions for their words
    TELL  narrator explains what just happened, the clip is muted
    SHOW  next clip, original audio
    TELL  next explanation
    ...   and it ends on the outro card

Because the segments do not overlap in time, "TTS over the person's voice" is
not something that has to be mixed carefully - it cannot happen. The narration
audio only exists inside TELL windows and the source audio only inside SHOW
windows, and each segment is rendered as its own file with its own audio track.

The planner needs two things from the footage that the old pipeline never
looked at: where the speech actually is (speech_spans, an energy VAD) and what
is being said (faster-whisper word timestamps, for the captions). Both are
computed once per clip and cached as JSON next to it.

    python dialogue.py plan  --clips work/clips --target 45 --out plan.json
    python dialogue.py clip  work/clips/c001.mp4     # inspect one clip
"""
import argparse
import json
import os
import subprocess
import sys
import wave

import numpy as np

import config

SR = 16000
WIN = 0.02              # 20 ms analysis frames


# --------------------------------------------------------------- audio read
def _wav_path(clip_path, work_dir):
    w = os.path.join(work_dir, os.path.basename(clip_path) + ".16k.wav")
    if not os.path.exists(w):
        subprocess.run([config.FFMPEG, "-y", "-v", "error", "-i", clip_path,
                        "-vn", "-ar", str(SR), "-ac", "1", w], check=True,
                       capture_output=True)
    return w


def _load_wav(path):
    with wave.open(path, "rb") as f:
        sr = f.getframerate()
        data = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
    return data.astype(np.float32) / 32768.0, sr


def _envelope(x, sr, win=WIN):
    n = x.size // int(sr * win)
    if n == 0:
        return np.zeros(1, dtype=np.float32)
    return np.sqrt((x[:n * int(sr * win)].reshape(n, -1) ** 2).mean(axis=1))


# ------------------------------------------------------------------- VAD
def speech_spans(wav_path, min_run=0.35, close_gap=0.30, pad=0.10):
    """-> [(t0, t1)] where somebody is talking.

    Energy VAD with an adaptive threshold rather than a fixed dB value. The
    floor is taken from the 20th percentile of the envelope (the quiet parts of
    *this* clip) and the level from the 95th, so a clip recorded hot and a clip
    recorded quietly are both segmented sensibly. Short gaps are closed and
    short runs dropped, because a natural pause inside a sentence is not a
    reason to cut away and hand the floor back to the narrator.
    """
    x, sr = _load_wav(wav_path)
    if x.size < sr // 4:
        return []
    env = _envelope(x, sr)
    if env.size < 4:
        return []
    floor = float(np.percentile(env, 20))
    loud = float(np.percentile(env, 95))
    thr = max(floor * 3.0, loud * 0.10)
    if thr <= 0:
        return []
    active = env > thr

    spans, i, n = [], 0, active.size
    while i < n:
        if not active[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and (active[j + 1] or _gap_shorter(active, j + 1,
                                                          close_gap / WIN)):
            j += 1
        spans.append([i * WIN - pad, (j + 1) * WIN + pad])
        i = j + 1

    merged = []
    for a, b in spans:
        if merged and a - merged[-1][1] <= close_gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(max(0.0, a), max(0.0, b)) for a, b in merged
            if b - a >= min_run]


def _gap_shorter(active, idx, want_frames):
    """True if the next run of quiet after idx is shorter than want_frames."""
    k = idx
    while k < active.size and not active[k]:
        k += 1
    return (k - idx) < want_frames


# ---------------------------------------------------------------- windows
def speech_windows(spans, want, clip_dur, min_len=1.6):
    """-> [(start, length)] speech-first windows of ~want seconds.

    One window per speech span, so a long interview clip yields several usable
    windows instead of one. Windows are trimmed to `want` and kept inside the
    clip. A span shorter than min_len produces nothing: half a second of
    somebody mid-sentence is worse than cutting to a different clip.
    """
    out = []
    for a, b in spans:
        if b - a < min_len:
            continue
        length = min(want, b - a, clip_dur - a)
        if length < min_len:
            continue
        start = a + (b - a - length) / 2.0      # centred in the span
        start = max(0.0, min(start, clip_dur - length))
        out.append((round(start, 3), round(length, 3)))
    return out


def quiet_windows(spans, want, clip_dur, min_len=1.6):
    """-> [(start, length)] windows with no speech, for the muted visuals.

    Used for TELL segments so the viewer never watches somebody's mouth move
    in silence while the narrator talks - that reads as a broken file.
    """
    busy = sorted(spans)
    gaps, cursor = [], 0.0
    for a, b in busy:
        if a - cursor > 0:
            gaps.append((cursor, a))
        cursor = max(cursor, b)
    if clip_dur - cursor > 0:
        gaps.append((cursor, clip_dur))
    return [(round(a, 3), round(min(want, b - a, clip_dur - a), 3))
            for a, b in gaps if b - a >= min_len]


# ------------------------------------------------------------------ clip
def analyze_clip(clip_path, work_dir, asr=True, model=None):
    """-> {'dur','spans','words'} cached as JSON beside the clip."""
    cache = os.path.join(work_dir, os.path.basename(clip_path) + ".dialogue.json")
    if os.path.exists(cache):
        try:
            with open(cache, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    info = {"dur": 0.0, "spans": [], "words": []}
    try:
        info["dur"] = float(subprocess.run(
            [config.FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", clip_path],
            capture_output=True, text=True).stdout.strip() or 0.0)
    except Exception:
        return info
    try:
        wav = _wav_path(clip_path, work_dir)
    except Exception as e:
        print(f"  [dialogue] {os.path.basename(clip_path)}: no audio ({e})")
        return info
    info["spans"] = [[round(a, 3), round(b, 3)] for a, b in speech_spans(wav)]
    if asr:
        info["words"] = transcribe_words(wav, model)
    try:
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(info, f)
    except Exception:
        pass
    return info


def transcribe_words(wav_path, model=None):
    """-> [[word, t0, t1]] from the footage's own audio, [] if ASR is absent.

    This is what makes captions-for-the-person possible. It is deliberately
    allowed to fail: a build with no transcripts shows no captions over the
    footage, which is honest, instead of showing the narrator's words over
    somebody else's mouth, which is not.
    """
    try:
        import speaker
        words, _lang = speaker.transcribe(wav_path, model_size=model)
        return [[w, round(a, 3), round(b, 3)] for w, a, b in words]
    except Exception as e:
        print(f"  [dialogue] ASR unavailable ({type(e).__name__}: {e})")
        return []


# ------------------------------------------------------------------ plan
def _seg(kind, t, dur, clip=None, src_in=0.0, line_i=None, audio=None,
         text="", words=None, intent="buildup", clip_id=None):
    return {"kind": kind, "t": round(t, 3), "dur": round(dur, 3),
            "clip": clip, "clip_id": clip_id, "src_in": round(src_in, 3),
            "line_i": line_i, "audio": audio, "text": text,
            "words": words or [], "intent": intent}


def build_dialogue(clips, lines, line_audios, target_dur, outro_dur,
                   work_dir, mode="short", all_words=None,
                   outro_audio=None, outro_words=None):
    """-> [segment] covering the whole video, alternating SHOW and TELL.

    clips: [{'path','id','dur','spans','words'}] - analyzed footage.
    lines: [(text, t0, t1, intent)] - the narration, one TELL per line.
    line_audios: [path] - the per-line TTS mp3, same order as lines.
    all_words: [(word, t0, t1)] with ABSOLUTE narration timings. Split per line
    here rather than by the caller, because the split has to agree with the
    same line boundaries the segments are cut on - a caption track that is
    shifted by even a few hundred ms drifts away from the audio it describes.

    The narration's total length is fixed before this runs (it is already
    spoken), so the footage budget is whatever is left. How long each SHOW
    runs is therefore derived, not chosen: budget / number of shows, clamped,
    and shortened further if the pool cannot supply speech that long.
    """
    tells_total = sum(t1 - t0 for _txt, t0, t1, _in in lines)
    body = max(4.0, target_dur - outro_dur)
    budget = body - tells_total

    # How many SHOW segments fit is decided by the budget and the minimum useful
    # length, NOT by the number of narration lines. One show per line sounds
    # like the obvious design and it is not: with a 45 s target the narration
    # only leaves ~10 s, and a show per line at the 1.8 s floor would run the
    # video to 64 s - a 40% overshoot that the duration gate then rejects.
    max_shows = max(1, int(budget / config.SHOW_MIN_SEC))
    n_shows = max(1, min(len(lines) + 1, max_shows))
    want = min(budget / n_shows, config.SHOW_MAX_SEC)
    want = max(config.SHOW_MIN_SEC, want)
    if budget / max(n_shows, 1) < config.SHOW_MIN_SEC:
        print(f"  [dialogue] only {budget:.1f}s left for footage after "
              f"{tells_total:.1f}s of narration - {n_shows} SHOW segment(s) "
              f"at the {config.SHOW_MIN_SEC}s floor")
    # which gaps between the explanations get a clip: spread evenly
    step = max(1, round((len(lines) + 1) / n_shows))
    show_at = set(range(0, len(lines) + 1, step))
    show_at = set(sorted(show_at)[:n_shows])

    # every usable speech window, best footage first
    pool_windows = []
    for c in clips:
        for start, length in speech_windows(c.get("spans", []), want,
                                            c.get("dur", 0.0),
                                            min_len=config.SHOW_MIN_SEC):
            pool_windows.append((c, start, length))
    quiet_pool = []
    for c in clips:
        for start, length in quiet_windows(c.get("spans", []), want,
                                           c.get("dur", 0.0),
                                           min_len=config.SHOW_MIN_SEC):
            quiet_pool.append((c, start, length))
    # quote windows first: the receipt was chosen before the script, so it
    # should be the SHOW, not whatever loud moment happens to be next
    pool_windows.sort(key=lambda item: (0 if item[0].get("quote") else 1, -item[1]))
    if not pool_windows:
        print("  [dialogue] WARNING: no clip contains a usable speech window")
    else:
        print(f"  [dialogue] {len(pool_windows)} speech windows in "
              f"{len(clips)} clips -> {n_shows} SHOW segments of ~{want:.1f}s")

    segments, t = [], 0.0
    wi = qi = 0
    for i, (text, l0, l1, intent) in enumerate(lines):
        # ---- SHOW: the person, in the gaps the budget allows
        w = pool_windows[wi % len(pool_windows)] if pool_windows else None
        if i not in show_at:
            w = None
        if w:
            wi += 1
            c, start, length = w
            words = _words_in(c.get("words", []), start, length)
            segments.append(_seg("show", t, length, clip=c["path"],
                                 clip_id=c.get("id"), src_in=start,
                                 text=_transcript_text(words), words=words,
                                 intent=intent))
            t += length

        # ---- TELL: the narrator explains, visuals muted
        dur = l1 - l0
        q = quiet_pool[qi % len(quiet_pool)] if quiet_pool else None
        if q:
            qi += 1
            c2, start2, length2 = q
        else:
            c2 = pool_windows[wi % len(pool_windows)][0] if pool_windows else None
            start2, length2 = 0.0, 0.0
        segments.append(_seg("tell", t, dur,
                             clip=(c2 or {}).get("path"),
                             clip_id=(c2 or {}).get("id"),
                             src_in=start2, line_i=i, audio=line_audios[i],
                             text=text,
                             words=_line_words(all_words, l0, l1),
                             intent=intent))
        t += dur

    # closing SHOW after the final explanation, if the budget has room
    w = pool_windows[wi % len(pool_windows)] if pool_windows else None
    if len(lines) not in show_at:
        w = None
    if w:
        c, start, length = w
        words = _words_in(c.get("words", []), start, length)
        segments.append(_seg("show", t, length, clip=c["path"],
                             clip_id=c.get("id"), src_in=start,
                             text=_transcript_text(words), words=words,
                             intent="payoff"))
        t += length

    # Outro: the card, and the spoken CTA that belongs to it. The card has to
    # say it AND the voice has to say it - a silent subscribe card is a missing
    # requirement, which is what this was until the track builder was written,
    # because the outro line lives outside the body lines the plan is built from.
    segments.append(_seg("outro", t, outro_dur, text=config.OUTRO_TEXT,
                         intent="outro", audio=outro_audio,
                         words=_line_words(outro_words, 0.0, max(outro_dur, 0.1))))
    return segments


def _line_words(all_words, l0, l1):
    """Narration words of one line, shifted to that line's local time."""
    if not all_words:
        return []
    return [[w, round(a - l0, 3), round(b - l0, 3)]
            for w, a, b in all_words if l0 <= a < l1]


def _words_in(words, start, length):
    """Transcript words inside a window, shifted to segment-local time."""
    out = []
    for w, a, b in words:
        if a >= start and b <= start + length:
            out.append([w, round(a - start, 3), round(b - start, 3)])
    return out


def _transcript_text(words):
    return " ".join(w for w, _a, _b in words)


def beats_from_plan(segments):
    """-> Beat objects, so the cue/SFX machinery keeps working unchanged.

    The rest of the pipeline thinks in beats: visual cues are attached to the
    beat whose line they belong to, and sound design keys off a beat's effect
    and intent. Rather than rewrite those, the dialogue plan is expressed as
    beats. The mapping is not cosmetic - it decides what the sound design does:
    SHOW segments get a clean cut, so the hundreds of speech onsets in the
    footage cannot trigger impacts, and only some TELL segments get a punch.
    """
    import pacing
    beats = []
    tells = 0
    for sg in segments:
        if sg["kind"] == "outro":
            continue
        fx = None
        if sg["kind"] == "show":
            fx = "fast_cut"
        else:
            tells += 1
            # one punch every third explanation: punctuation, not a metronome
            fx = "zoom_punch" if tells % 3 == 0 else None
        b = pacing.Beat(t=sg["t"], dur=sg["dur"],
                        # narration text only on TELL: a visual cue is attached
                        # by matching the line, and the footage's own words must
                        # never attract the narrator's graphics
                        line=sg["text"] if sg["kind"] == "tell" else "",
                        intent=sg["intent"], effect=fx, punchword="")
        b.clip_id = sg.get("clip_id")
        beats.append(b)
    return beats


# ------------------------------------------------------------------ audio
def build_track(segments, out_wav, work_dir, sr=None):
    """Write the dialogue soundtrack as one sample-exact wav.

    Why this exists instead of letting ffmpeg carry the audio through the
    render: concatenating per-segment AAC streams with -c copy does not
    preserve the timeline. Measured on a real build, the first segment of the
    concatenated file correlated only 0.49 with the segment that was written
    to disk - the audio had slid inside its own segment. Everything downstream
    inherited that: captions appeared slightly early, and the verification
    could not tell a correct build from a broken one.

    Building the track here has a second benefit: the alternation becomes an
    explicit, inspectable artifact. One zeroed array, and each segment's audio
    copied into its own window. The windows cannot overlap because the segments
    cannot - SHOW takes the footage's own audio, TELL takes the narration line,
    and nothing else is written anywhere. That is the whole requirement, in
    eight lines of arithmetic.

    -> (path, [(kind, t, dur, rms)]) so the levels are visible too.
    """
    sr = sr or config.TTS_SAMPLE_RATE
    total = total_duration(segments)
    n = int(round(total * sr))
    track = np.zeros(n, dtype=np.float32)
    report = []
    for s in segments:
        i0 = int(round(s["t"] * sr))
        want = int(round(s["dur"] * sr))
        if s["kind"] == "show" and s.get("clip"):
            x = _decode_audio_at(s["clip"], s["src_in"], s["dur"], sr,
                                 work_dir)
            gain = config.DIALOGUE_SOURCE_GAIN
        elif s.get("audio"):
            # TELL and the outro card: both carry a spoken line
            x = _decode_audio(s["audio"], sr)
            gain = 1.0
        else:
            x = np.zeros(0, dtype=np.float32)
            gain = 1.0
        if x.size:
            take = min(x.size, want)
            track[i0:i0 + take] += x[:take] * gain
        seg_rms = float(np.sqrt((track[i0:i0 + want] ** 2).mean())
                        if want else 0.0)
        report.append((s["kind"], round(s["t"], 2), round(s["dur"], 2),
                       round(seg_rms, 4)))
    # a very short fade at each cut: joins between two unrelated recordings
    # click otherwise, and a click is exactly what the impact detector would
    # later report as a hit
    track = _soften_cuts(track, segments, sr)
    peak = float(np.max(np.abs(track))) or 1.0
    if peak > 0.98:
        track = track / peak * 0.98
    _write_wav(out_wav, track, sr)
    return out_wav, report


def _soften_cuts(track, segments, sr, ms=12):
    """12 ms fade in/out at every segment boundary.

    Joining two unrelated recordings at full level clicks, and a click is
    indistinguishable from an impact to anything measuring the mix later. The
    fade is short enough to be inaudible on speech.
    """
    w = max(1, int(sr * ms / 1000))
    for s in segments:
        if s["kind"] == "outro":
            continue
        i0 = int(round(s["t"] * sr))
        i1 = min(i0 + int(round(s["dur"] * sr)), track.size)
        if i1 - i0 < 2 * w:
            continue
        track[i0:i0 + w] *= np.linspace(0.0, 1.0, w, dtype=np.float32)
        track[i1 - w:i1] *= np.linspace(1.0, 0.0, w, dtype=np.float32)
    return track


def _write_wav(path, x, sr):
    pcm = (np.clip(x, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def _decode_audio_at(clip_path, src_in, dur, sr, work_dir):
    """Decode one window of a clip's own audio."""
    out = subprocess.run(
        [config.FFMPEG, "-v", "error", "-ss", f"{src_in:.3f}", "-t",
         f"{dur:.3f}", "-i", clip_path, "-vn", "-ac", "1", "-ar", str(sr),
         "-f", "f32le", "-"], capture_output=True).stdout
    return np.frombuffer(out, dtype=np.float32)


# ------------------------------------------------------------- captions
def caption_plan(segments):
    """-> (tts_words_abs, source_words_abs) for captions.ass_dialogue().

    Absolute times, so the existing single-file caption burn keeps working.
    The two lists cannot overlap because the segments cannot overlap - that is
    the whole point of building the timeline this way.
    """
    tts, src = [], []
    for s in segments:
        if s["kind"] == "tell":
            for w, a, b in s.get("words") or []:
                tts.append((w, s["t"] + a, s["t"] + b))
        elif s["kind"] == "show":
            for w, a, b in s.get("words") or []:
                src.append((w, s["t"] + a, s["t"] + b))
    return tts, src


def total_duration(segments):
    return sum(s["dur"] for s in segments)


# ------------------------------------------------------------- verification
def verify(segments, video, work_dir, max_check=8, report=None):
    """Prove the alternation landed, by measuring the finished audio.

    For each checked segment the audio of that slice is correlated with what it
    is supposed to contain: a TELL slice against its narration line, a SHOW
    slice against the source clip's own audio near the planned in-point.

    Two things this function got wrong before, both measured on a five minute
    build where it reported FAIL on a soundtrack that was in fact correct:

    * The SHOW reference was taken at exactly the planned src_in. The rendered
      footage is frame quantised (29.97 fps) and the muxed clip can carry an
      audio delay, so a window whose audio is right can sit a frame or two off
      the plan and score 0.46 against the reference while scoring 0.75 against
      the same reference shifted by 50 ms. The reference is now searched over a
      small neighbourhood around the planned in-point.
    * "Narration bleed" was compared against every narration line that merely
      *touched* the window, including the one that starts exactly where the
      window ends. Zero-length overlap meant the whole window was compared
      against the first seconds of the following line, which is unrelated
      speech: it scored 0.61 by coincidence and failed the build. Bleed is now
      only measured where the two really overlap.

    Rows also print the level each window has against the track the build wrote,
    which is what shows audio being added on top of the person, but the pass/
    fail decision is the two gates above: the right person in the window, and
    the narrator absent from it.

    Measured on a correct build: own 0.85-0.94 for narration and 0.61-0.84 for
    footage (footage carries room tone, so it correlates lower), while different
    speech scores 0.17-0.24. Own >= 0.55 and bleed <= 0.55.
    """
    wav = os.path.join(work_dir, "_verify_audio.wav")
    subprocess.run([config.FFMPEG, "-y", "-v", "error", "-i", video, "-vn",
                    "-ar", str(SR), "-ac", "1", wav], check=True,
                   capture_output=True)
    x, sr = _load_wav(wav)

    def slice_of(t, dur):
        i0, i1 = int(t * sr), int(min((t + dur) * sr, x.size))
        return x[i0:i1]

    corr = lambda a, b: _env_corr(a, b, sr)

    # the level each window carries in the track the build wrote, for the level
    # column: t (rounded) -> rms. Not a gate - the music bed lifts quiet
    # windows, so the deviations are read, not thresholded.
    lvl = {}
    for row in (report or []):
        if len(row) >= 4:
            lvl[round(float(row[1]), 2)] = float(row[3])

    # check windows spread over the whole file, not just the opening
    cand = [i for i, s in enumerate(segments) if s["kind"] in ("tell", "show")]
    if len(cand) > max_check:
        step = len(cand) / float(max_check)
        pick = []
        for k in range(max_check):
            i = cand[int(k * step)]
            if i not in pick:
                pick.append(i)
        cand = pick

    rows, ok = [], True
    for i in cand:
        s = segments[i]
        seg = slice_of(s["t"], s["dur"])
        shift = 0.0
        if s["kind"] == "tell":
            ref = _decode_audio(s["audio"])
            own = corr(seg, ref)
            # What could wrongly be here: the person's voice. Comparing a TELL
            # window against another narration line is meaningless - it is the
            # same TTS voice, and unrelated lines of it score 0.44-0.59 by
            # themselves. The mirror of the SHOW test is the useful one: is the
            # footage audio, which belongs to the SHOW windows, present here?
            cross = 0.0
            near = sorted((n for n in segments
                           if n["kind"] == "show" and n.get("clip")),
                          key=lambda n: abs(n["t"] - s["t"]))[:4]
            for n in near:
                # the footage that could leak in is the one on screen around
                # this point, i.e. the neighbouring SHOW windows
                cross = max(cross, corr(seg, slice_of(n["t"], n["dur"])))
        else:
            own, shift = _clip_own(seg, s, sr, work_dir)
            cross = 0.0
            for n in segments:
                if n["kind"] != "tell":
                    continue
                # only where the narration and this window truly overlap; a
                # boundary touch is the intended structure, not a defect
                ov = min(s["t"] + s["dur"], n["t"] + n["dur"]) - max(s["t"], n["t"])
                if ov < 0.25:
                    continue
                o = max(0.0, s["t"] - n["t"])
                cross = max(cross, corr(seg, _decode_audio(n["audio"])[int(o * sr):]))
        dev = None
        r = lvl.get(round(s["t"], 2))
        if r:
            dev = 20.0 * float(np.log10(max(1e-6, _rms_of(seg)) / r))
        rows.append((s["kind"], s["t"], s["dur"], own, cross, shift, dev))
        if own < 0.55 or cross > 0.55:
            ok = False

    devs = [r[6] for r in rows if r[6] is not None]
    mid = float(np.median(devs)) if devs else 0.0
    print(f"  {'seg':5s} {'start':>7s} {'dur':>6s} {'own audio':>10s} "
          f"{'other voice':>12s} {'src shift':>10s} {'level':>8s}")
    for kind, t, dur, own, cross, shift, dev in rows:
        flag = ""
        if own < 0.55:
            flag = "   <-- wrong audio in this segment"
        if cross > 0.55:
            flag = ("   <-- the narrator is in the footage window"
                    if kind == "show" else
                    "   <-- the person's voice is in the narrator window")
        print(f"  {kind:5s} {t:7.2f} {dur:6.2f} {own:10.3f} {cross:12.3f} "
              f"{shift:+9.2f}s "
              + (f"{dev - mid:+7.1f}dB" if dev is not None else f"{'':>9s}") + flag)
    print(f"  verification: {'PASS' if ok else 'FAIL'}"
          "   (own >= 0.55, bleed <= 0.55, narration vs footage; "
          "different speech scores ~0.2)")
    return {"ok": ok, "rows": rows}


def _clip_own(seg, s, sr, work_dir, tol=0.40, step=0.02):
    """Best envelope match for a SHOW window inside its source clip.

    The window is expected at s["src_in"], so the search stays inside a
    neighbourhood of that in-point: this is checking *where in the clip the
    window audio came from*, not hunting for any matching audio in the file.
    -> (correlation, offset found relative to the plan)
    """
    span = s["dur"] + 2 * tol
    start = max(0.0, s["src_in"] - tol)
    x = _decode_audio_at(s["clip"], start, span, sr, work_dir)
    best, best_t = -1.0, 0.0
    n = int(s["dur"] * sr)
    if x.size < n:
        return 0.0, 0.0
    for k in range(0, max(1, int((span - s["dur"]) * sr)), max(1, int(step * sr))):
        c = _env_corr(seg, x[k:k + n], sr)
        if c > best:
            best, best_t = c, k / float(sr) - tol
    return best, best_t


def _speech_band(x, sr, fc=250.0):
    """High-pass at 250 Hz, so the music bed cannot vote on who is speaking.

    Measured on a real build: the built track correlates 0.98 with its narration
    line, the same window in the finished file only 0.48 - the drone bed adds
    uncorrelated energy to the envelope and dilutes the match. Filtering both
    sides to the speech band lifts those same windows to 0.67-0.84 while leaving
    unrelated speech where it was, because a drone has no speech-band content to
    contribute. This is a measurement fix: the audio was correct all along.
    """
    if x.size == 0:
        return x
    try:
        from scipy.signal import butter, filtfilt
        b, a = butter(2, fc / (sr / 2.0), "high")
        return filtfilt(b, a, x.astype(np.float64)).astype(np.float32)
    except Exception:
        # no scipy: a first-order difference removes some of the low end
        y = np.empty_like(x)
        y[0] = x[0]
        y[1:] = x[1:] - x[:-1]
        return y


def _env_corr(a, b, sr):
    """Correlation of two signals' energy envelopes.

    Not a waveform comparison. The finished segment has been through AAC, a
    music bed, a limiter and a loudnorm pass, none of which preserve sample
    values - but they do preserve *when the energy happens*, which is the
    question being asked: is this person's voice in this segment, or the
    narrator's? Envelope correlation answers that and survives the mix.
    """
    a, b = _speech_band(a, sr), _speech_band(b, sr)
    ea, eb = _envelope_norm(a, sr), _envelope_norm(b, sr)
    n = min(ea.size, eb.size)
    if n < 5:
        return 0.0
    ea, eb = ea[:n] - ea[:n].mean(), eb[:n] - eb[:n].mean()
    na, nb = np.linalg.norm(ea), np.linalg.norm(eb)
    return float(np.dot(ea, eb) / (na * nb + 1e-9))


def _rms_of(x):
    return float(np.sqrt((x ** 2).mean())) if x.size else 0.0


def _envelope_norm(x, sr, win=0.01):
    w = max(1, int(sr * win))
    n = x.size // w
    if n == 0:
        return np.zeros(1, dtype=np.float32)
    return np.sqrt((x[:n * w].reshape(n, w) ** 2).mean(axis=1))


def _decode_audio(path, sr=SR):
    out = subprocess.run([config.FFMPEG, "-v", "error", "-i", path, "-vn",
                          "-ar", str(sr), "-ac", "1", "-f", "f32le", "-"],
                         capture_output=True).stdout
    return np.frombuffer(out, dtype=np.float32)


def _clip_slice(clip_path, src_in, dur, work_dir):
    wav = _wav_path(clip_path, work_dir)
    x, sr = _load_wav(wav)
    i0, i1 = int(src_in * sr), int((src_in + dur) * sr)
    return x[i0:i1]


# ------------------------------------------------------------------ CLI
def _main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    pc = sub.add_parser("clip", help="analyze one clip (speech spans + words)")
    pc.add_argument("path")
    pc.add_argument("--work", default="/tmp/dialogue")
    pp = sub.add_parser("plan", help="plan a dialogue from a clip folder")
    pp.add_argument("--clips", required=True)
    pp.add_argument("--target", type=float, default=45.0)
    pp.add_argument("--outro", type=float, default=5.0)
    pp.add_argument("--out", default="")
    a = ap.parse_args()

    if a.cmd == "clip":
        os.makedirs(a.work, exist_ok=True)
        info = analyze_clip(a.path, a.work)
        print(f"  duration {info['dur']:.2f}s")
        print(f"  speech spans ({len(info['spans'])}):")
        for s0, s1 in info["spans"]:
            print(f"    {s0:6.2f} - {s1:6.2f}  ({s1 - s0:.2f}s)")
        print(f"  transcript words: {len(info['words'])}")
        if info["words"]:
            print("   ", " ".join(w for w, _a, _b in info["words"][:14]), "...")
        w = speech_windows(info["spans"], 3.0, info["dur"])
        print(f"  usable ~3s speech windows: {len(w)}")
        return 0

    if a.cmd == "plan":
        os.makedirs("/tmp/dialogue", exist_ok=True)
        clips = []
        for fn in sorted(os.listdir(a.clips)):
            if not fn.endswith((".mp4", ".mkv", ".webm")):
                continue
            p = os.path.join(a.clips, fn)
            info = analyze_clip(p, "/tmp/dialogue")
            clips.append({"path": p, "id": fn, "dur": info["dur"],
                          "spans": info["spans"], "words": info["words"]})
        lines = [("What if none of this was an accident?", 0.0, 3.4, "shock"),
                 ("The footage tells a different story.", 3.4, 7.1, "context"),
                 ("And the numbers agree with the footage.", 7.1, 11.0, "shock")]
        segs = build_dialogue(clips, lines, ["", "", ""], a.target, a.outro,
                              "/tmp/dialogue")
        for s in segs:
            print(f"  {s['kind']:5s} {s['t']:6.2f} +{s['dur']:5.2f}  "
                  f"{os.path.basename(s['clip'] or '-'):28s} "
                  f"in={s['src_in']:5.2f}  {s['text'][:44]}")
        print(f"  total {total_duration(segs):.1f}s")
        if a.out:
            with open(a.out, "w", encoding="utf-8") as f:
                json.dump(segs, f, indent=1)
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(_main())
