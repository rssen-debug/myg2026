# FINAL2026 — Ultimate Video Autopilot

One-command Windows launcher for Shorts + YouTube documentaries.

## Start
1. Download this repository as ZIP and extract it.
2. Install Python 3.12+.
3. Double-click `FINAL2026_START.bat` or run `py FINAL2026.py`.

The launcher automatically installs Python requirements and npm packages, tries to install Node.js LTS / Chrome / FFmpeg through winget, downloads the vision model, asks for the Cerebras key once, saves it in the Windows user environment (not Git), then opens the existing production menu.

## What you choose
- Short — 9:16, ~45s
- YouTube — 16:9, 5 min
- YouTube — 16:9, 10 min
- YouTube — 16:9, 15 min
- Preview — fast low-resolution render
- LLM test

Use `auto` as the topic to let the scout find a story.

## Premium TTS
The current pipeline is already wired for timestamped Edge TTS. For premium narration, add `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID`; FINAL2026 can be extended to route narration through ElevenLabs and use Scribe v2 for word-level timestamps.

## Never commit API keys
The Cerebras key is intentionally NOT stored in the repository. Because API keys are credentials, rotate any key that has been pasted into public chat/logs and paste the replacement when the launcher asks.
