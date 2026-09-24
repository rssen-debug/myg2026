"""Segment renderer: face-tracked crop, per-beat effects, outro card,
concat + final audio mix. One ffmpeg pass per segment (robust, debuggable)."""
import json
import os
import subprocess

import config
import captions
import vision
from cinema import GRADE, GRAIN, VIGNETTE
from sources import ffprobe_meta
from tts import probe_duration

EVEN = lambda v: max(2, int(v) & ~1)


def _run(cmd):
    return _run_cwd(cmd, cwd=None)


def _run_cwd(cmd, cwd=None):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        tail = "\n".join(r.stderr.splitlines()[-6:])
        raise RuntimeError(f"ffmpeg failed: {' '.join(cmd[:6])} ...\n{tail}")
    return r


def _overlay_filters(overlays, ow, oh, dur):
    """Build the filter_complex tail for a list of overlays.

    overlays: [{"png", "scale", "pos", "t_in", "t_out"}]
      scale = fraction of the output width (1.0 = full frame)
      pos   = 'center' | 'lower_safe' | 'fill'

    Returns (input_args, fragment, n_inputs_added). Each overlay is looped for
    the segment duration so its timeline matches the base video, which lets the
    fade timings line up without any PTS gymnastics.
    """
    args, frags = [], []
    added = 0
    for ov in overlays:
        png = ov.get("png")
        if not png or not os.path.exists(png):
            continue
        args += ["-loop", "1", "-t", f"{dur:.3f}", "-i", os.path.abspath(png)]
        added += 1
        idx = added                     # input 0 is the source video
        t_in = float(ov.get("t_in", 0.0))
        t_out = float(ov.get("t_out", dur))
        t_out = max(t_in + 0.2, min(t_out, dur))
        fade = min(0.25, (t_out - t_in) / 2.5)
        frags.append((idx, ov, t_in, t_out, fade))
    return args, frags


def _pos_expr(pos, ow, oh):
    """x,y for the overlay filter. Keeps graphics out of the caption zone."""
    if pos == "lower_safe":
        return "(W-w)/2", f"H*0.66"
    if pos == "fill":
        return "0", "0"
    return "(W-w)/2", "(H-h)/2"


def render_segment(src, t0, dur, out, fmt, cx, effect=None,
                   punch_z=1.18, has_audio=True, overlays=None,
                   audio_src=None, audio_gain=None):
    """Render one beat segment with face-centered crop + effect + overlays.
    short: crop to 9:16 window tracking cx. doc: punch-in zoom toward face.
    overlays: optional list of GFX assets to composite in this same pass, so
    adding a lower third does not cost an extra re-encode of the segment.

    audio_src: an audio file to use as this segment's soundtrack INSTEAD of the
    source clip's own audio - how a TELL segment carries the narration line it
    belongs to. The clip is then silent, which is the point: the narrator is
    talking and the footage must not be. When None, has_audio decides between
    the clip's own audio (SHOW) and silence.
    audio_gain: multiply the source clip's audio by this. SHOW segments use
    config.DIALOGUE_SOURCE_GAIN instead of the ducking-era
    config.SOURCE_AUDIO_VOLUME, because nothing is competing with them.
    """
    meta = ffprobe_meta(src)
    W, H = meta["width"], meta["height"]
    if not W or not H:
        raise RuntimeError(f"cannot probe {src}")
    t0 = max(0.0, min(t0, max(0.0, meta["duration"] - dur - 0.05)))
    ow, oh = fmt["w"], fmt["h"]
    fx_extra = ""

    if fmt["aspect"] == "9:16":
        bw = EVEN(H * 9 / 16)
        x0 = int(min(max(cx - bw / 2, 0), max(0, W - bw)))
        if effect == "zoom_punch":
            zw, zh = EVEN(bw / punch_z), EVEN(H / punch_z)
            zx = int(min(max(cx - zw / 2, 0), max(0, W - zw)))
            zy = int((H - zh) / 2)
            crop = f"crop={zw}:{zh}:x={EVEN(zx)}:y={EVEN(zy)}"
        elif effect == "shake":
            crop = (f"crop={bw}:{H}:x='{x0}+10*sin(2*PI*t*12)':y=0")
        else:
            crop = f"crop={bw}:{H}:x={x0}:y=0"
    else:  # 16:9 punch-in
        z = punch_z if effect == "zoom_punch" else 1.08
        cw, ch = EVEN(W / z), EVEN(H / z)
        x = int(min(max(cx - cw / 2, 0), max(0, W - cw)))
        y = int((H - ch) / 2)
        crop = f"crop={cw}:{ch}:x={EVEN(x)}:y={EVEN(y)}"

    if effect == "flash":
        fx_extra = ",eq=brightness='if(lt(t,0.22),0.35*(1-t/0.22),0)'"
    elif effect == "glitch":
        fx_extra = ",hue=h='if(lt(t,0.25),120*sin(t*70),0)'"
    elif effect == "fast_cut":
        fx_extra = ",fade=t=in:st=0:d=0.10"
    elif effect == "slide":
        fx_extra = ",fade=t=in:st=0:d=0.18"
    elif effect == "fade_slide":
        fx_extra = ",fade=t=in:st=0:d=0.25"

    base_chain = (f"{crop},scale={ow}:{oh}:flags=bicubic,"
                  f"setsar=1,fps={config.OUTPUT_FPS}")

    ov_args, ov_frags = _overlay_filters(overlays or [], ow, oh, dur)

    cmd = [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{t0:.3f}", "-t", f"{dur:.3f}", "-i", src]
    cmd += ov_args

    # ALL inputs must be declared before ANY output option (-map, -vf, ...).
    # Interleaving them makes ffmpeg read -map as an *input* option and abort:
    # "Option map cannot be applied to input url anullsrc". This path is only
    # hit when a source clip has no audio track.
    # Inputs are numbered in the order they are declared, so declare them all
    # here, before the first -vf/-map: 0 = clip, then overlays, then the
    # narration line (TELL segments), then a silence generator if the clip has
    # neither. Declaring the narration after -map made ffmpeg read -map as an
    # input option and abort.
    n_inputs = 1 + len(ov_frags)
    audio_idx = None
    if audio_src:
        audio_idx = n_inputs
        cmd += ["-i", audio_src]
        n_inputs += 1
    elif not has_audio:
        audio_idx = n_inputs
        cmd += ["-f", "lavfi", "-t", f"{dur:.3f}",
                "-i", f"anullsrc=r={config.TTS_SAMPLE_RATE}:cl=stereo"]
        n_inputs += 1

    if ov_frags:
        parts = [f"[0:v]{base_chain}[v0]"]
        prev = "v0"
        for n, (idx, ov, t_in, t_out, fade) in enumerate(ov_frags, start=1):
            frac = float(ov.get("scale", 0.5))
            pos = ov.get("pos", "center")
            if pos == "fill":
                sc = f"scale={ow}:{oh}:force_original_aspect_ratio=disable"
            else:
                sc = f"scale={int(ow * frac)}:-1"
            parts.append(
                f"[{idx}:v]{sc},format=rgba,"
                f"fade=t=in:st={t_in:.2f}:d={fade:.2f}:alpha=1,"
                f"fade=t=out:st={max(t_in, t_out - fade):.2f}:d={fade:.2f}:alpha=1"
                f"[o{n}]")
            x, y = _pos_expr(pos, ow, oh)
            parts.append(
                f"[{prev}][o{n}]overlay=x={x}:y={y}:"
                f"enable='between(t,{t_in:.2f},{t_out:.2f})':"
                f"eof_action=pass[v{n}]")
            prev = f"v{n}"
        parts.append(f"[{prev}]format=yuv420p{fx_extra}[vout]")
        cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]"]
    else:
        cmd += ["-vf", f"{base_chain},format=yuv420p{fx_extra}", "-map", "0:v:0"]

    if audio_src:
        # apad to the exact segment length: a short narration line must not
        # shorten the segment, and -shortest would otherwise clip the video to
        # the length of the audio
        cmd += ["-map", f"{audio_idx}:a:0",
                "-af", f"apad,atrim=0:{dur:.3f},asetpts=N/SR/TB"]
    elif has_audio:
        gain = config.DIALOGUE_SOURCE_GAIN if audio_gain is None else audio_gain
        cmd += ["-map", "0:a:0?", "-af", f"volume={gain}"]
    else:
        cmd += ["-map", f"{audio_idx}:a:0"]
    cmd += ["-c:v", "libx264", "-preset", config.ENCODE_PRESET,
            "-crf", str(config.ENCODE_CRF), "-r", str(config.OUTPUT_FPS),
            "-c:a", "aac", "-ar", str(config.TTS_SAMPLE_RATE), "-ac", "2",
            "-b:a", "128k", "-shortest", out]
    _run(cmd)
    return out


def render_still_zoom(frame_png, dur, out, fmt, overlays=None):
    """Ken Burns fallback when no clip covers a beat. Accepts overlays so a
    lower third on a beat is not lost just because the clip failed."""
    ow, oh = fmt["w"], fmt["h"]
    n = max(2, int(dur * config.OUTPUT_FPS))
    base = (f"scale={ow * 2}:{oh * 2}:force_original_aspect_ratio=increase,"
            f"crop={ow * 2}:{oh * 2},"
            f"zoompan=z='1.0+0.15*on/{n}':x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':d={n}:s={ow}x{oh}:fps={config.OUTPUT_FPS}")

    ov_args, ov_frags = _overlay_filters(overlays or [], ow, oh, dur)
    cmd = [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-loop", "1", "-t", f"{dur:.3f}", "-i", frame_png]
    cmd += ov_args
    cmd += ["-f", "lavfi", "-t", f"{dur:.3f}",
            "-i", f"anullsrc=r={config.TTS_SAMPLE_RATE}:cl=stereo"]
    silent_idx = 1 + len(ov_frags)

    if ov_frags:
        parts = [f"[0:v]{base}[v0]"]
        prev = "v0"
        for k, (idx, ov, t_in, t_out, fade) in enumerate(ov_frags, start=1):
            frac = float(ov.get("scale", 0.5))
            pos = ov.get("pos", "center")
            sc = (f"scale={ow}:{oh}:force_original_aspect_ratio=disable"
                  if pos == "fill" else f"scale={int(ow * frac)}:-1")
            parts.append(
                f"[{idx}:v]{sc},format=rgba,"
                f"fade=t=in:st={t_in:.2f}:d={fade:.2f}:alpha=1,"
                f"fade=t=out:st={max(t_in, t_out - fade):.2f}:d={fade:.2f}:alpha=1"
                f"[o{k}]")
            x, y = _pos_expr(pos, ow, oh)
            parts.append(f"[{prev}][o{k}]overlay=x={x}:y={y}:"
                         f"enable='between(t,{t_in:.2f},{t_out:.2f})':"
                         f"eof_action=pass[v{k}]")
            prev = f"v{k}"
        parts.append(f"[{prev}]format=yuv420p[vout]")
        cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]"]
    else:
        cmd += ["-vf", f"{base},format=yuv420p", "-map", "0:v:0"]

    cmd += ["-map", f"{silent_idx}:a:0",
            "-c:v", "libx264", "-preset", config.ENCODE_PRESET,
            "-crf", str(config.ENCODE_CRF), "-r", str(config.OUTPUT_FPS),
            "-c:a", "aac", "-ar", str(config.TTS_SAMPLE_RATE), "-ac", "2", out]
    _run(cmd)
    return out


def render_black(dur, out, fmt):
    """Last-resort filler: black plate of EXACTLY dur seconds. Never skip a beat."""
    ow, oh = fmt["w"], fmt["h"]
    cmd = [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-t", f"{dur:.3f}",
           "-i", f"color=c=black:s={ow}x{oh}:r={config.OUTPUT_FPS}",
           "-f", "lavfi", "-t", f"{dur:.3f}",
           "-i", f"anullsrc=r={config.TTS_SAMPLE_RATE}:cl=stereo",
           "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "libx264", "-preset", config.ENCODE_PRESET,
           "-crf", str(config.ENCODE_CRF), "-r", str(config.OUTPUT_FPS),
           "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-ar", str(config.TTS_SAMPLE_RATE), "-ac", "2", out]
    _run(cmd)
    return out


def render_outro_card(out, fmt, ass_path, cwd=None, seconds=None):
    """Outro end card. seconds defaults to config.OUTRO_SECONDS but the caller
    passes the MEASURED length of the spoken outro line: TTS never produces
    exactly the budgeted length, and a card shorter than the audio truncates
    the required "subscribe and like" closer mid-sentence.
    """
    secs = config.OUTRO_SECONDS if seconds is None else max(1.0, seconds)
    ow, oh = fmt["w"], fmt["h"]
    cmd = [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-t", f"{secs:.2f}",
           "-i", f"color=c=0x0a0a12:s={ow}x{oh}:r={config.OUTPUT_FPS}",
           "-f", "lavfi", "-t", f"{secs:.2f}",
           "-i", f"anullsrc=r={config.TTS_SAMPLE_RATE}:cl=stereo",
           "-vf", f"ass={ass_path},format=yuv420p",
           "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "libx264", "-preset", config.ENCODE_PRESET,
            "-crf", str(config.ENCODE_CRF), "-r", str(config.OUTPUT_FPS),
           "-c:a", "aac", "-ar", str(config.TTS_SAMPLE_RATE), "-ac", "2",
           os.path.abspath(out)]
    _run_cwd(cmd, cwd=cwd)
    return out


def concat_segments(seg_paths, out, work_dir):
    lst = os.path.join(work_dir, "concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in seg_paths:
            f.write("file '" + os.path.abspath(p).replace("\\", "/") + "'\n")
    _run([config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
          "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])
    os.remove(lst)
    return out


def cinema_pass(video, ass_relpath, out, fmt, cwd=None,
                hook_png=None, hook_end=2.5,
                stat_png=None, stat_t0=None, stat_dur=1.6,
                lut_relpath=None):
    """Final studio pass: grade + grain + vignette, optional hook glow-title
    overlay (first hook_end seconds) and optional centered stat-card pop, then
    kinetic captions. ass_relpath is RELATIVE; cwd holds it.

    If lut_relpath is given (a .cube filename relative to cwd) the grade is
    applied as a single lut3d pass instead of the chained eq/colorbalance
    filters - same job, one filter, and the same file also works in DaVinci
    or Lumetri. Falls back to GRADE if it is missing.
    Overlays are optional - omit either to skip gracefully."""
    W, H = fmt["w"], fmt["h"]
    dur = probe_duration(video) or 60.0
    inputs = ["-i", os.path.abspath(video)]
    if lut_relpath and cwd and os.path.exists(os.path.join(cwd, lut_relpath)):
        grade = f"lut3d=file={lut_relpath}"
    else:
        grade = GRADE
    fc = f"[0:v]{grade},{GRAIN},{VIGNETTE}[g]"
    prev, idx = "g", 1

    if hook_png and os.path.exists(hook_png):
        inputs += ["-loop", "1", "-t", f"{hook_end:.2f}",
                   "-i", os.path.abspath(hook_png)]
        hw = int(W * 0.74)
        hy = int(H * 0.16)
        fc += (f";[{idx}:v]scale={hw}:-1,format=rgba,"
               f"fade=t=in:st=0:d=0.25:alpha=1,"
               f"fade=t=out:st={max(hook_end - 0.3, 0):.2f}:d=0.3:alpha=1[hook];"
               f"[{prev}][hook]overlay=x=(W-w)/2:y={hy}:"
               f"enable='between(t,0,{hook_end:.2f})':eof_action=pass[h1]")
        prev, idx = "h1", idx + 1

    if stat_png and os.path.exists(stat_png) and stat_t0 is not None:
        inputs += ["-loop", "1", "-t", f"{stat_dur:.2f}",
                   "-i", os.path.abspath(stat_png)]
        sw = int(W * 0.42)
        fc += (f";[{idx}:v]scale={sw}:-1,format=rgba,"
               f"fade=t=in:st=0:d=0.2:alpha=1,"
               f"fade=t=out:st={max(stat_dur - 0.2, 0):.2f}:d=0.2:alpha=1,"
               f"setpts=PTS-STARTPTS+{stat_t0}/TB[stat];"
               f"[{prev}][stat]overlay=x=(W-w)/2:y=(H-h)/2:eof_action=pass[c]")
        prev, idx = "c", idx + 1

    fc += f";[{prev}]ass={ass_relpath}[v]"
    cmd = [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error"] + inputs + [
        "-filter_complex", fc,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", config.ENCODE_PRESET,
        "-crf", str(config.ENCODE_CRF),
        "-c:a", "copy", "-t", f"{dur:.3f}", os.path.abspath(out)]
    _run_cwd(cmd, cwd=cwd)
    return out


def mix_final(video, narr_wav, music_wav, out, sfx_wav=None, target=None):
    """Stitched source audio (already in video) + narration + music (+ optional
    SFX bed), then loudnorm.

    IMPORTANT: amix divides every input by the input count unless normalize=0.
    With four streams that silently costs ~7 dB and, worse, flattens the
    dynamics - the SFX impacts stop standing out from the music, which is
    exactly what the MANDATE audio-peak check measures. So normalize=0 plus
    explicit weights, with narration deliberately dominant.

    The SFX bed is mixed BEFORE loudnorm so the hits are part of the level
    calculation instead of pushing peaks past the true-peak ceiling after it.

    The audio is padded/trimmed to the FULL video length. It used to be
    trimmed to the narration length instead, on the theory that the video
    drifts longer than the narration and the extra tail is dead air. It is not
    dead air - it is the closing subscribe card. Trimming to the narration cut
    the last ~5 s of audio, which silenced the outro card and chopped off the
    music fade-out and the outro sting. `target` is kept for callers that know
    the intended length, but it can only ever be a fallback: the mix covers
    whatever the video is, so the end of the file is never mute.
    """
    dur = probe_duration(video) or (target or 0.0)
    if dur <= 0:
        raise RuntimeError(f"cannot probe duration of {video}")
    fade_start = max(0.0, dur - 2.5)
    inputs = ["-i", video, "-i", narr_wav, "-i", music_wav]

    # Source audio at its own level, split: one copy for the mix, one as the
    # ducking key.
    src = f"[0:a]volume={config.SOURCE_AUDIO_VOLUME}"
    if config.DUCK_ENABLED:
        src += ",asplit=2[src][key]"
        # DUCKING. This is what makes the footage audible without the narrator
        # and the clip talking over each other: while the clip is loud (i.e.
        # somebody in the footage is speaking), the narration is compressed
        # down by roughly the ratio, then the release lets it back up. Both
        # streams stay in the mix, so the story never stops - it just yields
        # the floor to whoever the camera is actually pointed at.
        duck = (f"[nraw][key]sidechaincompress="
                f"threshold={config.DUCK_THRESHOLD}:ratio={config.DUCK_RATIO}:"
                f"attack={config.DUCK_ATTACK}:release={config.DUCK_RELEASE}:"
                f"makeup=1[n]")
    else:
        src += "[src]"
        duck = "[nraw]anull[n]"

    fc = (f"[1:a]volume={config.NARRATION_VOLUME}[nraw];"
          f"{src};{duck};"
          f"[2:a]volume={config.MUSIC_VOLUME},"
          f"afade=t=in:st=0:d=1.5,afade=t=out:st={fade_start:.2f}:d=2.5[m];")
    if sfx_wav and os.path.exists(sfx_wav):
        inputs += ["-i", sfx_wav]
        fc += f"[3:a]volume={config.SFX_VOLUME}[s];"
        # source now carries its own gain, so its amix weight is 1.0
        fc += ("[n][src][m][s]amix=inputs=4:duration=first:"
               "dropout_transition=0:normalize=0:"
               "weights='1.0 1.0 0.6 0.5',")
    else:
        fc += ("[n][src][m]amix=inputs=3:duration=first:"
               "dropout_transition=0:normalize=0:weights='1.0 1.0 0.6',")
    # apad + atrim pins the mix to the video length: pad if a stem ran short,
    # trim if one ran long. Without this the amix length is whatever the first
    # input happens to be, which is how the outro lost its audio.
    fc += (f"apad,atrim=0:{dur:.3f},asetpts=N/SR/TB,"
           f"alimiter=limit=0.95,"
           f"loudnorm=I={config.TARGET_LUFS}:TP=-1.5:LRA=11[a]")
    _run([config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error"] + inputs +
         ["-filter_complex", fc,
          "-map", "0:v", "-map", "[a]", "-c:v", "copy",
          "-c:a", "aac", "-b:a", "192k", "-t", f"{dur:.3f}", out])
    return out


def _loudnorm_measure(path):
    """-> measured loudness of the mixed audio, for a correct second pass.

    One-pass loudnorm does not reach its target on this material. Measured on a
    finished dialogue cut: I=-14 requested, -16.1 delivered, because the filter
    only has one look at the signal and speech with alternating speakers seems
    to make it conservative. Two-pass is the documented way to hit the number:
    measure, then apply with the measurement.
    """
    r = subprocess.run([config.FFMPEG, "-hide_banner", "-i", path, "-af",
                        f"loudnorm=I={config.TARGET_LUFS}:TP=-1.5:LRA=11:"
                        f"print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    txt = r.stderr[r.stderr.rfind("{"):r.stderr.rfind("}") + 1]
    try:
        return json.loads(txt)
    except Exception:
        return None


def _loudnorm_af(m):
    """Gain to the target plus a ceiling limiter, instead of linear loudnorm.

    Linear loudnorm clamps its gain to keep true peaks under TP, and on this
    material that clamp is what the level hits: measured -20.8 LUFS with peaks
    at -5.9 dBTP needs +6.1 dB, which would put peaks over the ceiling, so
    loudnorm delivered -15.7 LUFS instead of -14. Applying the measured gain
    directly and letting a limiter catch only the peaks that overshoot lands on
    the target while leaving the built mix untouched apart from those peaks.
    """
    if not m:
        return f"loudnorm=I={config.TARGET_LUFS}:TP=-1.5:LRA=11"
    try:
        gain = config.TARGET_LUFS - float(m.get("input_i"))
    except (TypeError, ValueError):
        return f"loudnorm=I={config.TARGET_LUFS}:TP=-1.5:LRA=11"
    gain = max(-18.0, min(18.0, gain))
    # The ceiling has to leave room for what the AAC encoder adds: measured on
    # a finished 5 minute file, a limiter at 0.80 (-1.9 dBFS) came out at
    # -0.73 dBTP, i.e. roughly 1.2 dB of overshoot from the codec, and
    # audio_check flags anything above -1.0 dBTP. 0.78 (-2.2 dBFS) lands the
    # delivered true peak near -1.2 without the corrections having to fight
    # the limiter: at 0.72 each pass only closed about a third of the gap and
    # the level settled at -14.7 instead of -14.
    return (f"volume={gain:.2f}dB,"
            f"alimiter=limit=0.78:level=disabled:attack=5:release=50")


def _gain_only_af(gain_db):
    return (f"volume={gain_db:.2f}dB,"
            f"alimiter=limit=0.78:level=disabled:attack=5:release=50")


def mix_dialogue(video, dialogue_wav, music_wav, out, sfx_wav=None):
    """Dialogue track + music bed (+ SFX) over the picture.

    dialogue_wav comes from dialogue.build_track(): one waveform in which each
    SHOW window holds the footage's own audio and each TELL window holds the
    narration line, with nothing anywhere else. It is mixed here rather than
    carried inside the video because concatenating per-segment AAC with -c copy
    slid the audio inside its own segment (measured: 0.49 correlation against
    the segment as written). The video's own audio stream is ignored.

    The bed is the only thing that plays continuously, which is deliberate: it
    is not speech, so it can sit under both speakers without either of them
    being talked over.
    """
    dur = probe_duration(video)
    if dur <= 0:
        raise RuntimeError(f"cannot probe duration of {video}")
    fade_start = max(0.0, dur - 2.5)
    inputs = ["-i", video, "-i", dialogue_wav, "-i", music_wav]
    fc = (f"[2:a]volume={config.MUSIC_VOLUME},"
          f"afade=t=in:st=0:d=1.5,afade=t=out:st={fade_start:.2f}:d=2.5[m];")
    if sfx_wav and os.path.exists(sfx_wav):
        inputs += ["-i", sfx_wav]
        fc += f"[3:a]volume={config.SFX_VOLUME}[s];"
        fc += ("[1:a][m][s]amix=inputs=3:duration=first:"
               "dropout_transition=0:normalize=0:weights='1.0 0.6 0.5',")
    else:
        fc += ("[1:a][m]amix=inputs=2:duration=first:"
               "dropout_transition=0:normalize=0:weights='1.0 0.6',")
    fc += (f"apad,atrim=0:{dur:.3f},asetpts=N/SR/TB,"
           f"alimiter=limit=0.95[a]")
    # pass 1: mix to a temp audio file (no loudnorm) so the real level can be
    # measured, then pass 2 applies it with those measurements
    tmp_wav = out + ".mix.wav"
    _run([config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error"] + inputs +
         ["-filter_complex", fc, "-map", "[a]", "-c:a", "pcm_s16le", tmp_wav])
    m = _loudnorm_measure(tmp_wav)

    def _encode(af, dst):
        _run([config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
              "-i", video, "-i", tmp_wav,
              "-map", "0:v", "-map", "1:a",
              "-af", f"{af},apad,atrim=0:{dur:.3f}",
              "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
              "-t", f"{dur:.3f}", dst])

    _encode(_loudnorm_af(m), out)
    # Verify the delivered audio rather than trusting the filter: whatever the
    # material does to the gain, the file itself is measured and corrected.
    check = _loudnorm_measure(out)
    # Up to three tries: on material where the limiter is working hard the first
    # correction only lands part of the way (measured -15.2 -> wanted +1.2 dB,
    # delivered -14.5 because limiting ate the rest). Chasing the residual until
    # it is inside 0.3 LU is what makes the level come out the same every run.
    # Correct in measured steps until the delivered file is on target. The
    # limiter eats part of every gain that is applied - measured, each pass used
    # to close only about a third of the gap (-16.1 -> -15.4 -> -15.0 -> -14.7
    # where -14 was wanted), so the requested gain is overdriven and the loop
    # keeps going until the file itself reads right.
    for _ in range(6):
        if not check or check.get("input_i") is None:
            break
        try:
            residual = config.TARGET_LUFS - float(check["input_i"])
        except (TypeError, ValueError):
            break
        if abs(residual) <= 0.25:
            break
        step = max(-4.0, min(4.0, residual * 1.8))
        print(f"  [mix] delivered {check['input_i']} LUFS -> correcting "
              f"{step:+.2f} dB (asked {residual:+.2f})")
        fix = out + ".fix.mp4"
        _run([config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
              "-i", out, "-af", _gain_only_af(step),
              "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", fix])
        os.replace(fix, out)
        check = _loudnorm_measure(out) or check
    try:
        os.remove(tmp_wav)
    except OSError:
        pass
    if check:
        print(f"  [mix] delivered {check.get('input_i')} LUFS / "
              f"{check.get('input_tp')} dBTP (target {config.TARGET_LUFS} LUFS)")
    if m:
        print(f"  [mix] measured {m.get('input_i')} LUFS -> "
              f"target {config.TARGET_LUFS} (gain + ceiling limiter)")
    return out


def extract_frame(src, t, out_png):
    subprocess.run([config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{t:.2f}", "-i", src, "-frames:v", "1", out_png],
                   check=True, capture_output=True)
    return out_png
