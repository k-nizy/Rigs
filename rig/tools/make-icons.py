#!/usr/bin/env python3
"""Generate the whole Rig 03 icon set from one geometry definition.

The mark is the app itself, reduced: three pedals sitting on the rail that
runs along the bottom of every screen, with the middle pedal — "go" on all
but the recording screen — carrying the signal colour. It is built on a
32-unit grid so every edge lands on a whole pixel at 16px and 32px, which
is where a favicon actually has to survive.

The proportions are set by that 16px case, not by how it looks large. An
earlier pass used a 3-unit rail and taller pedals; at 16px the rail
collapsed into a grey smear and the pedals blurred together. A 4-unit rail
(2 solid pixels) and shorter pedals survive the downsample.

Run:  python3 tools/make-icons.py
"""
from PIL import Image, ImageDraw

OUT = "assets"
SS = 8  # supersample factor, downsampled with LANCZOS for clean edges

INK_LIGHT = "#0b0b0c"   # on a light tab strip / light page
INK_DARK  = "#f2f3f5"   # on a dark tab strip / dark page
ACCENT    = "#ffc400"   # --hail. Set to the INK value for a monochrome mark.
GROUND    = "#ffffff"

# (x, y, w, h, radius) on a 32-unit grid. `accent` marks the middle pedal.
PEDAL_L = (2,  5,  8, 14, 2.0)
PEDAL_M = (12, 5,  8, 14, 2.0)
PEDAL_R = (22, 5,  8, 14, 2.0)
RAIL    = (2,  23, 28, 4, 1.5)


def draw(size, ink, accent, ground=None, inset=1.0):
    """Render the mark. `inset` shrinks the glyph about the centre, which is
    how the maskable icon keeps clear of a launcher's crop.

    A `ground` produces an opaque RGB image with no alpha channel at all —
    iOS composites stray alpha against black, so the home-screen and
    installed-app plates must not carry one."""
    s = size * SS
    img = Image.new("RGBA", (s, s), ground or (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = s / 32.0

    def rect(box, fill):
        x, y, w, h, r = box
        cx = cy = 16.0
        x, y = cx + (x - cx) * inset, cy + (y - cy) * inset
        w, h, r = w * inset, h * inset, r * inset
        d.rounded_rectangle([x * u, y * u, (x + w) * u, (y + h) * u],
                            radius=r * u, fill=fill)

    rect(PEDAL_L, ink)
    rect(PEDAL_M, accent)
    rect(PEDAL_R, ink)
    rect(RAIL, ink)
    img = img.resize((size, size), Image.LANCZOS)
    return img.convert("RGB") if ground else img


def svg():
    """Theme-adaptive vector favicon: transparent ground, ink flips on
    prefers-color-scheme so it reads on a dark and a light tab strip."""
    def r(box, cls):
        x, y, w, h, rad = box
        return (f'  <rect class="{cls}" x="{x:g}" y="{y:g}" '
                f'width="{w:g}" height="{h:g}" rx="{rad:g}"/>')
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" role="img" aria-label="Rig 03">
  <title>Rig 03</title>
  <style>
    .ink {{ fill: {INK_LIGHT}; }}
    .live {{ fill: {ACCENT}; }}
    @media (prefers-color-scheme: dark) {{
      .ink {{ fill: {INK_DARK}; }}
    }}
  </style>
{r(PEDAL_L, "ink")}
{r(PEDAL_M, "live")}
{r(PEDAL_R, "ink")}
{r(RAIL, "ink")}
</svg>
'''


made = []

# Tab favicons — transparent, one per theme.
for name, ink in (("icon-light", INK_LIGHT), ("icon-dark", INK_DARK)):
    for px in (16, 32):
        p = f"{OUT}/{name}-{px}.png"
        draw(px, ink, ACCENT).save(p)
        made.append(p)

# Legacy .ico at the site root, so a bare /favicon.ico probe never 404s.
ico = draw(64, INK_LIGHT, ACCENT)
ico.save("favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
made.append("favicon.ico")

# Vector favicon — the one modern browsers actually pick.
open(f"{OUT}/icon.svg", "w").write(svg())
made.append(f"{OUT}/icon.svg")

# iOS home screen: no transparency, slight breathing room.
p = f"{OUT}/apple-touch-icon.png"
draw(180, INK_LIGHT, ACCENT, ground=GROUND, inset=0.76).save(p)
made.append(p)

# Installed-app icons. A manifest has no theme switching, so these are a
# fixed white plate that sits correctly on a dark or a light launcher.
for px in (192, 512):
    p = f"{OUT}/icon-{px}.png"
    draw(px, INK_LIGHT, ACCENT, ground=GROUND, inset=0.84).save(p)
    made.append(p)

# Maskable: glyph inside the inner 80% the platform is allowed to crop to.
p = f"{OUT}/icon-maskable-512.png"
draw(512, INK_LIGHT, ACCENT, ground=GROUND, inset=0.58).save(p)
made.append(p)

for p in made:
    print(p)
