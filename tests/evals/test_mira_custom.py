"""Custom multi-turn ConversationalGEval metrics for Mira.

Three judge-LLM rubrics:
  - ProfessionalNoFabrication
  - DeliverableMatchesRequest
  - GroundedNoFabrication

Important: evaluation_params MUST be drawn from
`deepeval.metrics.g_eval.utils.CONVERSATIONAL_G_EVAL_PARAMS` — the allowed
keys are {ROLE, CONTENT, METADATA, TAGS, SCENARIO, EXPECTED_OUTCOME,
RETRIEVAL_CONTEXT, TOOLS_CALLED}. CHATBOT_ROLE / USER_DESCRIPTION / CONTEXT
are NOT in that dict and will KeyError at runtime.
"""
from __future__ import annotations

import pytest

from deepeval import assert_test
from deepeval.metrics import ConversationalGEval
from deepeval.test_case.conversational_test_case import MultiTurnParams

from claude_cli_judge import ClaudeCliJudge

from tests.evals._driver import (
    build_conversational,
    drive_mira,
    golden_id,
    load_goldens,
)
from tests.evals._metrics_config import filter_active, make_skip_mark


judge = ClaudeCliJudge()


METRICS = filter_active([
    ConversationalGEval(
        name="ProfessionalNoFabrication",
        criteria=(
            "The assistant maintains a professional, concise tone consistent "
            "with a workplace AI agent. It does NOT fabricate prior context, "
            "user identity, or capabilities not declared. When the user is "
            "ambiguous it asks for clarification rather than guessing. It "
            "does not leak internal LLM mechanics (e.g. 'I have no memory of "
            "past sessions') in a way that breaks the professional persona."
        ),
        evaluation_params=[
            MultiTurnParams.ROLE,
            MultiTurnParams.CONTENT,
            MultiTurnParams.SCENARIO,
            MultiTurnParams.EXPECTED_OUTCOME,
        ],
        threshold=0.7,
        model=judge,
        async_mode=False,
    ),
    ConversationalGEval(
        name="DeliverableMatchesRequest",
        criteria=(
            "The user's request specifies one or more concrete deliverables "
            "(e.g. a PPT/Excel/PDF/image file, a candidate list of size N, a "
            "structured analysis with sections, a salary range table, a "
            "recruitment plan with steps). Evaluate whether the assistant's "
            "reply actually produces — or credibly attempts to produce — "
            "EACH requested deliverable. A high score requires every "
            "deliverable in the request is addressed. A verbal description "
            "without the actual artifact (when an artifact was requested) is "
            "a mid score. Ignoring or substituting unrelated content is a "
            "low score. When the agent legitimately cannot access a data "
            "source (e.g. live LinkedIn data) it should say so explicitly "
            "instead of fabricating."
        ),
        evaluation_params=[
            MultiTurnParams.CONTENT,
            MultiTurnParams.TOOLS_CALLED,
            MultiTurnParams.SCENARIO,
            MultiTurnParams.EXPECTED_OUTCOME,
        ],
        threshold=0.6,
        model=judge,
        async_mode=False,
    ),
    ConversationalGEval(
        name="GroundedNoFabrication",
        criteria=(
            "Every factual claim — company names, people names, statistics, "
            "salary numbers, URLs, dates, market sizing — must be either "
            "(a) backed by an explicit source/citation, OR (b) flagged with "
            "an explicit data-limitation disclaimer such as '据公开信息推断' / "
            "'数据局限' / 'I cannot verify this in real time'. The assistant "
            "must NOT invent specific people, companies, or precise figures "
            "without verification. Hedged generic guidance ('typical ranges') "
            "is acceptable. Confidently asserting unverifiable specifics is "
            "a low score."
        ),
        evaluation_params=[
            MultiTurnParams.CONTENT,
            MultiTurnParams.TOOLS_CALLED,
            MultiTurnParams.SCENARIO,
            MultiTurnParams.EXPECTED_OUTCOME,
        ],
        threshold=0.6,
        model=judge,
        async_mode=False,
    ),
])

pytestmark = make_skip_mark(__file__, METRICS)


@pytest.mark.parametrize("golden", load_goldens(), ids=golden_id)
def test_mira_custom(golden: dict):
    _session, turns = drive_mira(golden)
    test_case = build_conversational(golden, turns)
    assert_test(test_case=test_case, metrics=METRICS)
