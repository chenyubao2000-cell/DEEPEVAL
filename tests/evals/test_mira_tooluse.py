"""Tool-use metrics for Mira (mixed multi-turn + per-turn).

Multi-turn (one test, two metrics):
  - ExpectedToolPathGEval (ConversationalGEval): compare Mira's actual
    tool calls against the bootstrapped `_expected_tools` reference path
    stored on each golden. The judge tolerates semantic equivalence
    (functionally equivalent tool substitutes, paraphrased args, extra
    auxiliary tools, out-of-order calls).
  - TopicAdherenceMetric: guards on-topic conversation.

Per-turn (parametrized): one LLMTestCase per assistant turn that actually
called a tool — checked with ArgumentCorrectnessMetric. This is a separate
signal axis from ExpectedToolPathGEval (per-turn argument granularity rather
than whole-conversation path matching), so we keep it.

History (2026-05-15): replaced the old ToolUseMetric → LeanToolUseMetric
chain with a single ConversationalGEval driven off `metadata.expected_tools`.
The bootstrapped reference path is generated once by
`tests/evals/bootstrap_expected_tools.py` and lives in
`tests/evals/.dataset.json` under `_expected_tools`.
See reports/token_audit_report.md for the savings analysis.
"""
from __future__ import annotations

import pytest

from deepeval import assert_test
from deepeval.metrics import (
    ArgumentCorrectnessMetric,
    ConversationalGEval,
    TopicAdherenceMetric,
)
from deepeval.test_case import MultiTurnParams

from claude_cli_judge import ClaudeCliJudge

from tests.evals._driver import (
    build_conversational,
    drive_mira,
    explode_to_llm_cases,
    golden_id,
    load_goldens,
)
from tests.evals._metrics_config import filter_active, is_active, is_file_active


judge = ClaudeCliJudge()


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


# --- Expected-path metric (replaces the old ToolUseMetric) -------------------
#
# The criteria text is the contract: judge reads (TOOLS_CALLED per turn,
# metadata.expected_tools, scenario) and produces a 0-1 score.
ExpectedToolPathGEval = ConversationalGEval(
    name="ExpectedToolPath",
    criteria=(
        "评估 agent 实际调用的工具序列是否达成用户目标。"
        "参考标准答案 metadata['expected_tools']（已经过 bootstrap 生成 + 人工 review 的理想路径）"
        "与 agent 实际在各 turn 中执行的 TOOLS_CALLED 做语义对比。\n"
        "\n"
        "宽容规则（不扣分）：\n"
        "  1. 工具名不同但功能等价（例如用 search 替代 company_search 拿到相同信息；"
        "用 sb_file_create 替代 sb_docx_create 实现同等交付）\n"
        "  2. input_parameters 措辞不同但语义相同（例如 query='上海AI' 和 query='上海人工智能'）\n"
        "  3. 调用了额外辅助工具（write_todos、view_skill、clarify_question 等）\n"
        "  4. 工具调用顺序与 expected 不一致\n"
        "  5. expected 列了多个工具，实际只用了少数但功能上等价完成了目标\n"
        "  6. 实际工具调用总数与 expected 的步数不一致 —— expected 的 3-8 步只是"
        "bootstrap 的参考量级，实际多于或少于都可以接受，重点是完成了功能而非匹配步数\n"
        "  7. 同一个工具被多次重复调用（尤其 search、company_search、people_search "
        "这类检索工具）默认算正确 —— 不同 input_parameters 通常是 agent 在做"
        "多维度补充查询（如先按行业再按城市），是合理行为而非冗余\n"
        "\n"
        "扣分维度：\n"
        "  - 漏掉 expected 中明确的关键功能步骤（如 expected 要求生成 Excel 但实际未生成）\n"
        "  - 调用了与目标无关的工具且偏离主线\n"
        "  - 关键工具的 input_parameters 跟用户目标完全不匹配"
        "（如用户问上海但 query 写成了北京）\n"
        "  - assistant 显式声明无法访问数据 / 没调用任何工具就 complete\n"
        "\n"
        "评分（请按 0-10 整数返回，后端会映射到 0-1）：\n"
        "  10：全部关键功能目标达成；\n"
        "  7-9：少量瑕疵但不影响交付；\n"
        "  4-6：漏 1-2 个关键步骤；\n"
        "  0-3：完全跑偏或没有有效工具调用。"
    ),
    evaluation_params=[
        MultiTurnParams.TOOLS_CALLED,
        MultiTurnParams.METADATA,    # expected_tools lives here
        MultiTurnParams.SCENARIO,
    ],
    model=judge,
    threshold=0.7,
    async_mode=False,
)


# --- Multi-turn metrics -------------------------------------------------------

MULTI_TURN_METRICS = filter_active([
    ExpectedToolPathGEval,
    TopicAdherenceMetric(
        relevant_topics=RELEVANT_TOPICS,
        threshold=0.5,
        model=judge,
        async_mode=False,
    ),
])


# --- Per-turn metric ----------------------------------------------------------

ARG_CORRECTNESS = ArgumentCorrectnessMetric(
    threshold=0.5, model=judge, async_mode=False,
)
_ARG_CORRECTNESS_ACTIVE = is_active(ARG_CORRECTNESS)


# Module-level skip: applies to BOTH parametrized tests below. Triggers when
# either (a) this file isn't in ACTIVE_FILES, or (b) neither MULTI_TURN_METRICS
# nor ARG_CORRECTNESS is enabled — i.e. nothing in this file would actually run.
def _skip_reason() -> str | None:
    if not is_file_active(__file__):
        return "test_mira_tooluse.py not in ACTIVE_FILES (tests/evals/_metrics_config.py)"
    if not MULTI_TURN_METRICS and not _ARG_CORRECTNESS_ACTIVE:
        return "no active tool-use metrics (tests/evals/_metrics_config.py)"
    return None


pytestmark = pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")


@pytest.mark.skipif(
    not MULTI_TURN_METRICS,
    reason="ExpectedToolPath / TopicAdherenceMetric not in ACTIVE_METRICS",
)
@pytest.mark.parametrize("golden", load_goldens(), ids=golden_id)
def test_mira_tool_use_multiturn(golden: dict):
    _session, turns = drive_mira(golden)
    test_case = build_conversational(golden, turns)
    assert_test(test_case=test_case, metrics=MULTI_TURN_METRICS)


@pytest.mark.skipif(
    not _ARG_CORRECTNESS_ACTIVE,
    reason="ArgumentCorrectnessMetric not in ACTIVE_METRICS",
)
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
