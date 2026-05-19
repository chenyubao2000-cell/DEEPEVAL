"""Shared base class for value-style custom metrics.

Most of our custom metrics are *value* metrics: they emit a raw number
(tokens, USD, seconds) rather than a 0-1 quality score. The deviation from
deepeval's default convention:

- ``score`` carries the raw value (may be a count in the thousands).
- ``is_successful()`` compares ``score`` against ``threshold`` in a direction
  set by ``higher_is_better`` (default False — lower cost/time = better).
- ``informational=True`` makes the metric record-only: ``is_successful()``
  always returns True, the report renders it as ℹ️ INFO, and it does not
  count toward PASS/FAIL totals or pass-rate. Use for tokens / cost / latency
  signals you want to monitor without gating runs.
- Errors (missing conv_id, no Langfuse trace) set ``error`` + ``success=False``
  + ``score=0``; the eval still completes so the failure is visible in the
  report rather than crashing the suite. (For informational metrics, errors
  still surface in the report's ``reason``/``error`` text but do not flip
  ``success`` — informational means "never gate".)
"""
from __future__ import annotations

import math
from typing import Any, Optional

from deepeval.metrics import BaseMetric


def get_conv_id(test_case: Any) -> Optional[str]:
    """Pull ``conv_id`` off ``test_case.metadata``.

    Accepts either an ``LLMTestCase`` or ``ConversationalTestCase`` — both
    expose ``metadata`` (with ``additional_metadata`` as deprecated alias).
    Returns ``None`` if no metadata or no ``conv_id`` key; callers treat
    that as a fatal config error.
    """
    md = getattr(test_case, "metadata", None)
    if md is None:
        md = getattr(test_case, "additional_metadata", None)
    if not md:
        return None
    return md.get("conv_id")


class BaseValueMetric(BaseMetric):
    """Numeric value metric base class.

    Subclasses implement ``measure(test_case)`` to set ``self.score`` (raw
    value) and ``self.reason`` (one-line explanation). Pass/fail flips via
    ``higher_is_better``. Pass ``informational=True`` to skip gating entirely
    (the metric still records its value but never fails the run).
    """

    higher_is_better: bool = False
    informational: bool = False

    def __init__(
        self,
        threshold: Optional[float] = None,
        *,
        higher_is_better: bool = False,
        informational: bool = False,
        verbose_mode: bool = False,
    ) -> None:
        # threshold is optional for informational metrics — they don't gate,
        # so a sensible "never trip" default keeps the rendering machinery
        # happy without forcing callers to invent a number.
        if threshold is None:
            threshold = math.inf
        self.threshold = threshold
        self.higher_is_better = higher_is_better
        self.informational = informational
        self.async_mode = False
        self.strict_mode = False
        self.verbose_mode = verbose_mode
        self.include_reason = True
        self.score = None
        self.reason = None
        self.success = None
        self.error = None
        self.evaluation_cost = None

    def is_successful(self) -> bool:
        # Informational metrics never gate — they record a value and always
        # report success so pytest/assert_test won't fail the run. The report
        # renderer reads ``self.informational`` separately to emit an INFO
        # verdict instead of PASS.
        if self.informational:
            self.success = True
            return True
        if self.error is not None or self.score is None:
            self.success = False
            return False
        self.success = (
            self.score >= self.threshold
            if self.higher_is_better
            else self.score <= self.threshold
        )
        return self.success

    async def a_measure(self, test_case: Any, *args: Any, **kwargs: Any) -> float:
        return self.measure(test_case, *args, **kwargs)

    def _bail(self, reason: str) -> float:
        """Mark the metric as failed-with-error and return 0.

        For informational metrics, ``is_successful()`` will still report
        True afterwards — ``error`` and ``reason`` are kept for the report
        but the run isn't gated.
        """
        self.score = 0.0
        self.reason = reason
        self.error = reason
        self.success = False
        return 0.0
