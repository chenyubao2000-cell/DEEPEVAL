"""Multi-golden evaluation runner with a polished Markdown + JSON report.

Drives Mira once per matching golden, then scores every metric defined
across the 5 test files (e2e / custom / tooluse / safety / others) on the
resulting session. Aggregates everything into:

  * <out>.json  — machine-readable per-golden detail
  * <out>.md    — formatted human-readable report

Filtering options (all combinable; AND semantics):
  --category crm,voice,ci_email,ci_dingding   _category field
  --tier light|heavy                           _tier field
  --index 10,11,12                             0-based positions in .dataset.json
  --scenario 上海                              substring match against scenario

If no filter is given, runs the 4 new categories (crm/voice/ci_email/ci_dingding)
by default — the 8 freshly-added goldens.

Examples:
  .venv/bin/python report.py                              # the 8 new goldens
  .venv/bin/python report.py --category voice             # voice 2 cases only
  .venv/bin/python report.py --index 11                   # voice case 2 (xlsx upload)
  .venv/bin/python report.py --tier light --out reports/light
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "evals"))

from _env import current_env, load_env   # type: ignore[import-not-found]
from _langfuse_tools import (             # type: ignore[import-not-found]
    available_tool_registry,
    is_stale,
    load_cached,
    refresh_cache_from_session,
)
from _driver import (  # type: ignore[import-not-found]
    build_conversational,
    drive_mira,
    explode_to_llm_cases,
    load_goldens,
    set_registry,
)

import test_mira_e2e as f_e2e            # type: ignore[import-not-found]
import test_mira_custom as f_custom      # type: ignore[import-not-found]
import test_mira_tooluse as f_tooluse    # type: ignore[import-not-found]
import test_mira_safety as f_safety      # type: ignore[import-not-found]
import test_mira_others as f_others      # type: ignore[import-not-found]
import test_mira_run as f_run            # type: ignore[import-not-found]

from deepeval.errors import MissingTestCaseParamsError


DEFAULT_CATEGORIES = ["crm", "voice", "ci_email", "ci_dingding"]


# ── metric registry ─────────────────────────────────────────────────────────

def _collect_metrics() -> list[tuple[str, str, Any]]:
    """Return (file_label, scope, metric_instance) for every metric in the suite.
    scope = 'multi' (ConversationalTestCase) or 'single' (LLMTestCase per turn).
    """
    rows: list[tuple[str, str, Any]] = []
    for m in f_run.METRICS:
        rows.append(("run", "multi", m))
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


def _metric_label(metric) -> str:
    name = getattr(metric, "__name__", None) or type(metric).__name__
    cls = type(metric).__name__
    if cls == "ConversationalGEval":
        return f"GEval/{name}"
    return cls


# ── filtering ───────────────────────────────────────────────────────────────

def _select_goldens(args, goldens: list[dict]) -> list[tuple[int, dict]]:
    indexed = list(enumerate(goldens))

    if args.index:
        wanted = {int(i) for i in args.index.split(",")}
        indexed = [(i, g) for i, g in indexed if i in wanted]

    if args.tier:
        indexed = [(i, g) for i, g in indexed if g.get("_tier") == args.tier]

    if args.scenario:
        needle = args.scenario
        indexed = [(i, g) for i, g in indexed if needle in (g.get("scenario") or "")]

    if args.category:
        cats = {c.strip() for c in args.category.split(",") if c.strip()}
        indexed = [(i, g) for i, g in indexed if g.get("_category") in cats]
    elif not (args.index or args.tier or args.scenario or args.all):
        # default: the 4 new categories
        indexed = [(i, g) for i, g in indexed if g.get("_category") in DEFAULT_CATEGORIES]

    return indexed


# ── scoring ─────────────────────────────────────────────────────────────────

@dataclass
class MetricResult:
    file: str
    scope: str
    metric: str             # display label, e.g. "GEval/GroundedNoFabrication"
    cls: str                # underlying class name
    score: float | None
    threshold: float | None
    success: bool | None
    reason: str | None
    error: str | None
    elapsed_s: float
    n_cases: int = 1


@dataclass
class GoldenResult:
    index: int
    scenario: str
    tier: str | None
    category: str | None
    mira_elapsed_s: float
    n_user_turns: int
    n_assistant_turns: int
    n_tool_calls: int
    tools_observed: list[str]
    conv_id: str
    session_warnings: list[str]
    metric_results: list[MetricResult] = field(default_factory=list)
    drive_error: str | None = None


def _score_one(metric, test_case) -> dict:
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
    except MissingTestCaseParamsError as e:
        # Test case is missing a field this metric needs (e.g.
        # ArgumentCorrectnessMetric on a turn with zero tool calls). This is a
        # "metric does not apply to this case" signal, not a failure — return
        # a NONE verdict rather than ERROR so aggregations stay honest.
        return {
            "score": None,
            "threshold": getattr(metric, "threshold", None),
            "success": None,
            "reason": f"not applicable: {e}",
            "error": None,
            "elapsed": time.time() - t0,
        }
    except Exception as e:  # judge crash / parse error / etc.
        return {
            "score": None,
            "threshold": getattr(metric, "threshold", None),
            "success": False,
            "reason": None,
            "error": f"{type(e).__name__}: {e}",
            "elapsed": time.time() - t0,
        }


def _verdict(r: MetricResult) -> str:
    if r.error:
        return "ERROR"
    if r.success is True:
        return "PASS"
    if r.success is False:
        return "FAIL"
    if r.score is None or r.threshold is None:
        return "NONE"
    return "PASS" if float(r.score) >= float(r.threshold) else "FAIL"


# ── runner ──────────────────────────────────────────────────────────────────

def run_one_golden(idx: int, golden: dict) -> GoldenResult:
    scenario = golden.get("scenario") or f"golden #{idx}"
    print(f"\n──── [{idx}] {scenario[:80]} ────")
    print(f"     tier={golden.get('_tier')}  category={golden.get('_category')}")

    t0 = time.time()
    try:
        session, turns = drive_mira(golden)
    except Exception as e:
        print(f"     drive failed: {e}")
        return GoldenResult(
            index=idx, scenario=scenario, tier=golden.get("_tier"),
            category=golden.get("_category"), mira_elapsed_s=time.time() - t0,
            n_user_turns=0, n_assistant_turns=0, n_tool_calls=0,
            tools_observed=[], conv_id="", session_warnings=[],
            drive_error=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-500:]}",
        )

    mira_elapsed = time.time() - t0
    n_user = sum(1 for t in turns if t.role == "user")
    n_asst = sum(1 for t in turns if t.role == "assistant")
    tool_names = [tc.name for t in turns for tc in (t.tools_called or [])]
    print(f"     Mira done in {mira_elapsed:.1f}s → {n_user}U/{n_asst}A turns, {len(tool_names)} tool call(s)")
    if session.warnings:
        print(f"     ⚠ warnings: {session.warnings}")

    conv_case = build_conversational(golden, turns, session=session)
    llm_cases = explode_to_llm_cases(turns, scenario=scenario)

    out = GoldenResult(
        index=idx, scenario=scenario, tier=golden.get("_tier"),
        category=golden.get("_category"), mira_elapsed_s=mira_elapsed,
        n_user_turns=n_user, n_assistant_turns=n_asst,
        n_tool_calls=len(tool_names), tools_observed=tool_names,
        conv_id=session.conversation_id, session_warnings=list(session.warnings),
    )

    metric_rows = _collect_metrics()
    for file_label, scope, metric in metric_rows:
        display = _metric_label(metric)
        cls = type(metric).__name__
        t1 = time.time()
        if scope == "multi":
            r = _score_one(metric, conv_case)
            mr = MetricResult(
                file=file_label, scope=scope, metric=display, cls=cls,
                score=r["score"], threshold=r["threshold"],
                success=r["success"], reason=r["reason"], error=r["error"],
                elapsed_s=r["elapsed"], n_cases=1,
            )
        else:
            if not llm_cases:
                mr = MetricResult(
                    file=file_label, scope=scope, metric=display, cls=cls,
                    score=None, threshold=getattr(metric, "threshold", None),
                    success=None, reason="no assistant turns", error=None,
                    elapsed_s=0.0, n_cases=0,
                )
            else:
                per_case = [_score_one(metric, lc) for lc in llm_cases]
                scores = [pc["score"] for pc in per_case if pc["score"] is not None]
                errors = [pc["error"] for pc in per_case if pc["error"]]
                # success=None marks "metric did not apply" — exclude from the
                # all() aggregate so N/A cases don't flip a passing metric to FAIL.
                real_successes = [pc["success"] for pc in per_case if pc["success"] is not None]
                mr = MetricResult(
                    file=file_label, scope=scope, metric=display, cls=cls,
                    score=(sum(scores) / len(scores)) if scores else None,
                    threshold=getattr(metric, "threshold", None),
                    success=all(real_successes) if real_successes else None,
                    reason=next((pc["reason"] for pc in per_case if pc.get("reason")), None),
                    error=errors[0] if errors else None,
                    elapsed_s=sum(pc["elapsed"] for pc in per_case),
                    n_cases=len(llm_cases),
                )
        out.metric_results.append(mr)
        verdict = _verdict(mr)
        flag = {"PASS": "✓", "FAIL": "✗", "ERROR": "!", "NONE": "·"}.get(verdict, "?")
        score_s = f"{mr.score:.2f}" if mr.score is not None else "  —  "
        thr_s = f"≥{mr.threshold:.2f}" if mr.threshold is not None else "    "
        print(f"     {flag} [{file_label:<7}/{scope:<6}] {display:<38} {score_s} {thr_s} {verdict:<5}  {mr.elapsed_s:>5.1f}s")
    return out


# ── reporters ───────────────────────────────────────────────────────────────

def _fmt_score(s: float | None) -> str:
    return f"{s:.2f}" if isinstance(s, (int, float)) else "—"


def write_json(results: list[GoldenResult], out_path: Path, meta: dict) -> None:
    payload = {
        "meta": meta,
        "results": [
            {
                "index": gr.index,
                "scenario": gr.scenario,
                "tier": gr.tier,
                "category": gr.category,
                "mira_elapsed_s": round(gr.mira_elapsed_s, 1),
                "n_user_turns": gr.n_user_turns,
                "n_assistant_turns": gr.n_assistant_turns,
                "n_tool_calls": gr.n_tool_calls,
                "tools_observed": gr.tools_observed,
                "conv_id": gr.conv_id,
                "session_warnings": gr.session_warnings,
                "drive_error": gr.drive_error,
                "metrics": [
                    {
                        "file": mr.file,
                        "scope": mr.scope,
                        "metric": mr.metric,
                        "cls": mr.cls,
                        "score": (float(mr.score) if mr.score is not None else None),
                        "threshold": (float(mr.threshold) if mr.threshold is not None else None),
                        "success": mr.success,
                        "verdict": _verdict(mr),
                        "reason": mr.reason,
                        "error": mr.error,
                        "elapsed_s": round(mr.elapsed_s, 1),
                        "n_cases": mr.n_cases,
                    }
                    for mr in gr.metric_results
                ],
            }
            for gr in results
        ],
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _aggregate_metric(results: list[GoldenResult]) -> list[dict]:
    """Per-metric aggregate across all goldens."""
    by_metric: dict[str, dict] = {}
    for gr in results:
        for mr in gr.metric_results:
            key = f"{mr.file}/{mr.metric}"
            d = by_metric.setdefault(key, {
                "file": mr.file, "metric": mr.metric, "cls": mr.cls,
                "scores": [], "pass": 0, "fail": 0, "error": 0, "none": 0,
                "threshold": mr.threshold,
            })
            v = _verdict(mr)
            d[v.lower()] = d.get(v.lower(), 0) + 1
            if mr.score is not None:
                d["scores"].append(mr.score)
    rows = []
    for key, d in by_metric.items():
        avg = (sum(d["scores"]) / len(d["scores"])) if d["scores"] else None
        rows.append({
            "file": d["file"], "metric": d["metric"], "cls": d["cls"],
            "avg_score": avg, "threshold": d["threshold"],
            "pass": d.get("pass", 0), "fail": d.get("fail", 0),
            "error": d.get("error", 0), "none": d.get("none", 0),
        })
    # Sort: most failures first, then by file
    rows.sort(key=lambda r: (-(r["fail"] + r["error"]), r["file"], r["metric"]))
    return rows


def _aggregate_category(results: list[GoldenResult]) -> list[dict]:
    by_cat: dict[str, dict] = {}
    for gr in results:
        cat = gr.category or "(uncat)"
        d = by_cat.setdefault(cat, {
            "category": cat, "n_goldens": 0,
            "pass": 0, "fail": 0, "error": 0, "none": 0,
            "scores": [],
        })
        d["n_goldens"] += 1
        for mr in gr.metric_results:
            v = _verdict(mr).lower()
            d[v] = d.get(v, 0) + 1
            if mr.score is not None:
                d["scores"].append(mr.score)
    rows = []
    for d in by_cat.values():
        d["avg_score"] = (sum(d["scores"]) / len(d["scores"])) if d["scores"] else None
        d.pop("scores", None)
        rows.append(d)
    rows.sort(key=lambda r: r["category"])
    return rows


def write_markdown(results: list[GoldenResult], out_path: Path, meta: dict) -> None:
    lines: list[str] = []
    add = lines.append

    # ── HEADER ──────────────────────────────────────────────────────────────
    add(f"# Mira 评测报告")
    add("")
    add(f"- 生成时间：`{meta['generated_at']}`")
    add(f"- 环境：`{meta['env']}`")
    add(f"- BFF：`{meta['bff_url']}`")
    add(f"- Langfuse：`{meta['langfuse_host']}`")
    add(f"- 工具清单：{meta['n_available_tools']} 个（来源：`{meta['tools_source']}`）")
    add(f"- 命令：`{meta['invocation']}`")
    add(f"- Goldens：{meta['n_goldens']} 条  ·  指标：{meta['n_metrics']} 项 / golden  ·  总评估次数：{meta['n_goldens'] * meta['n_metrics']}")
    add(f"- 总耗时：**{meta['total_elapsed_s']:.0f}s**（Mira 驱动 {meta['mira_total_s']:.0f}s + Judge 评分 {meta['judge_total_s']:.0f}s）")
    add("")

    # ── TOP-LEVEL VERDICT ──────────────────────────────────────────────────
    n_pass = sum(1 for gr in results for mr in gr.metric_results if _verdict(mr) == "PASS")
    n_fail = sum(1 for gr in results for mr in gr.metric_results if _verdict(mr) == "FAIL")
    n_err = sum(1 for gr in results for mr in gr.metric_results if _verdict(mr) == "ERROR")
    n_none = sum(1 for gr in results for mr in gr.metric_results if _verdict(mr) == "NONE")
    total = n_pass + n_fail + n_err + n_none
    pass_rate = (n_pass / total * 100) if total else 0.0
    add(f"## 总评")
    add("")
    add(f"| 通过 ✓ | 失败 ✗ | 错误 ! | 缺失 · | 通过率 |")
    add(f"|---:|---:|---:|---:|---:|")
    add(f"| **{n_pass}** | **{n_fail}** | **{n_err}** | **{n_none}** | **{pass_rate:.1f}%** |")
    add("")

    # ── PER-CATEGORY ROLLUP ────────────────────────────────────────────────
    cat_rows = _aggregate_category(results)
    add(f"## 按类别汇总")
    add("")
    add("| 类别 | Goldens | 平均分 | PASS | FAIL | ERROR | NONE |")
    add("|---|---:|---:|---:|---:|---:|---:|")
    for r in cat_rows:
        add(f"| `{r['category']}` | {r['n_goldens']} | {_fmt_score(r['avg_score'])} | {r.get('pass',0)} | {r.get('fail',0)} | {r.get('error',0)} | {r.get('none',0)} |")
    add("")

    # ── PER-GOLDEN ROLLUP ──────────────────────────────────────────────────
    add(f"## 按用例汇总")
    add("")
    add("| # | 类别 | Tier | 场景 | Mira 耗时 | 工具调用 | PASS / FAIL / ERR |")
    add("|---:|:---:|:---:|---|---:|---:|---|")
    for gr in results:
        p = sum(1 for mr in gr.metric_results if _verdict(mr) == "PASS")
        f = sum(1 for mr in gr.metric_results if _verdict(mr) == "FAIL")
        e = sum(1 for mr in gr.metric_results if _verdict(mr) == "ERROR")
        cat = gr.category or "—"
        scenario_short = (gr.scenario or "")[:60]
        add(f"| {gr.index} | `{cat}` | {gr.tier or '—'} | {scenario_short} | {gr.mira_elapsed_s:.0f}s | {gr.n_tool_calls} | {p} / {f} / {e} |")
    add("")

    # ── PER-METRIC ROLLUP ──────────────────────────────────────────────────
    metric_rows = _aggregate_metric(results)
    add(f"## 按指标汇总")
    add("")
    add("| 文件 | 指标 | 平均分 | 阈值 | PASS | FAIL | ERROR | NONE |")
    add("|---|---|---:|---:|---:|---:|---:|---:|")
    for r in metric_rows:
        thr = f"≥{r['threshold']:.2f}" if r['threshold'] is not None else "—"
        add(f"| `{r['file']}` | `{r['metric']}` | {_fmt_score(r['avg_score'])} | {thr} | {r['pass']} | {r['fail']} | {r['error']} | {r['none']} |")
    add("")

    # ── DETAILED PER-GOLDEN BLOCKS ─────────────────────────────────────────
    add(f"## 每条用例详情")
    add("")
    for gr in results:
        add(f"### [{gr.index}] {gr.scenario}")
        add("")
        meta_bits = [
            f"`tier={gr.tier or '—'}`",
            f"`category={gr.category or '—'}`",
            f"`mira={gr.mira_elapsed_s:.1f}s`",
            f"`turns={gr.n_user_turns}U/{gr.n_assistant_turns}A`",
            f"`tools={gr.n_tool_calls}`",
            f"`conv={gr.conv_id}`",
        ]
        add(" · ".join(meta_bits))
        add("")
        if gr.session_warnings:
            add(f"> ⚠ session warnings: `{gr.session_warnings}`")
            add("")
        if gr.tools_observed:
            from collections import Counter
            tc = Counter(gr.tools_observed)
            add("**工具调用：** " + ", ".join(f"`{k}`×{v}" for k, v in tc.most_common()))
            add("")
        if gr.drive_error:
            add(f"> 🛑 **Mira 驱动失败：** ```{gr.drive_error[:400]}```")
            add("")
            continue

        add("| 文件 | 指标 | 分数 | 阈值 | 判定 | 耗时 |")
        add("|---|---|---:|---:|:---:|---:|")
        for mr in gr.metric_results:
            v = _verdict(mr)
            badge = {"PASS": "✅ PASS", "FAIL": "❌ FAIL", "ERROR": "🚨 ERR", "NONE": "· NONE"}.get(v, v)
            thr = f"≥{mr.threshold:.2f}" if mr.threshold is not None else "—"
            score = _fmt_score(mr.score)
            add(f"| `{mr.file}` | `{mr.metric}` | {score} | {thr} | {badge} | {mr.elapsed_s:.1f}s |")
        add("")

        non_pass = [mr for mr in gr.metric_results if _verdict(mr) != "PASS"]
        if non_pass:
            add("<details><summary>非 PASS 项的 reason / error</summary>")
            add("")
            for mr in non_pass:
                v = _verdict(mr)
                add(f"- **`{mr.metric}` ({v})** — score={_fmt_score(mr.score)} thr={_fmt_score(mr.threshold)}")
                if mr.error:
                    add(f"  - 🛑 error: `{mr.error[:300]}`")
                if mr.reason:
                    reason = mr.reason.replace("\n", " ").strip()[:600]
                    add(f"  - 💬 reason: {reason}")
            add("")
            add("</details>")
            add("")

    # ── FOOTER ─────────────────────────────────────────────────────────────
    add("---")
    add("")
    add(f"完整 JSON 详情见 `{meta['json_path']}`。")

    out_path.write_text("\n".join(lines), encoding="utf-8")


# ── main ────────────────────────────────────────────────────────────────────

def _install_registry(env: str) -> tuple[int, str]:
    """Push the cached tool registry into the driver and ToolUseMetric. Returns
    (tool_count, source_label) for the report header."""
    cached = load_cached(env)
    registry = {t["name"]: t for t in (cached or {}).get("tools", [])}
    set_registry(registry)
    f_tooluse._patch_tooluse_metrics_in_place(registry)
    if not cached:
        return 0, "(empty: no cache yet)"
    return len(registry), f".cache/tools-{env}.json @ {cached.get('fetched_at','?')}"


def main() -> None:
    import os
    ap = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter, description=__doc__)
    ap.add_argument("--env", help="environment name (default: $MIRA_ENV or 'preview'); selects .env.<name>")
    ap.add_argument("--category", help="comma-separated _category filter (e.g. crm,voice)")
    ap.add_argument("--tier", choices=["light", "heavy"], help="filter by _tier")
    ap.add_argument("--index", help="comma-separated 0-based indices")
    ap.add_argument("--scenario", help="substring match on scenario")
    ap.add_argument("--all", action="store_true", help="run all goldens (overrides default-category filter)")
    ap.add_argument("--out", default="reports/report", help="output basename (will write .json + .md)")
    ap.add_argument("--refresh-tools", action="store_true",
                    help="force a Langfuse refresh of the tool-registry cache after the first golden")
    ap.add_argument("--no-langfuse-refresh", action="store_true",
                    help="never query Langfuse; use cache only (offline / CI)")
    args = ap.parse_args()

    # ── env + cache bootstrap ───────────────────────────────────────────────
    env = load_env(args.env)
    cached = load_cached(env)
    stale = bool(cached and is_stale(cached))
    n_tools, tools_source = _install_registry(env)
    if cached is None and not args.no_langfuse_refresh:
        print(f"     no tool cache for env={env}; will populate from Langfuse after first golden")
    elif stale and not args.no_langfuse_refresh:
        print(f"     ⚠ tool cache for env={env} is older than 7 days; will refresh after first golden")
    elif cached:
        print(f"     using cached tool registry: {n_tools} tools  ({tools_source})")

    goldens = load_goldens()
    selected = _select_goldens(args, goldens)
    if not selected:
        print("No goldens matched the filters."); sys.exit(2)

    print(f"Running {len(selected)} golden(s):")
    for idx, g in selected:
        print(f"  [{idx}] tier={g.get('_tier'):<5}  category={g.get('_category') or '—':<14}  {(g.get('scenario') or '')[:80]}")

    n_metrics = len(_collect_metrics())
    print(f"\nMetrics per golden: {n_metrics}")
    print(f"Total measurements: {len(selected) * n_metrics}")
    print()

    needs_refresh = (cached is None or stale or args.refresh_tools) and not args.no_langfuse_refresh

    start = time.time()
    results: list[GoldenResult] = []
    for i, (idx, g) in enumerate(selected):
        results.append(run_one_golden(idx, g))
        # Refresh tool cache after the first golden lands so subsequent goldens
        # use the up-to-date registry. We only do it once per run.
        if i == 0 and needs_refresh and results[0].conv_id:
            print(f"     refreshing tool cache from Langfuse session {results[0].conv_id}...")
            new_cache = refresh_cache_from_session(results[0].conv_id, env=env)
            if new_cache:
                n_tools, tools_source = _install_registry(env)
                print(f"     ✓ tool cache refreshed: {n_tools} tools")
                needs_refresh = False  # done

    total_elapsed = time.time() - start
    mira_total = sum(gr.mira_elapsed_s for gr in results)
    judge_total = sum(mr.elapsed_s for gr in results for mr in gr.metric_results)

    out_base = Path(args.out)
    out_base.parent.mkdir(parents=True, exist_ok=True)
    json_path = out_base.with_suffix(".json")
    md_path = out_base.with_suffix(".md")

    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "env": env,
        "bff_url": os.environ.get("MIRA_BFF_URL", ""),
        "langfuse_host": os.environ.get("LANGFUSE_HOST", ""),
        "n_available_tools": n_tools,
        "tools_source": tools_source,
        "invocation": " ".join(["python", "report.py", *sys.argv[1:]]),
        "n_goldens": len(results),
        "n_metrics": n_metrics,
        "total_elapsed_s": total_elapsed,
        "mira_total_s": mira_total,
        "judge_total_s": judge_total,
        "json_path": str(json_path.name),
    }

    write_json(results, json_path, meta)
    write_markdown(results, md_path, meta)

    n_pass = sum(1 for gr in results for mr in gr.metric_results if _verdict(mr) == "PASS")
    n_fail = sum(1 for gr in results for mr in gr.metric_results if _verdict(mr) == "FAIL")
    n_err = sum(1 for gr in results for mr in gr.metric_results if _verdict(mr) == "ERROR")
    n_none = sum(1 for gr in results for mr in gr.metric_results if _verdict(mr) == "NONE")
    total = n_pass + n_fail + n_err + n_none

    print()
    print("=" * 80)
    print(f"DONE in {total_elapsed:.0f}s ({len(results)} goldens × {n_metrics} metrics = {len(results)*n_metrics} measurements)")
    print(f"PASS={n_pass}  FAIL={n_fail}  ERROR={n_err}  NONE={n_none}  ({(n_pass/total*100) if total else 0:.1f}% pass)")
    print()
    print(f"📊 Markdown report : {md_path}")
    print(f"🧾 JSON details    : {json_path}")
    print("=" * 80)


if __name__ == "__main__":
    main()
