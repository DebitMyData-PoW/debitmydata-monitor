"""The DMD Stats app's icons (web/images): the DebitMyData pool's app icons (the DMD coin with a
red badge at the bottom right), with a white chart going up in the badge instead of the pickaxe,
so every DebitMyData app has the same design. Needs Pillow and numpy.

    python3 tools/make-app-icons.py <the pool's web/images/app folder> web/images/app

It writes the app icons (192, 512, 1024, the maskable ones for Android, the iPhone one), the
top bar's badge (web/images/dmd-stats-25/50/75.png) and web/favicon.ico.
"""

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

SRC = Path(sys.argv[1])
OUT = Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
RED = (200, 8, 21, 255)


def badge_geometry(im):
    a = np.array(im.convert("RGBA")).astype(int)
    red = (a[..., 0] > 180) & (a[..., 1] < 40) & (a[..., 2] < 50) & (a[..., 3] > 200)
    ys, xs = np.nonzero(red)
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    return (x0 + x1) / 2, (y0 + y1) / 2, max(x1 - x0, y1 - y0) / 2


def chart(draw, cx, cy, r, scale, width_factor=0.15):
    w = max(2, round(r * width_factor * scale))
    pts = [(-0.52, 0.36), (-0.17, -0.02), (0.08, 0.2), (0.5, -0.3)]
    P = [((cx + x * r) * scale, (cy + y * r) * scale) for x, y in pts]
    draw.line(P, fill=(255, 255, 255, 255), width=w, joint="curve")
    for p in P:
        draw.ellipse([p[0] - w / 2, p[1] - w / 2, p[0] + w / 2, p[1] + w / 2], fill=(255, 255, 255, 255))
    # the arrow's head at the end
    ex, ey = P[-1]
    for dx, dy in ((-0.30, 0.0), (0.0, 0.30)):
        q = (ex + dx * r * scale, ey + dy * r * scale)
        draw.line([P[-1], q], fill=(255, 255, 255, 255), width=w)
        draw.ellipse([q[0] - w / 2, q[1] - w / 2, q[0] + w / 2, q[1] + w / 2], fill=(255, 255, 255, 255))


def rebadge(name):
    im = Image.open(SRC / name).convert("RGBA")
    cx, cy, r = badge_geometry(im)
    S = 4
    big = im.resize((im.width * S, im.height * S), Image.LANCZOS)
    d = ImageDraw.Draw(big)
    rr = (r + 1.0) * S
    d.ellipse([cx * S - rr, cy * S - rr, cx * S + rr, cy * S + rr], fill=RED)
    chart(d, cx, cy, r, S)
    out = big.resize(im.size, Image.LANCZOS)
    out.save(OUT / name, optimize=True)


for name in [
    "icon-192.png",
    "icon-512.png",
    "icon-1024.png",
    "icon-maskable-512.png",
    "icon-maskable-1024.png",
    "apple-touch-icon.png",
]:
    rebadge(name)

# the top bar's brand: the badge alone (25, 50, 75 px)
for size in (25, 50, 75, 32, 16):
    S = 8
    n = size * S
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse([0, 0, n - 1, n - 1], fill=RED)
    chart(d, size / 2, size / 2, size / 2 * 0.98, S, 0.16)
    small = im.resize((size, size), Image.LANCZOS)
    if size in (25, 50, 75):
        small.save(OUT.parent / f"dmd-stats-{size}.png", optimize=True)
    else:
        small.save(OUT / f"favicon-{size}.png")
fav = [Image.open(OUT / f"favicon-{s}.png") for s in (16, 32)]
fav[1].save(OUT.parent.parent / "favicon.ico", sizes=[(16, 16), (32, 32)])
for s in (16, 32):
    (OUT / f"favicon-{s}.png").unlink()
print("ok")
