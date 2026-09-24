#!/usr/bin/env python3
"""FINAL2026: one-file Windows bootstrap + launcher.

Run:
    py FINAL2026.py

It installs missing Python dependencies, installs Node/Chrome/FFmpeg via
winget when needed, downloads the vision model, stores the Cerebras key in the
Windows user environment (never in Git), then launches start.py.
"""
from __future__ import annotations
import getpass, os, shutil, subprocess, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent
PY=Path(sys.executable)

def run(cmd, cwd=ROOT, check=True):
    print("\n>", " ".join(map(str,cmd)))
    return subprocess.run(cmd,cwd=str(cwd),check=check)

def have(cmd):
    return shutil.which(cmd) is not None

def add_windows_paths():
    candidates=[
        Path(r"C:\Program Files\nodejs"),
        Path(r"C:\Program Files\Google\Chrome\Application"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application"),
        Path(r"C:\Program Files\Microsoft\Edge\Application"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application"),
    ]
    old=os.environ.get("PATH","").split(os.pathsep)
    for p in candidates:
        if p.exists() and str(p) not in old:
            old.insert(0,str(p))
    os.environ["PATH"]=os.pathsep.join(old)

def winget_install(package_id):
    if not have("winget"):
        print(f"[!!] winget saknas. Installera manuellt: {package_id}")
        return False
    r=run(["winget","install","-e","--id",package_id],
          check=False)
    add_windows_paths()
    return r.returncode==0

def ensure_python_deps():
    run([str(PY),"-m","pip","install","--disable-pip-version-check","-r","requirements.txt"])
    # config.py can expose the imageio-ffmpeg fallback as bare ffmpeg.
    try:
        import config
        config._expose_ffmpeg_on_path()
    except Exception as e:
        print("[warn] config preflight:",e)

def ensure_node():
    add_windows_paths()
    if have("npm"):
        return True
    print("[setup] Node.js/npm saknas. Installerar Node.js LTS via winget.")
    ok=winget_install("OpenJS.NodeJS.LTS")
    add_windows_paths()
    if not have("npm"):
        print("[!!] npm kunde inte hittas efter installationen.")
        return ok and False
    return True

def ensure_browser():
    add_windows_paths()
    for exe in ("chrome","msedge","chromium","google-chrome","microsoft-edge"):
        if have(exe): return True
    known=[
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ]
    if any(p.exists() for p in known): return True
    print("[setup] Chrome/Edge saknas. Installerar Chrome via winget.")
    ok=winget_install("Google.Chrome")
    add_windows_paths()
    return ok

def ensure_ffmpeg():
    add_windows_paths()
    if have("ffmpeg"):
        return True
    # requirements.txt includes imageio-ffmpeg; the project can expose that
    # binary through config.py, so only use winget as a second option.
    try:
        import config
        if config.FFMPEG and Path(config.FFMPEG).exists():
            config._expose_ffmpeg_on_path()
            if have("ffmpeg"): return True
    except Exception:
        pass
    print("[setup] System-FFmpeg saknas. Försöker installera Gyan FFmpeg.")
    winget_install("Gyan.FFmpeg")
    add_windows_paths()
    try:
        import config
        config._expose_ffmpeg_on_path()
    except Exception:
        pass
    return have("ffmpeg") or bool(shutil.which("ffmpeg"))

def ensure_models():
    model=ROOT/"models"/"face_detection_yunet_2023mar.onnx"
    if model.exists(): return True
    p=ROOT/"setup_models.py"
    if not p.exists():
        print("[!!] setup_models.py saknas.")
        return False
    print("[setup] Laddar ner vision-modellen...")
    return run([str(PY),str(p)],check=False).returncode==0 and model.exists()

def persist_cerebras_key():
    key=os.environ.get("CEREBRAS_API_KEY","").strip()
    if not key:
        print("\nCerebras behövs för bästa autopilot-manus/research.")
        key=getpass.getpass("Klistra in CEREBRAS API key (döljs): ").strip()
        if key:
            os.environ["CEREBRAS_API_KEY"]=key
            if os.name=="nt":
                r=run(["setx","CEREBRAS_API_KEY",key],check=False)
                if r.returncode==0:
                    print("[ok] Nyckeln sparad i Windows user environment.")
                else:
                    print("[warn] Kunde inte spara permanent; den används i detta fönster.")
    os.environ.setdefault("CEREBRAS_MODEL","gpt-oss-120b")

def main():
    os.chdir(ROOT)
    print("\n==============================")
    print(" FINAL2026 — AUTOPILOT STUDIO")
    print("==============================")
    if os.name!="nt":
        print("[info] Det här bootstrap-flödet är gjort för Windows; Python-paketen kan ändå installeras.")
    ensure_python_deps()
    if not ensure_node():
        print("[!!] Node/npm krävs för grafiklagret. Kör filen igen efter Node-installation.")
        return 1
    run(["npm","install"],check=False)
    ensure_ffmpeg()
    if not ensure_browser():
        print("[!!] Chrome/Edge krävs för DOM/WebGL-renderingen.")
        return 1
    ensure_models()
    persist_cerebras_key()
    print("\n[OK] Miljön är redo. Startar FINAL2026-menyn...\n")
    return subprocess.call([str(PY),str(ROOT/"start.py")],cwd=str(ROOT))

if __name__=="__main__":
    raise SystemExit(main())
