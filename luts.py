"""luts.py — generate spec-valid 3D .cube LUTs for the studio grade.

Why a LUT instead of the chained eq/colorbalance in cinema.GRADE:
  * ffmpeg applies it in ONE pass (lut3d), so the grade is cheap
  * the same .cube file also works in DaVinci, Premiere, Lumetri, OBS
  * the look is a data file you can tweak without touching code

Correctness notes (these are the things generated LUTs usually get wrong):
  * .cube write order is b slowest, g middle, r FASTEST - one line per grid
    point, "R G B". Getting this wrong transposes the cube and the look breaks.
  * every value MUST be within 0.0-1.0. LUTs that emit -0.03 or 1.03 are out
    of spec and different tools handle them differently (clip, refuse, wrap).
    We clamp explicitly.
  * the tone curve is applied with a smoothstep, which is bounded in 0..1 by
    construction, so no clamping surprises.

Run:  python luts.py          -> assets/luts/*.cube
"""
import os

import numpy as np

import config

LUT_DIR = os.path.join(config.ASSETS_DIR, "luts")
SIZE = 33                      # standard 3D LUT edge length
LUMA = (0.2126, 0.7152, 0.0722)


SOFT = 0.45          # how much of the linear ramp survives the S-curve


def _scurve(x, pivot=0.44, contrast=1.06):
    """Contrast around a pivot, then a smoothstep for a filmic shoulder/toe.
    smoothstep is bounded in [0,1] by construction, so nothing can escape the
    legal range. It is blended with the linear ramp because a full smoothstep
    crushes mid-shadows (0.15 -> 0.048), which eats detail the grade is
    supposed to keep."""
    y = (x - pivot) * contrast + pivot
    y = np.clip(y, 0.0, 1.0)
    s = y * y * (3.0 - 2.0 * y)
    return (1.0 - SOFT) * s + SOFT * y


def _grade(r, g, b, teal, warm, sat, contrast, pivot):
    """Vectorised teal/shadow - orange/highlight grade. r/g/b in 0..1 arrays."""
    lr, lg, lb = LUMA
    luma = lr * r + lg * g + lb * b
    shadow_w = (1.0 - luma) ** 2        # strongest in the shadows
    high_w = luma ** 2                  # strongest in the highlights

    r2 = r - teal * shadow_w
    g2 = g + teal * 0.45 * shadow_w
    b2 = b + teal * shadow_w

    r2 = r2 + warm * high_w
    g2 = g2 + warm * 0.25 * high_w
    b2 = b2 - warm * 0.55 * high_w

    # luma-preserving desaturation
    l2 = lr * r2 + lg * g2 + lb * b2
    r2 = l2 + sat * (r2 - l2)
    g2 = l2 + sat * (g2 - l2)
    b2 = l2 + sat * (b2 - l2)

    return (_scurve(r2, pivot, contrast),
            _scurve(g2, pivot, contrast),
            _scurve(b2, pivot, contrast))


def write_cube(path, r, g, b, size=SIZE, title=""):
    """Write a .cube file. Arrays must be flat in (b,g,r) C-order, i.e. r
    varying fastest - exactly what _grid() produces."""
    n = size ** 3
    assert len(r) == n, f"expected {n} points, got {len(r)}"
    lo, hi = float(min(r.min(), g.min(), b.min())), float(max(r.max(), g.max(), b.max()))
    if lo < 0.0 or hi > 1.0:
        raise ValueError(f"LUT out of spec range: min={lo:.4f} max={hi:.4f}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f'TITLE "{title}"\n')
        f.write(f"LUT_3D_SIZE {size}\n")
        f.write("DOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n")
        for i in range(n):
            f.write(f"{r[i]:.6f} {g[i]:.6f} {b[i]:.6f}\n")
    return path


def _grid(size=SIZE):
    """Returns flat R,G,B arrays in .cube write order (r fastest)."""
    ax = np.linspace(0.0, 1.0, size)
    # meshgrid with indexing='ij' over (b, g, r) gives shape (size,size,size)
    # where axis0=b, axis1=g, axis2=r. C-order ravel => b slowest, r fastest.
    B, G, R = np.meshgrid(ax, ax, ax, indexing="ij")
    return R.ravel(), G.ravel(), B.ravel()


# ----------------------------------------------------------------- presets --
PRESETS = {
    "teal_orange": dict(
        teal=0.055, warm=0.045, sat=0.92, contrast=1.06, pivot=0.44,
        title="STUDIO teal-orange (documentary default)"),
    "cold_archive": dict(
        teal=0.090, warm=0.008, sat=0.62, contrast=1.02, pivot=0.47,
        title="STUDIO cold archive (context / archival beats)"),
    "heat_reveal": dict(
        teal=0.015, warm=0.085, sat=1.04, contrast=1.10, pivot=0.42,
        title="STUDIO heat reveal (shock / payoff beats)"),
}


def build_luts(outdir=None, size=SIZE):
    """Generate every preset. Returns {name: path}."""
    outdir = outdir or LUT_DIR
    r0, g0, b0 = _grid(size)
    paths = {}
    for name, p in PRESETS.items():
        kw = {k: v for k, v in p.items() if k != "title"}
        r, g, b = _grade(r0, g0, b0, **kw)
        paths[name] = write_cube(os.path.join(outdir, f"{name}.cube"),
                                 r, g, b, size, p["title"])
    return paths


def validate(path):
    """Re-read a .cube and check the things tools actually reject.
    Returns (ok, message)."""
    size, n, bad, vals = None, 0, 0, []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(("#", "TITLE", "DOMAIN_")):
                continue
            if line.startswith("LUT_3D_SIZE"):
                size = int(line.split()[1])
                continue
            parts = line.split()
            if len(parts) != 3:
                return False, f"line with {len(parts)} fields (expected 3)"
            try:
                v = [float(x) for x in parts]
            except ValueError:
                return False, f"non-numeric line: {line}"
            n += 1
            vals.extend(v)
            if any(x < 0.0 or x > 1.0 for x in v):
                bad += 1
    if size is None:
        return False, "missing LUT_3D_SIZE"
    if n != size ** 3:
        return False, f"expected {size**3} entries, found {n}"
    if bad:
        return False, f"{bad} entries outside 0.0-1.0"
    if max(vals) == min(vals):
        return False, "flat LUT (does nothing)"
    return True, f"ok: {size}^3 = {n} entries, range {min(vals):.3f}-{max(vals):.3f}"


def main():
    paths = build_luts()
    for name, p in paths.items():
        ok, msg = validate(p)
        print(f"  [{'ok' if ok else '!!'}] {os.path.basename(p):20s} {msg}")
    print(f"\nLUTs written to {LUT_DIR}")


if __name__ == "__main__":
    main()
