# MYG 2026

One command:

```
python start.py
```

Pick a number. Research, script, voice and graphics run themselves.
The mp4 lands in `output/`. You upload that file. There is no YouTube login
in this repo.

# SUNNY PIPELINE v4

Autopilot video factory: Director -> script -> gated sources -> face-tracked
beats -> karaoke captions -> QC. Shorts (9:16) + docs (5/10/15 min, 16:9).
Every video ends with the **devdrippy** outro card + spoken CTA.

## Setup (Windows)

1. Install **Python 3.11+** (3.12 works, YuNet replaces MediaPipe).
2. Install **ffmpeg FULL build** (gyan.dev "full" or BtbN `win64-gpl`) and put
   it on PATH. Verify libass: `ffmpeg -filters | findstr ass`
3. Install **Node.js**, then `npm install` in this folder. Graphics render in
   Chrome or Edge. If neither is on PATH, set `CHROME` to the exe.
4. `pip install -r requirements.txt`
5. `python setup_models.py` (downloads YuNet into `./models/`)
6. Set a Cerebras key for research + scripts. Never commit it.
   `set CEREBRAS_API_KEY=csk-...`
   Without a key you get template hooks and fallback scripts.

## Graphics, not After Effects

Sunny type, cards and the stat slam are HTML/CSS, drawn at the real frame
size. A 16:9 documentary is not a stretched 9:16 short. Three.js draws the
ring and particles behind the number. Chrome or Edge is the camera
(`gfx/domgfx.mjs`), driven by a virtual clock so frame 40 is the same on
every machine. React is not in that loop: a film frame has to be a function
of `t`, and React is a UI runtime.

Shorts have no narrator. TTS is the documentary voice
(`en-US-AndrewMultilingualNeural`, with a fallback ladder down to GuyNeural).
`edge-tts` must be 7.2.8 or Microsoft answers 403 and the documentary is
silent. Word timestamps still come from that voice, so the ASS karaoke on a
doc stays locked to the waveform. A short is their voice, their words as
captions, inside the safe area so nothing falls off a 320p frame.

```
export CEREBRAS_API_KEY=csk-...          # never commit this
python main.py --mode short --topic "MrBeast" --preview
python main.py --mode doc10 --topic "MrBeast"
python main.py --mode short --topic auto
```

Research runs before the script. Cerebras writes the hook and the documentary
narration from those claims. Shorts still have no narrator: the model only
cleans the on-screen words, and only if they are the words that were spoken.

YouTube upload is not in this repo. There is no OAuth client. A finished mp4
is what you upload yourself.

## Evidence layer (ported)

Before the script is written the run now gathers claims, a spoken quote, a
payable hook, a checked title and name-locked stills. Invented numbers are
rewritten. `--topic auto` scouts Kick, Reddit and News. `--preview` renders
320p so a cut can be watched immediately.

```
python main.py --mode short --topic "MrBeast" --preview --minutes 0.4
python main.py --mode short --topic auto --preview
```

## Run

```
python main.py                                  # English CMD menu
python main.py --mode short --topic "MrBeast"   # headless autopilot
python main.py --test-llm                       # check LLM key
python main.py --memory                         # story memory (dedupe log)
```

Menu: 1=SHORT 45s, 2/3/4=DOC 5/10/15 min, 5=URL mode (paste your own links),
6=LLM test, 7=story memory.

Verify a finished video against the MANDATE gate manually:

```
python verify.py output/<video>.mp4 <target_seconds>
```

Output: `output/<mode>_<timestamp>_<title>.mp4`
Working files (segments, captions, crop data): `work/<run>/`

## Tuning

All "craziness" knobs live in `config.py`:
`MAX_HEAVY_FX_PER_10S`, `MAX_STILL_SEC`, `MIN_COVERAGE`, `HOOK_MAX_SEC`,
`PUNCH_WORDS`, `SOURCE_SCORE_MIN`, `TARGET_LUFS` ...

## Pipeline

```
Director (LLM brief) -> Script (+QC retry w/ feedback)
  -> edge-TTS + WordBoundary (zero-drift kinetic karaoke)
  -> Sources: search per line -> relevance gate -> download -> probe
     (face_ratio / letterbox / shot score) -> transcript verify -> pHash dedupe
  -> Beats every 2.2-3.6s, decide_effect (RMS z + punch words)
  -> Render: face-centered 9:16 crop / 16:9 punch-in, per-beat fx
  -> Cinema pass: teal/orange grade + grain + vignette + hook glow + stat card
  -> concat -> kinetic captions burn -> music + narration mix (-14 LUFS)
  -> outro card (devdrippy) -> final QC -> MANDATE verify gate -> story memory
```

## Studio kit (ported from sunnyv2youtube)

- `gfx.py` — Pillow assets: glow titles, lower thirds, tweet/discord/phone UI,
  stat cards, timeline, bar chart, HUD, light leak, circle portraits,
  anamorphic flare, thumbnail generator. Cross-platform font resolver.
- `cinema.py` — ffmpeg building blocks: easing/overshoot, parallax, teal/orange
  `GRADE`, `GRAIN`, `VIGNETTE`, `LOOKS` per intent, `lut3d()`, 90 BPM music, SFX.
- `luts.py` — generates spec-valid 33³ `.cube` LUTs (teal_orange, cold_archive,
  heat_reveal). Every value clamped to 0–1, correct write order.
- `sfx.py` — synthesises cinematic WAVs (braaam, riser, sub_drop, hit, whoosh,
  glitch, click) from math + a bed builder that places them on the beats.
  No downloads, no licensing questions.
- `captions.py` — `ass_kinetic()` kinetic typography with accent-red keyword
  highlight, rotation jitter, pop-in animation (uses real TTS word timestamps).
- `verify.py` — MANDATE.md anti-slideshow gate (motion + audio-peak + duration).
  Runs automatically after every render; REJECTs static "textspel" output.
- `studio.py` — one-shot builder + font report: `python studio.py`
- `MANDATE.md` — the 12 binding rules and which are auto-enforced.

### First run on a new machine
```
python setup_models.py     # face + whisper models
python studio.py           # LUTs, SFX, font report
```
Then drop `Anton-Regular.ttf`, `ArchivoBlack-Regular.ttf` and
`BebasNeue-Regular.ttf` (free, Google Fonts, OFL) into `assets/fonts/` for the
intended display typography. Without them the pipeline still runs and says so —
it falls back to a system font rather than rendering invisible text.

### Tuning knobs (config.py)
| Knob | Meaning |
|---|---|
| `USE_LUT` | grade via one `lut3d` pass instead of chained eq/colorbalance |
| `LUT_FILE` | which `.cube` to use |
| `SFX_ENABLED` | mix the beat-derived SFX bed into the final audio |
| `SFX_VOLUME` | bed level relative to narration/music |
| `THUMBNAIL_ENABLED` | write a thumbnail next to every finished video |
