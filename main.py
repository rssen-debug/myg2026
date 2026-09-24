#!/usr/bin/env python3
"""SUNNY PIPELINE v4 - autopilot video factory.
One entry file. Run:  python main.py   (interactive menu)
or headless:          python main.py --mode short --topic "MrBeast"
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import llm  # noqa: E402
import sources  # noqa: E402
from orchestrator import StoryMemory, run_autopilot  # noqa: E402

MENU = """
================================================
   SUNNY PIPELINE v4   //   autopilot factory
================================================
  1) SHORT   9:16  ~45s   (YouTube Shorts)
  2) DOC     16:9  ~5 min (documentary)
  3) DOC     16:9  ~10 min
  4) DOC     16:9  ~15 min
  5) URL MODE (use your own pasted links)
  6) TEST LLM
  7) SHOW STORY MEMORY
  8) VERIFY A VIDEO (anti-slideshow gate)
  0) EXIT
================================================"""

MODE_BY_CHOICE = {"1": "short", "2": "doc5", "3": "doc10", "4": "doc15"}


def check_env():
    print("\n-- environment check --")
    ok = True
    if not config.FFMPEG or not os.path.exists(config.FFMPEG):
        print("  [!!] ffmpeg not found - install a full build "
              "(gyan.dev/BtbN on Windows; libass is required for captions)")
        ok = False
    else:
        print(f"  [ok] ffmpeg  {config.FFMPEG}")
        print(f"       ffprobe {config.FFPROBE or '(missing - using ffmpeg stderr fallback)'}")
        # An ffmpeg without working https cannot download ranged sections, and
        # yt-dlp then fails every clip with a bare "ffmpeg is not installed".
        # Surfacing it here turns a confusing mid-run failure into a warning.
        if not config.ffmpeg_http_ok():
            print("  [!!] this ffmpeg cannot read https URLs. Source downloads "
                  "will fail.")
            print("       -> install a real build:  apt install ffmpeg   "
                  "(or gyan.dev on Windows)")
            ok = False
    for mod, name in [("cv2", "opencv-contrib-python"), ("yt_dlp", "yt-dlp"),
                      ("edge_tts", "edge-tts"), ("numpy", "numpy")]:
        try:
            __import__(mod)
            print(f"  [ok] {name}")
        except ImportError:
            print(f"  [!!] {name} missing -> pip install -r requirements.txt")
            ok = False
    if not os.path.exists(os.path.join(config.MODELS_DIR,
                                       "face_detection_yunet_2023mar.onnx")):
        print("  [!!] vision models missing -> python setup_models.py")
        ok = False
    else:
        print("  [ok] vision models")
    if llm.have_key():
        # show the RESOLVED provider+model, not the OpenRouter default name -
        # printing config.LLM_MODEL reported "openai/gpt-4o-mini" even when the
        # active provider was Cerebras on gpt-oss-120b
        print(f"  [ok] LLM {llm.describe()}")
    else:
        print("  [??] no LLM key -> generic fallback scripts. Set "
              "CEREBRAS_API_KEY (fastest), or OPENROUTER_API_KEY, or "
              "OPENAI_API_KEY")
    return ok


def ask_urls():
    print("Paste YouTube URLs (one per line, empty line to finish):")
    urls = []
    while True:
        try:
            line = input("  > ").strip()
        except EOFError:
            break
        if not line:
            break
        urls.append(line)
    return urls


def interactive():
    print(MENU)
    choice = input("Choose: ").strip()
    if choice == "0":
        return
    if choice == "6":
        r = llm.chat([{"role": "user", "content": "Reply with exactly: LLM OK"}])
        print("LLM answer:", r if r else "(no key / failed - check env vars)")
        input("\n[enter] to exit"); return
    if choice == "7":
        for e in StoryMemory().show():
            print(" ", e)
        input("\n[enter] to exit"); return
    if choice == "9":
        import studio
        studio.build_all()
        input("\n[enter] to exit"); return
    if choice == "8":
        import glob
        import verify
        out_dir = config.OUT_DIR
        vids = sorted(glob.glob(os.path.join(out_dir, "*.mp4")),
                      key=os.path.getmtime, reverse=True)
        if not vids:
            print(f"  no videos in {out_dir} yet")
            input("\n[enter] to exit"); return
        print("  recent videos:")
        for i, v in enumerate(vids[:9], 1):
            print(f"   {i}) {os.path.basename(v)}")
        sel = input("Pick number (or paste a full path): ").strip()
        if sel.isdigit() and 1 <= int(sel) <= len(vids[:9]):
            path = vids[int(sel) - 1]
        else:
            path = sel.strip('"')
        tgt = input("Target duration in seconds [blank = skip]: ").strip()
        ok, _, _ = verify.verify_build(
            path, float(tgt) if tgt else None, quiet=False)
        print(f"\n  RESULT: {'PASS' if ok else 'REJECT'}")
        input("\n[enter] to exit"); return
    if choice == "5":
        mode = MODE_BY_CHOICE.get(input("Format? (1=short 2=doc5 3=doc10 4=doc15): ").strip(), "short")
        topic = input("Topic: ").strip() or "pasted footage"
        urls = ask_urls()
        run_autopilot(mode, topic, urls=urls)
        input("\n[enter] to exit"); return
    mode = MODE_BY_CHOICE.get(choice)
    if not mode:
        print("unknown choice"); return
    topic = input("Topic: ").strip()
    if not topic:
        print("no topic given"); return
    niche = input("Niche [general]: ").strip() or "general"
    print(f"\nStarting autopilot: {mode} | '{topic}' | niche={niche}")
    run_autopilot(mode, topic, niche=niche)
    input("\n[enter] to exit")


def list_outputs():
    """Show what has been rendered, so the files are never a mystery.

    output/ is deliberately never cleaned by the pipeline (only work/ is), but
    a folder full of timestamped names is still hard to read when you just want
    to know what finished. Sorted newest first with duration, resolution and
    size, and the thumbnail is listed next to its video.
    """
    if not os.path.isdir(config.OUT_DIR):
        print("\noutput/ does not exist yet - nothing has been rendered.\n"
              "  run:  python main.py --mode short --topic \"your topic\"")
        return
    files = [f for f in os.listdir(config.OUT_DIR) if f.endswith(".mp4")]
    if not files:
        print("\noutput/ is empty - nothing has been rendered yet.")
        return
    files.sort(key=lambda f: os.path.getmtime(os.path.join(config.OUT_DIR, f)),
               reverse=True)
    print(f"\n{len(files)} video(s) in {config.OUT_DIR}\n")
    total = 0
    for f in files:
        p = os.path.join(config.OUT_DIR, f)
        size = os.path.getsize(p)
        total += size
        m = sources.ffprobe_meta(p)
        thumb = os.path.join(config.THUMBNAIL_DIR, os.path.splitext(f)[0] + ".jpg")
        print(f"  {f}")
        print(f"    {m.get('width', 0)}x{m.get('height', 0)}  "
              f"{m.get('duration', 0):.1f}s  {size / 1e6:.1f} MB  "
              f"{datetime.fromtimestamp(os.path.getmtime(p)):%Y-%m-%d %H:%M}")
        if os.path.exists(thumb):
            print(f"    thumbnail: {os.path.relpath(thumb, config.ROOT)}")
    print(f"\n  total {total / 1e6:.1f} MB.  Work files live in work/ and are "
          f"deleted after each successful render;\n  output/ is never touched "
          f"by the pipeline.")
    print("  verify a video:  python main.py --verify output/<file>.mp4 "
          "<target_seconds>")


def main():
    ap = argparse.ArgumentParser(description="Sunny Pipeline v4")
    ap.add_argument("--mode", choices=["short", "doc5", "doc10", "doc15"])
    ap.add_argument("--topic")
    ap.add_argument("--niche", default="general")
    ap.add_argument("--urls", nargs="*", default=None)
    ap.add_argument("--minutes", type=float, default=None,
                    help="override the format's target length in minutes "
                         "(e.g. --mode doc5 --minutes 2 for a 2-minute cut)")
    ap.add_argument("--preview", action="store_true",
                    help="320p fast cut so a run can be watched immediately")
    ap.add_argument("--scout", action="store_true",
                    help="pick the topic from Kick / Reddit / News (or pass --topic auto)")
    ap.add_argument("--tts", action="store_true",
                    help="shorts: burn a narrator in (off by default; TTS is for docs)")
    ap.add_argument("--test-llm", action="store_true")
    ap.add_argument("--memory", action="store_true")
    ap.add_argument("--verify", metavar="VIDEO",
                    help="run the MANDATE anti-slideshow gate on a video")
    ap.add_argument("--list", action="store_true",
                    help="list rendered videos in output/ (newest first)")
    ap.add_argument("--build-assets", action="store_true",
                    help="generate LUTs + SFX and report font resolution")
    ap.add_argument("--verify-target", type=float, default=None,
                    help="expected duration in seconds for --verify (±5%% gate)")
    args = ap.parse_args()

    if not check_env() and not args.verify:
        print("\nFix the issues above first.")
        if not (args.mode and args.topic):
            sys.exit(1)

    if args.test_llm:
        # llm.self_test checks BOTH a plain completion and json_mode, which is
        # what every structured call in the pipeline actually depends on - a
        # plain "LLM OK" reply says nothing about whether Director/script JSON
        # will parse. It also reports latency and the resolved provider.
        print("LLM:", llm.describe())
        ok = llm.self_test()
        print("self-test:", "PASS" if ok else "FAIL")
        return
    if args.memory:
        for e in StoryMemory().show():
            print(e)
        return
    if args.verify:
        import verify
        ok, _, _ = verify.verify_build(args.verify, args.verify_target)
        print("\nRESULT:", "PASS" if ok else "REJECT")
        return
    if args.list:
        list_outputs()
        return

    if args.build_assets:
        import studio
        studio.build_all()
        return
    if args.scout and not args.topic:
        args.topic = "auto"
    if args.mode and args.topic:
        if args.minutes:
            # shorten the target before planning: the word budget, the beat
            # count and the duration gate all derive from FORMATS[mode]
            config.FORMATS[args.mode]["duration"] = int(args.minutes * 60)
            print(f"-- target length overridden to {args.minutes:g} min "
                  f"({int(args.minutes * 60)}s)")
        if args.tts:
            config.SHORTS_TTS = True
            print("-- shorts TTS forced on")
        if args.preview:
            fmt = config.FORMATS[args.mode]
            if fmt["aspect"] == "9:16":
                fmt["w"], fmt["h"] = 320, 568
            else:
                fmt["w"], fmt["h"] = 568, 320
            config.MIN_SOURCE_HEIGHT = 0
            config.SOURCE_SCORE_MIN = 0.4
            config.SECTION_SECONDS = 12
            config.MAX_DOWNLOADS_PER_RUN = 2
            config.QUERY_CAPS[args.mode] = min(config.QUERY_CAPS.get(args.mode, 8), 4)
            config.ENCODE_PRESET = "ultrafast"
            config.ENCODE_CRF = 28
            config.OUTPUT_FPS = 24
            print(f"-- preview {fmt['w']}x{fmt['h']} @ {config.OUTPUT_FPS}fps")
        run_autopilot(args.mode, args.topic, niche=args.niche, urls=args.urls)
        return
    from start import menu
    menu()


if __name__ == "__main__":
    main()
