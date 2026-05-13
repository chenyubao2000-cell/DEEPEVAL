"""Active-metric & active-file allowlist for the Mira eval suite.

Two layers of soft-disable, both edited from this single file:

  - ``ACTIVE_METRICS`` — which metric classes (or GEval rubrics) participate.
  - ``ACTIVE_FILES``   — which ``test_mira_*.py`` files participate.

A test file is skipped (module-level, one log line per file) when either
its basename is not in ``ACTIVE_FILES`` OR its filtered ``METRICS`` list
is empty after applying ``ACTIVE_METRICS``.

Nothing is *deleted*: metric definitions and test files stay on disk and
keep working. Add a name back to ``ACTIVE_METRICS`` / ``ACTIVE_FILES`` to
re-enable. To run the entire suite ignoring both layers, set
``MIRA_METRICS_ALL=1``.

Env-var overrides (no edit, single run)
---------------------------------------
- ``MIRA_METRICS_ALL=1``        run everything, bypass BOTH layers
- ``MIRA_METRICS_ALLOWLIST=A,B``  comma-separated, replaces ACTIVE_METRICS
- ``MIRA_FILES_ALLOWLIST=x.py,y.py`` comma-separated, replaces ACTIVE_FILES
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
#     used to single out individual ConversationalGEval rubrics that all
#     share the class name "ConversationalGEval".
ACTIVE_METRICS: frozenset[str] = frozenset({
    # Ops / health (custom_metrics)
    "SessionHealthMetric",
    "ToolDependencyMetric",
    "TokensMetric",
    "SessionCostMetric",
    "TimeToFirstTokenMetric",
    "SessionDurationMetric",
    # Multi-turn quality (deepeval built-ins)
    "GoalAccuracyMetric",
    "RoleAdherenceMetric",
    # Tool-use
    "ToolUseMetric",
    "ArgumentCorrectnessMetric",
    # Custom GEval rubric
    "GEval/DeliverableMatchesRequest",
})


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2: which test files may participate
# ─────────────────────────────────────────────────────────────────────────────
# Use the filename (basename, with .py). Comment out a line to disable that
# file at module level — no pytest.skip noise per golden, just one line.
#
# test_mira.py is currently commented out because every metric it can run
# (RoleAdherence, GoalAccuracy) is already covered by test_mira_e2e.py.
# Enable it again if you re-introduce its file-unique GEval rubrics into
# ACTIVE_METRICS above.
ACTIVE_FILES: frozenset[str] = frozenset({
    # "test_mira.py",            # superseded by test_mira_e2e.py for the active set
    "test_mira_e2e.py",
    "test_mira_custom.py",
    "test_mira_tooluse.py",
    "test_mira_ops.py",
    # "test_mira_safety.py",     # no active metrics in the default 11-metric set
    # "test_mira_others.py",     # no active metrics in the default 11-metric set
})


# ─────────────────────────────────────────────────────────────────────────────
# Resolution helpers
# ─────────────────────────────────────────────────────────────────────────────

def _label(metric) -> str:
    """Build the allowlist key for a metric instance.

    For ConversationalGEval, deepeval 4.0 makes ``__name__`` return
    "<rubric> [Conversational GEval]" — we use ``metric.name`` (which holds
    just the rubric) for a clean "GEval/<rubric>" key. Falls back to
    ``__name__`` then to the class name for plain metrics.
    """
    cls = type(metric).__name__
    if cls == "ConversationalGEval":
        rubric = getattr(metric, "name", None) or getattr(metric, "__name__", "")
        return f"GEval/{rubric}"
    return cls


def _all_mode() -> bool:
    return os.environ.get("MIRA_METRICS_ALL") == "1"


def _resolve_allowlist() -> frozenset[str] | None:
    """Active metric allowlist for this process.

    Returns None when MIRA_METRICS_ALL=1 (meaning "do not filter").
    """
    if _all_mode():
        return None
    override = os.environ.get("MIRA_METRICS_ALLOWLIST", "").strip()
    if override:
        return frozenset(s.strip() for s in override.split(",") if s.strip())
    return ACTIVE_METRICS


def _resolve_files() -> frozenset[str] | None:
    """Active file allowlist for this process.

    Returns None when MIRA_METRICS_ALL=1 (meaning "do not filter").
    """
    if _all_mode():
        return None
    override = os.environ.get("MIRA_FILES_ALLOWLIST", "").strip()
    if override:
        return frozenset(s.strip() for s in override.split(",") if s.strip())
    return ACTIVE_FILES


# ─────────────────────────────────────────────────────────────────────────────
# Public API used by test modules
# ─────────────────────────────────────────────────────────────────────────────

def is_active(metric) -> bool:
    """True iff this metric should run under the current allowlist policy."""
    active = _resolve_allowlist()
    if active is None:
        return True
    return _label(metric) in active or type(metric).__name__ in active


def filter_active(metrics: list) -> list:
    """Drop any metric not in the active allowlist.

    Construction still happens (cheap — constructors only store params), but
    the returned list reflects what will actually be measured.
    """
    return [m for m in metrics if is_active(m)]


def is_file_active(file_path: str) -> bool:
    """True iff this test file's basename is in the active file allowlist."""
    active = _resolve_files()
    if active is None:
        return True
    return Path(file_path).name in active


def skip_reason(file_path: str, metrics: list) -> str | None:
    """Return None if this file should run; else a short reason string."""
    name = Path(file_path).name
    if not is_file_active(file_path):
        return f"{name} not in ACTIVE_FILES (tests/evals/_metrics_config.py)"
    if not metrics:
        return f"no active metrics for {name} (tests/evals/_metrics_config.py)"
    return None


def make_skip_mark(file_path: str, metrics: list):
    """Build the pytestmark this file should use.

    Usage at module top, after METRICS = filter_active([...])::

        pytestmark = make_skip_mark(__file__, METRICS)
    """
    import pytest  # local import keeps this module pytest-free at import time
    reason = skip_reason(file_path, metrics)
    return pytest.mark.skipif(reason is not None, reason=reason or "")
