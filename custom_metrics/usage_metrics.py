"""Token and USD-cost metrics, sourced from Langfuse traces."""
from __future__ import annotations

from typing import Any

from ._base import BaseValueMetric, get_conv_id
from ._langfuse import (
    fetch_session_traces,
    hydrate_trace,
    llm_generations,
    merge_observations,
    obs_cost_usd,
    trace_cost_usd,
    usage_input_tokens,
    usage_output_tokens,
)


class TokensMetric(BaseValueMetric):
    """Sum of ``usage.input + usage.output`` across all LLM generations in
    the Mira session bound to ``test_case.metadata['conv_id']``.

    ``score`` is the absolute token count (not normalised). Pass = under the
    budget. Default 200k is a generous ceiling for multi-turn sessions; tune
    per use case.
    """

    def __init__(self, threshold: float = 200_000, **kwargs: Any) -> None:
        # ``higher_is_better`` is fixed for this metric type — kwargs override
        # would be wrong. ``deepeval.copy_metrics`` re-instantiates us with
        # introspected kwargs, so we ``pop`` to avoid duplicate-keyword TypeError.
        kwargs.pop("higher_is_better", None)
        super().__init__(threshold=threshold, higher_is_better=False, **kwargs)

    @property
    def __name__(self) -> str:  # noqa: D401
        return "Tokens"

    def measure(self, test_case: Any, *_: Any, **__: Any) -> float:
        conv_id = get_conv_id(test_case)
        if not conv_id:
            return self._bail("no conv_id on test_case.metadata")

        traces = fetch_session_traces(conv_id)
        if not traces:
            return self._bail(f"no Langfuse traces for session_id={conv_id}")

        obs = merge_observations(traces)
        gens = llm_generations(obs)
        if not gens:
            return self._bail(f"no LLM generations in {len(traces)} trace(s)")

        total_in = sum(usage_input_tokens(g) for g in gens)
        total_out = sum(usage_output_tokens(g) for g in gens)
        total = total_in + total_out

        self.score = float(total)
        self.reason = (
            f"input={total_in}, output={total_out}, total={total} "
            f"(traces={len(traces)}, generations={len(gens)})"
        )
        self.is_successful()
        return self.score


class SessionCostMetric(BaseValueMetric):
    """Total USD cost across all traces for the session.

    Priority cascade per trace (mirrors Mira_Validation's evaluator):
    ``trace.totalCost`` → ``trace.calculatedTotalCost`` → ``trace.cost`` →
    sum of observation-level costs.

    ``score`` is in USD. Default threshold $0.50 is a per-conversation soft
    cap; tune per use case.
    """

    def __init__(self, threshold: float = 0.50, **kwargs: Any) -> None:
        kwargs.pop("higher_is_better", None)
        super().__init__(threshold=threshold, higher_is_better=False, **kwargs)

    @property
    def __name__(self) -> str:  # noqa: D401
        return "SessionCost"

    def measure(self, test_case: Any, *_: Any, **__: Any) -> float:
        conv_id = get_conv_id(test_case)
        if not conv_id:
            return self._bail("no conv_id on test_case.metadata")

        traces = fetch_session_traces(conv_id)
        if not traces:
            return self._bail(f"no Langfuse traces for session_id={conv_id}")

        total = 0.0
        for short in traces:
            full = hydrate_trace(short)
            trace_total = trace_cost_usd(full)
            if trace_total > 0:
                total += trace_total
                continue
            # Fall back to summing per-observation cost.
            total += sum(obs_cost_usd(o) for o in (full.observations or []))

        self.score = round(total, 6)
        self.reason = f"${self.score:.6f} across {len(traces)} trace(s)"
        self.is_successful()
        return self.score
