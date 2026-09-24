"""Central config. Tune "craziness level" here, never in the logic."""
import os
import subprocess

# ---------- Paths ----------
ROOT = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(ROOT, "models")
WORK_DIR = os.path.join(ROOT, "work")

# Downloaded clip sections are cached here and reused across runs. A run that
# fails at the render stage used to re-download every source before it could
# get back to the same place, which for pasted archive.org urls is minutes of
# waiting per re-run. Set SUNNY_CLIP_CACHE to move it (it is gitignored).
CLIP_CACHE_DIR = os.environ.get("SUNNY_CLIP_CACHE",
                                os.path.join(ROOT, "cache", "clips"))
OUT_DIR = os.path.join(ROOT, "output")
ASSETS_DIR = os.path.join(ROOT, "assets")
STORY_MEMORY_FILE = os.path.join(ROOT, ".story_memory.json")

# ---------- LLM ----------
# Providers are tried in this order; the first key found wins.
#   Cerebras  -> fastest, OpenAI-compatible, CEREBRAS_API_KEY (csk-...)
#   OpenRouter -> many models, OPENROUTER_API_KEY
#   OpenAI    -> OPENAI_API_KEY
# Set LLM_PROVIDER to force one ("cerebras" / "openrouter" / "openai").
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
CEREBRAS_MODEL = os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b")
CEREBRAS_BASE_URL = os.environ.get("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1")

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "openai/gpt-4o-mini")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
OPENAI_BASE_URL = "https://api.openai.com/v1"
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "").strip().lower()

# ---------- TTS ----------
# Andrew Multilingual is the best free documentary voice that still emits
# WordBoundary, so karaoke captions stay locked to the waveform. GuyNeural is
# the last fallback, not the voice. Dragon HD / ElevenLabs need a paid key
# and do not give word times, so they are not the default for mass runs.
# Override with TTS_VOICE=en-US-BrianMultilingualNeural if you want another.
TTS_VOICE_EN = os.environ.get("TTS_VOICE", "en-US-AndrewMultilingualNeural")
TTS_VOICE_FALLBACKS = [
    "en-US-BrianMultilingualNeural",
    "en-US-AndrewNeural",
    "en-US-ChristopherNeural",
    "en-US-GuyNeural",
]
TTS_VOICE_SV = "sv-SE-MattiasNeural"
TTS_RATE = os.environ.get("TTS_RATE", "+4%")
# Closed loop on narration length. The words-per-second rate is a property of
# the voice, the +N% setting and the backend, so a fixed word budget can only
# ever approximate the target duration. After synthesising, the pipeline can
# measure what it actually got and re-speak the script slightly faster or
# slower to land on the format length. One correction pass, only when the miss
# is worth fixing.
NARRATION_AUTOFIT = True
NARRATION_TOLERANCE = 0.06      # 6% -> ~2.7 s on a 45 s short

# ---------- Outro (mandatory) ----------
OUTRO_TEXT = "This was devdrippy on YouTube. Subscribe and like for more!"
OUTRO_CARD_LINE1 = "devdrippy on YouTube"
OUTRO_CARD_LINE2 = "SUBSCRIBE and LIKE for more"
OUTRO_SECONDS = 3.0

# ---------- Formats ----------
FORMATS = {
    "short": {"duration": 45,  "w": 1080, "h": 1920, "aspect": "9:16"},
    "doc5":  {"duration": 300, "w": 1920, "h": 1080, "aspect": "16:9"},
    "doc10": {"duration": 600, "w": 1920, "h": 1080, "aspect": "16:9"},
    "doc15": {"duration": 900, "w": 1920, "h": 1080, "aspect": "16:9"},
}

# ---------- Budgets / gates ----------
HOOK_MAX_SEC = 3.0
HOOK_MAX_WORDS = 9
MAX_STILL_SEC = 4.0
MAX_HEAVY_FX_PER_10S = 1.0
MIN_COVERAGE = 0.85
CUT_MIN_SEC = 2.2
CUT_MAX_SEC = 3.6
MAX_SCRIPT_RETRIES = 2
MAX_STORY_ATTEMPTS = 3
RANDOM_SEED = 42

# relevance gate 1: minimum similarity between search result title and the
# script line's query (embedding cosine OR normalized token overlap fallback)
RELEVANCE_MIN = 0.50
# gate 3: whisper-verify that queried entities actually appear in the clip.
# CPU-heavy (45s of ASR per candidate) - off by default
VERIFY_TRANSCRIPT = False
PHASH_MAX_DIST = 6   # hamming distance tolerance -> catches re-encodes

# per-line search queries allowed per format (SunnyV2 = query per beat)
QUERY_CAPS = {"short": 14, "doc5": 24, "doc10": 40, "doc15": 55}

# ---------- ffmpeg ----------
def _find_ffmpeg():
    """Prefer a system ffmpeg (the gyan.dev/BtbN builds on Windows have libass
    and lut3d). Fall back to the static binary bundled by imageio-ffmpeg, which
    makes the pipeline work on a machine where ffmpeg was never installed."""
    import shutil
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _find_ffprobe():
    """ffprobe is the reliable way to read metadata, but it is NOT bundled by
    imageio-ffmpeg. Look on PATH, then next to whatever ffmpeg we found. When
    it is missing, sources.probe falls back to parsing `ffmpeg -i` output."""
    import shutil
    p = shutil.which("ffprobe")
    if p:
        return p
    d = os.path.dirname(FFMPEG)
    for cand in ("ffprobe", "ffprobe.exe"):
        c = os.path.join(d, cand)
        if os.path.exists(c):
            return c
    return ""


FFMPEG = _find_ffmpeg()
FFPROBE = _find_ffprobe()


def _expose_ffmpeg_on_path():
    """Symlink the discovered binaries to bare 'ffmpeg'/'ffprobe' names and put
    that directory on PATH.

    This is not cosmetic. Several things look for the literal executable name
    and cannot be told otherwise:
      * yt-dlp's partial download support calls FFmpegFD.available(), which
        only checks PATH - upstream marks it "Fixme: This may be wrong when
        --ffmpeg-location is used" - so every ranged download died with
        "ffmpeg is not installed" even though ffmpeg_location was set.
      * main.py's preflight check is a shutil.which("ffmpeg") call.
      * any subprocess that shells out to a bare `ffmpeg`.
    The imageio-ffmpeg build ships as 'ffmpeg-linux-x86_64-v7.0.2', which
    satisfies none of those, so the shim makes the bundled binary a first-class
    ffmpeg on this machine.
    """
    import shutil
    import tempfile
    if not FFMPEG or os.path.basename(FFMPEG) in ("ffmpeg", "ffmpeg.exe"):
        return                                  # already a plain name / on PATH
    if not os.path.exists(FFMPEG):
        return
    shim = os.path.join(tempfile.gettempdir(), "sunny_ffbin")
    try:
        os.makedirs(shim, exist_ok=True)
        for name, src in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE)):
            if not src:
                continue
            dst = os.path.join(shim, name)
            if os.path.exists(dst) or os.path.islink(dst):
                continue
            try:
                os.symlink(src, dst)
            except OSError:
                shutil.copy2(src, dst)          # Windows / no-symlink support
        if shim not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = shim + os.pathsep + os.environ.get("PATH", "")
    except Exception as e:                       # never fatal - PATH is a bonus
        print(f"  [config] ffmpeg PATH shim skipped: {type(e).__name__}")


_expose_ffmpeg_on_path()


FONTCONFIG_ALIAS = '''<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <!-- written by sunny_pipeline. The captions ask for "Arial Black" (the
       Windows face the MANDATE specifies); elsewhere that name does not
       exist and fontconfig answered with DejaVu Sans, so the most visible
       text in the video rendered in the wrong typeface. -->
  <alias binding="same">
    <family>Arial Black</family>
    <prefer><family>Archivo Black</family></prefer>
  </alias>
  <alias binding="same">
    <family>Arial</family>
    <prefer><family>Archivo Black</family></prefer>
  </alias>
</fontconfig>
'''


def _register_fonts():
    """Make the bundled fonts findable by name, and alias Arial Black to
    Archivo Black.

    Why: the captions in `captions.py` ask for "Arial Black", which is correct
    on Windows but does not exist on Linux - fc-match resolved it to DejaVu
    Sans, so the single most visible text in the video (the kinetic captions)
    rendered in the wrong typeface. The bundled Archivo Black is the closest
    match to Arial Black and is the face the MANDATE asks for, so it is
    installed for fontconfig and aliased under the requested name.

    No-op when already resolvable, and never fatal: Windows has Arial Black
    natively and needs none of this.
    """
    import shutil
    fontdir = os.path.join(ASSETS_DIR, "fonts")
    if not os.path.isdir(fontdir) or not shutil.which("fc-cache"):
        return                                   # no bundled fonts / no fontconfig
    home = os.path.expanduser("~")
    dest = os.path.join(home, ".local", "share", "fonts", "sunny-pipeline")
    alias = os.path.join(home, ".config", "fontconfig", "conf.d",
                         "99-sunny-pipeline.conf")
    try:
        probe = subprocess.run(["fc-match", "-f", "%{family}", "Archivo Black"],
                               capture_output=True, text=True, timeout=20).stdout
        if "Archivo" in probe and os.path.exists(alias):
            return                               # already registered
        os.makedirs(dest, exist_ok=True)
        shutil.copytree(fontdir, dest, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("*.txt", "OFL*"))
        os.makedirs(os.path.dirname(alias), exist_ok=True)
        with open(alias, "w", encoding="utf-8") as f:
            f.write(FONTCONFIG_ALIAS)
        subprocess.run(["fc-cache", "-f"], capture_output=True, timeout=90)
    except Exception as e:
        print(f"  [config] font registration skipped: {type(e).__name__}")


_register_fonts()


def ffmpeg_http_ok(cache={}):
    """Can this ffmpeg read an https:// input? Cached - it costs a subprocess.

    Some bundled static builds segfault on https input while working perfectly
    on local files. That combination is nasty: local renders pass, and only the
    source download fails, with yt-dlp reporting the misleading "ffmpeg is not
    installed". A one-off probe lets the preflight say what is actually wrong.
    """
    if "v" in cache:
        return cache["v"]
    import subprocess
    try:
        r = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-t", "1",
             "-i", "https://raw.githubusercontent.com/ffmpeg/ffmpeg/master/README.md",
             "-f", "null", "-"],
            capture_output=True, timeout=25)
        # a segfault shows up as a negative returncode (signal == -11); a build
        # that speaks https fails on "invalid data" instead, which passes here
        cache["v"] = r.returncode >= 0
    except Exception:
        cache["v"] = False
    return cache["v"]

# ---------- Studio kit (LUT / SFX / thumbnails) ----------
# A spec-valid .cube applied in one ffmpeg pass (lut3d) instead of the chained
# eq/colorbalance GRADE. Generate with:  python luts.py
LUT_DIR = os.path.join(ASSETS_DIR, "luts")
USE_LUT = True                      # False -> fall back to cinema.GRADE
LUT_FILE = os.path.join(LUT_DIR, "teal_orange.cube")
SFX_DIR = os.path.join(ASSETS_DIR, "sfx")
# Auto-place risers/impacts/whoosh from the story beats. Generate the WAVs
# first with:  python sfx.py      then flip this to True.
SFX_ENABLED = True
# Impacts are punctuation, not percussion. One every ~6 s reads as a hit; one
# every 2 s reads as a broken notification sound, which is what the first
# version did - it fired on every zoom_punch, and zoom_punch was the most
# common effect in the edit.
SFX_MIN_GAP = 6.0                    # seconds between two impacts
SFX_MAX_PER_VIDEO = 6                # hard ceiling regardless of length
SFX_VOLUME = 0.85                   # bed prints ~-14 dBFS peak; under a
                                    # narration at 1.7 this lands the impacts
                                    # ~11 dB down, i.e. audible but not on top
THUMBNAIL_ENABLED = True
THUMBNAIL_DIR = os.path.join(OUT_DIR, "thumbnails")

# ---------- Visual cues (Director-tagged graphics) ----------
# The Director tags script lines with cues; cues.py validates and draws them.
# This is what makes lower thirds / tweet cards / timeline cards appear at the
# right moment instead of never.
CUES_ENABLED = True
CUE_MAX_PER_VIDEO = 6               # 2-5 is the SunnyV2 sweet spot; 6 is the cap
ATMOSPHERE_ENABLED = True           # flare / light leak / HUD mood layers

# ---------- Source gates ----------
SOURCE_SCORE_MIN = 1.2
FACE_RATIO_MIN = 0.25
# The floor a source framerate must clear. Do NOT use 24 as the threshold:
# 23.976 fps is the standard film rate and is extremely common on YouTube, and
# "fps < 24" rejected every such clip with a score of 0.0 (which reads as a
# scoring bug rather than what it is - a threshold that excludes a normal
# framerate). Anything at or above 23 is real motion, not a slideshow.
MIN_SOURCE_FPS = 23.0
SHOT_SCORE_MIN = 0.25
MAX_DOWNLOADS_PER_RUN = 10
MIN_SOURCE_HEIGHT = 720
# If NOTHING passes at MIN_SOURCE_HEIGHT the run would produce zero clips and
# therefore a video with no footage at all. A hard floor is right for quality
# but wrong as a single point of failure: YouTube serves 360p only to some IPs
# (flagged datacenters, aggressive throttling, some regions). So the floor
# relaxes once, loudly, instead of failing the whole run.
# 0 = "whatever is actually available" - a relaxed floor of e.g. 480p is
# useless if the ceiling is 360p, since the run would still ship an empty video.
MIN_SOURCE_HEIGHT_FALLBACK = 0

# Source videos are long; only a few seconds of each is ever used. Slicing the
# download turns a 400 MB / 35 s transfer into a few MB, and stops a two-hour
# podcast from being fetched in full just to be rejected by the quality gate.
# Keep work/ after a run for debugging. Off by default: a finished run leaves
# ~80 MB of scratch files that are never read again.
KEEP_WORK = False

# Measured: 100 s of 1080p is a 32 MB transfer and takes ~3.5 min on a slow
# link. Each clip ends up covering ~2-4 beats, i.e. about 5-15 s of screen
# time, so 100 s was buying bandwidth nobody watched. 40 s still leaves the
# crop path room to pan and gives every beat a different part of the clip,
# at under half the transfer.
SECTION_SECONDS = 40         # how much footage to fetch per clip
SECTION_START = 45           # skip intros/title cards
MAX_SOURCE_DURATION = 7200   # informational: long VODs are fine, we slice them

BAD_TITLE_WORDS = ["compilation", "reaction", "reacts", "best of", "top 10",
                   "meme", "tiktok", "#shorts", "mashup", "funny moments"]

# ---------- Effects ----------
PUNCH_WORDS = {
    "insane", "destroyed", "exposed", "million", "billion", "never", "banned",
    "collapsed", "shocking", "secret", "lie", "lies", "lost", "everything",
    "broke", "ruined", "banned", "caught", "leaked", "worst", "best", "first",
    "final", "nobody", "everyone", "forever", "gone", "over",
}

# ---------- Audio ----------
TARGET_LUFS = -14
NARRATION_VOLUME = 1.7
# Mysterious drone, not a beat bed: it plays for the whole video (see
# music.STYLES["mysterious"]). Sits lower than a beat would, because a
# continuous tone is far more noticeable than a pulse.
# Measured, not guessed: the synthesised bed has rms 0.297, and at 0.30 with
# the 0.6 amix weight that put it at 0.053 in the mix - 4.5 dB below a narration
# line (0.090) and LOUDER than the footage audio in a SHOW window (0.050). The
# person was quieter than the background music. 0.14 lands the bed ~11 dB under
# the narrator, which is where a documentary bed sits, and still audible.
MUSIC_VOLUME = 0.14

# Which music style the run uses. "mysterious" = tension drone; the others
# ("intro", "rising", "climax", "outro") are kick-and-hat beds.
MUSIC_STYLE = "mysterious"

# ---------------------------------------------------------------------------
# DIALOGUE STRUCTURE
# ---------------------------------------------------------------------------
# True: the video alternates SHOW (the person talks, their own audio) and TELL
# (the narrator explains, the clip muted). See dialogue.py - the segments do
# not overlap in time, so narration over the person's voice is impossible by
# construction rather than by mixing carefully.
# False: the old voiceover behaviour, kept for comparison.
DIALOGUE_MODE = True

# Shorts do not get a narrator. TTS is the documentary voice. A short is the
# person's own words, with HTML/Three.js graphics inside the safe area.
# Set True only to force the old voiceover short.
SHORTS_TTS = False
# Documentary cards (hook, stat, lower third) are the same HTML/Three.js
# layer as Shorts. Captions on a doc stay the ASS track locked to the voice.
HTML_GFX = True

# How much of the body duration the footage gets. The rest is the narrator.
# This is the number to turn if the videos feelTalky: raise it and the script
# gets shorter because the word budget is derived from it.
SHOW_SHARE = 0.34

# Bounds on one SHOW segment. Below ~1.6s there is no time to say anything, and
# past ~6s the person is essentially doing the explaining for us.
SHOW_MIN_SEC = 1.8
SHOW_MAX_SEC = 6.0

# Gain on the footage's own audio inside a SHOW segment. It is the only thing
# playing there, so it does not need to win a fight with anything.
DIALOGUE_SOURCE_GAIN = 1.0

# The footage is transcribed so its captions can be shown while the person
# talks. "base" is the accuracy/wait tradeoff on CPU; "tiny" is ~2x faster.
SOURCE_ASR_MODEL = "base"
# The clip's own audio. This is the whole point of using real footage: you
# are supposed to HEAR the person talk. It used to be 0.18, then got weighted
# 0.6 in the mix against narration at 1.7 - about 6% of the narrator, i.e.
# inaudible, which is why the videos sounded like someone talking over a
# muted interview.
SOURCE_AUDIO_VOLUME = 0.55

# When the clip has actual speech, the narrator steps back so both are not
# talking at once. sidechaincompress watches the source audio and turns the
# narration down while it is loud, then lets it back up. Threshold is low
# because we want this to trigger on real speech, not only on peaks.
DUCK_ENABLED = True
DUCK_THRESHOLD = 0.045      # sidechain level that starts ducking
DUCK_RATIO = 5.0            # 5:1 -> a clear step back, not a mute
DUCK_ATTACK = 25            # ms - fast enough to catch the first syllable
DUCK_RELEASE = 600          # ms - slow enough to not pump between words
WHISPER_MODEL = "small"      # tiny/base/small - small = good CPU balance
TTS_SAMPLE_RATE = 44100

# ---------- Render ----------
FFMPEG_THREADS = 0           # 0 = auto
ENCODE_PRESET = "veryfast"
ENCODE_CRF = 19
OUTPUT_FPS = 30
