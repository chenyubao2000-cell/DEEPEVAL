"""Latency / throughput metrics, sourced from Langfuse trace timings."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Optional

from ...tracing.langfuse import (
    fetch_session_traces,
    hydrate_trace,
    llm_generations,
    merge_observations,
    usage_output_tokens,
)
from .._base import BaseValueMetric, get_conv_id


def _as_timestamp(value: Any) -> Optional[float]:
    """Coerce Langfuse's mixed datetime/str/None into a Unix timestamp."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        # ISO 8601 with optional Z suffix.
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _llm_observation_span(observations: Iterable[Any]) -> tuple[Optional[float], Optional[float]]:
    """Min start_time and max end_time across LLM generations, as Unix ts."""
    starts: list[float] = []
    ends: list[float] = []
    for obs in observations:
        s = _as_timestamp(getattr(obs, "start_time", None) or getattr(obs, "startTime", None))
        e = _as_timestamp(getattr(obs, "end_time", None) or getattr(obs, "endTime", None))
        if s is not None:
            starts.append(s)
        if e is not None:
            ends.append(e)
    return (min(starts) if starts else None, max(ends) if ends else None)


class TimeToFirstTokenMetric(BaseValueMetric):
    """Minimum ``timeToFirstToken`` across LLM generations in the session (seconds)."""

    def __init__(self, threshold: float = 5.0, **kwargs: Any) -> None:
        kwargs.pop("higher_is_better", None)
        super().__init__(threshold=threshold, higher_is_better=False, **kwargs)

    @property
    def __name__(self) -> str:  # noqa: D401
        return "TimeToFirstToken"

    def measure(self, test_case: Any, *_: Any, **__: Any) -> float:
        conv_id = get_conv_id(test_case)
        if not conv_id:
            return self._bail("no conv_id on test_case.metadata")

        traces = fetch_session_traces(conv_id)
        if not traces:
            return self._bail(f"no Langfuse traces for session_id={conv_id}")

        gens = llm_generations(merge_observations(traces))
        ttfts: list[float] = []
        for g in gens:
            ttft = getattr(g, "time_to_first_token", None) or getattr(g, "timeToFirstToken", None)
            if isinstance(ttft, (int, float)) and ttft > 0:
                ttfts.append(float(ttft))
        if not ttfts:
            return self._bail("no timeToFirstToken on any LLM observation")

        self.score = round(min(ttfts), 3)
        self.reason = f"{self.score}s (min of {len(ttfts)} generation(s))"
        self.is_successful()
        return self.score


class SessionDurationMetric(BaseValueMetric):
    """Total wall-clock seconds: ``max(endTime) - min(startTime)`` over
    every observation in the session, plus the trace's own start/end."""

    def __init__(self, threshold: float = 60.0, **kwargs: Any) -> None:
        kwargs.pop("higher_is_better", None)
        super().__init__(threshold=threshold, higher_is_better=False, **kwargs)

    @property
    def __name__(self) -> str:  # noqa: D401
        return "SessionDuration"

    def measure(self, test_case: Any, *_: Any, **__: Any) -> float:
        conv_id = get_conv_id(test_case)
        if not conv_id:
            return self._bail("no conv_id on test_case.metadata")

        traces = fetch_session_traces(conv_id)
        if not traces:
            return self._bail(f"no Langfuse traces for session_id={conv_id}")

        starts: list[float] = []
        ends: list[float] = []
        for short in traces:
            full = hydrate_trace(short)
            ts = _as_timestamp(getattr(full, "timestamp", None))
            te = _as_timestamp(getattr(full, "end_time", None) or getattr(full, "endTime", None))
            if ts is not None:
                starts.append(ts)
            if te is not None:
                ends.append(te)
            for obs in (full.observations or []):
                s = _as_timestamp(getattr(obs, "start_time", None) or getattr(obs, "startTime", None))
                e = _as_timestamp(getattr(obs, "end_time", None) or getattr(obs, "endTime", None))
                if s is not None:
                    starts.append(s)
                if e is not None:
                    ends.append(e)

        if not starts or not ends:
            return self._bail("trace/observations missing start/end timestamps")

        duration = max(ends) - min(starts)
        self.score = round(duration, 3)
        self.reason = f"{self.score}s across {len(traces)} trace(s)"
        self.is_successful()
        return self.score


class OutputTokensPerSecMetric(BaseValueMetric):
    """Throughput: ``sum(usage.output) / session_duration_s``."""

    def __init__(self, threshold: float = 20.0, **kwargs: Any) -> None:
        kwargs.pop("higher_is_better", None)
        super().__init__(threshold=threshold, higher_is_better=True, **kwargs)

    @property
    def __name__(self) -> str:  # noqa: D401
        return "OutputTokensPerSec"

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
            return self._bail("no LLM generations")

        total_out = sum(usage_output_tokens(g) for g in gens)
        start, end = _llm_observation_span(gens)
        if start is None or end is None or end <= start:
            return self._bail("LLM observations lack valid start/end timestamps")

        duration = end - start
        rate = total_out / duration if duration > 0 else 0.0
        self.score = round(rate, 2)
        self.reason = f"{total_out} output tokens / {round(duration, 3)}s = {self.score} tok/s"
        self.is_successful()
        return self.score


class NTurnsMetric(BaseValueMetric):
    """Number of user messages in the conversation.

    Counted from the *last* ``ai.streamText.doStream`` observation's input
    messages, matching Mira_Validation."""

    def __init__(self, threshold: float = 10.0, **kwargs: Any) -> None:
        kwargs.pop("higher_is_better", None)
        super().__init__(threshold=threshold, higher_is_better=False, **kwargs)

    @property
    def __name__(self) -> str:  # noqa: D401
        return "NTurns"

    def measure(self, test_case: Any, *_: Any, **__: Any) -> float:
        conv_id = get_conv_id(test_case)
        if not conv_id:
            return self._bail("no conv_id on test_case.metadata")

        traces = fetch_session_traces(conv_id)
        if not traces:
            return self._bail(f"no Langfuse traces for session_id={conv_id}")

        gens = llm_generations(merge_observations(traces))
        if not gens:
            return self._bail("no LLM generations")

        gens_sorted = sorted(
            gens,
            key=lambda g: _as_timestamp(
                getattr(g, "start_time", None) or getattr(g, "startTime", None)
            ) or 0,
        )
        last = gens_sorted[-1]
        messages = getattr(last, "input", None)
        if isinstance(messages, dict):
            messages = messages.get("messages")
        if not isinstance(messages, list):
            return self._bail("last doStream input has no messages array")

        n_user = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "user")
        self.score = float(n_user)
        self.reason = f"{n_user} user message(s) in last doStream input"
        self.is_successful()
        return self.score
