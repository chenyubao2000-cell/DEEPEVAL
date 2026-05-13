"""End-to-end multi-turn metrics for Mira.

Five built-in conversational metrics:
  - ConversationCompletenessMetric
  - TurnRelevancyMetric
  - KnowledgeRetentionMetric
  - RoleAdherenceMetric (uses ConversationalTestCase.chatbot_role)
  - GoalAccuracyMetric

All metrics share one ClaudeCliJudge (async_mode=False; CLI lock is serial).
"""
from __future__ import annotations

import pytest

from deepeval import assert_test
from deepeval.metrics import (
    ConversationCompletenessMetric,
    GoalAccuracyMetric,
    KnowledgeRetentionMetric,
    RoleAdherenceMetric,
    TurnRelevancyMetric,
)

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
    ConversationCompletenessMetric(threshold=0.5, model=judge, async_mode=False),
    TurnRelevancyMetric(threshold=0.5, model=judge, async_mode=False),
    KnowledgeRetentionMetric(threshold=0.5, model=judge, async_mode=False),
    RoleAdherenceMetric(threshold=0.7, model=judge, async_mode=False),
    GoalAccuracyMetric(threshold=0.5, model=judge, async_mode=False),
])

pytestmark = make_skip_mark(__file__, METRICS)


@pytest.mark.parametrize("golden", load_goldens(), ids=golden_id)
def test_mira_e2e(golden: dict):
    _session, turns = drive_mira(golden)
    test_case = build_conversational(golden, turns)
    assert_test(test_case=test_case, metrics=METRICS)
