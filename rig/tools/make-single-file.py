#!/usr/bin/env python3
"""Inline the rig app into one self-contained HTML file.

The project is served as index.html + assets/ + ../shared/, which is right
for a real deploy. A single file is what you need to hand someone a link,
so this regenerates dist/rig.html from the actual current sources - never
edit the output by hand, it will be overwritten.

The pushed schedule is baked in too: a lone file has nothing to fetch.

Run:  python3 tools/make-single-file.py
"""
import json, re, pathlib

root = pathlib.Path(__file__).resolve().parent.parent

html     = (root / "index.html").read_text()
css      = (root / "assets/rig.css").read_text()
js       = (root / "assets/rig.js").read_text()
engine   = (root.parent / "shared/rotation-engine.js").read_text()
roster   = (root.parent / "shared/demo-roster.js").read_text()
schedule = json.loads((root / "schedule.json").read_text())

body = re.search(r"<body>\n(.*)\n</body>", html, re.S).group(1)
# Local script tags are replaced by the inlined sources below.
body = re.sub(r'^\s*<script src="[^"]*"[^>]*></script>\n?', "", body, flags=re.M)
fonts = "\n".join(re.findall(r'^<link rel="(?:preconnect|stylesheet)"[^>]*fonts\.[^>]*>$',
                             html, re.M))

out = f"""<meta charset="utf-8">
<title>{schedule['rigId']} Pedal Loop</title>
{fonts}

<style>
{css}</style>

{body}

<script>
{engine}</script>
<script>
{roster}</script>
<script>
/* The schedule the desk pushed to this rig, baked in at build time. */
window.PUSHED_SCHEDULE = {json.dumps(schedule, indent=2)};
</script>
<script>
{js}</script>
"""

leftovers = [m for m in re.findall(r'(?:href|src)="([^"]+)"', out)
             if not m.startswith(("http", "#"))]
assert not leftovers, f"single file still references local assets: {leftovers}"

(root / "dist/rig.html").write_text(out)
print(f"rig/dist/rig.html  {len(out):,} bytes, no local asset references")
