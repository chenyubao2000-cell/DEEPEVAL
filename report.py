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
import concurrent.futures
import copy
import json
import os
import re
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
import test_mira_ops as f_ops            # type: ignore[import-not-found]

from deepeval.errors import MissingTestCaseParamsError


DEFAULT_CATEGORIES = ["crm", "voice", "ci_email", "ci_dingding"]


# ── metric registry ─────────────────────────────────────────────────────────

def _collect_metrics() -> list[tuple[str, str, Any]]:
    """Return (file_label, scope, metric_instance) for every metric in the suite.
    scope = 'multi' (ConversationalTestCase) or 'single' (LLMTestCase per turn).

    The 'ops' bucket holds custom operational metrics (tokens / cost / latency
    / completion / tool-dependency) — all consume one ConversationalTestCase
    via duck-typing on metadata.conv_id, so they live under scope='multi'.
    """
    rows: list[tuple[str, str, Any]] = []
    for m in f_ops.METRICS:
        rows.append(("ops", "multi", m))
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


def _derive_task_url_base() -> str:
    """Build a task / share URL base from MIRA_BFF_URL / overrides.

    Priority:
      1. `MIRA_TASK_URL_BASE` env var (full https://host[/path] root).
      2. `MIRA_BFF_URL` host — Mira's BFF serves both `/api/task` (SSE) and
         the frontend `/task/{id}` / `/share/{id}` pages on the same host
         (confirmed against mira-bff-preview.up.railway.app and mina.ciwork.cn).
    Returns a base WITHOUT trailing slash. Append "/task/{conv_id}" or
    "/share/{conv_id}?token=…".
    """
    explicit = os.environ.get("MIRA_TASK_URL_BASE", "").strip().rstrip("/")
    if explicit:
        return explicit
    return os.environ.get("MIRA_BFF_URL", "").strip().rstrip("/")


def _task_url(base: str, conv_id: str) -> str:
    if not base or not conv_id:
        return ""
    return f"{base}/task/{conv_id}"


def _ensure_share_url(conv_id: str) -> str | None:
    """Get a public share URL for this task — viewer permission, no expiry.

    Reuses an existing active viewer share if one is already on the task
    (avoids spamming the DB with duplicates across re-runs). Falls back to
    None on any error so the caller can still surface the (login-gated)
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
        # Otherwise create a fresh permanent viewer share.
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


# ── skip-by-category policy ────────────────────────────────────────────────
#
# Some metrics produce no useful signal on certain golden categories — they
# just burn judge quota. We skip them by default, but keep them visible in
# the run log so silent-skip ≠ secret-skip. Override per-golden by setting
# `_skip_metrics` (a list of class names or "GEval/<name>" labels) on the
# golden — that takes precedence over these category defaults.
#
# Reasoning behind each entry:
#   - Bias / Toxicity:      Chinese recruiter dialog → near-constant 0.00 PASS,
#                           no discrimination signal observed in 4+ runs.
#   - PIILeakage:           voice/ci_email cases require Mira to echo back
#                           candidate name + phone the user *provided*; judge
#                           flags this as a leak — structural false-positive.
#   - RoleViolation:        Mira never breaks character across observed runs.
#   - KnowledgeRetention:   Goldens with 1U/1A have no prior facts to retain —
#                           always 0.00 FAIL (no signal). Skip globally for
#                           single-turn goldens; turn back on once we have
#                           genuine multi-turn dialogue.
#
# Goal: cut ~25-30% of judge calls (and tokens) per run without losing signal.
# ── Layer 1: metric signal profile ─────────────────────────────────────────
#
# Tags every metric we ship with one of three signal qualities. Each tier
# changes how the metric is treated in the report:
#
#   "signal" → counts toward overall PASS rate. Stable, well-defined judge
#              prompt, low historical false-positive rate. Decision-grade.
#
#   "noisy"  → runs and shows in the report under "辅助参考", but does NOT
#              count toward overall PASS rate. Examples: judge known to flip
#              on the same case across runs; metric structurally false-
#              positives in a category (e.g. PIILeakage on voice cases where
#              the user *asked* Mira to echo a phone number); or the metric
#              demands inputs we don't have (KnowledgeRetention on 1U/1A).
#
#   "broken" → globally skipped. DeepEval's implementation is known to be
#              self-contradictory (RoleViolationMetric's verdicts and reason
#              come from independent LLM calls and disagree), or the metric
#              produces zero usable signal on our entire corpus (Bias /
#              Toxicity on Chinese recruiter dialogue).
#
# Keys are either ClassName (matches type(metric).__name__) or the
# display label like "GEval/ProfessionalNoFabrication" (matches _metric_label).
_METRIC_PROFILE: dict[str, str] = {
    # signal — decision-grade
    "SessionHealthMetric":              "signal",  # client + trace + DB layered gate, deterministic (1 LLM-free check per layer)
    "ConversationCompletenessMetric":   "signal",
    "TurnRelevancyMetric":              "signal",
    "RoleAdherenceMetric":              "signal",
    "GoalAccuracyMetric":               "signal",
    "GEval/ProfessionalNoFabrication":  "signal",
    "GEval/DeliverableMatchesRequest":  "signal",
    "GEval/GroundedNoFabrication":      "signal",
    "GEval/ExpectedToolPath":           "signal",  # replaced ToolUseMetric
    "ArgumentCorrectnessMetric":        "signal",
    "AnswerRelevancyMetric":            "signal",
    "PromptAlignmentMetric":            "signal",
    # noisy — supplementary, won't drag PASS rate
    "TopicAdherenceMetric":             "noisy",   # judge flips between runs, topics list hard to keep complete
    "KnowledgeRetentionMetric":         "noisy",   # always 0.00 on single-turn (1U/1A) goldens
    # broken — globally skipped (4 safety metrics share the same fate: zero
    # decision-grade signal on Chinese recruiter dialogue, and PIILeakage
    # structurally false-pos when the user-provided PII IS the task input)
    "RoleViolationMetric":              "broken",  # verdicts vs reason self-contradict (DeepEval 4.0 two-stage judge)
    "BiasMetric":                       "broken",  # Chinese recruiter corpus → near-constant 0.00; judge jitters out false FAILs
    "ToxicityMetric":                   "broken",  # same as above
    "PIILeakageMetric":                 "broken",  # PII is the task input for voice / email / crm — judge can't disambiguate
}


# ── Reader-facing metric explanations ──────────────────────────────────────
# What each metric is actually testing, in 1 sentence — surfaced in the
# report so non-engineer reviewers can read it without context. Keyed by
# class name OR by "GEval/<rubric>" for ConversationalGEval rubrics.
_METRIC_EXPLANATIONS: dict[str, str] = {
    # ops — health + dependency
    "SessionHealthMetric":
        "这次会话是否干净跑完：client（SSE/工具错误）+ trace（Langfuse 落盘）"
        "+ persistence（Postgres 消息表）三层都通过才给 1.0，任何一层失败即 FAIL。",
    "ToolDependencyMetric":
        "工具调用顺序是否违反硬约束（例如 people_search 之后必须 complete 收尾、"
        "generate 之前必须先 search）。LLM-as-judge 评分。",
    # ops — informational (tokens / cost / latency)
    "TokensMetric":
        "整段会话 token 总用量（input + output）。仅记录，不计入 PASS 率。",
    "SessionCostMetric":
        "整段会话的 LLM 调用总成本（USD）。仅记录。",
    "TimeToFirstTokenMetric":
        "首个 token 的最短到达时间（秒）。仅记录，用于看延迟趋势。",
    "SessionDurationMetric":
        "整段 trace 的总耗时（秒）。仅记录。",
    # e2e — multi-turn quality (DeepEval built-ins)
    "RoleAdherenceMetric":
        "助手回复是否始终保持 Mira 的专业 AI 代理角色，没有破人设或跑题。",
    "GoalAccuracyMetric":
        "助手的整体行为（计划 + 执行）是否真的完成了用户在 scenario 里要的目标。",
    # tooluse — DeepEval built-ins
    "ToolUseMetric":
        "针对任务，助手选用的工具集合是否合理（选对了工具吗）。",
    "ArgumentCorrectnessMetric":
        "每次工具调用的参数是否填对了（参数与输入需求是否匹配）。",
    # custom GEval rubric
    "GEval/DeliverableMatchesRequest":
        "用户在 scenario 里要的具体交付物（PPT/Excel/候选人名单/报告等）"
        "是否真的产出了，而不只是口头描述。",
    # noisy — kept for completeness
    "TopicAdherenceMetric":
        "对话是否始终围绕给定的话题列表。Mira 场景下判官抖动大，列为 noisy。",
    "KnowledgeRetentionMetric":
        "助手是否记住了多轮上下文里的关键信息。单轮 golden 上恒为 0，列为 noisy。",
}

# 一句话的极简说明，用在「按指标汇总」表的「说明」列，保持列宽。
_METRIC_TAGLINES: dict[str, str] = {
    "SessionHealthMetric":             "会话是否干净跑完",
    "ToolDependencyMetric":            "工具调用顺序是否违规",
    "TokensMetric":                    "token 总用量",
    "SessionCostMetric":               "会话总成本（USD）",
    "TimeToFirstTokenMetric":          "首 token 延迟",
    "SessionDurationMetric":           "会话总耗时",
    "RoleAdherenceMetric":             "是否保持 Mira 角色",
    "GoalAccuracyMetric":              "是否完成用户目标",
    "ToolUseMetric":                   "工具选择是否合理",
    "ArgumentCorrectnessMetric":       "工具参数是否正确",
    "GEval/DeliverableMatchesRequest": "交付物是否真的产出",
    "TopicAdherenceMetric":            "是否扣住给定话题",
    "KnowledgeRetentionMetric":        "是否记住上下文",
}


def _explanation_for(metric_name: str) -> str:
    """Look up the long explanation for a metric label.

    Handles the "GEval/Rubric [Conversational GEval]" suffix that
    ConversationalGEval metrics carry in their display name.
    """
    key = metric_name.split(" [")[0]
    return _METRIC_EXPLANATIONS.get(key, "")


def _tagline_for(metric_name: str) -> str:
    key = metric_name.split(" [")[0]
    return _METRIC_TAGLINES.get(key, "—")


def _profile_for(metric) -> str:
    """Look up metric's signal profile. Unknown metrics default to 'signal'."""
    label = _metric_label(metric)
    cls = type(metric).__name__
    return _METRIC_PROFILE.get(label) or _METRIC_PROFILE.get(cls) or "signal"


def _profile_for_label(label: str, cls: str) -> str:
    return _METRIC_PROFILE.get(label) or _METRIC_PROFILE.get(cls) or "signal"


# ── Layer 1 (per-category) + Layer 1 (broken global) skip policy ───────────
_CATEGORY_DEFAULT_SKIPS: dict[str, set[str]] = {
    "voice":       {"BiasMetric", "ToxicityMetric", "PIILeakageMetric",
                    "RoleViolationMetric", "KnowledgeRetentionMetric"},
    "ci_email":    {"BiasMetric", "ToxicityMetric", "PIILeakageMetric",
                    "RoleViolationMetric", "KnowledgeRetentionMetric"},
    "ci_dingding": {"BiasMetric", "ToxicityMetric", "PIILeakageMetric",
                    "RoleViolationMetric", "KnowledgeRetentionMetric"},
    "crm":         {"BiasMetric", "ToxicityMetric", "RoleViolationMetric",
                    "KnowledgeRetentionMetric"},
    # Uncategorised research/sourcing goldens keep the full 17 — they often
    # produce multi-turn content where these metrics still earn their keep.
}


def _resolve_skip_set(golden: dict) -> set[str]:
    """Class names + GEval labels to skip for this golden.

    Per-golden `_skip_metrics` overrides category defaults entirely. To
    *extend* defaults instead, the golden can set `_skip_metrics_extra`.
    """
    cat = golden.get("_category") or ""
    if "_skip_metrics" in golden:
        return set(golden["_skip_metrics"] or [])
    base = set(_CATEGORY_DEFAULT_SKIPS.get(cat, set()))
    extra = set(golden.get("_skip_metrics_extra") or [])
    return base | extra


def _should_skip(metric, golden: dict) -> tuple[bool, str | None]:
    """Decide whether to skip `metric` for this golden.

    Returns (skip?, reason). Reasons:
      - "broken-profile"  → Layer 1 global skip (DeepEval impl unreliable)
      - "category-policy" → Layer 1 per-category skip (no signal here)
    """
    if _profile_for(metric) == "broken":
        return True, "broken-profile"
    cls = type(metric).__name__
    skip = _resolve_skip_set(golden)
    if cls in skip or _metric_label(metric) in skip:
        return True, "category-policy"
    return False, None


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
    informational: bool = False  # record-only; not counted toward PASS/FAIL totals
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
    share_url: str | None = None  # public share URL (set in run_one_golden)


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


def _verdict(r: MetricResult) -> str:
    """Resolve a metric result to PASS / FAIL / ERROR / NONE / INCONCLUSIVE.

    Layer 2: when the audit detected the judge contradicting itself
    (verdict says FAIL but reason praises the output, or vice versa), we
    return INCONCLUSIVE instead of the original verdict. INCONCLUSIVE never
    counts toward PASS rate — it's a flag for "this measurement is untrustworthy".
    """
    # Informational metrics never gate — they're recorded for trend monitoring.
    # The renderer pulls the raw score/reason; verdict just controls which
    # bucket (and which icon) the metric lands in.
    if r.informational:
        return "INFO"
    if r.error:
        return "ERROR"
    if r.audit_warning:
        return "INCONCLUSIVE"
    if r.success is True:
        return "PASS"
    if r.success is False:
        return "FAIL"
    if r.score is None or r.threshold is None:
        return "NONE"
    return "PASS" if float(r.score) >= float(r.threshold) else "FAIL"


# ── Layer 2: judge consistency audit ───────────────────────────────────────
#
# DeepEval metrics use two independent LLM calls — `_generate_verdicts`
# (does the violation exist?) and `_generate_reason` (write me an
# explanation). These calls can disagree; we've seen RoleViolationMetric
# verdicts=yes (score 0.00) paired with reason="no actual role-breaking
# behavior occurred ... assistant fully maintained boundaries". The metric's
# success bit follows the score, so the bug poses as a real FAIL.
#
# This audit scans the reason text for phrases that flatly contradict the
# numeric verdict. When found, the result is marked INCONCLUSIVE and the
# warning is surfaced in the report.
_INNOCENT_PHRASES = (
    "no actual", "no role violation", "no role-breaking",
    "fully maintained", "properly used", "preserved its identity",
    "did not fabricate", "did not invent", "did not claim",
    "no bias detected", "no toxicity detected", "no privacy",
    "no pii", "respected", "no violation",
)
_GUILTY_PHRASES = (
    "fabricated", "violated", "failed to deliver", "did not deliver",
    "made up", "invented", "leaked", "incorrect", "wrong",
    "off-topic", "unwarranted",
)


_SCORE_DECL_RE = re.compile(
    r"(?:分数(?:为|是)|得分(?:为|是)|score\s+is|score\s*[:=])\s*([0-9](?:\.[0-9]+)?)",
    re.IGNORECASE,
)


def _audit_judge_consistency(score, threshold, success, reason) -> str | None:
    """Return a warning string if verdict and reason directionally disagree,
    OR if reason declares multiple distinct numeric scores (judge wrote a
    contradictory multi-paragraph reason — observed on ToolUseMetric where
    one paragraph says '分数为 0.5' and another says '得分为 1.0').
    """
    if reason is None:
        return None
    text = reason.lower()

    # ── Self-contradicting numeric scores within a single reason ────────────
    matches = _SCORE_DECL_RE.findall(reason)
    distinct = {f"{float(m):.2f}" for m in matches if 0.0 <= float(m) <= 1.0}
    if len(distinct) >= 2:
        return (
            f"judge contradiction: reason declares {len(distinct)} different scores "
            f"({', '.join(sorted(distinct))}) — multi-paragraph judge output disagrees with itself"
        )

    # Determine the *numeric* verdict (ignoring our INCONCLUSIVE machinery)
    if success is True:
        numeric_verdict = "PASS"
    elif success is False:
        numeric_verdict = "FAIL"
    elif score is None or threshold is None:
        return None
    else:
        numeric_verdict = "PASS" if float(score) >= float(threshold) else "FAIL"

    if numeric_verdict == "FAIL":
        hits = sum(p in text for p in _INNOCENT_PHRASES)
        guilty = sum(p in text for p in _GUILTY_PHRASES)
        if hits >= 2 and guilty == 0:
            return f"judge contradiction: verdict=FAIL but reason describes no violation ({hits} innocent phrases)"
    elif numeric_verdict == "PASS":
        guilty = sum(p in text for p in _GUILTY_PHRASES)
        innocent = sum(p in text for p in _INNOCENT_PHRASES)
        if guilty >= 3 and innocent == 0:
            return f"judge contradiction: verdict=PASS but reason describes problems ({guilty} guilty phrases)"
    return None


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

    # Deepcopy every metric instance so this golden gets its own state. The
    # module-level metrics in test_mira_*.py are shared across goldens; when
    # report runs goldens in parallel, two threads measuring on the same
    # instance would race on self.score / self.reason / self.evaluation_steps.
    # Deepcopy is cheap (~10 ms per metric set) and removes the entire class
    # of races without needing per-metric locks.
    metric_rows = [(f, s, copy.deepcopy(m)) for f, s, m in _collect_metrics()]
    for file_label, scope, metric in metric_rows:
        display = _metric_label(metric)
        cls = type(metric).__name__
        profile = _profile_for(metric)
        skip, skip_reason = _should_skip(metric, golden)
        if skip:
            tag = "broken" if skip_reason == "broken-profile" else "category-policy"
            print(f"     ⊘ [{file_label:<7}/{scope:<6}] {display:<38}    —      —  SKIP   ({tag})")
            continue
        t1 = time.time()
        informational = bool(getattr(metric, "informational", False))
        if scope == "multi":
            r = _score_one(metric, conv_case)
            mr = MetricResult(
                file=file_label, scope=scope, metric=display, cls=cls,
                score=r["score"], threshold=r["threshold"],
                success=r["success"], reason=r["reason"], error=r["error"],
                elapsed_s=r["elapsed"], n_cases=1,
                profile=profile,
                audit_warning=_audit_judge_consistency(
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
                # success=None marks "metric did not apply" — exclude from the
                # all() aggregate so N/A cases don't flip a passing metric to FAIL.
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
                    audit_warning=_audit_judge_consistency(avg_score, thr, agg_success, first_reason),
                    informational=informational,
                )
        out.metric_results.append(mr)
        verdict = _verdict(mr)
        flag = {"PASS": "✓", "FAIL": "✗", "ERROR": "!", "NONE": "·", "INCONCLUSIVE": "?", "INFO": "ℹ"}.get(verdict, "?")
        score_s = f"{mr.score:.2f}" if mr.score is not None else "  —  "
        thr_s = f"≥{mr.threshold:.2f}" if mr.threshold is not None else "    "
        tail = "  " + mr.audit_warning if mr.audit_warning else ""
        prof = f" [{profile}]" if profile != "signal" else ""
        print(f"     {flag} [{file_label:<7}/{scope:<6}] {display:<38} {score_s} {thr_s} {verdict:<13}{prof}  {mr.elapsed_s:>5.1f}s{tail}")
    return out


# ── reporters ───────────────────────────────────────────────────────────────

def _fmt_score(s: float | None) -> str:
    return f"{s:.2f}" if isinstance(s, (int, float)) else "—"


def _fmt_threshold(thr: float | None, informational: bool = False) -> str:
    """Render a threshold cell — empty for informational / inf / None.

    Informational metrics inherit a numeric default threshold from their
    subclass (e.g. TokensMetric defaults to 200_000), but the value is
    meaningless when the metric never gates — render it as "—" so readers
    don't mistake it for an active limit.
    """
    if informational:
        return "—"
    if thr is None:
        return "—"
    if isinstance(thr, float) and (thr == float("inf") or thr != thr):  # noqa: PLR0124 NaN check
        return "—"
    return f"≥{thr:.2f}"


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
                "task_url": _task_url(meta.get("task_url_base", ""), gr.conv_id),
                "share_url": gr.share_url,
                "session_warnings": gr.session_warnings,
                "drive_error": gr.drive_error,
                "metrics": [
                    {
                        "file": mr.file,
                        "scope": mr.scope,
                        "metric": mr.metric,
                        "cls": mr.cls,
                        "profile": mr.profile,
                        "informational": mr.informational,
                        "score": (float(mr.score) if mr.score is not None else None),
                        # Drop threshold for informational metrics: the subclass
                        # may carry a stale default (e.g. TokensMetric=200_000)
                        # but the metric never gates, so the value is misleading.
                        "threshold": (
                            None
                            if mr.informational
                            or mr.threshold is None
                            or mr.threshold == float("inf")
                            else float(mr.threshold)
                        ),
                        "success": mr.success,
                        "verdict": _verdict(mr),
                        "audit_warning": mr.audit_warning,
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
                "scores": [], "pass": 0, "fail": 0, "error": 0, "none": 0, "info": 0,
                "threshold": mr.threshold,
                "informational": mr.informational,
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
            "informational": d["informational"],
            "pass": d.get("pass", 0), "fail": d.get("fail", 0),
            "error": d.get("error", 0), "none": d.get("none", 0),
            "info": d.get("info", 0),
        })
    # Sort: most failures first, info-only metrics last
    rows.sort(key=lambda r: (
        r["informational"],
        -(r["fail"] + r["error"]),
        r["file"],
        r["metric"],
    ))
    return rows


# Metric buckets whose `score` is NOT a 0-1 quality measure — these get a
# PASS/FAIL count but are excluded from the category-level `avg_score`
# rollup (mixing tokens=12345 with relevancy=0.87 would be nonsense).
_NON_QUALITY_BUCKETS: frozenset[str] = frozenset({"ops"})


def _aggregate_category(results: list[GoldenResult]) -> list[dict]:
    by_cat: dict[str, dict] = {}
    for gr in results:
        cat = gr.category or "(uncat)"
        d = by_cat.setdefault(cat, {
            "category": cat, "n_goldens": 0,
            "pass": 0, "fail": 0, "error": 0, "none": 0, "info": 0,
            "scores": [],
        })
        d["n_goldens"] += 1
        for mr in gr.metric_results:
            v = _verdict(mr).lower()
            d[v] = d.get(v, 0) + 1
            # Skip non-quality buckets from the avg — absolute values
            # (tokens, USD, seconds) don't share a scale with 0-1 quality
            # scores and would skew the mean.
            if mr.score is not None and mr.file not in _NON_QUALITY_BUCKETS:
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

    # ── TOP-LEVEL VERDICT (Layer 4: split by profile) ──────────────────────
    # PASS 率只统计 signal 类指标。noisy 仅作辅助参考。broken 已 skip。
    # INFO（informational=True，主要是 ops 桶里的 token/cost/latency）也在
    # 这里计数但不进 PASS 率分母 —— 与 §按类别/按指标 的口径一致。
    def _tally(predicate) -> dict:
        d = {"PASS": 0, "FAIL": 0, "ERROR": 0, "NONE": 0, "INCONCLUSIVE": 0, "INFO": 0}
        for gr in results:
            for mr in gr.metric_results:
                if not predicate(mr):
                    continue
                v = _verdict(mr)
                d[v] = d.get(v, 0) + 1
        d["TOTAL"] = sum(d.values())
        # PASS 率：只看真正参与 gate 的两档；INFO/ERROR/NONE/INCONCLUSIVE 都不进分母
        denom = d["PASS"] + d["FAIL"]
        d["RATE"] = (d["PASS"] / denom * 100) if denom else 0.0
        return d

    signal_t = _tally(lambda mr: mr.profile == "signal")
    noisy_t  = _tally(lambda mr: mr.profile == "noisy")

    # Count metrics globally skipped via `broken` profile (per golden × per metric)
    n_broken_skipped = sum(
        1
        for gr in results
        for label, cls in [(m, c) for m, c in _METRIC_PROFILE.items()] if False
    )  # placeholder — broken skips don't produce MetricResults, so count differently:
    broken_metrics = [k for k, v in _METRIC_PROFILE.items() if v == "broken"]

    add(f"## 总评 — 仅信号指标（signal）")
    add("")
    add(f"> 信号指标 = 11 个可信、有判别力的 metric；这一行才是 Mira 真实表现的决策依据。")
    add(f"> Noisy / Broken 的分布看下面两节。`ℹ INFO` 是 `informational=True` 的 ops 指标（token/cost/latency），仅记录、不进 PASS 率分母。")
    add("")
    add(f"| 通过 ✓ | 失败 ✗ | 错误 ! | 缺失 · | 自相矛盾 ? | 仅记录 ℹ | 通过率（PASS / (PASS+FAIL)）|")
    add(f"|---:|---:|---:|---:|---:|---:|---:|")
    add(
        f"| **{signal_t['PASS']}** | **{signal_t['FAIL']}** | **{signal_t['ERROR']}** | "
        f"**{signal_t['NONE']}** | **{signal_t['INCONCLUSIVE']}** | **{signal_t['INFO']}** | "
        f"**{signal_t['RATE']:.1f}%** |"
    )
    add("")

    add(f"## 辅助参考 — noisy 指标（不进 PASS 率，仅供观察）")
    add("")
    if noisy_t["TOTAL"] == 0:
        add("（本次跑未产生 noisy 指标结果。）")
    else:
        add(f"| 通过 ✓ | 失败 ✗ | 错误 ! | 缺失 · | 自相矛盾 ? | 仅记录 ℹ |")
        add(f"|---:|---:|---:|---:|---:|---:|")
        add(
            f"| {noisy_t['PASS']} | {noisy_t['FAIL']} | {noisy_t['ERROR']} | "
            f"{noisy_t['NONE']} | {noisy_t['INCONCLUSIVE']} | {noisy_t['INFO']} |"
        )
        add("")
        add(f"> 这些 metric 在我们场景下判官抖动大或结构性假阳/假阴。不计入总评。")
    add("")

    add(f"## 全局 skip — broken 指标")
    add("")
    add(f"以下 metric 在 DeepEval 4.0 上对 Mira 场景被判定不可信，**所有 golden 一律跳过**：")
    add("")
    for k in broken_metrics:
        add(f"- `{k}` — _METRIC_PROFILE 标记为 broken")
    add("")

    # ── METRIC EXPLANATIONS ────────────────────────────────────────────────
    # 只列出本次跑实际产出的 metric — 让读者在看下面 PASS/FAIL 之前先弄清楚
    # 每个指标在测什么。
    seen_metrics: dict[str, dict] = {}
    for gr in results:
        for mr in gr.metric_results:
            seen_metrics.setdefault(
                mr.metric,
                {"file": mr.file, "informational": mr.informational},
            )
    if seen_metrics:
        add(f"## 指标说明")
        add("")
        add(f"> 下面每个指标分别在测什么 —— 看后面的 PASS/FAIL 时对照本节。"
            f"标 ℹ 的是 informational 指标，仅记录、不进 PASS 率。")
        add("")
        add("| 文件 | 指标 | 这个指标在测什么 |")
        add("|---|---|---|")
        for name in sorted(seen_metrics, key=lambda n: (seen_metrics[n]["file"], n)):
            info = seen_metrics[name]
            desc = _explanation_for(name) or "—"
            tag = " ℹ" if info["informational"] else ""
            add(f"| `{info['file']}` | `{name}`{tag} | {desc} |")
        add("")

    # ── PER-CATEGORY ROLLUP ────────────────────────────────────────────────
    cat_rows = _aggregate_category(results)
    add(f"## 按类别汇总")
    add("")
    add("| 类别 | Goldens | 平均分 | PASS | FAIL | ERROR | NONE | INFO |")
    add("|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in cat_rows:
        add(
            f"| `{r['category']}` | {r['n_goldens']} | {_fmt_score(r['avg_score'])} | "
            f"{r.get('pass',0)} | {r.get('fail',0)} | {r.get('error',0)} | "
            f"{r.get('none',0)} | {r.get('info',0)} |"
        )
    add("")

    # ── PER-GOLDEN ROLLUP ──────────────────────────────────────────────────
    add(f"## 按用例汇总")
    add("")
    add("| # | 类别 | Tier | 场景 | Mira 耗时 | 工具调用 | PASS / FAIL / ERR / INFO |")
    add("|---:|:---:|:---:|---|---:|---:|---|")
    for gr in results:
        p = sum(1 for mr in gr.metric_results if _verdict(mr) == "PASS")
        f = sum(1 for mr in gr.metric_results if _verdict(mr) == "FAIL")
        e = sum(1 for mr in gr.metric_results if _verdict(mr) == "ERROR")
        i = sum(1 for mr in gr.metric_results if _verdict(mr) == "INFO")
        cat = gr.category or "—"
        scenario_short = (gr.scenario or "")[:60]
        add(f"| {gr.index} | `{cat}` | {gr.tier or '—'} | {scenario_short} | {gr.mira_elapsed_s:.0f}s | {gr.n_tool_calls} | {p} / {f} / {e} / {i} |")
    add("")

    # ── PER-METRIC ROLLUP ──────────────────────────────────────────────────
    metric_rows = _aggregate_metric(results)
    add(f"## 按指标汇总")
    add("")
    add("| 文件 | 指标 | 说明 | 类型 | 平均值 | 阈值 | PASS | FAIL | ERROR | NONE | INFO |")
    add("|---|---|---|:---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in metric_rows:
        kind = "ℹ" if r["informational"] else "gate"
        thr = _fmt_threshold(r["threshold"], r["informational"])
        desc = _tagline_for(r["metric"])
        add(
            f"| `{r['file']}` | `{r['metric']}` | {desc} | {kind} | {_fmt_score(r['avg_score'])} | "
            f"{thr} | {r['pass']} | {r['fail']} | {r['error']} | {r['none']} | {r['info']} |"
        )
    add("")

    # ── DETAILED PER-GOLDEN BLOCKS ─────────────────────────────────────────
    add(f"## 每条用例详情")
    add("")
    url_base = meta.get("task_url_base") or ""
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
        share = (gr.share_url or "").strip()
        task = _task_url(url_base, gr.conv_id)
        if share:
            add(f"🔗 分享链接（公开访问）：<{share}>")
            add("")
        elif task:
            add(f"🔗 任务页面（需登录）：<{task}>")
            add("")
        url = share or task  # for the non-PASS block below
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

        add("| 文件 | 指标 | profile | 分数 | 阈值 | 判定 | 耗时 |")
        add("|---|---|:---:|---:|---:|:---:|---:|")
        for mr in gr.metric_results:
            v = _verdict(mr)
            badge = {
                "PASS": "✅ PASS", "FAIL": "❌ FAIL", "ERROR": "🚨 ERR",
                "NONE": "· NONE", "INCONCLUSIVE": "🟡 INCONCLUSIVE",
            }.get(v, v)
            thr = f"≥{mr.threshold:.2f}" if mr.threshold is not None else "—"
            score = _fmt_score(mr.score)
            prof_badge = {"signal": "🟢 signal", "noisy": "🟠 noisy"}.get(mr.profile, mr.profile)
            add(f"| `{mr.file}` | `{mr.metric}` | {prof_badge} | {score} | {thr} | {badge} | {mr.elapsed_s:.1f}s |")
        add("")

        # 全部 gate 类指标的 reason / error / audit。PASS 项也展开 ——
        # 没有判官的解释，读者看不懂分数是怎么来的。INFO 单独有自己的 block。
        gating = [mr for mr in gr.metric_results if _verdict(mr) != "INFO"]
        if gating:
            add("<details><summary>所有指标的 reason / error / audit（点击展开）</summary>")
            add("")
            if url:
                label = "分享链接（公开访问）" if share else "任务页面（需登录）"
                add(f"🔗 {label}：<{url}>")
                add("")
            for mr in gating:
                v = _verdict(mr)
                add(f"- **`{mr.metric}` ({v}, profile={mr.profile})** — score={_fmt_score(mr.score)} thr={_fmt_score(mr.threshold)}")
                if mr.audit_warning:
                    add(f"  - 🟡 audit: {mr.audit_warning}")
                if mr.error:
                    add(f"  - 🛑 error: `{mr.error[:300]}`")
                if mr.reason:
                    reason = mr.reason.replace("\n", " ").strip()[:600]
                    add(f"  - 💬 reason: {reason}")
            add("")
            add("</details>")
            add("")

        # INFO metrics (tokens / cost / latency) — show the raw recorded
        # values in a collapsed block so trends are easy to skim without
        # bloating the main verdict table.
        info_rows = [mr for mr in gr.metric_results if _verdict(mr) == "INFO"]
        if info_rows:
            add("<details><summary>ℹ 仅记录的指标（不参与判定）</summary>")
            add("")
            for mr in info_rows:
                score = _fmt_score(mr.score)
                bits = [f"- **`{mr.metric}`** = {score}"]
                if mr.reason:
                    bits.append(f"_{mr.reason.replace(chr(10), ' ').strip()[:300]}_")
                if mr.error:
                    bits.append(f"⚠ {mr.error[:200]}")
                add("  ".join(bits))
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
    """Push the cached tool registry into the driver. Returns
    (tool_count, source_label) for the report header."""
    cached = load_cached(env)
    registry = {t["name"]: t for t in (cached or {}).get("tools", [])}
    set_registry(registry)
    if not cached:
        return 0, "(empty: no cache yet)"
    return len(registry), f".cache/tools-{env}.json @ {cached.get('fetched_at','?')}"


def main() -> None:
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
    ap.add_argument("--golden-concurrency", type=int,
                    default=int(os.environ.get("MIRA_GOLDEN_CONCURRENCY", "4")),
                    help="max goldens to run in parallel after the first one "
                         "(default: $MIRA_GOLDEN_CONCURRENCY or 4; "
                         "set to 1 for fully sequential, e.g. when debugging)")
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

    # Strategy: run the first golden synchronously so we can refresh the tool
    # cache from its conv_id *before* the rest fire (so they all see the fresh
    # registry). Then parallelize the remaining goldens — each Mira session is
    # independent (different conv_id), and judge calls underneath are throttled
    # by claude_cli_judge's BoundedSemaphore so we don't flood the API.
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
        # Live progress: prints from concurrent goldens will interleave; each
        # line is self-identifying via the "[idx]" header / 5-space indent.
        # For a clean per-golden view, read reports/<out>.md after the run.
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
    n_info = sum(1 for gr in results for mr in gr.metric_results if _verdict(mr) == "INFO")
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
    print(f"🧾 JSON1 details    : {json_path}")
    print("=" * 80)


if __name__ == "__main__":
    main()
