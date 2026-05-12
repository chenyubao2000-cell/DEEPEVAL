"""Run-health metric for Mira: did the agent's execution complete cleanly?

This metric is intentionally **not** a judgement of output quality or goal
achievement — it's a mechanical check on execution-time signals. Use
GoalAccuracyMetric / DeliverableMatchesRequest for "did the agent achieve
the user's goal".

Fail conditions (any one triggers FAIL):
  - `stream_truncated` warning in `MiraSession.warnings` (SSE was cut by peer
    before the response finished — agent cannot have produced a complete reply)
  - Any tool call ended with `status == "error"`
  - An assistant turn whose content is empty / whitespace-only

Explicitly **not** a fail condition:
  - `auto_approve round N: <tool>(no_output)` warnings — these are common in
    HITL flows (confirm / clarify_question with default-accept) and were
    flagged as acceptable per project decision.

The metric reads `test_case.metadata['mira_warnings']` and
`test_case.metadata['mira_tool_errors']`, populated by
`_driver.build_conversational(..., session=session)`.

No LLM judge — score is binary 1.0 / 0.0, zero cost, instant.
"""
from __future__ import annotations

import pytest

from deepeval import assert_test
from deepeval.metrics import BaseConversationalMetric
from deepeval.test_case import ConversationalTestCase

from tests.evals._driver import (
    build_conversational,
    drive_mira,
    golden_id,
    load_goldens,
)


# Substrings in MiraSession.warnings that we treat as hard failures.
# Keep this list small and explicit; everything else is informational.
_FATAL_WARNING_SUBSTRS: tuple[str, ...] = ("stream_truncated",)


class RunCompletionMetric(BaseConversationalMetric):
    def __init__(self, threshold: float = 1.0, async_mode: bool = False):
        self.threshold = threshold
        self.async_mode = async_mode
        self.score: float | None = None
        self.success: bool | None = None
        self.reason: str | None = None
        self.error: str | None = None
        self.evaluation_cost = 0  # no LLM call → no spend

    def measure(self, test_case: ConversationalTestCase) -> float:
        try:
            issues: list[str] = []
            meta = test_case.metadata or {}

            for w in (meta.get("mira_warnings") or []):
                if any(s in w for s in _FATAL_WARNING_SUBSTRS):
                    issues.append(w[:120])

            for terr in (meta.get("mira_tool_errors") or []):
                issues.append(f"tool error: {terr[:100]}")

            for i, turn in enumerate(test_case.turns or []):
                if turn.role == "assistant" and not (turn.content or "").strip():
                    issues.append(f"empty assistant turn @ idx {i}")

            if issues:
                self.score = 0.0
                self.success = False
                self.reason = "; ".join(issues)
            else:
                self.score = 1.0
                self.success = True
                self.reason = "ok"
            return self.score
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            raise

    async def a_measure(self, test_case: ConversationalTestCase) -> float:
        # No I/O. Sync path is the source of truth.
        return self.measure(test_case)

    def is_successful(self) -> bool:
        if self.error is not None:
            self.success = False
        else:
            try:
                self.success = self.score is not None and self.score >= self.threshold
            except TypeError:
                self.success = False
        return self.success

    @property
    def __name__(self):
        return "RunCompletion"


METRICS = [
    RunCompletionMetric(threshold=1.0, async_mode=False),
]


@pytest.mark.parametrize("golden", load_goldens(), ids=golden_id)
def test_mira_run_completion(golden: dict):
    session, turns = drive_mira(golden)
    test_case = build_conversational(golden, turns, session=session)
    assert_test(test_case=test_case, metrics=METRICS)
