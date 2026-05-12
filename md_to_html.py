"""Render one of report.py's Markdown files into a styled, self-contained HTML.

  .venv/bin/python md_to_html.py reports/voice-20260511-2017.md
  → writes reports/voice-20260511-2017.html alongside it.

  .venv/bin/python md_to_html.py reports/voice-20260511-2017.md -o out.html
  → custom output path.

Highlights:
  - markdown-it-py for GFM tables + fenced code + emoji autolink-safe
  - inline CSS, no external assets (works offline / from file://)
  - score / verdict cells are auto-colored by regex post-pass:
      ✅ PASS → green, ❌ FAIL → red, 🚨 ERR → amber, · NONE → grey
  - <details> blocks rendered open-by-default for searchability
"""
from __future__ import annotations

import argparse
import html as html_lib
import re
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import sys
from pathlib import Path

from markdown_it import MarkdownIt


CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff;
  --fg: #1f2328;
  --muted: #59636e;
  --border: #d0d7de;
  --card: #f6f8fa;
  --code-bg: #eef1f5;
  --link: #0969da;
  --pass: #1a7f37;
  --fail: #cf222e;
  --err:  #bf8700;
  --none: #6e7681;
  --pass-bg: #dafbe1;
  --fail-bg: #ffebe9;
  --err-bg:  #fff8c5;
  --none-bg: #eaeef2;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117;
    --fg: #e6edf3;
    --muted: #8d96a0;
    --border: #30363d;
    --card: #161b22;
    --code-bg: #1f242c;
    --link: #58a6ff;
    --pass: #3fb950;
    --fail: #ff7b72;
    --err:  #d29922;
    --none: #8b949e;
    --pass-bg: #04341a;
    --fail-bg: #421118;
    --err-bg:  #3d2c04;
    --none-bg: #1f242c;
  }
}
* { box-sizing: border-box; }
html, body { background: var(--bg); color: var(--fg); }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
               "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  font-size: 14.5px;
  line-height: 1.55;
  margin: 0;
}
main {
  max-width: 1180px;
  margin: 0 auto;
  padding: 36px 28px 80px;
}
h1, h2, h3, h4 {
  border-bottom: 1px solid var(--border);
  padding-bottom: 6px;
  margin-top: 1.8em;
  margin-bottom: 0.7em;
  font-weight: 600;
}
h1 { font-size: 28px; margin-top: 0; }
h2 { font-size: 21px; }
h3 { font-size: 17px; border-bottom-style: dashed; }
h4 { font-size: 15px; border-bottom: none; color: var(--muted); }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
p, ul, ol { margin: 0.6em 0; }
ul, ol { padding-left: 1.6em; }
li { margin: 0.15em 0; }
code {
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.9em;
  background: var(--code-bg);
  padding: 2px 6px;
  border-radius: 4px;
  color: var(--fg);
}
pre {
  background: var(--code-bg);
  padding: 12px 14px;
  border-radius: 6px;
  overflow-x: auto;
}
pre code { background: none; padding: 0; }
blockquote {
  margin: 0.8em 0;
  padding: 4px 14px;
  border-left: 3px solid var(--border);
  color: var(--muted);
  background: var(--card);
  border-radius: 0 6px 6px 0;
}
details {
  margin: 0.8em 0;
  padding: 8px 14px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 6px;
}
details > summary {
  cursor: pointer;
  font-weight: 600;
  color: var(--fg);
}
details[open] > summary { margin-bottom: 0.4em; }
hr {
  border: 0;
  border-top: 1px solid var(--border);
  margin: 2em 0;
}
table {
  border-collapse: collapse;
  width: 100%;
  margin: 1em 0;
  font-size: 13.5px;
}
th, td {
  border: 1px solid var(--border);
  padding: 7px 10px;
  text-align: left;
  vertical-align: top;
}
th {
  background: var(--card);
  font-weight: 600;
  position: sticky;
  top: 0;
  z-index: 1;
}
tbody tr:nth-child(odd) { background: color-mix(in srgb, var(--card) 35%, transparent); }
tbody tr:hover { background: color-mix(in srgb, var(--link) 8%, transparent); }
td code, th code { background: transparent; padding: 0; font-size: 0.85em; }

/* verdict pills */
.pill {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  line-height: 1.55;
  letter-spacing: 0.02em;
  white-space: nowrap;
}
.pill.pass { color: var(--pass); background: var(--pass-bg); }
.pill.fail { color: var(--fail); background: var(--fail-bg); }
.pill.err  { color: var(--err);  background: var(--err-bg);  }
.pill.none { color: var(--none); background: var(--none-bg); }
.pill.inconclusive { color: #9a6700; background: #fff8c5; }
.pill.profile-signal { color: var(--pass); background: var(--pass-bg); }
.pill.profile-noisy  { color: #b35900; background: #ffe4cc; }
@media (prefers-color-scheme: dark) {
  .pill.inconclusive { color: #f0c674; background: #3a2f0a; }
  .pill.profile-noisy { color: #ffcb96; background: #3b240e; }
}

/* aggregate badge bar at the top of the report */
.totals {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin: 14px 0 22px;
}
.totals .pill { font-size: 13px; padding: 4px 12px; }

/* the rendered emoji prefix in a verdict pill is unnecessary; keep it for accessibility */
.muted { color: var(--muted); }
.right { text-align: right; }

/* "report meta" first ul: render as compact cards */
main > ul:first-of-type {
  list-style: none;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 8px;
  margin-bottom: 18px;
}
main > ul:first-of-type li {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 13px;
}

footer {
  margin-top: 40px;
  padding-top: 14px;
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-size: 12px;
}
"""


# Regex post-processors. Run after markdown-it has produced the HTML.
# We work on the rendered HTML string because GFM table cells are simple
# <td>...</td> blocks once converted.
_VERDICT_PATTERNS = [
    # exact-match badges inside cells: "✅ PASS" / "❌ FAIL" / "🚨 ERR" / "· NONE" / "🟡 INCONCLUSIVE"
    (re.compile(r"✅\s*PASS"), '<span class="pill pass">✅ PASS</span>'),
    (re.compile(r"❌\s*FAIL"), '<span class="pill fail">❌ FAIL</span>'),
    (re.compile(r"🚨\s*ERR"),  '<span class="pill err">🚨 ERR</span>'),
    (re.compile(r"·\s*NONE"),  '<span class="pill none">· NONE</span>'),
    (re.compile(r"🟡\s*INCONCLUSIVE"), '<span class="pill inconclusive">🟡 INCONCLUSIVE</span>'),
    # profile badges
    (re.compile(r"🟢\s*signal"), '<span class="pill profile-signal">🟢 signal</span>'),
    (re.compile(r"🟠\s*noisy"),  '<span class="pill profile-noisy">🟠 noisy</span>'),
    # bare uppercase verdicts in details bullets: " (PASS)" / " (FAIL)" etc.
    (re.compile(r"\((PASS)\)"), r'(<span class="pill pass">\1</span>)'),
    (re.compile(r"\((FAIL)\)"), r'(<span class="pill fail">\1</span>)'),
    (re.compile(r"\((ERROR)\)"), r'(<span class="pill err">\1</span>)'),
    (re.compile(r"\((NONE)\)"), r'(<span class="pill none">\1</span>)'),
    (re.compile(r"\((INCONCLUSIVE)\)"), r'(<span class="pill inconclusive">\1</span>)'),
]


def _build_totals_strip(md_text: str) -> str:
    """If the report's headline totals table is present, surface the four
    counters as quick-glance pills above the first <h2>."""
    m = re.search(
        r"\|\s*\*\*(\d+)\*\*\s*\|\s*\*\*(\d+)\*\*\s*\|\s*\*\*(\d+)\*\*\s*\|\s*\*\*(\d+)\*\*\s*\|\s*\*\*([\d.]+%)\*\*\s*\|",
        md_text,
    )
    if not m:
        return ""
    p, f, e, n, rate = m.groups()
    return (
        '<div class="totals">'
        f'<span class="pill pass">✓ PASS · {p}</span>'
        f'<span class="pill fail">✗ FAIL · {f}</span>'
        f'<span class="pill err">! ERROR · {e}</span>'
        f'<span class="pill none">· NONE · {n}</span>'
        f'<span class="pill" style="background:var(--code-bg);color:var(--fg);">通过率 {rate}</span>'
        "</div>"
    )


def render(md_text: str, title: str | None = None) -> str:
    md = (
        MarkdownIt("gfm-like", {"html": False, "linkify": False, "typographer": False})
        .enable("table")
        .enable("strikethrough")
    )
    body = md.render(md_text)

    # Open <details> blocks so reasons are searchable / printable without clicking
    body = body.replace("<details>", "<details open>")

    # Verdict pills
    for pat, repl in _VERDICT_PATTERNS:
        body = pat.sub(repl, body)

    # Inject the totals strip right after the first <h1>
    totals = _build_totals_strip(md_text)
    if totals:
        body = re.sub(r"(</h1>)", r"\1\n" + totals, body, count=1)

    title_html = html_lib.escape(title or "Mira 评测报告")
    return f"""<!doctype html>
<html lang="zh-Hans">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title_html}</title>
  <style>{CSS}</style>
</head>
<body>
<main>
{body}
<footer>由 md_to_html.py 渲染。完整 JSON 详情见同目录 .json 文件。</footer>
</main>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a report.py Markdown file into styled HTML.")
    ap.add_argument("md_path", help="path to the .md report")
    ap.add_argument("-o", "--out", help="output .html path (default: same basename, .html)")
    args = ap.parse_args()

    src = Path(args.md_path)
    if not src.is_file():
        print(f"not found: {src}", file=sys.stderr)
        sys.exit(2)

    out = Path(args.out) if args.out else src.with_suffix(".html")
    md_text = src.read_text(encoding="utf-8")
    title = next((l[2:].strip() for l in md_text.splitlines() if l.startswith("# ")), None)
    out.write_text(render(md_text, title=title), encoding="utf-8")
    print(f"✓ wrote {out}  ({out.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
