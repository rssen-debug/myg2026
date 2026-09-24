"""CINEMA KIT — reusable ffmpeg building blocks for the "studio look".
Ported from sunnyv2youtube/scripts/cinema.py. Pure ffmpeg expression strings,
so it is OS-independent. Import: from cinema import GRADE, GRAIN, ease, ...
"""
import math  # noqa: F401  (kept for parity with upstream)

# ---------------- easing (F9/Easy Ease & overshoot) ----------------
GRADE = ("eq=contrast=1.12:saturation=1.16:gamma=0.98,"
         "colorbalance=rs=-0.045:bs=0.06:rm=0.02:bm=-0.03")
# filmic grain (t = temporal, u = uniform)
GRAIN = "noise=alls=6:allf=t+u"
# subtle vignette to pair with the grade
VIGNETTE = "vignette=angle=PI/5"


def beat(t, bpm=90, tol=None):
    """Nearest beat time on a bpm grid (for beat-synced hits/cuts).

    tol: with a tolerance, only snap when the point is already close to a beat
    and otherwise return t unchanged. Audio impacts want to land on the music's
    beat, but moving an impact by half a beat (~0.24 s at 126 bpm) would visibly
    desync it from the cut it belongs to. Snapping within a small tolerance
    gets the musical alignment without ever trading away picture sync.
    """
    step = 60.0 / bpm
    snapped = round(t / step) * step
    if tol is not None and abs(snapped - t) > tol:
        return t
    return snapped


# ---------------- PARALLAX (2.5D) ----------------
