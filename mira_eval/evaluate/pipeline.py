"""Multi-golden × N-metric evaluation pipeline.

This is the orchestration core. The 6-stage mental model materialises here:

  ① user        — args parsed by cli/run.py, passed in as a Namespace
  ② data prep   — load_env() + load_goldens() + install_registry()
  ③ agent       — drive_mira() per golden (parallelised after the first)
  ④ evidence    — metric.measure() pulls Langfuse / Postgres as needed
  ⑤ eval        — _score_one() runs every metric, audit annotates verdicts
  ⑥ report      — write_json + write_markdown at the end

The first golden runs sequentially so its conv_id can refresh the tool
registry cache before the rest fire (so all parallel goldens see the fresh
registry). Remaining goldens run in a ThreadPoolExecutor.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from deepeval.errors import MissingTestCaseParamsError

from ..config import load_env
from ..dataset import load_goldens
from ..metrics import collect_metric_rows
from ..tracing.tool_registry import (
    available_tool_registry,
    is_stale,
    load_cached,
    refresh_cache_from_session,
)
from . import audit
from .driver import (
    build_conversational,
    drive_mira,
    explode_to_llm_cases,
    set_registry,
)


DEFAULT_CATEGORIES = ["crm", "voice", "ci_email", "ci_dingding"]


# ─────────────────────────────────────────────────────────────────────────────
# Share URL helpers
# ─────────────────────────────────────────────────────────────────────────────

def _derive_task_url_base() -> str:
    """Build a task / share URL base from MIRA_BFF_URL / overrides.

    Priority:
      1. ``MIRA_TASK_URL_BASE`` env var.
      2. ``MIRA_BFF_URL`` host — Mira's BFF serves both `/api/task` (SSE) and
         the frontend `/task/{id}` / `/share/{id}` pages on the same host.
    Returns a base WITHOUT trailing slash.
    """
    explicit = os.environ.get("MIRA_TASK_URL_BASE", "").strip().rstrip("/")
    if explicit:
        return explicit
    return os.environ.get("MIRA_BFF_URL", "").strip().rstrip("/")


def task_url(base: str, conv_id: str) -> str:
    if not base or not conv_id:
        return ""
    return f"{base}/task/{conv_id}"


def _ensure_share_url(conv_id: str) -> str | None:
    """Get a public share URL for this task — viewer permission, no expiry.

    Reuses an existing active viewer share if one is on the task. Falls back
    to None on any error so the caller can still surface the (login-gated)
    task URL.
    """
    import httpx
    bff = os.environ.get("MIRA_BFF_URL", "").strip().rstrip("/")
    tok = os.environ.get("MIRA_SESSION_TOKEN", "")
    cookie_name = os.environ.get("MIRA_COOKIE_NAME", "__Secure-better-auth.session_token")
    if not bff or not tok or not conv_id:
        return None
    cookies = {cookie_name: tok}
    try:
        # Reuse an active viewer share if one already exists for this task.
        r = httpx.get(f"{bff}/api/tasks/{conv_id}/share", cookies=cookies, timeout=60)
        if r.status_code == 200:
            shares = (r.json() or {}).get("shares") or []
            for s in shares:
                if s.get("isActive") and s.get("permission") == "viewer" and not s.get("expiresAt"):
                    return f"{bff}/share/{conv_id}?token={s.get('shareToken')}"
        r = httpx.post(
            f"{bff}/api/tasks/{conv_id}/share",
            cookies=cookies,
            json={"chatId": conv_id, "permission": "viewer"},
            timeout=60,
        )
        if r.status_code == 200:
            url = (r.json() or {}).get("shareUrl")
            return url or None
    except Exception:
        return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Skip policy
# ─────────────────────────────────────────────────────────────────────────────
# Some metrics produce no useful signal on certain golden categories — they
# just burn judge quota. We skip them by default, but keep them visible in
# the run log so silent-skip ≠ secret-skip. Override per-golden via
# `_skip_metrics` (replace defaults) or `_skip_metrics_extra` (extend).
_CATEGORY_DEFAULT_SKIPS: dict[str, set[str]] = {
    "voice":       {"BiasMetric", "ToxicityMetric", "PIILeakageMetric",
                    "RoleViolationMetric", "KnowledgeRetentionMetric"},
    "ci_email":    {"BiasMetric", "ToxicityMetric", "PIILeakageMetric",
                    "RoleViolationMetric", "KnowledgeRetentionMetric"},
    "ci_dingding": {"BiasMetric", "ToxicityMetric", "PIILeakageMetric",
                    "RoleViolationMetric", "KnowledgeRetentionMetric"},
    "crm":         {"BiasMetric", "ToxicityMetric", "RoleViolationMetric",
                    "KnowledgeRetentionMetric"},
    # Uncategorised research/sourcing goldens keep the full 17.
}


def _resolve_skip_set(golden: dict) -> set[str]:
    """Class names + GEval labels to skip for this golden."""
    cat = golden.get("_category") or ""
    if "_skip_metrics" in golden:
        return set(golden["_skip_metrics"] or [])
    base = set(_CATEGORY_DEFAULT_SKIPS.get(cat, set()))
    extra = set(golden.get("_skip_metrics_extra") or [])
    return base | extra


def _should_skip(metric, golden: dict) -> tuple[bool, str | None]:
    """Decide whether to skip ``metric`` for this golden.

    Reasons:
      - "broken-profile"  → Layer 1 global skip (DeepEval impl unreliable)
      - "category-policy" → Layer 1 per-category skip (no signal here)
    """
    if audit.profile_for(metric) == "broken":
        return True, "broken-profile"
    cls = type(metric).__name__
    skip = _resolve_skip_set(golden)
    if cls in skip or audit.metric_label(metric) in skip:
        return True, "category-policy"
    return False, None


# ─────────────────────────────────────────────────────────────────────────────
# Filtering
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────────────────

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
    informational: bool = False
    profile: str = "signal"           # signal | noisy | broken (Layer 1)
    audit_warning: str | None = None  # Layer 2: judge self-contradicted


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
    share_url: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────

def _score_one(metric, test_case) -> dict:
    t0 = time.time()
    informational = bool(getattr(metric, "informational", False))
    try:
        metric.measure(test_case)
        return {
            "score": getattr(metric, "score", None),
            "threshold": getattr(metric, "threshold", None),
            "success": getattr(metric, "success", None),
            "reason": getattr(metric, "reason", None),
            "error": getattr(metric, "error", None),
            "elapsed": time.time() - t0,
            "informational": informational,
        }
    except MissingTestCaseParamsError as e:
        # Test case is missing a field this metric needs — return NONE
        # rather than ERROR so aggregations stay honest.
        return {
            "score": None,
            "threshold": getattr(metric, "threshold", None),
            "success": None,
            "reason": f"not applicable: {e}",
            "error": None,
            "elapsed": time.time() - t0,
            "informational": informational,
        }
    except Exception as e:  # judge crash / parse error / etc.
        return {
            "score": None,
            "threshold": getattr(metric, "threshold", None),
            "success": False,
            "reason": None,
            "error": f"{type(e).__name__}: {e}",
            "elapsed": time.time() - t0,
            "informational": informational,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

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

    share_url = _ensure_share_url(session.conversation_id)
    if share_url:
        print(f"     🔗 share: {share_url}")
    out = GoldenResult(
        index=idx, scenario=scenario, tier=golden.get("_tier"),
        category=golden.get("_category"), mira_elapsed_s=mira_elapsed,
        n_user_turns=n_user, n_assistant_turns=n_asst,
        n_tool_calls=len(tool_names), tools_observed=tool_names,
        conv_id=session.conversation_id, session_warnings=list(session.warnings),
        share_url=share_url,
    )

    # Deepcopy every metric instance so this golden gets its own state. Module-
    # level metric instances are shared across goldens; running goldens in
    # parallel would race two threads on the same self.score / self.reason.
    metric_rows = [(f, s, copy.deepcopy(m)) for f, s, m in collect_metric_rows()]
    for file_label, scope, metric in metric_rows:
        display = audit.metric_label(metric)
        cls = type(metric).__name__
        profile = audit.profile_for(metric)
        skip, skip_why = _should_skip(metric, golden)
        if skip:
            tag = "broken" if skip_why == "broken-profile" else "category-policy"
            print(f"     ⊘ [{file_label:<7}/{scope:<6}] {display:<38}    —      —  SKIP   ({tag})")
            continue
        informational = bool(getattr(metric, "informational", False))
        if scope == "multi":
            r = _score_one(metric, conv_case)
            mr = MetricResult(
                file=file_label, scope=scope, metric=display, cls=cls,
                score=r["score"], threshold=r["threshold"],
                success=r["success"], reason=r["reason"], error=r["error"],
                elapsed_s=r["elapsed"], n_cases=1,
                profile=profile,
                audit_warning=audit.audit_judge_consistency(
                    r["score"], r["threshold"], r["success"], r["reason"]
                ),
                informational=informational,
            )
        else:
            if not llm_cases:
                mr = MetricResult(
                    file=file_label, scope=scope, metric=display, cls=cls,
                    score=None, threshold=getattr(metric, "threshold", None),
                    success=None, reason="no assistant turns", error=None,
                    elapsed_s=0.0, n_cases=0,
                    profile=profile,
                    informational=informational,
                )
            else:
                per_case = [_score_one(metric, lc) for lc in llm_cases]
                scores = [pc["score"] for pc in per_case if pc["score"] is not None]
                errors = [pc["error"] for pc in per_case if pc["error"]]
                real_successes = [pc["success"] for pc in per_case if pc["success"] is not None]
                avg_score = (sum(scores) / len(scores)) if scores else None
                agg_success = all(real_successes) if real_successes else None
                first_reason = next((pc["reason"] for pc in per_case if pc.get("reason")), None)
                thr = getattr(metric, "threshold", None)
                mr = MetricResult(
                    file=file_label, scope=scope, metric=display, cls=cls,
                    score=avg_score, threshold=thr,
                    success=agg_success, reason=first_reason,
                    error=errors[0] if errors else None,
                    elapsed_s=sum(pc["elapsed"] for pc in per_case),
                    n_cases=len(llm_cases),
                    profile=profile,
                    audit_warning=audit.audit_judge_consistency(avg_score, thr, agg_success, first_reason),
                    informational=informational,
                )
        out.metric_results.append(mr)
        v = audit.verdict(mr)
        flag = {"PASS": "✓", "FAIL": "✗", "ERROR": "!", "NONE": "·", "INCONCLUSIVE": "?", "INFO": "ℹ"}.get(v, "?")
        score_s = f"{mr.score:.2f}" if mr.score is not None else "  —  "
        thr_s = f"≥{mr.threshold:.2f}" if mr.threshold is not None else "    "
        tail = "  " + mr.audit_warning if mr.audit_warning else ""
        prof = f" [{profile}]" if profile != "signal" else ""
        print(f"     {flag} [{file_label:<7}/{scope:<6}] {display:<38} {score_s} {thr_s} {v:<13}{prof}  {mr.elapsed_s:>5.1f}s{tail}")
    return out


def _install_registry(env: str) -> tuple[int, str]:
    """Push the cached tool registry into the driver. Returns
    (tool_count, source_label) for the report header."""
    cached = load_cached(env)
    registry = {t["name"]: t for t in (cached or {}).get("tools", [])}
    set_registry(registry)
    if not cached:
        return 0, "(empty: no cache yet)"
    return len(registry), f".cache/tools-{env}.json @ {cached.get('fetched_at','?')}"


def run(args: argparse.Namespace) -> int:
    """Execute the pipeline. Returns process exit code (0 OK, 2 no match)."""
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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
        print("No goldens matched the filters.")
        return 2

    print(f"Running {len(selected)} golden(s):")
    for idx, g in selected:
        print(f"  [{idx}] tier={g.get('_tier'):<5}  category={g.get('_category') or '—':<14}  {(g.get('scenario') or '')[:80]}")

    n_metrics = len(collect_metric_rows())
    print(f"\nMetrics per golden: {n_metrics}")
    print(f"Total measurements: {len(selected) * n_metrics}")
    print()

    needs_refresh = (cached is None or stale or args.refresh_tools) and not args.no_langfuse_refresh

    start = time.time()
    results: list[GoldenResult] = []

    # First golden runs synchronously so its conv_id can refresh the tool cache
    # before the rest fire (so they all see the fresh registry).
    first_idx, first_golden = selected[0]
    results.append(run_one_golden(first_idx, first_golden))
    if needs_refresh and results[0].conv_id:
        print(f"     refreshing tool cache from Langfuse session {results[0].conv_id}...")
        new_cache = refresh_cache_from_session(results[0].conv_id, env=env)
        if new_cache:
            n_tools, tools_source = _install_registry(env)
            print(f"     ✓ tool cache refreshed: {n_tools} tools")
            needs_refresh = False

    remaining = selected[1:]
    if remaining:
        worker_n = min(args.golden_concurrency, len(remaining))
        print(f"\n──── 并发跑剩下 {len(remaining)} 条 golden（{worker_n} 路并行；输出会交错）────")
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_n) as ex:
            futures = [ex.submit(run_one_golden, idx, g) for idx, g in remaining]
            for fut in concurrent.futures.as_completed(futures):
                results.append(fut.result())

    # Sort results back to selected-order so downstream aggregation / report
    # tables stay in golden-index order (parallel completion order is random).
    results.sort(key=lambda r: r.index)

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
        "task_url_base": _derive_task_url_base(),
        "langfuse_host": os.environ.get("LANGFUSE_HOST", ""),
        "n_available_tools": n_tools,
        "tools_source": tools_source,
        "invocation": " ".join(["mira-eval", *sys.argv[1:]]),
        "n_goldens": len(results),
        "n_metrics": n_metrics,
        "total_elapsed_s": total_elapsed,
        "mira_total_s": mira_total,
        "judge_total_s": judge_total,
        "json_path": str(json_path.name),
    }

    # Imported here (rather than at module top) to avoid a circular import:
    # report.writers imports from .audit, but writers calls back into the
    # pipeline-built GoldenResult / MetricResult instances.
    from ..report import writers
    writers.write_json(results, json_path, meta)
    writers.write_markdown(results, md_path, meta)

    # Optional MySQL persistence (env-var gated). Non-fatal: failure here does
    # NOT roll back local JSON/MD output — DB is a downstream archive, not the
    # source of truth for the run.
    db_url = os.environ.get("MIRA_RESULTS_DB_URL")
    if db_url:
        try:
            from ..persistence import results_db
            engine = results_db.get_engine(db_url)
            results_db.init_schema(engine)            # idempotent
            run_uuid = results_db.persist_run(engine, results, meta)
            print(f"💾 DB persisted    : run_uuid={run_uuid}")
        except Exception as e:  # noqa: BLE001 — never block the run on DB issues
            print(f"⚠  DB persist failed (non-fatal): {type(e).__name__}: {e}")

    n_pass = sum(1 for gr in results for mr in gr.metric_results if audit.verdict(mr) == "PASS")
    n_fail = sum(1 for gr in results for mr in gr.metric_results if audit.verdict(mr) == "FAIL")
    n_err = sum(1 for gr in results for mr in gr.metric_results if audit.verdict(mr) == "ERROR")
    n_none = sum(1 for gr in results for mr in gr.metric_results if audit.verdict(mr) == "NONE")
    n_info = sum(1 for gr in results for mr in gr.metric_results if audit.verdict(mr) == "INFO")
    gated_total = n_pass + n_fail + n_err + n_none

    print()
    print("=" * 80)
    print(f"DONE in {total_elapsed:.0f}s ({len(results)} goldens × {n_metrics} metrics = {len(results)*n_metrics} measurements)")
    print(
        f"PASS={n_pass}  FAIL={n_fail}  ERROR={n_err}  NONE={n_none}  INFO={n_info}  "
        f"({(n_pass/gated_total*100) if gated_total else 0:.1f}% pass, INFO excluded)"
    )
    print()
    print(f"📊 Markdown report : {md_path}")
    print(f"🧾 JSON details    : {json_path}")
    print("=" * 80)
    return 0
