"""Multi-turn eval suite for Mira's BFF chat endpoint, evaluated against
real customer workflows (research, sourcing, document generation, etc.).

Per test:
  1. Drive Mira via `MiraSession.send()` over /api/task (SSE).
  2. Build a `ConversationalTestCase` from the resulting turns.
  3. Score it with multi-turn metrics whose LLM judge is the local `claude`
     CLI (no separate ANTHROPIC_API_KEY required).

Filter which goldens to run via the MIRA_GOLDEN_TIER env var:
  - MIRA_GOLDEN_TIER=light  -> only short, text-only tasks
  - MIRA_GOLDEN_TIER=heavy  -> only long-running artifact-producing tasks
  - unset / "all"           -> everything
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from deepeval import assert_test
from deepeval.metrics import (
    ConversationalGEval,
    ConversationCompletenessMetric,
    GoalAccuracyMetric,
    RoleAdherenceMetric,
)
from deepeval.test_case import ConversationalTestCase, Turn
from deepeval.test_case.conversational_test_case import MultiTurnParams

from claude_cli_judge import ClaudeCliJudge
from mira_client import MiraSession
from tests.evals._metrics_config import filter_active, make_skip_mark


DATASET_PATH = Path(__file__).parent / ".dataset.json"
TIER_FILTER = os.environ.get("MIRA_GOLDEN_TIER", "all").lower()


def _load_goldens() -> list[dict]:
    with DATASET_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    goldens = data["goldens"]
    if TIER_FILTER in ("light", "heavy"):
        goldens = [g for g in goldens if g.get("_tier") == TIER_FILTER]
    return goldens


# One shared judge across all metrics. Claude CLI calls are serialized internally.
judge = ClaudeCliJudge()


END_TO_END_METRICS = filter_active([
    RoleAdherenceMetric(threshold=0.7, model=judge, async_mode=False),
    ConversationCompletenessMetric(threshold=0.5, model=judge, async_mode=False),
    GoalAccuracyMetric(threshold=0.5, model=judge, async_mode=False),
    ConversationalGEval(
        name="ProfessionalToneAndNoFabrication",
        criteria=(
            "The assistant responds in a professional, concise tone befitting "
            "a workplace AI agent. It does not fabricate context, prior "
            "conversations, or capabilities it has not been told about. When "
            "the user is vague, it asks for clarification rather than guessing. "
            "It does not leak internal LLM mechanics (e.g. 'I have no memory "
            "of past sessions') to the user."
        ),
        evaluation_params=[
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
            "(e.g. a PPT/Excel/PDF file, an image, a candidate list of size N, "
            "a structured analysis, salary ranges, a recruitment plan). "
            "Evaluate whether the assistant's reply actually produces — or "
            "credibly attempts to produce — each requested deliverable. A "
            "high score requires that every deliverable in the request is "
            "addressed; a verbal description without the actual artifact "
            "(when an artifact was requested) deserves a mid score; ignoring "
            "or substituting unrelated content deserves a low score. When "
            "the agent legitimately cannot access a data source (e.g. live "
            "LinkedIn data), it should say so explicitly rather than fabricate."
        ),
        evaluation_params=[
            MultiTurnParams.SCENARIO,
            MultiTurnParams.EXPECTED_OUTCOME,
        ],
        threshold=0.6,
        model=judge,
        async_mode=False,
    ),
])

pytestmark = make_skip_mark(__file__, END_TO_END_METRICS)


def _drive_mira(golden: dict) -> ConversationalTestCase:
    session = MiraSession()
    attachments = golden.get("_attachments") or []
    turns: list[Turn] = []
    for idx, user_msg in enumerate(golden["user_inputs"]):
        attach_for_turn = attachments if idx == 0 else None
        reply = session.send(user_msg, attachments=attach_for_turn)
        sent_user = session.history[-2]
        turns.append(Turn(role="user", content=sent_user["content"]))
        turns.append(Turn(role="assistant", content=reply))
    return ConversationalTestCase(
        turns=turns,
        scenario=golden.get("scenario"),
        expected_outcome=golden.get("expected_outcome"),
        chatbot_role=golden.get("chatbot_role"),
    )


@pytest.mark.parametrize(
    "golden",
    _load_goldens(),
    ids=lambda g: (g.get("scenario") or "case")[:60],
)
def test_mira_multi_turn(golden: dict):
    test_case = _drive_mira(golden)
    assert_test(test_case=test_case, metrics=END_TO_END_METRICS)
