"""Tool-use metrics for Mira (mixed multi-turn + per-turn).

Multi-turn (one test, two metrics):
  - ToolUseMetric(available_tools=[ToolCall(name=..., description=...), ...])
  - TopicAdherenceMetric(relevant_topics=[...])

Per-turn (parametrized): one LLMTestCase per assistant turn that actually
called a tool — checked with ArgumentCorrectnessMetric.

`AVAILABLE_TOOLS` is sourced from the on-disk Langfuse cache
(`.cache/tools-<env>.json`), which is populated by report.py from real Mira
traces. Each placeholder ToolCall carries the tool's description so the judge
can evaluate tool choice against its declared scope. Empty list on first run
(before cache exists) — report.py then refreshes from the first golden's trace
and patches this metric in place via `_patch_tooluse_metrics_in_place`.
"""
from __future__ import annotations

import pytest

from deepeval import assert_test
from deepeval.metrics import (
    ArgumentCorrectnessMetric,
    TopicAdherenceMetric,
    ToolUseMetric,
)
from deepeval.test_case import ToolCall

from claude_cli_judge import ClaudeCliJudge

from tests.evals._driver import (
    build_conversational,
    drive_mira,
    explode_to_llm_cases,
    golden_id,
    load_goldens,
)
from tests.evals._langfuse_tools import available_tool_registry


judge = ClaudeCliJudge()


# --- Mira's tool registry (real, per-session, from Langfuse cache) ------------

def _build_available_tools() -> list[ToolCall]:
    reg = available_tool_registry()
    return [
        ToolCall(name=name, description=meta.get("description"))
        for name, meta in sorted(reg.items())
    ]


AVAILABLE_TOOLS: list[ToolCall] = _build_available_tools()


# --- Topics Mira is in-domain for ---------------------------------------------

RELEVANT_TOPICS = [
    # Original 6 — research / sourcing / docs (heritage from initial 10 goldens).
    "recruitment sourcing and talent search",
    "market and industry research",
    "business document generation (PPT / Excel / PDF / Word / images)",
    "talent profiling and candidate evaluation",
    "competitive analysis and company research",
    "career and compensation insights",
    # Operational recruiter workflows added 2026-05-11 for the 4 new categories
    # (CRM / voice / ci_email / ci_dingding). Without these, TopicAdherence
    # judges those goldens as off-topic false-negatives — see
    # reports/voice-compare-20260511-2112.html, where the same voice golden
    # flipped 1.00 → 0.00 on this metric between two equally on-topic runs.
    "CRM lookup and enterprise / funding intelligence",  # crm cases
    "AI-assisted outbound calls and call scheduling for candidate outreach",  # voice cases
    "recruiter email triage and outbound recruiting emails",  # ci_email cases
    "calendar / meeting / todo / task management for recruiters",  # ci_dingding cases
]


# --- Multi-turn metrics -------------------------------------------------------

MULTI_TURN_METRICS = [
    ToolUseMetric(
        available_tools=AVAILABLE_TOOLS,
        threshold=0.5,
        model=judge,
        async_mode=False,
    ),
    TopicAdherenceMetric(
        relevant_topics=RELEVANT_TOPICS,
        threshold=0.5,
        model=judge,
        async_mode=False,
    ),
]


# --- Per-turn metric ----------------------------------------------------------

ARG_CORRECTNESS = ArgumentCorrectnessMetric(
    threshold=0.5, model=judge, async_mode=False,
)


def _patch_tooluse_metrics_in_place(registry: dict[str, dict]) -> int:
    """Re-set `available_tools` on the `ToolUseMetric` instance from the freshly
    refreshed registry. Mutates in place so any caller that already holds a
    reference to the metric (e.g. report.py's `metric_rows`) sees the update.

    Returns the new tool count. Called by report.py after a cache refresh.
    """
    new_tools = [
        ToolCall(name=name, description=meta.get("description"))
        for name, meta in sorted(registry.items())
    ]
    for m in MULTI_TURN_METRICS:
        if isinstance(m, ToolUseMetric):
            m.available_tools = new_tools
    # Module-level constant kept in sync so anyone re-reading sees the new list.
    global AVAILABLE_TOOLS
    AVAILABLE_TOOLS = new_tools
    return len(new_tools)


@pytest.mark.parametrize("golden", load_goldens(), ids=golden_id)
def test_mira_tool_use_multiturn(golden: dict):
    _session, turns = drive_mira(golden)
    test_case = build_conversational(golden, turns)
    assert_test(test_case=test_case, metrics=MULTI_TURN_METRICS)


@pytest.mark.parametrize("golden", load_goldens(), ids=golden_id)
def test_mira_argument_correctness(golden: dict):
    """ArgumentCorrectness on each assistant turn that actually called tools.

    Turns with no tool calls are skipped (the metric needs `tools_called`).
    """
    _session, turns = drive_mira(golden)
    cases = explode_to_llm_cases(turns, scenario=golden.get("scenario"))
    tool_cases = [c for c in cases if c.tools_called]
    if not tool_cases:
        pytest.skip("no tool calls captured for this golden")
    # Run once per case so individual failures are visible in pytest output.
    for case in tool_cases:
        assert_test(test_case=case, metrics=[ARG_CORRECTNESS])
