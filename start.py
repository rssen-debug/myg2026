#!/usr/bin/env python3
"""The one command.

    python start.py

A menu. You pick. Research, script, voice, graphics and the mp4 are the
program's job. Headless flags still live in main.py.
"""
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))

MENU = """
========================================
  FINAL2026
  Välj. Resten körs själv.
========================================
  1  Short            9:16    45 sek
  2  YouTube          16:9    5 min
  3  YouTube          16:9    10 min
  4  YouTube          16:9    15 min
  5  Förhandsvisning  320p    snabb kolla
  6  Testa nyckeln
  0  Avsluta
========================================"""

MODES = {"1": "short", "2": "doc5", "3": "doc10", "4": "doc15"}


def _ask(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _missing_packages():
    missing = []
    for mod, pip_name in (("cv2", "opencv"), ("yt_dlp", "yt-dlp"),
                          ("edge_tts", "edge-tts"), ("numpy", "numpy"),
                          ("PIL", "pillow")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pip_name)
    return missing


def _chrome():
    if os.environ.get("CHROME") and os.path.exists(os.environ["CHROME"]):
        return os.environ["CHROME"]
    for name in ("chrome", "msedge", "chromium", "google-chrome", "microsoft-edge"):
        hit = shutil.which(name)
        if hit:
            return hit
    for cand in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/chromium",
        "/usr/bin/google-chrome",
    ):
        if os.path.exists(cand):
            return cand
    return ""


def _fix_missing():
    """Install what this folder can install. ffmpeg and Chrome are the user's."""
    missing = _missing_packages()
    need_npm = not os.path.isdir(os.path.join(HERE, "node_modules", "puppeteer-core"))
    need_model = not os.path.exists(os.path.join(
        HERE, "models", "face_detection_yunet_2023mar.onnx"))
    if not missing and not need_npm and not need_model:
        return True
    print("\nFörsta gången. Det här saknas:")
    if missing:
        print("  python-paket:", ", ".join(missing))
    if need_npm:
        print("  grafik: npm install")
    if need_model:
        print("  ansiktsmodell: python setup_models.py")
    go = _ask("Installera det nu? [J/n] ").lower()
    if go in ("n", "nej", "no"):
        return False
    if missing:
        subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                        os.path.join(HERE, "requirements.txt")], check=False)
    if need_npm:
        if not shutil.which("npm"):
            print("  [!!] Node.js saknas. Installera Node, sedan: npm install")
            return False
        subprocess.run(["npm", "install"], cwd=HERE, check=False)
    if need_model:
        subprocess.run([sys.executable, os.path.join(HERE, "setup_models.py")],
                       cwd=HERE, check=False)
    return not _missing_packages()


def _preview(mode):
    import config
    fmt = config.FORMATS[mode]
    if fmt["aspect"] == "9:16":
        fmt["w"], fmt["h"] = 320, 568
    else:
        fmt["w"], fmt["h"] = 568, 320
    config.MIN_SOURCE_HEIGHT = 0
    config.SOURCE_SCORE_MIN = 0.4
    config.SECTION_SECONDS = 12
    config.MAX_DOWNLOADS_PER_RUN = 2
    config.QUERY_CAPS[mode] = min(config.QUERY_CAPS.get(mode, 8), 4)
    config.ENCODE_PRESET = "ultrafast"
    config.ENCODE_CRF = 28
    config.OUTPUT_FPS = 24
    config.FORMATS[mode]["duration"] = 24 if mode != "short" else 20
    print(f"-- förhandsvisning {fmt['w']}x{fmt['h']}. Inte filen du laddar upp.")


def _run(mode, topic, preview=False):
    from orchestrator import run_autopilot
    if preview:
        _preview(mode)
    print(f"\nKör {mode}  |  {topic}")
    print("Research, manus, röst, grafik. Det tar en stund.\n")
    try:
        final = run_autopilot(mode, topic, niche="general")
    except KeyboardInterrupt:
        print("\nAvbruten.")
        return
    except Exception as e:
        print(f"\n[!!] {type(e).__name__}: {e}")
        return
    if final:
        print(f"\nKlar. Filen ligger här:\n  {final}")
    else:
        print("\nIngen film. Läs raderna ovan.")


def menu():
    os.chdir(HERE)
    print(MENU)
    if not shutil.which("ffmpeg"):
        print("\n[!!] ffmpeg saknas. Windows: gyan.dev full build, lägg på PATH.")
        print("     Testa:  ffmpeg -filters | findstr ass")
    if not _chrome():
        print("[!!] Chrome/Edge hittades inte. Sätt CHROME till exe-filen.")
    if not os.environ.get("CEREBRAS_API_KEY"):
        print("[??] Ingen CEREBRAS_API_KEY i det här fönstret.")
        print("     set CEREBRAS_API_KEY=csk-...     och starta om scriptet.")
    if not _fix_missing():
        print("Fixa det som saknas, kör python start.py igen.")
        return
    while True:
        print(MENU)
        choice = _ask("Val: ")
        if choice in ("0", "q", "exit", ""):
            if choice in ("0", "q", "exit"):
                return
            continue
        if choice == "6":
            import llm
            print("LLM:", llm.describe())
            print("self-test:", "PASS" if llm.self_test() else "FAIL")
            continue
        if choice == "5":
            sub = _ask("Vilken? 1 short  2 fem  3 tio  4 femton: ")
            mode = MODES.get(sub)
            if not mode:
                print("Okänt val.")
                continue
            topic = _ask("Ämne (eller auto): ") or "auto"
            _run(mode, topic, preview=True)
            continue
        mode = MODES.get(choice)
        if not mode:
            print("Okänt val.")
            continue
        topic = _ask("Ämne (eller auto): ") or "auto"
        _run(mode, topic, preview=False)


if __name__ == "__main__":
    menu()
