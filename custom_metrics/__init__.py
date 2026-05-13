"""Custom DeepEval metrics for the Mira eval harness.

Why these exist
---------------
DeepEval ships ~46 metrics for *quality* (answer relevance, faithfulness,
toxicity, …) but nothing for *operational* signals — tokens, USD cost,
latency, completion status, DB persistence. Mira_Validation has those 13
evaluators; we port the most useful subset here.

Inputs
------
All metrics consume Mira's ``conv_id`` (a.k.a. Langfuse ``session_id``)
stashed on ``test_case.metadata['conv_id']`` by ``tests/evals/_driver.py``.
``SessionHealthMetric`` additionally connects to the Mira Postgres
specified by ``TEST_DATABASE_URL`` for its persistence-layer sub-check.

Usage
-----
    from custom_metrics import SessionHealthMetric, TokensMetric
    from deepeval.test_case import ConversationalTestCase

    case = ConversationalTestCase(
        turns=[...],
        metadata={"conv_id": session.conversation_id},
    )
    assert_test(case, metrics=[
        SessionHealthMetric(),
        TokensMetric(informational=True),
    ])
"""
from .path_metrics import ToolDependencyMetric
from .perf_metrics import (
    NTurnsMetric,
    OutputTokensPerSecMetric,
    SessionDurationMetric,
    TimeToFirstTokenMetric,
)
from .status_metrics import SessionHealthMetric
from .usage_metrics import SessionCostMetric, TokensMetric

__all__ = [
    "NTurnsMetric",
    "OutputTokensPerSecMetric",
    "SessionCostMetric",
    "SessionDurationMetric",
    "SessionHealthMetric",
    "TimeToFirstTokenMetric",
    "TokensMetric",
    "ToolDependencyMetric",
]
