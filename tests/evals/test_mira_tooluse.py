"""Tool-use metrics for Mira (mixed multi-turn + per-turn).

Multi-turn (one test, two metrics):
  - ToolUseMetric(available_tools=[ToolCall(name=...), ...])
  - TopicAdherenceMetric(relevant_topics=[...])

Per-turn (parametrized): one LLMTestCase per assistant turn that actually
called a tool — checked with ArgumentCorrectnessMetric.

`available_tools` is populated from the Mira tool registry in
`.research/mira-tools.md` (19 static + 23 BUA + evaluate_people). We only
need the `name` field for ToolUseMetric's availability check — input/output
are left None on these placeholder ToolCalls.
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


judge = ClaudeCliJudge()


# --- Mira's full tool registry (from .research/mira-tools.md) -----------------

_STATIC_TOOLS = [
    "search", "company_search", "people_search", "clarify_question", "confirm",
    "write_todos", "complete", "code_interpreter", "sb_command_execute",
    "sb_file_create", "sb_file_rewrite", "sb_file_edit", "sb_docx_create",
    "sb_pptx_create", "sb_xlsx_create", "sb_pdf_create", "sb_image_create",
    "generate_people_data", "evaluate_people",
]

_BUA_TOOLS = [
    "bua_check_status", "bua_request_authorization", "bua_confirm_sensitive_action",
    "bua_navigate", "bua_click", "bua_type", "bua_scroll", "bua_extract_content",
    "bua_wait_for_user", "bua_snapshot", "bua_dom_tree", "bua_hover", "bua_fill",
    "bua_fill_form", "bua_press_key", "bua_handle_dialog", "bua_wait_for",
    "bua_screenshot", "bua_new_tab", "bua_list_tabs", "bua_select_tab",
    "bua_close_tab", "bua_evaluate",
]

AVAILABLE_TOOLS = [ToolCall(name=t) for t in _STATIC_TOOLS + _BUA_TOOLS]


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
