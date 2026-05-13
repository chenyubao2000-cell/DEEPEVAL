"""Replay current audit / verdict / share-URL logic over an existing report
JSON. Lets you "refresh" historical reports after evaluator changes WITHOUT
re-running the judge (zero token cost).

What gets refreshed in the JSON:
  • metric.audit_warning  ← _audit_judge_consistency on (score,thr,success,reason)
  • metric.verdict        ← _verdict(MetricResult) after audit applied
  • result.share_url      ← only with --add-share-urls; creates a viewer share
                            via /api/tasks/<conv_id>/share if not already set

What it does NOT do:
  • Re-call the Mira BFF — assistant turns / tool calls / reasons are frozen
  • Re-prompt the judge — score / reason values stay as recorded
  • Change expected_outcome / dataset fields — those affect future runs only

Usage:
  # In-place refresh of one report
  .venv/bin/python replay_audit.py reports/cci-mina.json

  # Refresh + regenerate the .md and .html beside it
  .venv/bin/python replay_audit.py reports/cci-mina.json --render

  # Refresh + backfill share URLs for goldens that don't have one
  .venv/bin/python replay_audit.py reports/cci-mina.json --add-share-urls --render

  # Glob over many
  .venv/bin/python replay_audit.py reports/cci-*.json --render

  # Write to a sibling file instead of in-place
  .venv/bin/python replay_audit.py reports/cci-mina.json --out reports/cci-mina-v2.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from glob import glob
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Make tests/evals/ importable so report.py + its peers can resolve their imports.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "evals"))

from _env import load_env  # type: ignore[import-not-found]

import report  # type: ignore[import-not-found]
from report import (
    GoldenResult,
    MetricResult,
    _audit_judge_consistency,
    _derive_task_url_base,
    _ensure_share_url,
    _profile_for_label,
    _verdict,
    write_json,
    write_markdown,
)


def _coerce_metric_result(m: dict) -> MetricResult:
    """Rebuild a MetricResult from a JSON-dict metric entry."""
    profile = m.get("profile") or _profile_for_label(m["metric"], m.get("cls", ""))
    return MetricResult(
        file=m["file"],
        scope=m["scope"],
        metric=m["metric"],
        cls=m.get("cls", ""),
        score=m.get("score"),
        threshold=m.get("threshold"),
        success=m.get("success"),
        reason=m.get("reason"),
        error=m.get("error"),
        elapsed_s=m.get("elapsed_s") or 0.0,
        n_cases=m.get("n_cases", 1),
        profile=profile,
        audit_warning=m.get("audit_warning"),
    )


def _refresh_metric(mr: MetricResult) -> tuple[str | None, str]:
    """Apply current audit + verdict. Returns (new_audit_warning, new_verdict)."""
    new_audit = _audit_judge_consistency(mr.score, mr.threshold, mr.success, mr.reason)
    mr.audit_warning = new_audit
    return new_audit, _verdict(mr)


def _rebuild_golden_result(g: dict) -> GoldenResult:
    """JSON-dict → GoldenResult. Needed by write_markdown / write_json."""
    metrics: list[MetricResult] = []
    for m in g.get("metrics", []):
        mr = _coerce_metric_result(m)
        new_audit, new_v = _refresh_metric(mr)
        metrics.append(mr)
        # Mirror the refreshed values back into the JSON dict so write_json
        # serialises the new state.
        m["audit_warning"] = new_audit
        m["verdict"] = new_v
    return GoldenResult(
        index=g["index"],
        scenario=g["scenario"],
        tier=g.get("tier"),
        category=g.get("category"),
        mira_elapsed_s=g.get("mira_elapsed_s") or 0,
        n_user_turns=g.get("n_user_turns", 0),
        n_assistant_turns=g.get("n_assistant_turns", 0),
        n_tool_calls=g.get("n_tool_calls", 0),
        tools_observed=g.get("tools_observed", []),
        conv_id=g.get("conv_id", ""),
        session_warnings=g.get("session_warnings") or [],
        metric_results=metrics,
        drive_error=g.get("drive_error"),
        share_url=g.get("share_url"),
    )


def replay_one(src: Path, *, out: Path | None, render: bool, add_share_urls: bool, env: str | None) -> dict:
    """Replay audit + (optionally) share URLs over one report JSON. Returns
    a stats dict for the CLI summary."""
    data = json.loads(src.read_text(encoding="utf-8"))
    stats = {"src": str(src), "verdict_flips": [], "share_backfills": 0, "audit_changes": 0}

    # Add share URLs first so write_json picks up the new field
    if add_share_urls:
        # share API lives on the env the run was against — derive from meta.bff_url
        # if present, otherwise rely on whatever env vars are already set
        meta_bff = (data.get("meta") or {}).get("bff_url")
        if meta_bff:
            os.environ["MIRA_BFF_URL"] = meta_bff
        for g in data.get("results", []):
            if g.get("share_url"):
                continue
            url = _ensure_share_url(g.get("conv_id") or "")
            if url:
                g["share_url"] = url
                stats["share_backfills"] += 1
                print(f"  [{g['index']}] share + {url[:80]}")

    # Apply audit to every metric, count flips
    results: list[GoldenResult] = []
    for g in data["results"]:
        for m in g.get("metrics", []):
            old_audit = m.get("audit_warning")
            old_verdict = m.get("verdict")
            # _rebuild_golden_result mutates m for us, but we want per-metric tracking
            # so do it inline first then trust _rebuild_golden_result to repeat
            mr = _coerce_metric_result(m)
            new_audit, new_v = _refresh_metric(mr)
            if new_audit != old_audit:
                stats["audit_changes"] += 1
            if new_v != old_verdict:
                stats["verdict_flips"].append((g["index"], m["metric"], old_verdict, new_v))
                print(f"  [{g['index']}] {m['metric'][:50]}  {old_verdict} → {new_v}  ({(new_audit or '')[:60]})")
        results.append(_rebuild_golden_result(g))

    # Decide where to write
    out_json = out or src
    meta = data.get("meta", {}).copy()
    meta.setdefault("env", "preview")
    meta.setdefault("langfuse_host", "?")
    meta.setdefault("n_available_tools", 0)
    meta.setdefault("tools_source", "?")
    meta["task_url_base"] = meta.get("task_url_base") or _derive_task_url_base()
    meta["json_path"] = out_json.name
    write_json(results, out_json, meta)
    print(f"  ✓ wrote {out_json}")

    # Optional render: regenerate .md and .html alongside
    if render:
        md_path = out_json.with_suffix(".md")
        write_markdown(results, md_path, meta)
        print(f"  ✓ wrote {md_path}")
        try:
            subprocess.run(
                [sys.executable, str(ROOT / "md_to_html.py"), str(md_path)],
                check=True, capture_output=True,
            )
            print(f"  ✓ wrote {md_path.with_suffix('.html')}")
        except Exception as e:
            print(f"  ⚠ md_to_html failed: {e}")

    return stats


def main() -> None:
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
    )
    ap.add_argument("paths", nargs="+", help="report .json file(s) or glob(s)")
    ap.add_argument("--out", type=Path, help="output path (single-file mode only). Default: in-place")
    ap.add_argument("--render", action="store_true",
                    help="also regenerate .md and .html alongside the JSON")
    ap.add_argument("--add-share-urls", action="store_true",
                    help="create + persist viewer share URLs for goldens that don't have one yet")
    ap.add_argument("--env", help="load .env.<name> first (for --add-share-urls auth)")
    args = ap.parse_args()

    # Resolve glob patterns
    files: list[Path] = []
    for pat in args.paths:
        matched = sorted(glob(pat))
        if not matched:
            print(f"⚠ no match for {pat}", file=sys.stderr)
            continue
        for m in matched:
            files.append(Path(m))
    if not files:
        sys.exit(2)

    if args.out and len(files) > 1:
        print("--out only works with a single input file", file=sys.stderr)
        sys.exit(2)

    if args.env:
        load_env(args.env)
    elif args.add_share_urls:
        # need *some* env to talk to BFF; default to whatever .env.preview / .env loads
        try:
            load_env(None)
        except Exception as e:
            print(f"warning: load_env failed ({e}); --add-share-urls may not work", file=sys.stderr)

    total_flips = 0
    total_share = 0
    for f in files:
        print(f"\n── {f}")
        s = replay_one(
            f, out=args.out, render=args.render,
            add_share_urls=args.add_share_urls, env=args.env,
        )
        total_flips += len(s["verdict_flips"])
        total_share += s["share_backfills"]

    print()
    print("=" * 70)
    print(f"DONE  ({len(files)} file(s))")
    print(f"  verdict flips:        {total_flips}")
    print(f"  share URLs backfilled: {total_share}")


if __name__ == "__main__":
    main()
