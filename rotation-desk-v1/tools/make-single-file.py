#!/usr/bin/env python3
"""Inline the desk app into one self-contained HTML file, for publishing.

Regenerates dist/rotation-desk.html from the current sources - never edit
the output by hand, it will be overwritten.

Run:  python3 tools/make-single-file.py
"""
import re, pathlib

# Every read and write below names its encoding. Without that they take
# the platform default, which is UTF-8 on Linux and cp1252 on Windows -
# so the same sources produced a valid file on one machine and a
# mojibaked one on the other, silently, with the difference only showing
# up in the em dashes.

def brand_css(repo):
    """packages/brand/brand.css, with the logo inlined as a data URI.

    Two things have to happen for a single file. The brand stylesheet is
    a separate <link> in the served app, so without this every var(--v-*)
    is undefined and the dist renders with no colours at all. And its
    mask url("logo.svg") resolves against the stylesheet when served and
    against dist/ when inlined, so the mark would silently vanish.
    """
    import base64
    css = (repo / "packages/brand/brand.css").read_text(encoding="utf-8")
    svg = (repo / "packages/brand/logo.svg").read_bytes()
    uri = "data:image/svg+xml;base64," + base64.b64encode(svg).decode()
    css = css.replace(chr(117)+chr(114)+chr(108)+chr(40)+chr(34)+"logo.svg"+chr(34)+chr(41),
                      chr(117)+chr(114)+chr(108)+chr(40)+chr(34)+uri+chr(34)+chr(41))
    assert "logo.svg" not in css, "a logo reference survived inlining"
    return css

root = pathlib.Path(__file__).resolve().parent.parent

html   = (root / "index.html").read_text(encoding="utf-8")
css    = (root / "assets/desk.css").read_text(encoding="utf-8")
js     = (root / "assets/desk.js").read_text(encoding="utf-8")
title  = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
repo   = root.parent
engine = (repo / "packages/engine/rotation-engine.js").read_text(encoding="utf-8")
roster = (repo / "packages/demo-roster/demo-roster.js").read_text(encoding="utf-8")

# The brand first, so the app's own rules can override it.
css = brand_css(repo) + chr(10) + css

body = re.search(r"<body>\n(.*)\n</body>", html, re.S).group(1)
# Local script tags are replaced by the inlined sources below.
body = re.sub(r'^\s*<script src="[^"]*"[^>]*></script>\n?', "", body, flags=re.M)
fonts = "\n".join(re.findall(r'^<link rel="(?:preconnect|stylesheet)"[^>]*fonts\.[^>]*>$',
                             html, re.M))

out = f"""<meta charset="utf-8">
<title>{title}</title>
{fonts}

<style>
{css}</style>

{body}

<script>
{engine}</script>
<script>
{roster}</script>
<script>
{js}</script>
"""

leftovers = [m for m in re.findall(r'(?:href|src)="([^"]+)"', out)
             if not m.startswith(("http", "#"))]
assert not leftovers, f"single file still references local assets: {leftovers}"

(root / "dist/rotation-desk.html").write_text(out, encoding="utf-8")
print(f"desk/dist/rotation-desk.html  {len(out):,} bytes, no local asset references")
