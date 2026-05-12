"""Unified Mira health-check: 1 golden × every metric, one Mira call total.

Drives Mira once for a single golden, then runs every metric defined across
the 5 test files against that captured session. Produces a single consolidated
report so you can tell at a glance which metrics work, which error, and what
the judge said about each.

Usage:
    .venv/bin/python healthcheck.py             # default golden (#6 salary research)
    .venv/bin/python healthcheck.py --golden 0  # by index
    .venv/bin/python healthcheck.py --golden "硅谷"  # by scenario substring
"""
from __future__ import annotations

import argparse
import json
import sys
import time

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path

# Make tests/evals/ importable as a package
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "evals"))

from _driver import (  # type: ignore[import-not-found]
    build_conversational,
    drive_mira,
    explode_to_llm_cases,
    load_goldens,
)

# Pull each test file's metric definitions (reuse, single source of truth)
import test_mira_e2e as f_e2e            # type: ignore[import-not-found]
import test_mira_custom as f_custom      # type: ignore[import-not-found]
import test_mira_tooluse as f_tooluse    # type: ignore[import-not-found]
import test_mira_safety as f_safety      # type: ignore[import-not-found]
import test_mira_others as f_others      # type: ignore[import-not-found]


# Map: metric instance -> (file_label, scope)  where scope = 'multi' | 'single'
def _collect_metrics():
    rows = []
    for m in f_e2e.METRICS:
        rows.append(("e2e", "multi", m))
    for m in f_custom.METRICS:
        rows.append(("custom", "multi", m))
    for m in f_tooluse.MULTI_TURN_METRICS:
        rows.append(("tooluse", "multi", m))
    rows.append(("tooluse", "single", f_tooluse.ARG_CORRECTNESS))
    for m in f_safety.METRICS:
        rows.append(("safety", "single", m))
    for m in f_others.METRICS:
        rows.append(("others", "single", m))
    return rows


def _pick_golden(spec: str | None) -> dict:
    goldens = load_goldens()
    if spec is None:
        # Default: golden #6 (Silicon Valley AI PM salary research) — light, text-only
        for g in goldens:
            if "薪酬" in (g.get("scenario") or ""):
                return g
        return goldens[0]
    if spec.isdigit():
        return goldens[int(spec)]
    matches = [g for g in goldens if spec in (g.get("scenario") or "")]
    if not matches:
        raise SystemExit(f"No golden matches {spec!r}; available scenarios:\n" + "\n".join(f"  - {g['scenario'][:80]}" for g in goldens))
    return matches[0]


def _metric_name(m) -> str:
    name = getattr(m, "__name__", None)
    if isinstance(name, str):
        return name
    return type(m).__name__


def _run_one(metric, test_case) -> dict:
    """Synchronously score one metric on one case. Capture score/error/reason."""
    t0 = time.time()
    try:
        metric.measure(test_case)
        return {
            "score": getattr(metric, "score", None),
            "threshold": getattr(metric, "threshold", None),
            "success": getattr(metric, "success", None),
            "reason": getattr(metric, "reason", None),
            "error": getattr(metric, "error", None),
            "elapsed": time.time() - t0,
        }
    except Exception as e:
        return {
            "score": None, "threshold": getattr(metric, "threshold", None),
            "success": False, "reason": None,
            "error": f"{type(e).__name__}: {e}", "elapsed": time.time() - t0,
        }


def _fmt_score(s):
    if s is None:
        return "  None  "
    try:
        return f"{float(s):>5.2f}  "
    except Exception:
        return f"{s!s:>7}"


def _verdict(r):
    """Use metric.success (set by DeepEval with correct polarity for inverse
    metrics like Bias/Toxicity). Fall back to score>=threshold only when
    success is None (e.g. ToolUseMetric's apparent bug where success is unset).
    """
    if r["error"]:
        return "ERROR"
    success = r.get("success")
    if success is True:
        return "PASS "
    if success is False:
        return "FAIL "
    # success is None -> degrade gracefully using score vs threshold
    s = r["score"]
    t = r["threshold"]
    if s is None:
        return "NONE "
    try:
        return "PASS " if float(s) >= float(t) else "FAIL "
    except Exception:
        return "?    "


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=None, help="scenario substring or numeric index")
    args = ap.parse_args()

    golden = _pick_golden(args.golden)
    print("=" * 100)
    print(f"GOLDEN: {golden.get('scenario')}")
    print(f"TIER:   {golden.get('_tier')}")
    print(f"INPUTS: {len(golden['user_inputs'])} message(s)")
    print(f"OUTCOME: {golden.get('expected_outcome', '')[:200]}")
    print("=" * 100)

    print("\n[1/3] Driving Mira (one call)...")
    t0 = time.time()
    session, turns = drive_mira(golden)
    mira_elapsed = time.time() - t0
    n_user = sum(1 for t in turns if t.role == "user")
    n_asst = sum(1 for t in turns if t.role == "assistant")
    n_tool = sum(len(t.tools_called or []) for t in turns if t.role == "assistant")
    print(f"  Mira done in {mira_elapsed:.1f}s -> {n_user} user / {n_asst} assistant turns, {n_tool} tool call(s)")
    if session.warnings:
        print(f"  ⚠ session warnings: {session.warnings}")
    print(f"  conv_id: {session.conversation_id}")
    if n_tool:
        print("  tools observed:", [tc.name for t in turns for tc in (t.tools_called or [])])

    conv_case = build_conversational(golden, turns)
    llm_cases = explode_to_llm_cases(turns, scenario=golden.get("scenario"))
    print(f"  built 1 ConversationalTestCase + {len(llm_cases)} LLMTestCase(s)")

    print("\n[2/3] Scoring all metrics...")
    rows = _collect_metrics()
    results = []
    for file_label, scope, metric in rows:
        name = _metric_name(metric)
        cls = type(metric).__name__
        if scope == "multi":
            r = _run_one(metric, conv_case)
            r.update({"file": file_label, "scope": "multi", "name": name, "cls": cls, "n_cases": 1})
            print(f"  [{file_label}/multi] {cls}({name}): score={_fmt_score(r['score']).strip()} in {r['elapsed']:.1f}s {'✗' if r['error'] else '✓'}")
            results.append(r)
        else:
            # single-turn: average score over all LLM cases (usually 1 for these goldens)
            per_case = []
            for lc in llm_cases:
                per_case.append(_run_one(metric, lc))
            scores = [pc["score"] for pc in per_case if pc["score"] is not None]
            errors = [pc["error"] for pc in per_case if pc["error"]]
            agg = {
                "file": file_label, "scope": "single", "name": name, "cls": cls,
                "n_cases": len(llm_cases),
                "score": (sum(scores) / len(scores)) if scores else None,
                "threshold": getattr(metric, "threshold", None),
                "success": all(pc.get("success") for pc in per_case) if per_case else None,
                "reason": next((pc["reason"] for pc in per_case if pc.get("reason")), None),
                "error": errors[0] if errors else None,
                "elapsed": sum(pc["elapsed"] for pc in per_case),
                "_per_case": per_case,
            }
            print(f"  [{file_label}/single] {cls}({name}): score={_fmt_score(agg['score']).strip()} in {agg['elapsed']:.1f}s {'✗' if agg['error'] else '✓'} (over {agg['n_cases']} turn(s))")
            results.append(agg)

    print("\n[3/3] Final report")
    print("=" * 100)
    print(f"{'File':<10}{'Scope':<8}{'Metric':<40}{'Score':<8}{'Thr':<7}{'Verdict':<8}{'Elapsed':<10}")
    print("-" * 100)
    for r in results:
        print(
            f"{r['file']:<10}"
            f"{r['scope']:<8}"
            f"{(r['cls'] + '/' + r['name'])[:38]:<40}"
            f"{_fmt_score(r['score'])}"
            f"{(_fmt_score(r['threshold']) if r['threshold'] is not None else '--    ')[:7]}"
            f"{_verdict(r):<8}"
            f"{r['elapsed']:>6.1f}s"
        )
    print("-" * 100)
    n_pass = sum(1 for r in results if _verdict(r) == "PASS ")
    n_fail = sum(1 for r in results if _verdict(r) == "FAIL ")
    n_err = sum(1 for r in results if _verdict(r) == "ERROR")
    n_none = sum(1 for r in results if _verdict(r) == "NONE ")
    print(f"PASS={n_pass}  FAIL={n_fail}  ERROR={n_err}  NONE={n_none}  total={len(results)}")
    print()

    # Detail block — show reason/error for anything that didn't cleanly PASS
    print("Details for non-PASS metrics:")
    print("-" * 100)
    for r in results:
        if _verdict(r) == "PASS ":
            continue
        print(f"  {r['cls']}({r['name']}) [{r['file']}/{r['scope']}]  verdict={_verdict(r).strip()}")
        if r["error"]:
            print(f"    error: {r['error'][:400]}")
        if r["reason"]:
            print(f"    reason: {r['reason'][:400]}")
    print("=" * 100)

    # Dump full result to disk for inspection
    out_path = ROOT / "healthcheck-report.json"
    serialisable = []
    for r in results:
        r2 = {k: v for k, v in r.items() if k != "_per_case"}
        r2["score"] = float(r["score"]) if r.get("score") is not None else None
        r2["threshold"] = float(r["threshold"]) if r.get("threshold") is not None else None
        serialisable.append(r2)
    out_path.write_text(json.dumps({
        "golden_scenario": golden.get("scenario"),
        "tier": golden.get("_tier"),
        "mira_elapsed_s": round(mira_elapsed, 1),
        "n_tool_calls": n_tool,
        "session_warnings": session.warnings,
        "results": serialisable,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nFull JSON: {out_path}")


if __name__ == "__main__":
    main()
