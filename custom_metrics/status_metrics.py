"""Health-check metrics: trace completion + DB persistence.

These don't measure quality at all — they ask "did the session run cleanly
end-to-end?" Use them as gates: if these fail, the per-turn quality scores
that follow can't be trusted.
"""
from __future__ import annotations

import json
from typing import Any

from deepeval.metrics import BaseMetric

from ._base import get_conv_id
from ._db import fetch_messages
from ._langfuse import fetch_session_traces, hydrate_trace


# Terminal states allowed on the last `parts[*]` of an assistant message.
# Mirrors the validation in Mira_Validation's databaseStatusEvaluator.
_VALID_LAST_PART_TYPES = {"tool-clarify_question", "tool-confirm"}


class CompletedMetric(BaseMetric):
    """Trace-level health check (binary 0/1).

    Passes when:
    1. At least one trace exists for the session.
    2. Some trace has a non-null ``end_time`` (or a ``mira-agent`` observation
       does), proving the assistant turn finished.
    3. The trace's ``level`` (when present) is ``DEFAULT`` — not ERROR/WARN.
    4. ``test_case.actual_output`` is non-empty.

    Fails fast on the first violation. Cheaper than the DB check, runs purely
    against Langfuse data.
    """

    def __init__(self) -> None:
        self.threshold = 1.0
        self.async_mode = False
        self.strict_mode = False
        self.verbose_mode = False
        self.include_reason = True
        self.score: float | None = None
        self.reason: str | None = None
        self.success: bool | None = None
        self.error: str | None = None
        self.evaluation_cost = None

    @property
    def __name__(self) -> str:  # noqa: D401
        return "Completed"

    def is_successful(self) -> bool:
        return bool(self.score and self.score >= self.threshold)

    def _fail(self, reason: str) -> float:
        self.score = 0.0
        self.reason = reason
        self.success = False
        return 0.0

    def _pass(self, reason: str) -> float:
        self.score = 1.0
        self.reason = reason
        self.success = True
        return 1.0

    def measure(self, test_case: Any, *_: Any, **__: Any) -> float:
        conv_id = get_conv_id(test_case)
        if not conv_id:
            self.error = "no conv_id on test_case.metadata"
            return self._fail(self.error)

        # Prefer last assistant turn's content (ConversationalTestCase path);
        # fall back to actual_output for LLMTestCase. Mirrors Mira_Validation's
        # completedEvaluator, which read BFF's finalOutput (= last assistant reply).
        turns = getattr(test_case, "turns", None) or []
        last_assistant = next(
            (t for t in reversed(turns) if getattr(t, "role", None) == "assistant"),
            None,
        )
        if last_assistant is not None:
            actual = getattr(last_assistant, "content", None) or ""
            empty_reason = "last assistant turn has empty content"
        else:
            actual = getattr(test_case, "actual_output", None) or ""
            empty_reason = "actual_output is empty"
        if not actual.strip():
            return self._fail(empty_reason)

        traces = fetch_session_traces(conv_id)
        if not traces:
            self.error = f"no Langfuse traces for session_id={conv_id}"
            return self._fail(self.error)

        # Check at least one trace has a valid end_time AND non-ERROR level.
        ok_any = False
        for short in traces:
            full = hydrate_trace(short)
            end_t = getattr(full, "end_time", None) or getattr(full, "endTime", None)
            mira_agent = [
                o for o in (full.observations or []) if getattr(o, "name", None) == "mira-agent"
            ]
            agent_done = bool(mira_agent) and all(
                (getattr(o, "end_time", None) or getattr(o, "endTime", None)) for o in mira_agent
            )
            level = getattr(full, "level", None) or "DEFAULT"
            if (end_t or agent_done) and level == "DEFAULT":
                ok_any = True
                break

        if not ok_any:
            return self._fail("no trace has end_time + level=DEFAULT")
        return self._pass(f"healthy: {len(traces)} trace(s), output non-empty")

    async def a_measure(self, test_case: Any, *args: Any, **kwargs: Any) -> float:
        return self.measure(test_case, *args, **kwargs)


class DatabaseStatusMetric(BaseMetric):
    """Verify the session's persisted messages in Mira Postgres (binary 0/1).

    Three checks (all must pass), mirroring Mira_Validation:

    1. ``user`` and ``assistant`` messages are strictly paired in sequence.
    2. Each assistant message ends with one of these ``parts[-1]`` shapes:
       - ``{type:"tool-complete", output.success: true}``
       - ``{type:"text", state:"done"}``
       - ``{type:"tool-clarify_question"}``
       - ``{type:"tool-confirm"}``
    3. Every ``parts[*]`` with ``type=text`` has ``state=done``, and every
       ``type=tool-*`` is not in ``input-streaming`` or ``output-error``.

    Skipped on assistants with ``metadata.aborted == true`` (intentional).
    """

    def __init__(self) -> None:
        self.threshold = 1.0
        self.async_mode = False
        self.strict_mode = False
        self.verbose_mode = False
        self.include_reason = True
        self.score: float | None = None
        self.reason: str | None = None
        self.success: bool | None = None
        self.error: str | None = None
        self.evaluation_cost = None

    @property
    def __name__(self) -> str:  # noqa: D401
        return "DatabaseStatus"

    def is_successful(self) -> bool:
        return bool(self.score and self.score >= self.threshold)

    def _fail(self, reason_obj: dict | str) -> float:
        self.score = 0.0
        self.reason = json.dumps(reason_obj, ensure_ascii=False) if isinstance(reason_obj, dict) else reason_obj
        self.success = False
        return 0.0

    def _pass(self, reason: str) -> float:
        self.score = 1.0
        self.reason = reason
        self.success = True
        return 1.0

    def measure(self, test_case: Any, *_: Any, **__: Any) -> float:
        conv_id = get_conv_id(test_case)
        if not conv_id:
            self.error = "no conv_id on test_case.metadata"
            return self._fail(self.error)

        try:
            rows = fetch_messages(conv_id)
        except Exception as e:  # noqa: BLE001 — surface DB issues to the report
            self.error = f"DB query failed: {e!s}"
            return self._fail(self.error)

        if not rows:
            return self._fail({"error": "no messages in DB for chat_id", "chat_id": conv_id})

        # 1) pair check
        pending_user = False
        pair_count = 0
        user_count = 0
        for r in rows:
            role = r.get("role")
            if role == "user":
                user_count += 1
                if pending_user:
                    return self._fail({"error": "two consecutive user messages", "chat_id": conv_id})
                pending_user = True
            elif role == "assistant":
                if not pending_user:
                    return self._fail({"error": "assistant before user", "chat_id": conv_id})
                pair_count += 1
                pending_user = False
        if pending_user:
            return self._fail({"error": "trailing user without assistant", "turns": user_count})
        if user_count > 0 and pair_count != user_count:
            return self._fail({"error": "pair count mismatch", "turns": user_count, "pairs": pair_count})

        # 2) + 3) per-assistant parts check
        for r in rows:
            if r.get("role") != "assistant":
                continue
            seq = r.get("sequence_num") or 0
            md = r.get("metadata") or {}
            if isinstance(md, str):
                try:
                    md = json.loads(md)
                except json.JSONDecodeError:
                    md = {}
            if md.get("aborted") is True:
                continue

            parts = r.get("parts") or []
            if isinstance(parts, str):
                try:
                    parts = json.loads(parts)
                except json.JSONDecodeError:
                    return self._fail({"error": "parts not JSON", "seq": seq})
            if not isinstance(parts, list) or not parts:
                return self._fail({"error": "parts empty", "seq": seq})

            last = parts[-1]
            last_type = (last or {}).get("type")
            last_state = (last or {}).get("state")
            last_output = (last or {}).get("output") or {}
            valid_last = (
                (last_type == "tool-complete" and last_output.get("success") is True)
                or (last_type == "text" and last_state == "done")
                or last_type in _VALID_LAST_PART_TYPES
            )
            if not valid_last:
                return self._fail({
                    "error": "invalid last part",
                    "seq": seq,
                    "type": last_type,
                    "state": last_state,
                })

            for i, p in enumerate(parts):
                if not isinstance(p, dict):
                    continue
                ptype = p.get("type")
                pstate = p.get("state")
                if ptype == "text" and pstate != "done":
                    return self._fail({
                        "error": "text part not done",
                        "seq": seq,
                        "index": i,
                        "state": pstate,
                    })
                if isinstance(ptype, str) and ptype.startswith("tool-"):
                    if pstate in ("input-streaming", "output-error"):
                        return self._fail({
                            "error": "tool part in bad state",
                            "seq": seq,
                            "index": i,
                            "type": ptype,
                            "state": pstate,
                        })

        return self._pass(f"validated {pair_count} user/assistant pair(s)")

    async def a_measure(self, test_case: Any, *args: Any, **kwargs: Any) -> float:
        return self.measure(test_case, *args, **kwargs)
