"""Miscellaneous single-turn metrics for Mira.

Per-assistant-turn LLMTestCase scored with:
  - AnswerRelevancyMetric
  - PromptAlignmentMetric(prompt_instructions=[...])

`prompt_instructions` are a hand-distilled subset of how Mira should behave
(see scenario/expected_outcome patterns in the dataset and the role string).
They are NOT pulled from Mira's actual system prompt — keep them as
high-level invariants the judge can check from `actual_output` alone.
"""
from __future__ import annotations

import pytest

from deepeval import assert_test
from deepeval.metrics import AnswerRelevancyMetric, PromptAlignmentMetric

from claude_cli_judge import ClaudeCliJudge

from tests.evals._driver import (
    drive_mira,
    explode_to_llm_cases,
    golden_id,
    load_goldens,
)
from tests.evals._metrics_config import filter_active, make_skip_mark


judge = ClaudeCliJudge()


MIRA_PROMPT_INSTRUCTIONS = [
    "Respond in a professional, concise tone appropriate for a workplace AI agent; avoid casual filler and emojis.",
    "When the user requests a concrete deliverable (PPT, Excel, PDF, image, candidate list, plan), explicitly produce or attempt to produce that deliverable rather than only describing it abstractly.",
    "Do not fabricate specific people, companies, statistics, salaries, or URLs; either back a claim with a verifiable source or explicitly flag it as a data limitation (e.g. '数据局限' / 'cannot verify in real time').",
    "If the user's request is ambiguous or missing critical inputs, ask a clarifying question instead of guessing silently.",
    "Structure substantive answers with clear sections, bullets, or step-by-step plans so the user can act on the output.",
]


METRICS = filter_active([
    AnswerRelevancyMetric(threshold=0.5, model=judge, async_mode=False),
    PromptAlignmentMetric(
        prompt_instructions=MIRA_PROMPT_INSTRUCTIONS,
        threshold=0.7,
        model=judge,
        async_mode=False,
    ),
])

pytestmark = make_skip_mark(__file__, METRICS)


@pytest.mark.parametrize("golden", load_goldens(), ids=golden_id)
def test_mira_others(golden: dict):
    _session, turns = drive_mira(golden)
    cases = explode_to_llm_cases(turns, scenario=golden.get("scenario"))
    if not cases:
        pytest.skip("no assistant turns produced")
    for case in cases:
        assert_test(test_case=case, metrics=METRICS)
