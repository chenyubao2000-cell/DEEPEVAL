"""Metrics — DeepEval built-in wrappers + custom rubrics + ops metrics.

Layout mirrors deepeval's `metrics/`: each non-trivial metric gets its own
subpackage (e.g. `expected_tool_path/`, `tool_dependency/`); thin wrappers
around DeepEval built-ins are collected in `builtins.py`.

  - registry.py        — ACTIVE_METRICS / ACTIVE_FILES allowlist
  - builtins.py        — DeepEval built-in metric instances (one list per bucket)
  - expected_tool_path/ — ConversationalGEval rubric for tool-call path
  - deliverable_match/  — ConversationalGEval for "produced what was asked"
  - tool_dependency/    — judge-based tool sequence constraint check
  - session_health/     — client + trace + persistence health gate
  - usage/              — tokens + cost (Langfuse-sourced)
  - perf/               — TTFT + duration + nturns + tok/s

Collect everything in pipeline order with `collect_metric_rows()`.
"""
from .registry import (
    ACTIVE_FILES,
    ACTIVE_METRICS,
    filter_active,
    is_active,
    is_file_active,
    make_skip_mark,
    skip_reason,
)

__all__ = [
    "ACTIVE_FILES",
    "ACTIVE_METRICS",
    "filter_active",
    "is_active",
    "is_file_active",
    "make_skip_mark",
    "skip_reason",
    "collect_metric_rows",
]


def collect_metric_rows():
    """Return (bucket_label, scope, metric_instance) tuples for the active suite.

    Imported lazily so that constructing judge / metric instances only happens
    when the pipeline actually starts — deepeval pulls in heavy deps on metric
    construction.
    """
    from . import builtins
    from .expected_tool_path import EXPECTED_TOOL_PATH_METRIC
    from .deliverable_match import GEVAL_METRICS
    from .tool_dependency import ToolDependencyMetric
    from .session_health import SessionHealthMetric
    from .usage import SessionCostMetric, TokensMetric
    from .perf import (
        NTurnsMetric,
        OutputTokensPerSecMetric,
        SessionDurationMetric,
        TimeToFirstTokenMetric,
    )

    # Ops bucket: assembled here so the metric subpackages stay lean.
    ops_metrics = filter_active([
        SessionHealthMetric(),
        NTurnsMetric(threshold=30),
        ToolDependencyMetric(threshold=0.8),
        TokensMetric(informational=True),
        SessionCostMetric(informational=True),
        TimeToFirstTokenMetric(informational=True),
        SessionDurationMetric(informational=True),
        OutputTokensPerSecMetric(informational=True),
    ])

    # Tooluse multi bucket: GEval (in own subpackage) + TopicAdherence (builtins).
    tooluse_multi = filter_active([
        EXPECTED_TOOL_PATH_METRIC,
        *builtins.TOOLUSE_MULTI_EXTRA,
    ])

    rows: list[tuple[str, str, object]] = []
    for m in ops_metrics:
        rows.append(("ops", "multi", m))
    for m in builtins.E2E_METRICS:
        rows.append(("e2e", "multi", m))
    for m in GEVAL_METRICS:
        rows.append(("custom", "multi", m))
    for m in tooluse_multi:
        rows.append(("tooluse", "multi", m))
    if builtins.ARG_CORRECTNESS_METRIC is not None:
        rows.append(("tooluse", "single", builtins.ARG_CORRECTNESS_METRIC))
    for m in builtins.SAFETY_METRICS:
        rows.append(("safety", "single", m))
    for m in builtins.OTHERS_METRICS:
        rows.append(("others", "single", m))
    return rows
