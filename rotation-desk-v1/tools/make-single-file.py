#!/usr/bin/env python3
"""Inline the desk app into one self-contained HTML file, for publishing.

Regenerates dist/rotation-desk.html from the current sources - never edit
the output by hand, it will be overwritten.

Run:  python3 tools/make-single-file.py
"""
import re, pathlib

root = pathlib.Path(__file__).resolve().parent.parent

html   = (root / "index.html").read_text()
css    = (root / "assets/desk.css").read_text()
js     = (root / "assets/desk.js").read_text()
title  = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
repo   = root.parent
engine = (repo / "packages/engine/rotation-engine.js").read_text()
roster = (repo / "packages/demo-roster/demo-roster.js").read_text()

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

(root / "dist/rotation-desk.html").write_text(out)
print(f"desk/dist/rotation-desk.html  {len(out):,} bytes, no local asset references")
