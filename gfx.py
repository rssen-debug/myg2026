"""GFX KIT v2 — "million dollar studio" assets (Pillow), ported from
sunnyv2youtube/scripts/gfx_kit.py with a cross-platform font resolver
(original hard-coded a Linux DejaVu path).

Builds: glow titles, lower thirds, tweet/discord/phone UI mockups, stat cards,
timeline cards, bar charts, HUD frames, light leaks, circle portraits.

Every builder returns the absolute path of the PNG it wrote so the renderer can
overlay it. Pass ``outdir`` to control where assets land (defaults to ./assets/gfx).
"""
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

import config

ACCENT = (232, 34, 46, 255)          # sunnyv2 red
GFX_DIR = os.path.join(config.ASSETS_DIR, "gfx")
FONT_DIR = os.path.join(config.ASSETS_DIR, "fonts")

# ---------------------------------------------------------------- fonts ----
_CANDIDATES = {
    "anton":   ["Anton-Regular.ttf", "anton.ttf", "Impact.ttf", "impact.ttf"],
    "archivo": ["ArchivoBlack-Regular.ttf", "archivo.ttf", "arialbd.ttf",
                "Arial Bold.ttf"],
    "bebas":   ["BebasNeue-Regular.ttf", "bebas.ttf", "arialbd.ttf"],
    "body":    ["arial.ttf", "Arial.ttf", "DejaVuSans.ttf"],
}

_WIN_FONTDIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
_LINUX_DIRS = ["/usr/share/fonts/truetype/dejavu",
               "/usr/share/fonts/truetype/liberation",
               "/usr/share/fonts/truetype/msttcorefonts",
               "/usr/share/fonts/TTF",
               "/Library/Fonts", "/System/Library/Fonts"]

# Last-resort real fonts. cv2 ships DejaVu and opencv-contrib-python is a hard
# requirement of this pipeline, so that path exists on any working install.
# ImageFont.load_default() is a FIXED-SIZE bitmap font that silently ignores
# the requested px, so a missing font shows up as comically tiny text. We
# therefore prefer any real ttf we can find over the bitmap default.
_GENERIC = ["DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "LiberationSans-Bold.ttf",
            "LiberationSans-Regular.ttf", "Montserrat-Bold.ttf",
            "Roboto-Medium.ttf", "arialbd.ttf", "arial.ttf"]


def _extra_dirs():
    dirs = []
    try:
        import cv2
        dirs.append(os.path.join(os.path.dirname(cv2.__file__), "qt", "fonts"))
    except Exception:
        pass
    try:
        import matplotlib
        dirs.append(os.path.join(os.path.dirname(matplotlib.__file__),
                                 "mpl-data", "fonts", "ttf"))
    except Exception:
        pass
    return dirs


def _search(*names):
    """Find the first existing font file among `names` across known dirs."""
    places = ([FONT_DIR, _WIN_FONTDIR] + _LINUX_DIRS + _extra_dirs())
    for name in names:
        for d in places:
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
    return None


_warned = False


def F(px, which="anton"):
    """Resolve a display font by role. Order: role-specific candidates ->
    any generic real font -> bitmap default (with a loud warning, since that
    path renders text at a fixed tiny size regardless of `px`)."""
    global _warned
    p = _search(*_CANDIDATES.get(which, []))
    if not p:
        p = _search(*_GENERIC)
    if p:
        try:
            return ImageFont.truetype(p, px)
        except Exception:
            pass
    if not _warned:
        _warned = True
        print("  [gfx] WARNING: no TrueType font found - text will render at a "
              "fixed small size. Drop Anton/Bebas/Archivo .ttf files into "
              f"{FONT_DIR} for the intended look.")
    return ImageFont.load_default()


def _tsize(draw, text, font, stroke=0):
    bb = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
    return bb[2] - bb[0], bb[3] - bb[1]


def _save(img, out, outdir=None):
    d = outdir or GFX_DIR
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, out)
    img.save(path)
    return path


# ---------------------------------------------------------- glow title ----
def glow_title(out, text, px=170, fill=(255, 255, 255, 255),
               glow=ACCENT[:3] + (160,), stroke=10, pad=140, outdir=None):
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    f = F(px, "anton")
    w, h = _tsize(tmp, text, f, stroke)
    W, H = w + pad * 2, h + px + pad
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    x, y = (W - w) // 2, pad // 2 + int(px * 0.18)
    for radius, alpha in [(34, glow[3]), (12, min(255, glow[3] + 60))]:
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((x, y), text, font=f, fill=glow[:3] + (alpha,),
                                   stroke_width=stroke, stroke_fill=glow[:3] + (alpha,))
        layer = layer.filter(ImageFilter.GaussianBlur(radius))
        canvas = Image.alpha_composite(canvas, layer)
    d = ImageDraw.Draw(canvas)
    d.text((x, y), text, font=f, fill=fill, stroke_width=stroke,
           stroke_fill=(8, 8, 10, 255))
    return _save(canvas, out, outdir)


# ---------------------------------------------------------- lower third ----
def lower_third(out, main, sub="", accent=ACCENT, W=1080, outdir=None):
    d0 = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    fm, fs = F(72, "archivo"), F(40, "bebas")
    mw, mh = _tsize(d0, main, fm)
    sw, sh = (_tsize(d0, sub, fs) if sub else (0, 0))
    H = 36 + mh + (14 + sh if sub else 0) + 34
    card = Image.new("RGBA", (W, H + 20), (0, 0, 0, 0))
    dr = ImageDraw.Draw(card)
    dr.rounded_rectangle([26, 10, W - 6, H + 6], 14, fill=(12, 12, 14, 208))
    dr.rounded_rectangle([26, 10, 40, H + 6], 7, fill=accent)
    ox = 64
    dr.text((ox, 24), main, font=fm, fill=(255, 255, 255, 255))
    if sub:
        dr.text((ox, 24 + mh + 12), sub, font=fs, fill=(190, 190, 196, 235))
    return _save(card, out, outdir)


# -------------------------------------------------------------- tweet ------
def _heart(dr, cx, cy, r, fill):
    dr.ellipse([cx - r, cy - r, cx, cy], fill=fill)
    dr.ellipse([cx, cy - r, cx + r, cy], fill=fill)
    dr.polygon([(cx - r, cy - r * 0.35), (cx + r, cy - r * 0.35), (cx, cy + r)], fill=fill)


def tweet_ui(out, name, handle, body, meta, likes, avatar_path="", W=940, outdir=None):
    d0 = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    fn, fh, fb, fm = F(44, "archivo"), F(36, "bebas"), F(46, "archivo"), F(34, "bebas")
    words, lines, cur = body.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if _tsize(d0, t, fb)[0] > W - 120:
            lines.append(cur); cur = w
        else:
            cur = t
    lines.append(cur)
    av = 92
    H = 40 + av + 26 + len(lines) * (58 + 8) + 30 + 46 + 34
    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(card)
    dr.rounded_rectangle([0, 0, W - 1, H - 1], 26, fill=(21, 24, 28, 242),
                         outline=(70, 76, 84, 255), width=2)
    if avatar_path and os.path.exists(avatar_path):
        a = ImageOps.fit(Image.open(avatar_path).convert("RGB"), (av, av), Image.LANCZOS)
        m = Image.new("L", (av, av), 0)
        ImageDraw.Draw(m).ellipse([0, 0, av, av], fill=255)
        card.paste(a, (40, 40), m)
    nx = 40 + av + 26
    dr.text((nx, 44), name, font=fn, fill=(255, 255, 255, 255))
    nw = _tsize(dr, name, fn)[0]
    dr.text((nx + nw + 16, 52), handle, font=fh, fill=(120, 128, 138, 255))
    y = 40 + av + 26
    for ln in lines:
        dr.text((40, y), ln, font=fb, fill=(232, 234, 238, 255))
        y += 58 + 8
    y += 12
    dr.text((40, y), meta, font=fm, fill=(110, 118, 128, 255))
    hy = y + 66
    _heart(dr, 56, hy, 16, (249, 24, 128, 255))
    dr.text((84, hy - 18), likes, font=fh, fill=(249, 24, 128, 255))
    return _save(card, out, outdir)


# -------------------------------------------------------------- stat -------
def stat_card(out, big, label, accent=ACCENT, outdir=None):
    d0 = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    fb, fl = F(200, "anton"), F(46, "bebas")
    bw, bh = _tsize(d0, big, fb)
    lw, lh = _tsize(d0, label, fl)
    W = max(bw, lw) + 160
    H = 30 + bh + 18 + lh + 40
    c = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(c)
    dr.rectangle([(W - min(bw, 420)) // 2, 18, (W + min(bw, 420)) // 2, 24], fill=accent)
    dr.text(((W - bw) // 2, 40), big, font=fb, fill=(255, 255, 255, 255),
            stroke_width=8, stroke_fill=(8, 8, 10, 255))
    dr.text(((W - lw) // 2, 40 + bh + 18), label, font=fl, fill=(205, 205, 212, 245))
    return _save(c, out, outdir)


# -------------------------------------------------------------- misc -------
def discord_ui(out, channel, name, body, time, avatar_path="", W=920, outdir=None):
    """Discord-style message card - second flavour of social evidence."""
    d0 = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    fn, fb, ft = F(42, "archivo"), F(42, "archivo"), F(32, "bebas")
    words, lines, cur = body.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if _tsize(d0, t, fb)[0] > W - 140:
            lines.append(cur); cur = w
        else:
            cur = t
    lines.append(cur)
    av = 80
    H = 34 + av + 24 + len(lines) * 60 + 30
    c = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(c)
    dr.rounded_rectangle([0, 0, W - 1, H - 1], 24, fill=(28, 30, 36, 240),
                         outline=(64, 68, 76, 255), width=2)
    cw = _tsize(dr, channel, ft)[0]
    dr.rounded_rectangle([26, 14, 26 + cw + 30, 14 + 40], 8, fill=(52, 56, 64, 255))
    dr.text((41, 20), channel, font=ft, fill=(214, 218, 226, 255))
    if avatar_path and os.path.exists(avatar_path):
        a = ImageOps.fit(Image.open(avatar_path).convert("RGB"), (av, av), Image.LANCZOS)
        m = Image.new("L", (av, av), 0)
        ImageDraw.Draw(m).ellipse([0, 0, av, av], fill=255)
        c.paste(a, (36, 68), m)
    nx = 36 + av + 22
    dr.text((nx, 74), name, font=fn, fill=(255, 255, 255, 255))
    nw = _tsize(dr, name, fn)[0]
    dr.text((nx + nw + 14, 82), time, font=ft, fill=(114, 120, 130, 255))
    y = 68 + av + 24
    for ln in lines:
        dr.text((36, y), ln, font=fb, fill=(220, 223, 229, 255))
        y += 60
    return _save(c, out, outdir)


def youtube_ui(out, title, channel, views, age, avatar_path="", thumb_path=None,
               W=1040, outdir=None):
    """YouTube video card - the 'it really happened, here is the upload' proof."""
    d0 = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    ft, fm = F(46, "archivo"), F(34, "bebas")
    th_h = 300
    tw, th = _tsize(d0, title, ft)
    H = 30 + th_h + 22 + th + 14 + 44 + 26
    c = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(c)
    dr.rounded_rectangle([0, 0, W - 1, H - 1], 22, fill=(16, 16, 18, 244),
                         outline=(70, 70, 76, 255), width=2)
    if thumb_path and os.path.exists(thumb_path):
        t = ImageOps.fit(Image.open(thumb_path).convert("RGB"),
                         (W - 60, th_h), Image.LANCZOS)
        c.paste(t, (30, 30))
        dr.rectangle([30, 30, 30 + W - 60, 30 + th_h],
                     outline=(90, 90, 96, 255), width=2)
    y = 30 + th_h + 22
    if avatar_path and os.path.exists(avatar_path):
        a = ImageOps.fit(Image.open(avatar_path).convert("RGB"), (64, 64), Image.LANCZOS)
        m = Image.new("L", (64, 64), 0)
        ImageDraw.Draw(m).ellipse([0, 0, 64, 64], fill=255)
        c.paste(a, (30, y), m)
    dr.text((110, y + 2), title, font=ft, fill=(255, 255, 255, 255))
    y += th + 12
    dr.text((110, y), f"{channel}  ·  {views}  ·  {age}", font=fm,
            fill=(150, 152, 158, 255))
    return _save(c, out, outdir)


def phone_chat(out, contact, lines, W=620, outdir=None):
    """iMessage-style bubbles. lines: [(side, text)] with side 'me' or 'them'."""
    d0 = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    fh, fb = F(38, "archivo"), F(36, "archivo")
    bubbles, y = [], 26 + 54 + 20
    for side, text in lines:
        words, ls, cur = str(text).split(), [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if _tsize(d0, t, fb)[0] > W - 150:
                ls.append(cur); cur = w
            else:
                cur = t
        ls.append(cur)
        bh = len(ls) * 52 + 26
        bubbles.append((side, ls, bh))
        y += bh + 14
    H = y + 16
    c = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(c)
    dr.rounded_rectangle([0, 0, W - 1, H - 1], 34, fill=(9, 10, 12, 246),
                         outline=(60, 62, 70, 255), width=2)
    cw = _tsize(dr, contact, fh)[0]
    dr.text((W // 2 - cw // 2, 26), contact, font=fh, fill=(235, 235, 240, 255))
    dr.line([20, 90, W - 20, 90], fill=(60, 62, 70, 255), width=2)
    y = 106
    for side, ls, bh in bubbles:
        bw = max(_tsize(dr, l, fb)[0] for l in ls) + 52
        x = W - 30 - bw if side == "me" else 30
        col = (28, 88, 58, 245) if side == "me" else (34, 36, 42, 245)
        dr.rounded_rectangle([x, y, x + bw, y + bh], 20, fill=col)
        ty = y + 12
        for l in ls:
            dr.text((x + 26, ty), l, font=fb, fill=(240, 240, 244, 255))
            ty += 52
        y += bh + 14
    return _save(c, out, outdir)


def hud_frame(out, W=1920, H=1080, alpha=70, outdir=None):
    c = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(c)
    L, m, col = 46, 26, (255, 255, 255, alpha)
    for (x, y, dx, dy) in [(m, m, 1, 1), (W - m, m, -1, 1),
                           (m, H - m, 1, -1), (W - m, H - m, -1, -1)]:
        dr.line([(x, y), (x + dx * L, y)], fill=col, width=3)
        dr.line([(x, y), (x, y + dy * L)], fill=col, width=3)
    cx = W // 2
    dr.line([(cx - 14, 24), (cx + 14, 24)], fill=col, width=2)
    dr.line([(cx - 14, H - 24), (cx + 14, H - 24)], fill=col, width=2)
    return _save(c, out, outdir)


def light_leak(out, W=1920, H=1080, outdir=None):
    c = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    blob = Image.new("L", (W, H), 0)
    db = ImageDraw.Draw(blob)
    db.ellipse([-W * 0.25, H * 0.55, W * 0.45, H * 1.35], fill=46)
    db.ellipse([W * 0.72, -H * 0.25, W * 1.25, H * 0.35], fill=26)
    blob = blob.filter(ImageFilter.GaussianBlur(90))
    warm = Image.new("RGBA", (W, H), (255, 128, 60, 0))
    warm.putalpha(blob)
    c = Image.alpha_composite(c, warm)
    return _save(c, out, outdir)


def timeline_card(out, items, accent=ACCENT, W=1240, outdir=None):
    """items: [(date, label)]"""
    d0 = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    fd, fl = F(40, "bebas"), F(34, "bebas")
    y0, H = 130, 330
    step = (W - 160) // max(len(items) - 1, 1)
    c = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(c)
    dr.line([80, y0, W - 80, y0], fill=(200, 200, 206, 180), width=4)
    for i, (date, label) in enumerate(items):
        x = 80 + i * step
        dr.ellipse([x - 13, y0 - 13, x + 13, y0 + 13], fill=accent)
        dw = _tsize(dr, date, fd)[0]
        dr.text((x - dw // 2, y0 - 68), date, font=fd, fill=(255, 255, 255, 255))
        lw = _tsize(dr, label, fl)[0]
        dr.text((max(10, x - lw // 2), y0 + 28), label, font=fl, fill=(206, 206, 212, 240))
    return _save(c, out, outdir)


def bar_chart(out, data, accent=ACCENT, W=1000, H=620, outdir=None):
    """data: [(label, value)]"""
    d0 = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    fb, fl = F(44, "anton"), F(36, "bebas")
    mx = max(v for _, v in data)
    n, gap = len(data), 40
    bw = (W - 120 - gap * (n - 1)) // n
    base = H - 90
    c = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(c)
    for i, (lab, v) in enumerate(data):
        x = 60 + i * (bw + gap)
        bh = int((H - 260) * v / mx)
        dr.rounded_rectangle([x, base - bh, x + bw, base], 10, fill=accent)
        vw = _tsize(dr, str(v), fb)[0]
        dr.text((x + bw // 2 - vw // 2, base - bh - 56), str(v), font=fb,
                fill=(255, 255, 255, 255), stroke_width=4, stroke_fill=(8, 8, 10, 255))
        lw = _tsize(dr, lab, fl)[0]
        dr.text((x + bw // 2 - lw // 2, base + 14), lab, font=fl, fill=(206, 206, 212, 240))
    return _save(c, out, outdir)


def _paste_layer(size, img, xy):
    """Place an RGBA image onto a fresh transparent layer of `size` at xy."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    layer.paste(img, xy, img)
    return layer


def anamorphic_flare(out, W=1920, H=1080, tint=(70, 150, 255),
                     strength=1.0, seed=3, outdir=None):
    """Blue anamorphic lens flare: a horizontal streak through the centre plus
    a soft core glow and a couple of lens ghosts. Built with numpy so the
    gradients are smooth instead of banded - the naive draw.rectangle approach
    produces visible steps.

    Sized for the frame you pass it, so it works for both 9:16 and 16:9.
    """
    import numpy as np

    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    cx, cy = W / 2.0, H / 2.0

    # horizontal anamorphic streak: thin in Y, long in X, fades at the edges
    vert = np.exp(-((yy - cy) ** 2) / (2.0 * (H * 0.012) ** 2))
    horiz = np.exp(-((xx - cx) ** 2) / (2.0 * (W * 0.42) ** 2))
    streak = vert * horiz

    # soft core glow
    r2 = ((xx - cx) ** 2 + (yy - cy) ** 2) / (2.0 * (H * 0.16) ** 2)
    core = np.exp(-r2)

    # two faint lens ghosts offset along the optical axis
    ghosts = np.zeros_like(core)
    for gx, gy, gs, ga in ((0.28, 0.20, 0.055, 0.30), (-0.34, -0.24, 0.085, 0.18)):
        r = ((xx - (cx + gx * W)) ** 2 + (yy - (cy + gy * H)) ** 2)
        ghosts += ga * np.exp(-r / (2.0 * (H * gs) ** 2))

    alpha = np.clip((streak * 0.55 + core * 0.50 + ghosts) * strength, 0.0, 1.0)
    alpha = alpha * 255.0

    rgb = np.zeros((H, W, 3), dtype=np.float32)
    rgb[..., 0] = tint[0]
    rgb[..., 1] = tint[1]
    rgb[..., 2] = tint[2]

    # the core blows out towards white, the streak stays tinted
    core_w = np.clip(core * 0.55 * strength, 0, 1)[..., None]
    rgb = rgb * (1.0 - core_w) + 255.0 * core_w

    img = Image.fromarray(
        np.dstack([rgb.astype(np.uint8), alpha.astype(np.uint8)]), mode="RGBA")
    return _save(img, out, outdir)


def thumbnail(out, title_lines, kicker=None, badge=None, portrait=None,
              focus=None, zoom=1.0, W=1280, H=720, outdir=None):
    """YouTube thumbnail: dark plate, big display type, accent band, optional
    circular portrait and badge. Text comes from the story brief, so nothing
    is hard-coded to a language or a topic.

    title_lines: list of 1-3 short lines (uppercased automatically)
    kicker:      small line above the title
    badge:       short string in the accent box, e.g. "EXPOSED"
    portrait:    path to an image to cut into a circle on the right
    focus:       optional (x, y, w, h) face box so the circle centres on it
    zoom:        <1.0 tightens the portrait crop further
    """
    lines = [str(t).upper().strip() for t in title_lines if str(t).strip()][:3]
    plate = Image.new("RGB", (W, H), (9, 10, 13))
    dr = ImageDraw.Draw(plate)

    # subtle vertical gradient so it does not read as a flat black box
    for y in range(H):
        v = int(9 + 16 * (y / H))
        dr.line([(0, y), (W, y)], fill=(v, v, v + 3))

    # portrait circle on the right (if given) - built by circle_portrait so the
    # thumbnail, the video overlay and any future card all share one look
    text_w = W
    if portrait and os.path.exists(portrait):
        try:
            size = int(H * 0.62)
            tmp = os.path.join(outdir or ".", "_thumb_portrait.png")
            circle_portrait(portrait, os.path.basename(tmp), ring=ACCENT,
                            size=size, focus=focus, zoom=zoom,
                            outdir=os.path.dirname(tmp) or ".")
            cp = Image.open(tmp).convert("RGBA")
            px = W - cp.width - int(W * 0.02)
            py = (H - cp.height) // 2
            plate = Image.alpha_composite(
                plate.convert("RGBA"),
                _paste_layer((W, H), cp, (px, py))).convert("RGB")
            dr = ImageDraw.Draw(plate)
            text_w = px - int(W * 0.03)
            try:
                os.remove(tmp)
            except OSError:
                pass
        except Exception:
            text_w = W

    # accent band along the bottom
    dr.rectangle([0, H - 14, W, H], fill=ACCENT[:3])

    # title: shrink the font until every line fits the text column
    pad = int(W * 0.045)
    avail = text_w - pad * 2
    size = 150
    while size > 34:
        f = F(size, "anton")
        if all(_tsize(dr, ln, f, 6)[0] <= avail for ln in lines):
            break
        size -= 4
    f = F(size, "anton")
    fk = F(max(26, size // 4), "bebas")

    y = int(H * 0.16)
    if kicker:
        dr.text((pad, y), str(kicker).upper(), font=fk, fill=ACCENT[:3])
        y += _tsize(dr, str(kicker).upper(), fk)[1] + 16
    for ln in lines:
        dr.text((pad, y), ln, font=f, fill=(255, 255, 255),
                stroke_width=6, stroke_fill=(0, 0, 0))
        y += _tsize(dr, ln, f, 6)[1] + int(size * 0.16)

    if badge:
        bf = F(max(30, size // 3), "archivo")
        bt = str(badge).upper()
        bw, bh = _tsize(dr, bt, bf)
        bx, by = pad, H - bh - int(H * 0.13)
        dr.rectangle([bx, by, bx + bw + 34, by + bh + 22], fill=ACCENT[:3])
        dr.text((bx + 17, by + 11), bt, font=bf, fill=(255, 255, 255))
    return _save(plate, out, outdir)


def circle_portrait(src_path, out, ring=(255, 255, 255, 255), size=1000,
                    focus=None, zoom=1.0, outdir=None):
    """Circular portrait with shadow and ring.

    focus: optional (x, y, w, h) face box in source pixels. Without it the crop
    is centered, which on a wide frame leaves the face small and off-centre -
    the whole point of passing a detected face is to centre ON it. zoom < 1
    tightens the crop further around the focus.
    """
    img = ImageOps.exif_transpose(Image.open(src_path).convert("RGB"))
    w, h = img.size
    if focus:
        fx, fy, fw, fh = [float(v) for v in focus]
        cx, cy = fx + fw / 2.0, fy + fh / 2.0
        # square window that comfortably contains the face, scaled by zoom
        s = max(fw, fh) * 2.6 / max(zoom, 0.05)
        s = min(s, min(w, h) if min(w, h) > 0 else s)
        left = cx - s / 2.0
        top = cy - s / 2.0 - s * 0.06          # bias slightly upward: headroom
    else:
        s = min(w, h)
        left = (w - s) / 2.0
        top = (h - s) * 0.05 if h > w else 0.0
    s = max(8.0, min(s, max(w, h)))
    left = max(0.0, min(left, w - s))
    top = max(0.0, min(top, h - s))
    img = img.crop((int(left), int(top), int(left + s), int(top + s))).resize(
        (size, size), Image.LANCZOS)
    pad = 60
    canvas = Image.new("RGBA", (size + pad * 2, size + pad * 2), (0, 0, 0, 0))
    sh = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse([pad + 10, pad + 26, pad + size + 10, pad + size + 26],
                               fill=(0, 0, 0, 170))
    sh = sh.filter(ImageFilter.GaussianBlur(22))
    canvas = Image.alpha_composite(canvas, sh)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size, size], fill=255)
    canvas.paste(img, (pad, pad), mask)
    d = ImageDraw.Draw(canvas)
    bw = 22
    d.ellipse([pad - bw // 2, pad - bw // 2, pad + size + bw // 2, pad + size + bw // 2],
              outline=ring, width=bw)
    return _save(canvas, out, outdir)
