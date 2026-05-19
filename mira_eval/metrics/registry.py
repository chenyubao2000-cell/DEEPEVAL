"""Active-metric & active-bucket allowlist.

Two layers of soft-disable:
  - ``ACTIVE_METRICS`` — which metric classes (or GEval rubrics) participate.
  - ``ACTIVE_FILES``   — which logical bucket participates. Buckets:
      e2e / custom / tooluse / safety / others / ops
    (Legacy ``test_mira_<bucket>.py`` filenames are accepted in env-var
    overrides for back-compat.)

A bucket is skipped when either its label is not in ``ACTIVE_FILES`` OR its
filtered metric list is empty after applying ``ACTIVE_METRICS``.

Nothing is *deleted*: metric definitions stay on disk and keep working.
Set ``MIRA_METRICS_ALL=1`` to bypass both layers.

Env-var overrides (no edit, single run):
  - ``MIRA_METRICS_ALL=1``                  run everything
  - ``MIRA_METRICS_ALLOWLIST=A,B``          replaces ACTIVE_METRICS
  - ``MIRA_FILES_ALLOWLIST=x.py,y.py``      replaces ACTIVE_FILES
"""
from __future__ import annotations

import os
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1: which metrics may participate
# ─────────────────────────────────────────────────────────────────────────────
# Keys are either:
#   - the metric class name        (e.g. "ToolUseMetric")
#   - "GEval/<rubric-name>"        (e.g. "GEval/DeliverableMatchesRequest")
ACTIVE_METRICS: frozenset[str] = frozenset({
    # Ops / health (custom)
    "SessionHealthMetric",
    "ToolDependencyMetric",
    "TokensMetric",
    "SessionCostMetric",
    "TimeToFirstTokenMetric",
    "SessionDurationMetric",
    # Multi-turn quality (deepeval built-ins)
    "GoalAccuracyMetric",
    "RoleAdherenceMetric",
    # Tool-use — ExpectedToolPathGEval (driven by _expected_tools per golden)
    # replaces the old ToolUseMetric. ArgumentCorrectnessMetric stays (different
    # signal axis — per-turn arg granularity).
    "GEval/ExpectedToolPath",
    "ArgumentCorrectnessMetric",
    # Custom GEval rubric
    "GEval/DeliverableMatchesRequest",
})


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2: which metric buckets may participate
# ─────────────────────────────────────────────────────────────────────────────
ACTIVE_FILES: frozenset[str] = frozenset({
    "test_mira_e2e.py",
    "test_mira_custom.py",
    "test_mira_tooluse.py",
    "test_mira_ops.py",
    # "test_mira_safety.py",     # no active metrics in default set
    # "test_mira_others.py",     # no active metrics in default set
})


_BUCKET_TO_LEGACY_FILE = {
    "e2e": "test_mira_e2e.py",
    "custom": "test_mira_custom.py",
    "tooluse": "test_mira_tooluse.py",
    "safety": "test_mira_safety.py",
    "others": "test_mira_others.py",
    "ops": "test_mira_ops.py",
}


# ─────────────────────────────────────────────────────────────────────────────
# Resolution
# ─────────────────────────────────────────────────────────────────────────────

def _label(metric) -> str:
    """Build the allowlist key for a metric instance.

    For ConversationalGEval, deepeval 4.0 makes ``__name__`` return
    "<rubric> [Conversational GEval]" — we use ``metric.name`` for a clean
    "GEval/<rubric>" key.
    """
    cls = type(metric).__name__
    if cls == "ConversationalGEval":
        rubric = getattr(metric, "name", None) or getattr(metric, "__name__", "")
        return f"GEval/{rubric}"
    return cls


def _all_mode() -> bool:
    return os.environ.get("MIRA_METRICS_ALL") == "1"


def _resolve_allowlist() -> frozenset[str] | None:
    if _all_mode():
        return None
    override = os.environ.get("MIRA_METRICS_ALLOWLIST", "").strip()
    if override:
        return frozenset(s.strip() for s in override.split(",") if s.strip())
    return ACTIVE_METRICS


def _resolve_files() -> frozenset[str] | None:
    if _all_mode():
        return None
    override = os.environ.get("MIRA_FILES_ALLOWLIST", "").strip()
    if override:
        return frozenset(s.strip() for s in override.split(",") if s.strip())
    return ACTIVE_FILES


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def is_active(metric) -> bool:
    """True iff this metric should run under the current allowlist policy."""
    active = _resolve_allowlist()
    if active is None:
        return True
    return _label(metric) in active or type(metric).__name__ in active


def filter_active(metrics: list) -> list:
    """Drop any metric not in the active allowlist."""
    return [m for m in metrics if is_active(m)]


def is_bucket_active(bucket_or_filename: str) -> bool:
    """True iff this bucket label or legacy filename is active."""
    active = _resolve_files()
    if active is None:
        return True
    if bucket_or_filename in active:
        return True
    legacy = _BUCKET_TO_LEGACY_FILE.get(bucket_or_filename)
    if legacy and legacy in active:
        return True
    return False


def is_file_active(file_path: str) -> bool:
    """Legacy alias accepting a filename or path."""
    name = Path(file_path).name
    return is_bucket_active(name)


def skip_reason(file_path: str, metrics: list) -> str | None:
    """Return None if this bucket should run; else a short reason string."""
    name = Path(file_path).name
    if not is_file_active(file_path):
        return f"{name} not in ACTIVE_FILES (mira_eval.metrics.registry)"
    if not metrics:
        return f"no active metrics for {name} (mira_eval.metrics.registry)"
    return None


def make_skip_mark(file_path: str, metrics: list):
    """Build the pytestmark a (legacy) pytest module should use."""
    import pytest  # local import keeps this module pytest-free at import time
    reason = skip_reason(file_path, metrics)
    return pytest.mark.skipif(reason is not None, reason=reason or "")
