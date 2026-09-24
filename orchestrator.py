"""The brain: Director -> script -> narration -> sources -> beats -> render -> QC.
Plan FIRST, render LAST. Every gate feeds concrete failures back upstream."""
import json
import os
import re
import shutil
import time
from datetime import datetime

import config
import captions
import gfx
import llm
import music
import pacing
import qc
import render
import speaker
import sources
import tts as TTS
import fallback_script
import dialogue
import verify
import vision
import evidence
import research
# Kept only as a last-resort default: tts.words_per_sec() supersedes it with a
# value measured on the machine that is actually speaking (see tts.calibrate).
WORDS_PER_SEC = 2.96


def _best_face_frame(pool, work, tries=6):
    """Find the largest face across the approved clips and export that frame.

    -> (png_path, (x, y, w, h)) or (None, None) when nothing is found (vision
    models missing, no faces in the footage). The box is handed back because
    the portrait circle needs to centre ON the face: a centered crop of a wide
    frame leaves the face small and off to one side."""
    if not pool:
        return None, None
    import cv2
    out = os.path.join(work, "thumb_face.png")
    eng = vision.engine()
    best = (0, None, None, None)     # (face area, video path, frame, box)
    for c in pool[:4]:
        path = c.get("path")
        if not path or not os.path.exists(path):
            continue
        for frame in vision.sample_frames(path, n=tries):
            for f in eng.detect_faces(frame):
                x, y, fw, fh = [float(v) for v in f["box"]]
                if fw * fh > best[0]:
                    best = (fw * fh, path, frame, (x, y, fw, fh))
    if best[2] is None:
        return None, None
    try:
        cv2.imwrite(out, best[2])
        return (out if os.path.exists(out) else None), best[3]
    except Exception:
        return None, None


def _extract_stat(lines):
    """Find the first concrete number in the script for the stat-card overlay.
    -> (big, label) or (None, None)."""
    pat = re.compile(r"(\$?\d[\d,.]*\s?(?:million|billion|trillion|k\b|m\b|%|percent|times|x\b))",
                     re.I)
    for ln in lines:
        m = pat.search(ln.get("text", ""))
        if m:
            big = m.group(1).upper().replace("  ", " ").strip()
            return big[:14], "FROM THE STORY"
    return None, None


# ---------------------------------------------------------------- story memory
class StoryMemory:
    def __init__(self, path=None):
        self.path = path or config.STORY_MEMORY_FILE
        self.data = []
        if os.path.exists(self.path):
            try:
                self.data = json.load(open(self.path, encoding="utf-8"))
            except Exception:
                self.data = []

    def seen(self, topic):
        t = topic.lower().strip()
        return any(e.get("topic", "").lower().strip() == t for e in self.data)

    def add(self, topic, title, mode):
        self.data.append({"topic": topic, "title": title, "mode": mode,
                          "date": datetime.now().isoformat(timespec="seconds")})
        json.dump(self.data, open(self.path, "w", encoding="utf-8"), indent=1)

    def show(self):
        return self.data


# ---------------------------------------------------------------- director
def plan_brief(topic, niche, mode, minutes):
    sys = ("You are the Director of a viral YouTube pipeline in the style of "
           "SunnyV2: punchy hooks, open loops, one clear story arc. "
           "Respond ONLY with JSON.")
    n_sec = max(3, int(minutes * 2))
    usr = (f"Topic: {topic}\nNiche: {niche}\nFormat: {mode} (~{minutes} min).\n"
           f"Return JSON: {{'title': str, 'angle': str, 'hook': str (max 9 words, "
           f"question or bold claim), 'sections': [{{'title': str, "
           f"'intent': 'shock|context|buildup|payoff|breather'}} x {n_sec} entries]}}")
    data = llm.chat_json([{"role": "system", "content": sys},
                          {"role": "user", "content": usr}], temperature=0.9)
    if data and data.get("sections"):
        return data
    # deterministic fallback (no LLM key). n_sec must be an int: minutes is a
    # float for every mode but the short, and "list * 2.5" is a TypeError, so
    # the doc5/doc10/doc15 fallback crashed before it produced a single line.
    n_sec = max(3, int(minutes * 2))
    intents = ["shock", "context"] + ["buildup", "payoff"] * ((n_sec - 2) // 2 + 1)
    # the hook is also burned on screen, so take it from the same generator
    # that writes the narration rather than from a second, weaker template
    return {"title": f"The Untold Story of {topic}",
            "angle": "what everyone missed",
            "hook": fallback_script.build_lines(topic, 40)[0]["text"],
            "sections": [{"title": f"{topic} - part {i + 1}",
                          "intent": intents[i % len(intents)]}
                         for i in range(n_sec)]}


CUE_HELP = """Each line may carry ONE optional "cue" object controlling an
on-screen graphic. Use them sparingly (2-5 per video, never on adjacent lines).
Available cues:
  {"type":"person","name":"...","sub":"role/context"}      <- when someone new is introduced
  {"type":"social","kind":"tweet","name":"..","handle":"@..","body":"..","meta":"date","likes":"4.1M"}
  {"type":"social","kind":"discord","channel":"#x","name":"..","body":"..","time":"Today"}
  {"type":"social","kind":"youtube","title":"..","channel":"..","views":"1.2M views","age":"3 days ago"}
  {"type":"social","kind":"phone","contact":"..","lines":[["them",".."],["me",".."]]}
  {"type":"stat","big":"$4.2M","label":"LOST IN ONE NIGHT"}  <- use a number that is IN the script
  {"type":"timeline","items":[["Aug 8","the stream"],["Aug 22","the castle"]]}
  {"type":"chart","data":[["before",3],["after",41]]}
At least one "stat" and one "social" cue must appear somewhere in the script.
Never invent numbers that are not in the narration. Put a "person" cue on the
line where that person is first named."""


def write_script(brief, mode, minutes, words_target, feedback=None):
    sys = ("You write narration scripts in the style of SunnyV2: short punchy "
           "sentences, present tense, open loops every few lines, no filler, "
           "no stage directions. Respond ONLY with JSON.")
    usr = (
        f"Brief: {json.dumps(brief, ensure_ascii=False)[:1500]}\n"
        f"Target: ~{words_target} spoken words (~{minutes} min).\n"
        "Return JSON: {\"lines\": [{\"text\": str (1-2 sentences, spoken narration only), "
        "\"intent\": \"shock|context|buildup|payoff|breather\", "
        "\"query\": str (see QUERY RULES), "
        "\"cue\": optional object (see below)}]}.\n"
        "Rules: line 1 is the hook (max 9 words).\n\n"
        + evidence.evidence_block(brief) +
        "QUERY RULES - this is a YouTube SEARCH BOX query, not a description "
        "of a shot.\n"
        "  * 2-5 words. The exact words a person types to find this footage.\n"
        "  * Name a real subject from the story - a person, company, place or "
        "event - so the results are actually about this story.\n"
        "  * GOOD: \"mrbeast interview\", \"mrbeast studio tour\", "
        "\"joe rogan mrbeast\", \"mrbeast chocolate factory\", "
        "\"youtube headquarters tour\".\n"
        "  * BAD (these return nothing usable): \"corporate logos displayed "
        "on a stadium billboard\", \"massive pile of cash being loaded onto a "
        "truck\", \"whiteboard with steps: 1. seed, 2. give away money\".\n"
        "  * Never use 'compilation' or 'reaction'. Vary the subjects across "
        "the lines; do not repeat one query.\n\n" + CUE_HELP)
    if feedback:
        usr += f"\nQC FEEDBACK TO FIX: {feedback}"
    data = llm.chat_json([{"role": "system", "content": sys},
                          {"role": "user", "content": usr}],
                         temperature=0.85, max_tokens=4000)
    if data and data.get("lines"):
        return data["lines"]
    # fallback: the deterministic generator in fallback_script.py. The old
    # template narrated its own section titles and repeated six filler lines,
    # which read as a placeholder - see that module for what replaced it.
    return fallback_script.build_lines(brief.get("title", ""), words_target)


# ---------------------------------------------------------------- narration
def build_narration(lines, work_dir):
    """TTS each line, collect word timestamps with accumulated offsets.
    -> (narr_wav, all_words, lines_timed)"""
    mp3s, all_words, lines_timed, offset = [], [], [], 0.0
    voice = config.TTS_VOICE_EN
    for i, ln in enumerate(lines):
        mp3 = os.path.join(work_dir, f"line{i:03d}.mp3")
        tries = 3
        words = None
        while tries and words is None:
            try:
                words = TTS.tts_with_words(ln["text"], mp3, voice=voice)
            except Exception as e:
                print(f"  [tts] retry line {i}: {e}")
                tries -= 1
                time.sleep(2)
        if not words:
            words = [(ln["text"], 0.0, 1.0)]
        dur = TTS.probe_duration(mp3) or (words[-1][2] if words else 1.0)
        mp3s.append(mp3)
        lines_timed.append((ln["text"], offset, offset + dur, ln.get("intent", "buildup")))
        for w, a, b in words:
            all_words.append((w, offset + a, offset + b))
        offset += dur
    narr = os.path.join(work_dir, "narration.wav")
    TTS.concat_audio(mp3s, narr)
    # add outro line timing (TTS'd separately in finalize, appended to words here)
    # mp3s are returned too: in dialogue mode each line is the soundtrack of its
    # own TELL segment, so the per-line files are the actual deliverable audio
    # rather than an intermediate that only existed to be concatenated.
    return narr, all_words, lines_timed, offset, mp3s


def build_outro_audio(work_dir):
    mp3 = os.path.join(work_dir, "outro.mp3")
    try:
        words = TTS.tts_with_words(config.OUTRO_TEXT, mp3)
    except Exception:
        return None, []
    return mp3, words


# ---------------------------------------------------------------- pipeline
def _new_run_dir(mode):
    os.makedirs(config.WORK_DIR, exist_ok=True)
    os.makedirs(config.OUT_DIR, exist_ok=True)
    d = os.path.join(config.WORK_DIR,
                     f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{mode}")
    os.makedirs(d, exist_ok=True)
    return d


def _cleanup_work(work_dir):
    """Delete a finished run's scratch directory.

    A completed render leaves ~80 MB of per-line mp3s, segment mp4s and cue
    PNGs behind that nothing will ever read again, which is how the workspace
    crept towards its size limit. Only called on success - a failed run keeps
    its work dir, because that is exactly the one worth inspecting.
    """
    if config.KEEP_WORK:
        print(f"  [work] kept (KEEP_WORK=True) -> {work_dir}")
        return
    freed = 0
    for root, _dirs, files in os.walk(config.WORK_DIR):
        for f in files:
            try:
                freed += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    # the whole work/ tree, not just this run's folder: a successful render is
    # the proof the pipeline works, so older run folders left behind by killed
    # or failed attempts are only holding the workspace above its size limit
    # (measured: 64 MB of them, which is why work/ never went away)
    shutil.rmtree(work_dir, ignore_errors=True)
    if os.path.isdir(config.WORK_DIR):
        shutil.rmtree(config.WORK_DIR, ignore_errors=True)
    print(f"  [work] cleaned up {freed / 1e6:.0f} MB of scratch files "
          f"(work/ removed)")


def run_autopilot(mode, topic, niche="general", urls=None):
    fmt = config.FORMATS[mode]
    minutes = fmt["duration"] / 60
    # In dialogue mode the footage gets a share of the running time and the
    # narrator gets the rest, so the word budget is derived from the narrator's
    # slice. Planning the script as if it had the whole video to itself (the old
    # behaviour) leaves nothing for the person to say.
    _speech_budget = fmt["duration"] * (1.0 - config.SHOW_SHARE) if config.DIALOGUE_MODE \
        else fmt["duration"]
    words_target = int(_speech_budget * TTS.words_per_sec())
    if config.DIALOGUE_MODE:
        print(f"  [dialogue] narrator gets {_speech_budget:.0f}s of {fmt['duration']}s "
              f"({config.SHOW_SHARE:.0%} reserved for footage) -> {words_target} words")
    work = _new_run_dir(mode)
    clips_dir = os.path.join(work, "clips")
    os.makedirs(clips_dir, exist_ok=True)
    t_start = time.time()
    timers = {}

    def step(name):
        timers[name] = time.time() - t_start
        print(f"  [{name}] @ {timers[name]:.0f}s")

    mem = StoryMemory()
    if mem.seen(topic):
        print("  [memory] topic already produced - continuing anyway (dedup warning)")

    packet = evidence.prepare(topic, work, mode)
    if packet.get("topic") and packet["topic"] != topic:
        topic = packet["topic"]
        print(f"  [evidence] topic is now {topic!r}")

    # Shorts: their voice only. TTS is the documentary. Graphics are HTML.
    if mode == "short" and not config.SHORTS_TTS:
        print("\n== SHORT (no TTS — HTML graphics over their voice) ==")
        import shorts_cut
        final = shorts_cut.build(packet, fmt, work, topic)
        if final:
            evidence.write_sidecar(final, packet)
            print(f"\nDONE [SHORT] -> {final}")
            return final
        print("  [short] no quote footage — falling back to the narrator cut")

    # 1) DIRECTOR -----------------------------------------------------------
    print("\n== DIRECTOR: planning brief ==")
    brief = plan_brief(topic, niche, mode, minutes)
    brief = evidence.apply_brief(brief, packet)
    print(f"  title: {brief.get('title')}\n  hook : {brief.get('hook')}")
    step("brief")

    # 2) SCRIPT with QC retry loop ------------------------------------------
    print("\n== SCRIPT ==")
    lines, feedback = None, None
    for attempt in range(config.MAX_SCRIPT_RETRIES + 1):
        cand = write_script(brief, mode, minutes, words_target, feedback)
        fails = qc.script_qc_text(cand, words_target)
        if not fails:
            lines = cand
            break
        print(f"  [script-qc] retry {attempt + 1}: {fails}")
        feedback = "; ".join(fails)
        lines = cand
    else:
        print(f"  [script-qc] WARN: retries exhausted - accepting last candidate "
              f"despite: {qc.script_qc_text(lines, words_target)}")
    print(f"  {len(lines)} lines, "
          f"{sum(len(l['text'].split()) for l in lines)} words")
    print("\n== FACTCHECK (numbers and names must be in the claims) ==")
    lines, packet["factcheck"] = research.factcheck(lines, packet.get("claims") or [], topic)
    lines = evidence.stamp_lines(lines, packet, topic)
    lines = evidence.trim_to_budget(lines, words_target)
    print(f"  [script] after factcheck+trim: {len(lines)} lines, "
          f"{sum(len(l['text'].split()) for l in lines)} words")
    step("script")

    # 3) NARRATION (TTS + word timestamps) -----------------------------------
    print("\n== NARRATION (edge-tts + word boundaries) ==")
    narr_wav, all_words, lines_timed, narr_dur, line_mp3s = build_narration(lines, work)

    # CLOSED LOOP: the word budget only estimates the length; this measures it.
    # If the read-out is off by more than NARRATION_TOLERANCE, re-speak the same
    # script with the rate adjusted by the ratio. That converts "the script is
    # a bit too long" from a failed duration gate into a slightly faster read,
    # and it keeps working when the voice or the backend changes underneath.
    speech_dur = narr_dur          # outro is added below, so this is speech only
    if config.NARRATION_AUTOFIT and speech_dur > 0:
        # In dialogue mode the narrator only owns part of the video, so the
        # auto-fit has to target the narrator's share. Targeting the full
        # duration re-inflates the narration back to the whole run time and
        # undoes the reservation, leaving nothing for the footage to say.
        want = fmt["duration"] * (1.0 - config.SHOW_SHARE) if config.DIALOGUE_MODE \
            else fmt["duration"]
        off = (speech_dur - want) / want
        if abs(off) > config.NARRATION_TOLERANCE:
            base = float(str(config.TTS_RATE).rstrip("%") or 0)
            # duration scales ~1/speed, so speed up by speech/want
            new_rate = (1 + base / 100.0) * (speech_dur / want) - 1
            new_rate = max(-0.35, min(0.6, new_rate))
            print(f"  [narration] {speech_dur:.1f}s vs {want}s target "
                  f"({off:+.1%}) -> re-speaking at {new_rate:+.0%} "
                  f"(was {config.TTS_RATE})")
            save_rate = config.TTS_RATE
            config.TTS_RATE = f"{new_rate * 100:+.0f}%"
            try:
                narr_wav, all_words, lines_timed, narr_dur, line_mp3s = \
                    build_narration(lines, work)
                speech_dur = narr_dur
                off2 = (speech_dur - want) / want
                print(f"  [narration] corrected to {speech_dur:.1f}s "
                      f"({off2:+.1%} off target)")
            except Exception as e:
                # the assignment above never happened, so narr_wav/all_words/
                # lines_timed/narr_dur still hold take 1 - just restore the rate
                print(f"  [narration] re-speak failed ({type(e).__name__}) - "
                      f"keeping the first take")
                config.TTS_RATE = save_rate

    # Snapshot the body before the outro is appended. The beats have to cover
    # the body only: the outro line's visual is the closing card, which is
    # appended after the last beat. Letting the outro line become a beat as
    # well produced both - the line was spoken over a normal clip and then the
    # card ran after it, so the file was a whole outro too long and the
    # duration gate rejected it (56s against a 51s target).
    body_dur = narr_dur
    body_words = list(all_words)
    body_lines = list(lines_timed)

    outro_mp3, outro_words = build_outro_audio(work)
    # the real length of the spoken outro, not the budgeted constant: the end
    # card, the verify target and the SFX timing all have to agree with it
    outro_dur = config.OUTRO_SECONDS
    if outro_mp3:
        od = TTS.probe_duration(outro_mp3) or \
            (outro_words[-1][2] if outro_words else config.OUTRO_SECONDS)
        outro_dur = od
        TTS.concat_audio([narr_wav, outro_mp3], narr_wav + ".full.wav")
        shutil.move(narr_wav + ".full.wav", narr_wav)
        for w, a, b in outro_words:
            all_words.append((w, narr_dur + a, narr_dur + b))
        lines_timed.append((config.OUTRO_TEXT, narr_dur, narr_dur + od, "outro"))
        narr_dur += od
    print(f"  narration: {narr_dur:.1f}s (target {fmt['duration']}s)")
    # post-TTS gate: validate REAL synthesized timing against the brief
    timing_fails = qc.script_qc(lines_timed, fmt["duration"])
    if timing_fails:
        print(f"  [script-qc-timing] WARN: {timing_fails}")
    step("narration")

    # 4) SOURCES (per-line queries, gated) ------------------------------------
    print("\n== SOURCES (search -> gates -> probe -> dedupe) ==")
    queries, seen = [], set()
    for ln in lines:
        q = (ln.get("query") or topic).strip()
        k = q.lower()
        if k not in seen:
            seen.add(k)
            queries.append(q)
    qcap = config.QUERY_CAPS.get(mode, 14)
    pool = sources.acquire_clip_pool(queries[:qcap], clips_dir, urls=urls,
                                     topic_query=topic)
    pool = evidence.prepend_quote_clips(pool, packet)
    print(f"  pool: {len(pool)} approved clips ({qcap} queries tried)")
    if not pool:
        print("  !! no clips passed the gates - video will use still-zoom fallbacks")
    step("sources")

    # 5) BEATS + EFFECTS -------------------------------------------------------
    print("\n== PACING (beats + decide_effect) ==")
    narr16k = os.path.join(work, "narr16k.wav")
    speaker.ensure_wav(narr_wav, narr16k, sr=16000)
    rms = speaker.wav_rms(narr16k)
    # body_dur/body_words/body_lines: the outro card is the visual for the
    # outro line, so the beats stop where the body stops and the card supplies
    # the final outro_dur seconds. Sum of beats + card = narr_dur.
    beats = pacing.build_beats(body_dur, body_lines, rms, body_words)

    # ---- DIALOGUE PLAN ------------------------------------------------------
    # Replaces what the beats were for: instead of one narration track laid over
    # the whole video, this splits the timeline into SHOW (the person talks,
    # their own audio) and TELL (the narrator explains, the clip muted). The
    # beats are then re-derived from the plan so the cue/SFX machinery keeps
    # working unchanged.
    dialogue_segments = None
    if config.DIALOGUE_MODE:
        print("\n== DIALOGUE PLAN (SHOW the person / TELL the narrator) ==")
        clip_infos = []
        for c in pool:
            info = dialogue.analyze_clip(c["path"], work, asr=not c.get("words"),
                                         model=config.SOURCE_ASR_MODEL)
            clip_infos.append({"path": c["path"], "id": c["id"],
                               "dur": info["dur"] or 8.0, "spans": c.get("spans") or info["spans"],
                               "words": c.get("words") or info["words"],
                               "quote": bool(c.get("quote"))})
        spoken = sum(1 for c in clip_infos if c["spans"])
        print(f"  {spoken}/{len(clip_infos)} clips contain speech; "
              f"{sum(len(c['words']) for c in clip_infos)} transcript words")
        dialogue_segments = dialogue.build_dialogue(
            clip_infos, body_lines, line_mp3s, fmt["duration"], outro_dur,
            work, mode, all_words=body_words, outro_audio=outro_mp3,
            outro_words=outro_words)
        beats = dialogue.beats_from_plan(dialogue_segments)
        print(f"  {sum(1 for s in dialogue_segments if s['kind'] == 'show')} SHOW / "
              f"{sum(1 for s in dialogue_segments if s['kind'] == 'tell')} TELL, "
              f"total {dialogue.total_duration(dialogue_segments):.1f}s")
        for sg in dialogue_segments[:12]:
            print(f"    {sg['kind']:5s} {sg['t']:6.2f} +{sg['dur']:5.2f}  "
                  f"{os.path.basename(sg.get('clip') or 'outro card'):30s} "
                  f"{sg['text'][:40]}")
        if len(dialogue_segments) > 12:
            print(f"    ... {len(dialogue_segments) - 12} more segments")
    qmap = {(ln.get("text") or ""): (ln.get("query") or topic) for ln in lines}

    def assign_all(only_uncovered=False):
        use_count = {}
        used = set()
        for b in beats:
            if b.clip_id:
                used.add(b.clip_id)
                use_count[b.clip_id] = use_count.get(b.clip_id, 0) + 1
        for b in beats:
            if only_uncovered and b.clip_id:
                continue
            c = (sources.assign_clip(b.line, pool, used, use_count)
                 if pool else None)
            if c:
                b.clip_id = c["id"]
                used.add(c["id"])
                use_count[c["id"]] = use_count.get(c["id"], 0) + 1

    if not config.DIALOGUE_MODE:
        assign_all()
    covered = sum(1 for b in beats if b.clip_id) / max(len(beats), 1)

    # timeline gate WITH feedback: coverage too low -> one extra acquisition
    # round with fresh queries generated from the uncovered lines
    if covered < config.MIN_COVERAGE:
        print(f"  [timeline-qc] coverage {covered:.0%} < "
              f"{config.MIN_COVERAGE:.0%} -> extra source round")
        extra, seen2 = [], {q.lower() for q in queries}
        for b in beats:
            if b.clip_id or len(extra) >= 12:
                continue
            q = qmap.get(b.line, b.line[:60] or topic).strip()
            if q.lower() not in seen2:
                seen2.add(q.lower())
                extra.append(q)
        if extra:
            new_pool = sources.acquire_clip_pool(extra, clips_dir,
                                                 topic_query=topic,
                                                 max_downloads=len(extra))
            pool.extend(new_pool)
            print(f"  extra round added {len(new_pool)} clips")
            assign_all(only_uncovered=True)
            covered = sum(1 for b in beats if b.clip_id) / max(len(beats), 1)

    clip_paths = {c["id"]: c["path"] for c in pool}
    tfails = qc.timeline_qc(beats, covered)
    if tfails:
        print(f"  [timeline-qc] remaining issues: {tfails} (non-blocking)")
    print(f"  {len(beats)} beats, coverage {covered:.0%}, "
          f"fx: {sum(1 for b in beats if b.effect)}")
    step("beats")

    # 5b) VISUAL CUES -----------------------------------------------------------
    # This is what turns the gfx library into actual graphics: the Director tags
    # lines, cues.py validates the tags, draws the assets and maps each one onto
    # the beat that starts its line. Without this step gfx is dead code.
    print("\n== VISUAL CUES (Director -> overlays) ==")
    overlays_by_beat = {}
    if config.CUES_ENABLED:
        try:
            import cues
            cue_plan = cues.plan(lines)
            if cue_plan:
                kinds = {}
                for _, c in cue_plan:
                    k = c.get("kind") if c["type"] == "social" else c["type"]
                    kinds[k] = kinds.get(k, 0) + 1
                print("  plan: " + ", ".join(f"{k}x{n}" for k, n in kinds.items()))
                attached, drawn = cues.attach(cue_plan, lines_timed, beats, work)
                atm = cues.atmosphere(beats, fmt, work)
                overlays_by_beat = cues.merge(attached, atm)
                total = sum(len(v) for v in overlays_by_beat.values())
                print(f"  {drawn} assets drawn, {total} overlays placed on "
                      f"{len(overlays_by_beat)} beats")
            else:
                print("  no usable cues from the Director")
        except Exception as e:
            print(f"  [cues] disabled for this run: {e}")
            overlays_by_beat = {}
    else:
        print("  disabled (config.CUES_ENABLED=False)")
    step("cues")

    # 6) RENDER segments --------------------------------------------------------
    print("\n== RENDER (face-tracked crop + effects) ==")
    # pre-plan source windows so crop analysis only covers the frames we use
    plan = []
    if config.DIALOGUE_MODE and dialogue_segments:
        # The window is chosen by the dialogue planner from where the speech
        # actually is, not by arithmetic on the beat time: a SHOW segment has to
        # land on somebody talking or it is just b-roll with a label.
        for i, sg in enumerate(dialogue_segments):
            b = beats[i] if i < len(beats) else None
            if sg["kind"] == "outro":
                plan.append({"b": b, "src": None, "seg": sg})
                continue
            src = sg.get("clip")
            meta = sources.ffprobe_meta(src) if src else None
            plan.append({"b": b, "src": src, "t0": sg["src_in"],
                         "meta": meta or {}, "seg": sg})
    else:
        for b in beats:
            if b.clip_id and b.clip_id in clip_paths:
                src = clip_paths[b.clip_id]
                meta = sources.ffprobe_meta(src)
                src_dur = meta.get("duration") or 10
                span = max(0.1, src_dur - b.dur - 0.2)
                plan.append({"b": b, "src": src,
                             "t0": (b.t * 0.9) % span, "meta": meta})
            else:
                plan.append({"b": b, "src": None})
    windows = {}
    for p in plan:
        if p["src"]:
            lo, hi = windows.get(p["src"], (1e18, -1e18))
            windows[p["src"]] = (min(lo, p["t0"] - 2.0),
                                 max(hi, p["t0"] + p["b"].dur + 2.0))
    crop_cache = {}

    def get_crop(path):
        if path not in crop_cache:
            lo, hi = windows[path]
            crop_cache[path] = vision.compute_crop_path(
                path, t_start=max(0.0, lo), t_end=hi)
        return crop_cache[path]

    # The outro card's text has to exist BEFORE the loop: in dialogue mode the
    # outro is a segment of the plan and is rendered inside the loop, but the
    # .ass file it needs used to be written after it.
    outro_ass = os.path.join(work, "outro.ass")
    with open(outro_ass, "w", encoding="utf-8") as f:
        f.write(captions.static_card_ass(
            [config.OUTRO_CARD_LINE1, config.OUTRO_CARD_LINE2],
            fmt["w"], fmt["h"]))

    seg_paths = []
    still_i = 0

    def still_for(i):
        """Rotating still frames across approved clips (never monotonous)."""
        nonlocal still_i
        png = os.path.join(work, f"still{i:04d}.png")
        evidence_stills = getattr(config, "EVIDENCE_STILLS", None) or []
        if evidence_stills:
            src = evidence_stills[still_i % len(evidence_stills)]
            still_i += 1
            try:
                import shutil as _sh
                _sh.copy2(src, png)
                return png
            except OSError:
                pass
        if pool:
            c = pool[still_i % len(pool)]
            still_i += 1
            try:
                return render.extract_frame(c["path"], 1.5 + (i % 7), png)
            except Exception:
                pass
        subprocess_none(png, fmt)
        return png

    for i, p in enumerate(plan):
        b = p["b"]
        out = os.path.join(work, f"seg{i:04d}.mp4")
        try:
            ovl = overlays_by_beat.get(i) or None
            seg = p.get("seg")
            if seg and seg["kind"] == "outro":
                render.render_outro_card(out, fmt, "outro.ass", cwd=work,
                                         seconds=seg["dur"])
            elif seg and seg["kind"] == "tell" and p["src"]:
                # narrated segment: the clip is silent and a TTS line is the
                # only soundtrack, so there is nothing for the narrator to
                # overlap - the footage's own audio is not in this file at all
                *_, path = get_crop(p["src"])
                cx = vision.cx_at(path, p["t0"] + seg["dur"] / 2)
                render.render_segment(p["src"], p["t0"], seg["dur"], out, fmt,
                                      cx, effect=b.effect, overlays=ovl,
                                      has_audio=False)
            elif seg and seg["kind"] == "tell":
                # no footage for this explanation: a still keeps the video
                # watchable. It must not reach the crop cache with src=None,
                # which is a KeyError, not a graceful fallback.
                render.render_still_zoom(still_for(i), seg["dur"], out, fmt,
                                         overlays=ovl)
            elif p["src"]:
                # exact b.dur - any overshift accumulates into A/V drift
                *_, path = get_crop(p["src"])
                cx = vision.cx_at(path, p["t0"] + b.dur / 2)
                render.render_segment(p["src"], p["t0"], b.dur, out, fmt,
                                      cx, effect=b.effect, overlays=ovl,
                                      has_audio=p["meta"].get("has_audio", False))
            else:
                render.render_still_zoom(still_for(i), b.dur, out, fmt,
                                         overlays=ovl)
        except Exception as e:
            # the fallback needs a duration and a beat does not always exist
            # (the outro segment has none), so derive one before using it
            fb_dur = (b.dur if b else (p.get("seg") or {}).get("dur")) or 2.0
            print(f"  [render] seg {i} failed ({e}) -> still fallback")
            try:
                render.render_still_zoom(still_for(i), fb_dur, out, fmt)
            except Exception as e2:
                print(f"  [render] still fallback failed ({e2}) -> black plate")
                # NEVER skip a beat: exact-length black plate keeps sync
                render.render_black(fb_dur, out, fmt)
        seg_paths.append(out)
        if (i + 1) % 25 == 0:
            print(f"  rendered {i + 1}/{len(beats)} segments")

    # outro card (the .ass was written before the render loop)
    if not config.DIALOGUE_MODE:
        outro_seg = os.path.join(work, "seg_outro.mp4")
        render.render_outro_card(outro_seg, fmt, "outro.ass", cwd=work,
                                 seconds=outro_dur)
        seg_paths.append(outro_seg)
    # in dialogue mode the outro is the last segment of the plan and was
    # rendered as part of the loop above, so it is not appended twice
    step("render")

    # 7) ASSEMBLE -----------------------------------------------------------------
    print("\n== ASSEMBLE (concat + cinema pass + music + mix) ==")
    concat_mp4 = os.path.join(work, "concat.mp4")
    render.concat_segments(seg_paths, concat_mp4, work)

    # kinetic captions with punch-word highlight (red)
    keywords = set(config.PUNCH_WORDS)
    keywords.update(b.punchword for b in beats if b.punchword)
    if dialogue_segments:
        # two non-overlapping tracks: the narrator's words during TELL, the
        # person's words during SHOW. They come from the same segment list the
        # video is cut from, so they cannot describe the wrong speaker.
        tts_words, src_words = dialogue.caption_plan(dialogue_segments)
        ass_content = captions.ass_dialogue(tts_words, src_words, fmt["w"],
                                            fmt["h"], keywords=keywords,
                                            hook_until=config.HOOK_MAX_SEC)
        print(f"  [captions] {len(tts_words)} narrator words + "
              f"{len(src_words)} footage words (separate tracks/styles)")
    else:
        ass_content = captions.ass_kinetic(all_words, fmt["w"], fmt["h"],
                                           keywords=keywords,
                                           hook_until=config.HOOK_MAX_SEC)
    with open(os.path.join(work, "captions.ass"), "w", encoding="utf-8") as f:
        f.write(ass_content)

    # Pillow hook/stat are the fallback. HTML_GFX burns the same cards later
    # so they are not drawn twice on top of the narration captions.
    hook_png, stat_png, stat_t0 = None, None, None
    if config.HTML_GFX:
        print("  [gfx] hook/stat will be the HTML layer, not Pillow")
    else:
        try:
            hook_txt = (brief.get("hook") or brief.get("title") or topic).strip()
            hook_txt = hook_txt.upper().strip(".,!?")[:24]
            if hook_txt:
                hook_png = gfx.glow_title(
                    "hook.png", hook_txt,
                    px=150 if len(hook_txt) <= 12 else 104,
                    glow=(232, 34, 46, 150), outdir=work)
        except Exception as e:
            print(f"  [gfx] hook title skipped: {e}")
        try:
            big, label = _extract_stat(lines)
            if big:
                stat_png = gfx.stat_card("stat.png", big, label, outdir=work)
                cands = [b for b in beats
                         if b.effect in ("zoom_punch", "shake") and b.t > 8.0]
                stat_t0 = (cands[0].t if cands else min(narr_dur * 0.5, narr_dur - 3))
        except Exception as e:
            print(f"  [gfx] stat card skipped: {e}")

    # grade as a single lut3d pass if a .cube is available. The LUT is copied
    # into the work dir so we can pass a RELATIVE name (Windows paths break
    # ffmpeg filter parsing on the colon).
    lut_rel = None
    if config.USE_LUT and os.path.exists(config.LUT_FILE):
        shutil.copy2(config.LUT_FILE, os.path.join(work, "grade.cube"))
        lut_rel = "grade.cube"
    else:
        print("  [lut] no .cube found -> falling back to chained eq grade "
              "(run: python luts.py)")

    subtitled = os.path.join(work, "subtitled.mp4")
    # cinema pass: grade+grain+vignette + hook/stat overlays + kinetic captions
    render.cinema_pass(concat_mp4, "captions.ass", subtitled, fmt, cwd=work,
                       hook_png=hook_png, hook_end=min(2.5, config.HOOK_MAX_SEC + 0.8),
                       stat_png=stat_png, stat_t0=stat_t0, lut_relpath=lut_rel)

    # "mysterious" is the default: a continuous tension drone instead of a
    # kick-and-hat bed. It carries the whole video, which is what holds
    # attention - the beat beds made a mystery story feel like a highlight
    # reel, and the percussion fought the narration for the same space.
    style = config.MUSIC_STYLE or ("climax" if mode == "short" else "rising")
    music_wav = os.path.join(work, "music.wav")
    music.make_music(music_wav, narr_dur + outro_dur + 2, style)

    # optional SFX bed derived from the beats (config.SFX_ENABLED)
    sfx_wav = None
    if config.SFX_ENABLED:
        try:
            import sfx as SFX
            if not os.path.exists(os.path.join(config.SFX_DIR, "hit.wav")):
                SFX.build_all()
            # the outro sting lands where the closing card starts, not at the
            # end of the audio: narr_dur is past the end of the video now that
            # the outro line really is in the narration, so this used to place
            # both sting hits beyond the last frame
            # In dialogue mode the outro card is an explicit segment, so read
            # its real start; otherwise the body duration is where the card
            # begins.
            outro_at = (next((sg["t"] for sg in dialogue_segments
                              if sg["kind"] == "outro"), None)
                        if dialogue_segments else None) or (body_dur + 0.2)
            # snap impacts to the tempo of the bed that is actually playing
            bed_bpm = music.STYLES.get(style, {}).get("bpm", 0)
            events = SFX.hit_events_for(beats, outro_at, bpm=bed_bpm)
            sfx_wav = SFX.build_bed(events, narr_dur + outro_dur,
                                    os.path.join(work, "sfx.wav"))
            n_snap = getattr(SFX.hit_events_for, "snapped", 0)
            n_drop = getattr(SFX.hit_events_for, "dropped", 0)
            # measure the bed that was actually built, not the plan: the mix
            # cannot show this number (speech buries it) and the plan could be
            # wrong about what landed on disk
            n_hit, gap_mean, gap_min = SFX.bed_stats(sfx_wav)
            print(f"  [sfx] {len(events)} impacts planned"
                  f"{f', {n_drop} dropped by the {config.SFX_MIN_GAP:g}s gap' if n_drop else ''}"
                  f"{f', {n_snap} on the beat' if n_snap else ''}")
            print(f"  [sfx] bed measured: {n_hit} impacts"
                  f"{f', one every {gap_mean:.1f}s' if n_hit > 1 else ''}"
                  f"{f', closest {gap_min:.1f}s' if n_hit > 1 else ''}")
        except Exception as e:
            print(f"  [sfx] bed skipped: {e}")
            sfx_wav = None
    else:
        print("  [sfx] disabled (config.SFX_ENABLED=False) - run: python sfx.py")

    if config.DUCK_ENABLED and not dialogue_segments:
        # ducking is what the voiceover mode needs. In dialogue mode the two
        # voices are never on the timeline together, so printing "narration
        # ducks 5:1" there would describe a mechanism that is not in use.
        print(f"  [mix] source audio {config.SOURCE_AUDIO_VOLUME:.2f}, narration "
              f"ducks {config.DUCK_RATIO:g}:1 while the footage speaks")
    music_note = "mysterious drone (plays through the whole video)" \
        if style == "mysterious" else f"{style} beat bed"
    m_atk, m_dur = music.bed_stats(music_wav)
    print(f"  [music] {music_note} at volume {config.MUSIC_VOLUME:.2f} "
          f"({m_dur:.0f}s, {m_atk:.0f} attacks/min - a beat bed measures ~120)")

    final = os.path.join(
        config.OUT_DIR,
        f"{mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_"
        f"{''.join(ch for ch in brief.get('title', topic)[:30] if ch.isalnum() or ch == ' ').strip().replace(' ', '_')}.mp4")
    if dialogue_segments:
        # the dialogue soundtrack is built as its own sample-exact wav (see
        # dialogue.build_track), so the mix gets it as a separate input instead
        # of picking audio up from the video
        track_wav, track_rows = dialogue.build_track(
            dialogue_segments, os.path.join(work, "dialogue.wav"), work)
        silent = [r for r in track_rows if r[3] < 0.005 and r[0] != "outro"]
        print(f"  [mix] dialogue track built: {len(track_rows)} windows, "
              f"{'all carry audio' if not silent else str(len(silent)) + ' silent'}")
        for kind, t, dur, rms in track_rows:
            print(f"    {kind:5s} {t:6.2f} +{dur:5.2f}  rms {rms:.4f}")
        render.mix_dialogue(subtitled, track_wav, music_wav, final,
                            sfx_wav=sfx_wav)
        print("  [mix] dialogue + music bed (the narrator is not a separate "
              "track that could sit on top of anyone)")
    else:
        render.mix_final(subtitled, narr_wav, music_wav, final, sfx_wav=sfx_wav,
                         target=narr_dur)
    step("assemble")

    if dialogue_segments:
        print("\n== DIALOGUE VERIFY (is the right audio in each segment?) ==")
        print("  show windows: the person, not the narrator. tell windows: the narrator, not the person.")
        try:
            dialogue.verify(dialogue_segments, final, work,
                            report=track_rows)
        except Exception as e:
            print(f"  [dialogue-verify] could not run: {type(e).__name__}: {e}")

    # 7b) THUMBNAIL --------------------------------------------------------------
    thumb = None
    if config.THUMBNAIL_ENABLED:
        try:
            os.makedirs(config.THUMBNAIL_DIR, exist_ok=True)
            title = (brief.get("title") or topic).upper()
            words, lines = title.split(), []
            while words and len(lines) < 3:
                ln = ""
                while words and len(ln) + len(words[0]) < 18:
                    ln = (ln + " " + words.pop(0)).strip()
                if ln:
                    lines.append(ln)
            if words:
                lines[-1] += " " + " ".join(words)
            kicker = brief.get("hook") or None

            # cut a real face out of the footage for the thumbnail portrait -
            # the same reason the crop is face-tracked: a person sells a click
            face_png, face_box = None, None
            try:
                face_png, face_box = _best_face_frame(pool, work)
                if face_png:
                    bx, by, bw_, bh_ = face_box
                    print(f"  [thumb] portrait: face {int(bw_)}x{int(bh_)} "
                          f"at ({int(bx)},{int(by)})")
                else:
                    print("  [thumb] no face found - text-only thumbnail")
            except Exception as e:
                print(f"  [thumb] portrait skipped: {e}")

            thumb = gfx.thumbnail(
                os.path.splitext(os.path.basename(final))[0] + ".jpg",
                lines, kicker=kicker, portrait=face_png, focus=face_box,
                outdir=config.THUMBNAIL_DIR)
            print(f"  [thumb] {thumb}")
            print(f"  [thumb] title text: {' / '.join(lines)}")
        except Exception as e:
            print(f"  [thumb] skipped: {e}")
    step("thumbnail")

    # 8) FINAL QC + MANDATE VERIFY GATE -------------------------------------------
    print("\n== FINAL QC ==")
    # compare against the duration this render actually planned for, so an
    # outro that came out 1s longer than the constant is not reported as a
    # script-length failure
    # In dialogue mode the plan is built to the format's length and the outro
    # card is one of its segments, so the card is inside the budget rather than
    # appended after it. Adding outro_dur here made the gate expect 49s and
    # reject a 45.0s video that was exactly the right length.
    target_dur = (fmt["duration"] if dialogue_segments
                  else fmt["duration"] + outro_dur)
    fails = qc.final_qc(final, target_dur)
    if fails:
        print(f"  [final-qc] ISSUES: {fails}")
    else:
        print("  [final-qc] PASS")
    print("\n== MANDATE VERIFY (anti-slideshow gate) ==")
    v_ok, v_fails, v_warns = verify.verify_build(final, target_dur, quiet=False)
    if v_ok:
        print("  [verify] PASS")
        mem.add(topic, brief.get("title", topic), mode)
    else:
        print(f"  [verify] REJECT: {v_fails}")
        print("  -> NOT recorded to story memory; re-run or review manually "
              "(see MANDATE notes in verify output above)")
    step("done")

    # only a verified PASS gets its scratch space deleted; a failed run keeps
    # work/ so the segments, cues and audio can be inspected
    if v_ok and not fails:
        _cleanup_work(work)

    print("\n== TIMING ==")
    for k, v in timers.items():
        print(f"  {k:12s} {v:7.0f}s")
    if config.HTML_GFX and mode != "short":
        print("\n== HTML CARDS (same layer as Shorts) ==")
        try:
            import html_cards
            final = html_cards.burn(final, packet, topic, fmt)
        except Exception as e:
            print(f"  [html] cards skipped: {type(e).__name__}: {e}")
    evidence.write_sidecar(final, packet)
    verdict = "PASS" if (v_ok and not fails) else "REVIEW"
    print(f"\nDONE [{verdict}] -> {final}")
    return final


def subprocess_none(out_png, fmt):
    """Solid dark frame when literally no footage exists."""
    import subprocess as sp
    sp.run([config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i",
            f"color=c=0x141420:s={fmt['w']}x{fmt['h']}",
            "-frames:v", "1", out_png], check=True, capture_output=True)
