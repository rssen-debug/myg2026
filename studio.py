"""studio.py — one-shot builder for the studio assets.

Generates everything the "cinema look" needs, all from math and the filesystem:
  * .cube LUTs      -> assets/luts/     (spec-valid, one lut3d pass in ffmpeg)
  * cinematic SFX   -> assets/sfx/      (WAVs synthesised, no licensing issues)
  * font report     -> tells you which display fonts were found, and what to
                        drop into assets/fonts/ if the fallbacks are in use

Run:  python studio.py     (or menu option 9 in main.py)
"""
import os

import config


def font_report():
    """Which display font each role will actually resolve to."""
    import gfx
    roles = ["anton", "archivo", "bebas"]
    out = {}
    for r in roles:
        p = gfx._search(*gfx._CANDIDATES.get(r, []))
        fallback = False
        if not p:
            p = gfx._search(*gfx._GENERIC)
            fallback = True
        out[r] = (p, fallback)
    return out


def build_all(quiet=False):
    print("\n== STUDIO ASSETS ==")

    print("\n-- LUTs (.cube) --")
    import luts
    for name, p in luts.build_luts().items():
        ok, msg = luts.validate(p)
        print(f"  [{'ok' if ok else '!!'}] {name:14s} {msg}")

    print("\n-- SFX (synthesised WAVs) --")
    import sfx
    for name, p in sfx.build_all().items():
        y, sr = sfx._read_wav(p)
        print(f"  [ok] {name:12s} {len(y) / sr:5.2f}s")

    print("\n-- Fonts --")
    fonts = font_report()
    missing = []
    for role, (path, is_fallback) in fonts.items():
        shown = os.path.basename(path) if path else "NONE (bitmap default!)"
        tag = "fallback" if is_fallback else "exact"
        print(f"  [{tag:8s}] {role:8s} -> {shown}")
        if is_fallback:
            missing.append(role)
    if missing:
        print("\n  For the intended look, drop these .ttf files into "
              f"{os.path.join(config.ASSETS_DIR, 'fonts')}:")
        print("    Anton-Regular.ttf, ArchivoBlack-Regular.ttf, BebasNeue-Regular.ttf")
        print("  (free from Google Fonts - each is a single .ttf, license: OFL)")
    else:
        print("\n  All display fonts resolved exactly.")

    print("\n-- Status --")
    print(f"  USE_LUT          = {config.USE_LUT}")
    print(f"  SFX_ENABLED      = {config.SFX_ENABLED}"
          f"{'  (flip to True in config.py to mix the bed)' if not config.SFX_ENABLED else ''}")
    print(f"  THUMBNAIL_ENABLED= {config.THUMBNAIL_ENABLED}")
    print("\nSTUDIO ASSETS READY")


if __name__ == "__main__":
    build_all()
