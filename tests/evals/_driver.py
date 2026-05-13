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
# Tool outputs (search results, file contents, voice protocol blobs, etc.)
# can be 50-500 KB; that blows out judge context and burns Claude CLI quota
# fast. We truncate per-call.
#
# 1500 chars is enough to keep:
#   - tool name + status
#   - the head of stdout (key fields like jobGroupId, counts, error name)
#   - the first 1-2 paragraphs of long protocol/instruction blobs
# but not 8 KB of voice_get_protocol's MANDATORY EXECUTION PROTOCOL text.
# Override per-run with MIRA_MAX_TOOL_OUTPUT_CHARS env var if you need more.
import os as _os
_MAX_TOOL_OUTPUT_CHARS = int(_os.environ.get("MIRA_MAX_TOOL_OUTPUT_CHARS", "1500"))

# Artifact-producing tools — the deliverable lives in `input.fileContents`,
# not in tool output. Without intervention, judges only see the success
# bool from tool output and miss the actual content; metrics like
# `DeliverableMatchesRequest` / `PromptAlignment` then incorrectly score the
# golden as "no deliverable" even though Mira created a full PPT / Markdown
# / Excel etc. Layer A: surface the artifact body inside ToolCall.output.
#
# sb_image_create is included even though it has no text body — the path
# alone is useful to judges.
_ARTIFACT_TOOLS: set[str] = {
    "sb_file_create",
    "sb_file_rewrite",
    "sb_file_edit",
    "sb_docx_create",
    "sb_pptx_create",
    "sb_xlsx_create",
    "sb_pdf_create",
    "sb_image_create",
}

# How much of an artifact body we embed per tool call. Independent from the
# generic tool-output cap because we WANT to give judges the full deliverable
# (up to this limit) rather than truncating to 1500 chars total.
_ARTIFACT_BODY_MAX_CHARS = int(_os.environ.get("MIRA_MAX_ARTIFACT_CHARS", "1500"))


def _extract_artifact(
    tool_name: str,
    input_dict: dict | None,
    output: Any,
) -> tuple[str | None, str | None]:
    """For artifact-producing tools, derive (filePath, body) from tool I/O.

    Field names vary across tools (`fileContents` for sb_file_* / sb_docx_*,
    `content` for some, output.data.filePath for write confirmations); this
    normalises them. Returns (None, None) when neither is present (e.g.
    sb_image_create returns only a URL in output).
    """
    inp = input_dict or {}
    path = (
        inp.get("filePath")
        or inp.get("file_path")
        or inp.get("path")
        or inp.get("filename")
    )
    # Fall back to output.data.filePath if input didn't carry the path
    if not path and isinstance(output, dict):
        try:
            path = (output.get("data") or {}).get("filePath") or path
        except Exception:
            pass

    body: str | None = None
    for key in ("fileContents", "file_contents", "content", "text"):
        if key in inp and inp[key]:
            body = str(inp[key])
            break

    return path, body


def _augment_output_with_artifact(
    tool_name: str,
    raw_output: Any,
    input_dict: dict | None,
) -> str:
    """Wrap a file-creating tool's output with the artifact preview so the
    judge sees the actual deliverable (head of file body + path) rather than
    just `"success": true`. Falls back to raw_output if nothing to surface.
    """
    path, body = _extract_artifact(tool_name, input_dict, raw_output)
    if not (path or body):
        return _coerce_output(raw_output)

    parts: list[str] = []
    if path:
        parts.append(f"📎 ARTIFACT: {path}")
    if body:
        truncated = body[:_ARTIFACT_BODY_MAX_CHARS]
        suffix = ""
        if len(body) > _ARTIFACT_BODY_MAX_CHARS:
            suffix = f"\n...[truncated {len(body) - _ARTIFACT_BODY_MAX_CHARS} chars of {len(body)} total]"
        parts.append(f"📄 CONTENT ({len(body)} chars total):\n{truncated}{suffix}")
    # Preserve the original ok/error status from the tool itself
    try:
        orig = (
            json.dumps(raw_output, ensure_ascii=False)
            if isinstance(raw_output, (dict, list))
            else str(raw_output)
        )
    except Exception:
        orig = str(raw_output)
    if orig and orig not in ("None", "null"):
        # cap the appended raw status alone, but don't clip the artifact body
        if len(orig) > 400:
            orig = orig[:400] + "...[truncated]"
        parts.append(f"[tool status]\n{orig}")
    return "\n\n".join(parts)


# Tool registry (name -> {description, input_schema}). Populated by report.py
# from the on-disk Langfuse cache before any goldens run, and re-set after the
# cache is refreshed. Empty dict means "no descriptions available" — tool calls
# still build correctly, they just lack description context for the judge.
_REGISTRY: dict[str, dict] = {}


def set_registry(registry: dict[str, dict]) -> None:
    """Install the tool registry used to enrich tool-call descriptions.

    Callers pass the dict returned by `_langfuse_tools.available_tool_registry`.
    """
    global _REGISTRY
    _REGISTRY = registry or {}


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
    """Convert a `MiraSession` tool-call dict to a DeepEval `ToolCall`.

    The tool's description (from the cached Langfuse registry) is attached so
    downstream metrics can judge tool choice against the tool's declared scope.

    Layer A: for file-creating tools (`_ARTIFACT_TOOLS`), the actual deliverable
    is in tool INPUT (`fileContents`), not OUTPUT — without help the judge sees
    only `{"success": true}` and concludes "no deliverable". We re-pack the
    input body into the output field the judge reads.
    """
    name = tc.get("tool_name") or "unknown_tool"
    meta = _REGISTRY.get(name) or {}
    raw_output = tc.get("output")
    input_dict = tc.get("input") if isinstance(tc.get("input"), dict) else None
    if name in _ARTIFACT_TOOLS:
        coerced_output = _augment_output_with_artifact(name, raw_output, input_dict)
    else:
        coerced_output = _coerce_output(raw_output)
    return ToolCall(
        name=name,
        input_parameters=input_dict,
        output=coerced_output,
        description=meta.get("description"),
        reasoning=None,
    )


def _build_artifact_section(turn_tools: list[ToolCall]) -> str:
    """If any artifact-producing tools fired in this turn, return a markdown
    section the judge will see appended to the assistant Turn content.

    Empty string when no artifact tools were used (avoids polluting plain
    chat turns). This is Layer B: even text-only metrics (PromptAlignment /
    AnswerRelevancy) that don't introspect `tools_called` now see the
    delivered file body inline.
    """
    artifacts: list[tuple[str, str | None, str | None]] = []
    for tc in turn_tools or []:
        if tc.name not in _ARTIFACT_TOOLS:
            continue
        path, body = _extract_artifact(tc.name, tc.input_parameters, tc.output)
        if not (path or body):
            continue
        artifacts.append((tc.name, path, body))
    if not artifacts:
        return ""

    lines = ["", "---", "", "### 📎 本轮产出的交付物（评测可见）", ""]
    for name, path, body in artifacts:
        if path:
            lines.append(f"**`{name}`** → `{path}`")
        else:
            lines.append(f"**`{name}`**")
        if body:
            preview = body[:_ARTIFACT_BODY_MAX_CHARS]
            suffix = ""
            if len(body) > _ARTIFACT_BODY_MAX_CHARS:
                suffix = f"\n...[已截断 {len(body) - _ARTIFACT_BODY_MAX_CHARS} 字 / 总长 {len(body)} 字]"
            lines.append("")
            # Fence on a 4-tick boundary to avoid colliding with any 3-tick
            # blocks the body might contain (markdown / code samples).
            lines.append("````")
            lines.append(preview + suffix)
            lines.append("````")
        lines.append("")
    return "\n".join(lines)


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
        # Layer B: if this turn produced files via sb_*_create, append a
        # "本轮产出的交付物" section to the assistant Turn content so even
        # text-only metrics (PromptAlignment / AnswerRelevancy / our 3 GEval
        # rubrics) that don't introspect tools_called see the deliverable.
        artifact_section = _build_artifact_section(tools_called)
        turn_content = (reply + artifact_section) if artifact_section else reply
        turns.append(Turn(
            role="assistant",
            content=turn_content,
            tools_called=tools_called or None,
        ))
    return session, turns


def build_conversational(
    golden: dict,
    turns: list[Turn],
    session: "MiraSession | None" = None,
) -> ConversationalTestCase:
    """Wrap turns in a ConversationalTestCase using golden metadata.

    If `session` is provided, stash session.warnings and any tool-error markers
    into `test_case.metadata`. RunCompletionMetric reads these to do mechanical
    health checks without firing an LLM judge. Other metrics ignore metadata.

    If the golden lists `_acceptable_paths`, append them to expected_outcome so
    every text-based metric (Completeness / Deliverable / Professional / Goal /
    PromptAlignment) sees "any of these decision paths counts as completion".
    Without this, judges anchor on a single fixed expected path and produce
    false FAILs when Mira takes a reasonable HITL branch (e.g. asking the user
    instead of force-creating on conflict). See dataset golden [16] commentary.
    """
    metadata: dict | None = None
    if session is not None:
        tool_errors: list[str] = []
        for entry in session.history:
            for tc in entry.get("tool_calls") or []:
                if tc.get("status") == "error":
                    name = tc.get("tool_name") or "?"
                    out = tc.get("output")
                    tool_errors.append(f"{name}: {str(out)[:80]}")
        metadata = {
            "mira_warnings": list(session.warnings),
            "mira_tool_errors": tool_errors,
        }
    expected_outcome = golden.get("expected_outcome") or ""
    acceptable = golden.get("_acceptable_paths") or []
    if acceptable:
        bullets = "\n".join(f"  - {p}" for p in acceptable)
        expected_outcome = (
            f"{expected_outcome}\n\n"
            f"【可接受的合法完成路径，命中任一即视为达成预期】\n{bullets}"
        )
    return ConversationalTestCase(
        turns=turns,
        scenario=golden.get("scenario"),
        expected_outcome=expected_outcome,
        chatbot_role=golden.get("chatbot_role"),
        metadata=metadata,
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
    with dataset_path.open(encoding="utf-8") as f:
        data = json.load(f)
    goldens = data["goldens"]
    tier = os.environ.get("MIRA_GOLDEN_TIER", "all").lower()
    if tier in ("light", "heavy"):
        goldens = [g for g in goldens if g.get("_tier") == tier]
    return goldens


def golden_id(g: dict) -> str:
    return (g.get("scenario") or "case")[:60]
