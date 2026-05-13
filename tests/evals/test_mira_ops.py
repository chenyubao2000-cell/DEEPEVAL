"""Operational metrics for Mira — token / cost / latency / completion checks.

These are *operational* signals, not output quality:
- Did the trace finish cleanly? (Completed)
- Are messages persisted correctly? (DatabaseStatus)
- How many tokens / USD / seconds did Mira burn? (Tokens, SessionCost,
  TimeToFirstToken, SessionDuration, OutputTokensPerSec, NTurns)
- Did the tool call path violate any documented constraint? (ToolDependency)

All metrics read ``conv_id`` from ``test_case.metadata`` (populated by
``_driver.build_conversational(session=...)``). 8 of the 9 are deterministic
(zero judge cost); ``ToolDependencyMetric`` is the only one that fires a
local claude CLI call, and only when tools were actually invoked.

Thresholds are deliberately permissive — they catch *broken* behaviour, not
"could be faster". Tune per workload.
"""
from __future__ import annotations

import pytest

from tests.evals._driver import (
    build_conversational,
    drive_mira,
    golden_id,
    load_goldens,
)

from custom_metrics import (
    CompletedMetric,
    DatabaseStatusMetric,
    NTurnsMetric,
    OutputTokensPerSecMetric,
    SessionCostMetric,
    SessionDurationMetric,
    TimeToFirstTokenMetric,
    TokensMetric,
    ToolDependencyMetric,
)


# Single METRICS list reused by both pytest and report.py's `_collect_metrics`.
# Reading values: pass/fail thresholds chosen to flag *broken* runs (timeouts,
# stalled streams, cost blow-outs) rather than micro-optimisations.
METRICS = [
    # Real gates — these can break a run.
    CompletedMetric(),                                 # binary: trace healthy
    DatabaseStatusMetric(),                            # binary: messages persisted ok
    NTurnsMetric(threshold=30),                        # runaway-conversation cap
    ToolDependencyMetric(threshold=0.8),               # ≥80% steps must comply
    # Informational — recorded for trend monitoring, never fails the run.
    # Pick a real threshold + drop ``informational=True`` if you decide to
    # gate any of these later.
    TokensMetric(informational=True),                  # tokens per session
    SessionCostMetric(informational=True),             # USD per session
    TimeToFirstTokenMetric(informational=True),        # seconds
    SessionDurationMetric(informational=True),         # seconds (wall clock)
    OutputTokensPerSecMetric(informational=True),      # tok/s streaming
]


@pytest.mark.parametrize("golden", load_goldens(), ids=golden_id)
def test_mira_ops(golden: dict) -> None:
    """Run all 9 ops metrics against one golden's resulting Mira session.

    Note: we bypass ``deepeval.assert_test`` here because these metrics
    inherit ``BaseMetric`` (single-turn) but consume a
    ``ConversationalTestCase`` via duck-typing on ``metadata``. Mixing
    multi-turn cases with single-turn metrics confuses deepeval's pipeline,
    so we drive the metrics directly and use plain ``assert``.
    """
    session, turns = drive_mira(golden)
    test_case = build_conversational(golden, turns, session=session)

    failures: list[str] = []
    for metric in METRICS:
        try:
            metric.measure(test_case)
            metric.is_successful()
        except Exception as e:  # noqa: BLE001 — surface judge / DB / network errors
            failures.append(f"{metric.__name__}: {type(e).__name__}: {e!s}")
            continue
        if metric.success is False:
            failures.append(
                f"{metric.__name__}: score={metric.score} threshold={metric.threshold} "
                f"reason={metric.reason}"
            )

    assert not failures, "ops metrics failed:\n  " + "\n  ".join(failures)
