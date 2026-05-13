"""Langfuse helpers: fetch Mira traces by session_id and merge observations.

Mira sends one trace per assistant turn with ``session_id == conv_id``. A
multi-turn conversation has N traces under the same session_id; these
helpers fetch and merge them.

Ingestion is async on Langfuse's side, so a freshly-ended conversation may
need a few seconds before its traces show up. ``fetch_session_traces``
polls every 2s up to ``poll_s`` to absorb that lag.
"""
from __future__ import annotations

import os
import time
from typing import Any, Iterable

# We re-use ``tests.evals._env.load_env`` to load + validate the .env, but we
# build our OWN Langfuse client with an extended timeout. The shared
# ``_env.langfuse_client()`` uses the SDK default (5s) which routinely times
# out against us.cloud.langfuse.com for fresh traces.
from tests.evals._env import load_env


_LANGFUSE_TIMEOUT_S = int(os.environ.get("CUSTOM_METRICS_LANGFUSE_TIMEOUT_S", "60"))
_FETCH_POLL_S = int(os.environ.get("CUSTOM_METRICS_FETCH_POLL_S", "30"))
_INGEST_LAG_S = float(os.environ.get("CUSTOM_METRICS_INGEST_LAG_S", "3"))

_CLIENT: Any = None

# Module-level per-session caches. deepeval schedules all metrics for a test
# case via ``asyncio.gather``; without caching, each of our 8 metrics would
# independently poll Langfuse (8 × up to 30s = wasted time, plus the first
# metric in the gather order keeps hitting Langfuse ingestion lag while
# later ones inadvertently benefit from the wait). One cache means the
# first metric absorbs the ingest lag; the rest get instant hits.
_TRACES_CACHE: dict[str, list[Any]] = {}
_HYDRATED_CACHE: dict[str, Any] = {}


def _client() -> Any:
    """Lazy singleton Langfuse client with a long HTTP timeout."""
    global _CLIENT
    if _CLIENT is None:
        load_env()
        from langfuse import Langfuse  # imported lazily to keep startup fast

        _CLIENT = Langfuse(timeout=_LANGFUSE_TIMEOUT_S)
    return _CLIENT


def fetch_session_traces(conv_id: str, *, poll_s: int | None = None) -> list[Any]:
    """Return all traces under ``session_id == conv_id`` (cached per conv_id).

    First call sleeps ``_INGEST_LAG_S`` (default 3s) before polling — Langfuse
    ingestion is async, and a freshly-ended conversation reliably needs a few
    seconds to land. Polls every 2s up to ``poll_s`` (default 30s). Returns
    ``[]`` on timeout — caller should treat as a soft failure.
    """
    cached = _TRACES_CACHE.get(conv_id)
    if cached is not None:
        return cached

    poll_s = _FETCH_POLL_S if poll_s is None else poll_s
    lf = _client()

    # Absorb ingestion lag up front so all parallel metrics share the wait.
    time.sleep(_INGEST_LAG_S)
    deadline = time.time() + poll_s
    while True:
        resp = lf.api.trace.list(session_id=conv_id, limit=20)
        traces = list(resp.data or [])
        if traces:
            _TRACES_CACHE[conv_id] = traces
            return traces
        if time.time() >= deadline:
            _TRACES_CACHE[conv_id] = []
            return []
        time.sleep(2)


def hydrate_trace(trace_short: Any) -> Any:
    """Fetch a Trace's full observation list (cached per trace id).

    The ``list`` endpoint returns ``TraceWithDetails`` where ``observations``
    is just ``List[str]`` (IDs). We must call ``trace.get(id)`` to get the
    full ``TraceWithFullDetails`` with actual observation objects.
    """
    trace_id = trace_short.id
    cached = _HYDRATED_CACHE.get(trace_id)
    if cached is not None:
        return cached
    full = _client().api.trace.get(trace_id)
    _HYDRATED_CACHE[trace_id] = full
    return full


def clear_caches() -> None:
    """Drop all cached traces. Call between independent evals if traces from
    a previous conversation could grow over time."""
    _TRACES_CACHE.clear()
    _HYDRATED_CACHE.clear()


def merge_observations(traces: Iterable[Any]) -> list[Any]:
    """Hydrate each trace and return the union of observations, dedup'd by id.

    ``trace.list`` returns ``TraceWithDetails`` whose ``observations`` field
    is just a list of observation IDs (``List[str]``). We must call
    ``trace.get(id)`` to get ``TraceWithFullDetails`` whose ``observations``
    are full objects. So we always hydrate — slower but correct.
    """
    seen: dict[str, Any] = {}
    for trace in traces:
        full = hydrate_trace(trace)
        for obs in (getattr(full, "observations", None) or []):
            if not hasattr(obs, "id"):
                # Defensive: skip stringy IDs in case SDK shape changes.
                continue
            seen[obs.id] = obs
    return list(seen.values())


def llm_generations(observations: Iterable[Any]) -> list[Any]:
    """Filter to LLM generation observations (matches Mira_Validation logic)."""
    out = []
    for obs in observations:
        otype = getattr(obs, "type", None)
        name = getattr(obs, "name", None) or ""
        if otype == "GENERATION" or name == "ai.streamText.doStream" or "streamText" in name:
            out.append(obs)
    return out


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """Try several attribute/dict-key names in order. Useful because Langfuse
    SDK occasionally renames fields between versions."""
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
        if isinstance(obj, dict) and obj.get(n) is not None:
            return obj[n]
    return default


def usage_input_tokens(obs: Any) -> int:
    """Read ``usage.input`` (or ``usage.promptTokens``) on a Langfuse observation."""
    usage = _attr(obs, "usage")
    if not usage:
        return 0
    return _attr(usage, "input", "promptTokens", "prompt_tokens", default=0) or 0


def usage_output_tokens(obs: Any) -> int:
    """Read ``usage.output`` (or ``usage.completionTokens``) on a Langfuse observation."""
    usage = _attr(obs, "usage")
    if not usage:
        return 0
    return _attr(usage, "output", "completionTokens", "completion_tokens", default=0) or 0


def obs_cost_usd(obs: Any) -> float:
    """Read per-observation cost in USD. Fields vary by Langfuse version."""
    for fld in ("calculatedTotalCost", "calculated_total_cost", "totalCost", "total_cost", "cost"):
        v = getattr(obs, fld, None)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return 0.0


def trace_cost_usd(trace: Any) -> float:
    """Read trace-level cost in USD. Same field cascade as ``obs_cost_usd``."""
    for fld in ("totalCost", "total_cost", "calculatedTotalCost", "calculated_total_cost", "cost"):
        v = getattr(trace, fld, None)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return 0.0
