"""Generate a side-by-side HTML comparison of two report.py JSON results.

  .venv/bin/python compare_reports.py \
      --left  reports/voice-20260511-2017.json --left-label  "mina.ciwork.cn" \
      --right reports/voice-railway-XXXX.json  --right-label "mira-bff-preview" \
      -o reports/voice-compare.html

Matches goldens by `index` first, then by `scenario` substring fallback.
For each (golden, metric) pair shows: L score | R score | Δ | verdict-pair.

Δ-color heuristic:
  |Δ| < 0.10 → grey  (noise)
  0.10–0.30  → blue / orange (small drift)
  ≥ 0.30     → green if R improves, red if R regresses
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import sys
from pathlib import Path
from typing import Any


# ── data model ──────────────────────────────────────────────────────────────

def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _match_goldens(left: dict, right: dict) -> list[tuple[dict | None, dict | None]]:
    """Pair goldens across sides by index, then by scenario prefix."""
    L = {g["index"]: g for g in left.get("results", [])}
    R = {g["index"]: g for g in right.get("results", [])}
    indices = sorted(set(L) | set(R))
    return [(L.get(i), R.get(i)) for i in indices]


def _index_metrics(g: dict | None) -> dict[str, dict]:
    if not g:
        return {}
    return {f"{m['file']}|{m['metric']}": m for m in g.get("metrics", [])}


# ── rendering ───────────────────────────────────────────────────────────────

_VERDICT_PILL = {
    "PASS":  '<span class="pill pass">✅ PASS</span>',
    "FAIL":  '<span class="pill fail">❌ FAIL</span>',
    "ERROR": '<span class="pill err">🚨 ERR</span>',
    "NONE":  '<span class="pill none">· NONE</span>',
}


def _fmt_score(v: Any) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.2f}"
    except Exception:
        return html_lib.escape(str(v))


def _delta_html(l_score, r_score) -> tuple[str, str]:
    """Return (cell text, css class) for the Δ column."""
    if l_score is None or r_score is None:
        return "—", "delta-na"
    d = float(r_score) - float(l_score)
    arrow = "▲" if d > 0 else ("▼" if d < 0 else "▬")
    cls = "delta-flat"
    abs_d = abs(d)
    if abs_d < 0.10:
        cls = "delta-flat"
    elif abs_d < 0.30:
        cls = "delta-small-up" if d > 0 else "delta-small-down"
    else:
        cls = "delta-big-up" if d > 0 else "delta-big-down"
    return f"{arrow} {d:+.2f}", cls


def _golden_block(left_g: dict | None, right_g: dict | None, left_label: str, right_label: str) -> str:
    if left_g and right_g:
        idx = left_g.get("index", right_g.get("index"))
        scenario = left_g.get("scenario") or right_g.get("scenario") or ""
        tier = left_g.get("tier") or right_g.get("tier") or "—"
        category = left_g.get("category") or right_g.get("category") or "—"
    else:
        g = left_g or right_g or {}
        idx = g.get("index", "?")
        scenario = g.get("scenario", "")
        tier = g.get("tier", "—")
        category = g.get("category", "—")

    L = _index_metrics(left_g)
    R = _index_metrics(right_g)
    all_keys = sorted(set(L) | set(R), key=lambda k: (k.split("|")[0], k.split("|")[1]))

    rows: list[str] = []
    summary_l = {"PASS": 0, "FAIL": 0, "ERROR": 0, "NONE": 0}
    summary_r = {"PASS": 0, "FAIL": 0, "ERROR": 0, "NONE": 0}
    deltas: list[float] = []

    for key in all_keys:
        l = L.get(key)
        r = R.get(key)
        file_part, metric_part = key.split("|", 1)
        l_score = l.get("score") if l else None
        r_score = r.get("score") if r else None
        l_thr = (l or {}).get("threshold")
        r_thr = (r or {}).get("threshold")
        l_verdict = (l or {}).get("verdict") or "NONE"
        r_verdict = (r or {}).get("verdict") or "NONE"
        summary_l[l_verdict] = summary_l.get(l_verdict, 0) + 1
        summary_r[r_verdict] = summary_r.get(r_verdict, 0) + 1

        delta_text, delta_cls = _delta_html(l_score, r_score)
        if l_score is not None and r_score is not None:
            deltas.append(float(r_score) - float(l_score))

        thr = l_thr if l_thr is not None else r_thr
        thr_s = f"≥{thr:.2f}" if isinstance(thr, (int, float)) else "—"

        rows.append(
            "<tr>"
            f"<td><code>{html_lib.escape(file_part)}</code></td>"
            f"<td><code>{html_lib.escape(metric_part)}</code></td>"
            f"<td class='right'>{_fmt_score(l_score)}</td>"
            f"<td>{_VERDICT_PILL[l_verdict]}</td>"
            f"<td class='right'>{_fmt_score(r_score)}</td>"
            f"<td>{_VERDICT_PILL[r_verdict]}</td>"
            f"<td class='right {delta_cls}'>{html_lib.escape(delta_text)}</td>"
            f"<td class='right muted'>{thr_s}</td>"
            "</tr>"
        )

    # per-golden summary
    avg_l = sum(m.get("score") or 0 for m in L.values() if m.get("score") is not None)
    cnt_l = sum(1 for m in L.values() if m.get("score") is not None)
    avg_l = avg_l / cnt_l if cnt_l else None
    avg_r = sum(m.get("score") or 0 for m in R.values() if m.get("score") is not None)
    cnt_r = sum(1 for m in R.values() if m.get("score") is not None)
    avg_r = avg_r / cnt_r if cnt_r else None

    # Meta info side by side
    def _meta_card(g: dict | None, label: str) -> str:
        if not g:
            return f'<div class="meta-card empty"><div class="meta-label">{html_lib.escape(label)}</div>缺失</div>'
        return (
            f'<div class="meta-card"><div class="meta-label">{html_lib.escape(label)}</div>'
            f"<div><b>Mira 耗时：</b>{g.get('mira_elapsed_s', '—')}s</div>"
            f"<div><b>对话回合：</b>{g.get('n_user_turns', 0)}U / {g.get('n_assistant_turns', 0)}A</div>"
            f"<div><b>工具调用：</b>{g.get('n_tool_calls', 0)}</div>"
            f"<div><b>conv_id：</b><code>{html_lib.escape(str(g.get('conv_id') or ''))}</code></div>"
            + (f'<div class="muted">⚠ {html_lib.escape(", ".join(g.get("session_warnings") or [])[:200])}</div>' if g.get('session_warnings') else "")
            + (f'<div class="error-line">🛑 drive_error: {html_lib.escape(str(g.get("drive_error", ""))[:300])}</div>' if g.get('drive_error') else "")
            + "</div>"
        )

    return (
        f'<section class="golden">'
        f'<h3>[{idx}] {html_lib.escape(scenario)}</h3>'
        f'<div class="meta-row">'
        f'<span class="tag">tier: <b>{html_lib.escape(str(tier))}</b></span>'
        f'<span class="tag">category: <b>{html_lib.escape(str(category))}</b></span>'
        f'<span class="tag">L 平均分: <b>{_fmt_score(avg_l)}</b></span>'
        f'<span class="tag">R 平均分: <b>{_fmt_score(avg_r)}</b></span>'
        f'<span class="tag">Δ 平均: <b>{_fmt_score((avg_r - avg_l) if (avg_l is not None and avg_r is not None) else None)}</b></span>'
        f"</div>"
        f'<div class="meta-grid">{_meta_card(left_g, left_label)}{_meta_card(right_g, right_label)}</div>'
        f'<table class="cmp-table">'
        f'<thead><tr>'
        f'<th>文件</th><th>指标</th>'
        f'<th class="right">{html_lib.escape(left_label)} 分</th><th>{html_lib.escape(left_label)} 判定</th>'
        f'<th class="right">{html_lib.escape(right_label)} 分</th><th>{html_lib.escape(right_label)} 判定</th>'
        f'<th class="right">Δ</th><th class="right">阈值</th>'
        f"</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        f'<div class="summary-row">'
        f'<span class="muted">{html_lib.escape(left_label)}:</span> '
        f'<span class="pill pass">PASS {summary_l["PASS"]}</span> '
        f'<span class="pill fail">FAIL {summary_l["FAIL"]}</span> '
        f'<span class="pill err">ERR {summary_l["ERROR"]}</span> '
        f'<span class="pill none">NONE {summary_l["NONE"]}</span> '
        f'&nbsp;&nbsp;&nbsp;'
        f'<span class="muted">{html_lib.escape(right_label)}:</span> '
        f'<span class="pill pass">PASS {summary_r["PASS"]}</span> '
        f'<span class="pill fail">FAIL {summary_r["FAIL"]}</span> '
        f'<span class="pill err">ERR {summary_r["ERROR"]}</span> '
        f'<span class="pill none">NONE {summary_r["NONE"]}</span>'
        f"</div>"
        "</section>"
    )


CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #1f2328; --muted: #59636e; --border: #d0d7de;
  --card: #f6f8fa; --code-bg: #eef1f5; --link: #0969da;
  --pass: #1a7f37; --fail: #cf222e; --err: #bf8700; --none: #6e7681;
  --pass-bg: #dafbe1; --fail-bg: #ffebe9; --err-bg: #fff8c5; --none-bg: #eaeef2;
  --up: #1a7f37; --down: #cf222e; --up-bg: #dafbe1; --down-bg: #ffebe9;
  --up-bg-soft: #ecfdf3; --down-bg-soft: #fff5f4; --flat: #59636e;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --fg: #e6edf3; --muted: #8d96a0; --border: #30363d;
    --card: #161b22; --code-bg: #1f242c; --link: #58a6ff;
    --pass: #3fb950; --fail: #ff7b72; --err: #d29922; --none: #8b949e;
    --pass-bg: #04341a; --fail-bg: #421118; --err-bg: #3d2c04; --none-bg: #1f242c;
    --up: #3fb950; --down: #ff7b72; --up-bg: #04341a; --down-bg: #421118;
    --up-bg-soft: #0d2418; --down-bg-soft: #2a131a; --flat: #8d96a0;
  }
}
* { box-sizing: border-box; }
html, body { background: var(--bg); color: var(--fg); }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
               "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  font-size: 14px; line-height: 1.55; margin: 0;
}
main { max-width: 1400px; margin: 0 auto; padding: 32px 26px 80px; }
h1 { font-size: 26px; margin: 0 0 14px; border-bottom: 1px solid var(--border); padding-bottom: 8px; }
h2 { font-size: 19px; margin-top: 1.6em; border-bottom: 1px solid var(--border); padding-bottom: 6px; }
h3 { font-size: 16px; margin: 1.4em 0 0.5em; }
code {
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.9em; background: var(--code-bg);
  padding: 1px 6px; border-radius: 4px;
}
table { border-collapse: collapse; width: 100%; margin: 0.8em 0; font-size: 13px; }
th, td { border: 1px solid var(--border); padding: 6px 9px; text-align: left; }
.right { text-align: right; }
.muted { color: var(--muted); }
.tag {
  display: inline-block; padding: 2px 9px; margin: 0 6px 6px 0;
  background: var(--card); border: 1px solid var(--border); border-radius: 999px;
  font-size: 12.5px;
}
.pill {
  display: inline-block; padding: 1px 8px; border-radius: 999px;
  font-size: 11.5px; font-weight: 600; line-height: 1.5; white-space: nowrap;
}
.pill.pass { color: var(--pass); background: var(--pass-bg); }
.pill.fail { color: var(--fail); background: var(--fail-bg); }
.pill.err  { color: var(--err);  background: var(--err-bg);  }
.pill.none { color: var(--none); background: var(--none-bg); }
.totals { display: flex; flex-wrap: wrap; gap: 10px; margin: 14px 0 10px; }
.totals .pill { font-size: 13px; padding: 4px 12px; }
.meta-row { display: flex; flex-wrap: wrap; margin: 4px 0 10px; }
.meta-grid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 8px 0 12px;
}
.meta-card {
  background: var(--card); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 14px; font-size: 12.5px;
}
.meta-card.empty { color: var(--muted); }
.meta-label { font-weight: 700; font-size: 12px; color: var(--muted); margin-bottom: 4px; letter-spacing: 0.04em; }
.meta-card .error-line { color: var(--fail); margin-top: 4px; }
.summary-row { margin: 8px 0 0; font-size: 12.5px; }
section.golden { margin: 30px 0; padding: 18px 20px; background: var(--card); border: 1px solid var(--border); border-radius: 10px; }
section.golden > .cmp-table { background: var(--bg); }
section.golden table { background: var(--bg); }
section.golden thead th { background: var(--code-bg); position: sticky; top: 0; }
tbody tr:hover { background: color-mix(in srgb, var(--link) 7%, transparent); }

/* delta cell colors */
.delta-flat       { color: var(--flat); }
.delta-small-up   { color: var(--up);   background: var(--up-bg-soft); font-weight: 600; }
.delta-small-down { color: var(--down); background: var(--down-bg-soft); font-weight: 600; }
.delta-big-up     { color: var(--up);   background: var(--up-bg);   font-weight: 700; }
.delta-big-down   { color: var(--down); background: var(--down-bg); font-weight: 700; }
.delta-na         { color: var(--muted); }

.legend {
  background: var(--card); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 14px; margin: 12px 0; font-size: 12.5px;
}
.legend code { font-size: 11.5px; }

footer { margin-top: 40px; padding-top: 14px; border-top: 1px solid var(--border); color: var(--muted); font-size: 12px; }
"""


def render_html(left: dict, right: dict, left_label: str, right_label: str) -> str:
    pairs = _match_goldens(left, right)
    blocks = "\n".join(_golden_block(L, R, left_label, right_label) for L, R in pairs)

    # ── overall summary ─────────────────────────────────────────────────────
    def _agg(side: dict, side_label: str) -> dict:
        s = {"PASS": 0, "FAIL": 0, "ERROR": 0, "NONE": 0}
        scores: list[float] = []
        for g in side.get("results", []):
            for m in g.get("metrics", []):
                s[m.get("verdict") or "NONE"] = s.get(m.get("verdict") or "NONE", 0) + 1
                if m.get("score") is not None:
                    scores.append(float(m["score"]))
        s["avg"] = (sum(scores) / len(scores)) if scores else None
        s["label"] = side_label
        s["bff"] = side.get("meta", {}).get("bff_url", "—")
        s["generated"] = side.get("meta", {}).get("generated_at", "—")
        s["total_s"] = side.get("meta", {}).get("total_elapsed_s")
        return s

    Lagg = _agg(left, left_label)
    Ragg = _agg(right, right_label)

    def _agg_pills(a: dict) -> str:
        return (
            f'<div class="totals">'
            f'<span class="pill pass">{html_lib.escape(a["label"])} · ✓ {a["PASS"]}</span>'
            f'<span class="pill fail">✗ {a["FAIL"]}</span>'
            f'<span class="pill err">! {a["ERROR"]}</span>'
            f'<span class="pill none">· {a["NONE"]}</span>'
            f'<span class="pill" style="background:var(--code-bg);color:var(--fg)">平均 {_fmt_score(a["avg"])}</span>'
            f"</div>"
        )

    meta_table = (
        '<table style="margin: 8px 0;">'
        "<thead><tr><th></th><th>" + html_lib.escape(left_label) + "</th><th>" + html_lib.escape(right_label) + "</th></tr></thead>"
        "<tbody>"
        f"<tr><td>BFF</td><td><code>{html_lib.escape(str(Lagg['bff']))}</code></td><td><code>{html_lib.escape(str(Ragg['bff']))}</code></td></tr>"
        f"<tr><td>评测时间</td><td>{html_lib.escape(str(Lagg['generated']))}</td><td>{html_lib.escape(str(Ragg['generated']))}</td></tr>"
        f"<tr><td>整体耗时</td><td>{_fmt_score(Lagg['total_s'])}s</td><td>{_fmt_score(Ragg['total_s'])}s</td></tr>"
        "</tbody></table>"
    )

    legend = (
        '<div class="legend">'
        "Δ 着色：<span class='delta-flat'>|Δ|&lt;0.10 灰</span> · "
        "<span class='delta-small-up'>+0.10~0.30</span> / "
        "<span class='delta-small-down'>-0.10~-0.30</span> · "
        "<span class='delta-big-up'>+0.30↑ 显著进步</span> / "
        "<span class='delta-big-down'>-0.30↑ 显著下滑</span>"
        "</div>"
    )

    return f"""<!doctype html>
<html lang="zh-Hans">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Mira 评测横向对比 · {html_lib.escape(left_label)} vs {html_lib.escape(right_label)}</title>
  <style>{CSS}</style>
</head>
<body>
<main>
<h1>Mira 评测横向对比</h1>
<p class="muted">左：<b>{html_lib.escape(left_label)}</b> · 右：<b>{html_lib.escape(right_label)}</b></p>
<h2>总评</h2>
{_agg_pills(Lagg)}
{_agg_pills(Ragg)}
{meta_table}
{legend}
<h2>逐用例对比</h2>
{blocks}
<footer>由 compare_reports.py 渲染。</footer>
</main>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Compare two report.py JSON results into a side-by-side HTML.")
    ap.add_argument("--left", required=True, help="path to left-side .json")
    ap.add_argument("--right", required=True, help="path to right-side .json")
    ap.add_argument("--left-label", default="left", help="label for left side")
    ap.add_argument("--right-label", default="right", help="label for right side")
    ap.add_argument("-o", "--out", required=True, help="output .html path")
    args = ap.parse_args()

    left = _load(Path(args.left))
    right = _load(Path(args.right))
    html_out = render_html(left, right, args.left_label, args.right_label)
    Path(args.out).write_text(html_out)
    print(f"✓ wrote {args.out}  ({Path(args.out).stat().st_size/1024:.1f} KB)")


if __name__ == "__main__":
    main()
