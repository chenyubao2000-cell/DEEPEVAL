"""Shared helpers for Mira evaluation tests.

All test modules call into here to drive the Mira BFF, materialise
`Turn` / `LLMTestCase` / `ConversationalTestCase` objects, and convert the
SSE-derived tool-call dicts (from `mira_client.MiraSession`) into DeepEval
`ToolCall` objects.

We deliberately keep Mira invocation OUT of pytest module-load: drivers are
only called inside test bodies (driver -> network). Calling at import time
caused collection failures before.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from deepeval.test_case import (
    ConversationalTestCase,
    LLMTestCase,
    ToolCall,
    Turn,
)

from mira_client import MiraSession


# Hard ceiling on how much of a tool output string we hand to the judge.
# Tool outputs (search results, file contents, etc.) can be 50-500 KB; that
# blows out judge context and slows the CLI. We truncate per-call.
_MAX_TOOL_OUTPUT_CHARS = 4000


def _coerce_output(raw: Any) -> Any:
    """Make a tool `output` field both JSON-serialisable and bounded in size."""
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        try:
            text = json.dumps(raw, ensure_ascii=False)
        except Exception:
            text = str(raw)
    else:
        text = str(raw)
    if len(text) > _MAX_TOOL_OUTPUT_CHARS:
        return text[:_MAX_TOOL_OUTPUT_CHARS] + f"\n...[truncated {len(text) - _MAX_TOOL_OUTPUT_CHARS} chars]"
    return text


def _tc_dict_to_toolcall(tc: dict) -> ToolCall:
    """Convert a `MiraSession` tool-call dict to a DeepEval `ToolCall`."""
    return ToolCall(
        name=tc.get("tool_name") or "unknown_tool",
        input_parameters=tc.get("input") if isinstance(tc.get("input"), dict) else None,
        output=_coerce_output(tc.get("output")),
        description=None,
        reasoning=None,
    )


def drive_mira(golden: dict) -> tuple[MiraSession, list[Turn]]:
    """Drive Mira through all `user_inputs` for one golden.

    Optional `_attachments` (list of local file paths) on the golden are
    uploaded before the FIRST user message. The resulting `[Uploaded File:
    ...]` markers are appended to user_inputs[0] by `MiraSession.send()`, so
    downstream judges see the same text the server processed.

    Returns the live `MiraSession` (carries history + warnings) and the
    constructed list of `Turn` objects (user/assistant pairs in order).
    Each assistant Turn already has `tools_called` populated.
    """
    session = MiraSession()
    attachments = golden.get("_attachments") or []
    turns: list[Turn] = []
    for idx, user_msg in enumerate(golden["user_inputs"]):
        attach_for_turn = attachments if idx == 0 else None
        reply = session.send(user_msg, attachments=attach_for_turn)
        # session.history[-2] is the user entry as actually sent (with file markers).
        sent_user = session.history[-2]
        turns.append(Turn(role="user", content=sent_user["content"]))
        last = session.history[-1]
        assert last["role"] == "assistant", "MiraSession history out of sync"
        tools_called = [_tc_dict_to_toolcall(tc) for tc in last.get("tool_calls", [])]
        turns.append(Turn(
            role="assistant",
            content=reply,
            tools_called=tools_called or None,
        ))
    return session, turns


def build_conversational(golden: dict, turns: list[Turn]) -> ConversationalTestCase:
    """Wrap turns in a ConversationalTestCase using golden metadata."""
    return ConversationalTestCase(
        turns=turns,
        scenario=golden.get("scenario"),
        expected_outcome=golden.get("expected_outcome"),
        chatbot_role=golden.get("chatbot_role"),
    )


def explode_to_llm_cases(turns: list[Turn], scenario: str | None = None) -> list[LLMTestCase]:
    """Pair adjacent user/assistant turns into single-turn LLMTestCases.

    Each resulting case represents one assistant reply with its triggering
    user message. Used by safety + answer-relevancy + per-turn judge metrics.
    `scenario` is stashed in `metadata` for traceability; metrics ignore it.
    """
    cases: list[LLMTestCase] = []
    pending_user: Turn | None = None
    for turn in turns:
        if turn.role == "user":
            pending_user = turn
            continue
        if turn.role == "assistant" and pending_user is not None:
            cases.append(LLMTestCase(
                input=pending_user.content,
                actual_output=turn.content,
                tools_called=turn.tools_called or None,
                metadata={"scenario": scenario} if scenario else None,
            ))
            pending_user = None
    return cases


def load_goldens() -> list[dict]:
    """Load the shared dataset; honour MIRA_GOLDEN_TIER filter."""
    import os

    dataset_path = Path(__file__).parent / ".dataset.json"
    with dataset_path.open() as f:
        data = json.load(f)
    goldens = data["goldens"]
    tier = os.environ.get("MIRA_GOLDEN_TIER", "all").lower()
    if tier in ("light", "heavy"):
        goldens = [g for g in goldens if g.get("_tier") == tier]
    return goldens


def golden_id(g: dict) -> str:
    return (g.get("scenario") or "case")[:60]
