"""Verdict resolution + judge-consistency audit.

Pure functions over MetricResult fields (score / threshold / success / reason).
Imported by pipeline.py (sets MetricResult.audit_warning during scoring) and
by report.writers (rendering PASS/FAIL/INFO badges + the broken-metrics list).

Three concepts live here because they all answer the question "is this result
trustworthy enough to gate the run":

  - METRIC_PROFILE          — Layer 1: per-metric tag {signal, noisy, broken}
  - audit_judge_consistency — Layer 2: scan reason text for self-contradiction
  - verdict                 — final PASS / FAIL / ERROR / NONE / INCONCLUSIVE / INFO
"""
from __future__ import annotations

import re
from typing import Protocol


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1: per-metric signal profile
# ─────────────────────────────────────────────────────────────────────────────
# Tags every metric with one of three signal qualities. Each tier changes how
# the metric is treated in the report:
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
# Keys are either ClassName or "GEval/<rubric>" labels.
METRIC_PROFILE: dict[str, str] = {
    # signal — decision-grade
    "SessionHealthMetric":              "signal",
    "ConversationCompletenessMetric":   "signal",
    "TurnRelevancyMetric":              "signal",
    "RoleAdherenceMetric":              "signal",
    "GoalAccuracyMetric":               "signal",
    "GEval/ProfessionalNoFabrication":  "signal",
    "GEval/DeliverableMatchesRequest":  "signal",
    "GEval/GroundedNoFabrication":      "signal",
    "GEval/ExpectedToolPath":           "signal",
    "ArgumentCorrectnessMetric":        "signal",
    "AnswerRelevancyMetric":            "signal",
    "PromptAlignmentMetric":            "signal",
    # noisy — supplementary, won't drag PASS rate
    "TopicAdherenceMetric":             "noisy",
    "KnowledgeRetentionMetric":         "noisy",
    # broken — globally skipped
    "RoleViolationMetric":              "broken",
    "BiasMetric":                       "broken",
    "ToxicityMetric":                   "broken",
    "PIILeakageMetric":                 "broken",
}


def metric_label(metric) -> str:
    """Display label: 'GEval/<rubric>' for ConversationalGEval, else class name."""
    name = getattr(metric, "__name__", None) or type(metric).__name__
    cls = type(metric).__name__
    if cls == "ConversationalGEval":
        return f"GEval/{name}"
    return cls


def profile_for(metric) -> str:
    """Look up metric's signal profile. Unknown metrics default to 'signal'."""
    label = metric_label(metric)
    cls = type(metric).__name__
    return METRIC_PROFILE.get(label) or METRIC_PROFILE.get(cls) or "signal"


def profile_for_label(label: str, cls: str) -> str:
    return METRIC_PROFILE.get(label) or METRIC_PROFILE.get(cls) or "signal"


def broken_metric_keys() -> list[str]:
    """Return all keys tagged 'broken' (for the report header)."""
    return [k for k, v in METRIC_PROFILE.items() if v == "broken"]


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2: judge consistency audit
# ─────────────────────────────────────────────────────────────────────────────
# DeepEval metrics use two independent LLM calls — `_generate_verdicts`
# (does the violation exist?) and `_generate_reason` (write me an
# explanation). These calls can disagree; we've seen RoleViolationMetric
# verdicts=yes (score 0.00) paired with reason="no actual role-breaking
# behavior occurred ... assistant fully maintained boundaries". The metric's
# success bit follows the score, so the bug poses as a real FAIL.
#
# This audit scans the reason text for phrases that flatly contradict the
# numeric verdict. When found, the result is marked INCONCLUSIVE.
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


def audit_judge_consistency(score, threshold, success, reason) -> str | None:
    """Return a warning string if verdict and reason directionally disagree.

    Two checks:
      1. Multiple distinct numeric scores declared in reason text
         (multi-paragraph judge output disagreeing with itself).
      2. Numeric verdict (PASS/FAIL) contradicts the lexical sentiment of
         the reason (FAIL but reason describes no violation, or vice versa).
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


# ─────────────────────────────────────────────────────────────────────────────
# Verdict resolution
# ─────────────────────────────────────────────────────────────────────────────

class _VerdictInput(Protocol):
    """Duck-typed contract — anything with these fields can be passed to verdict()."""
    informational: bool
    error: str | None
    audit_warning: str | None
    success: bool | None
    score: float | None
    threshold: float | None


def verdict(r: _VerdictInput) -> str:
    """Resolve a metric result to PASS / FAIL / ERROR / NONE / INCONCLUSIVE / INFO.

    Layer 2: when the audit detected the judge contradicting itself, returns
    INCONCLUSIVE instead of the original verdict. INCONCLUSIVE never counts
    toward PASS rate — it's a flag for "this measurement is untrustworthy".

    Informational metrics never gate — they're recorded for trend monitoring;
    the renderer pulls the raw score/reason and verdict just controls bucket.
    """
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
