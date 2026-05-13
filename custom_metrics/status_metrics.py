"""Session-health gate: did this Mira run complete cleanly?

A single binary (0/1) metric that aggregates three independent layers:

  - client      — in-memory MiraSession signals (SSE warnings, tool errors,
                  empty assistant turns). Read from ``test_case.metadata``;
                  no network.
  - trace       — Langfuse trace ``end_time``/``mira-agent`` observation
                  completion + ``level == DEFAULT`` + last assistant content
                  non-empty.
  - persistence — Mira Postgres ``messages`` table user/assistant pairing
                  + each row's ``parts[*]`` terminal-state validation.

All three layers run on every measure() call (no short-circuit) so the
report shows the full picture even when multiple layers fail. ``score``
is binary 1.0/0.0 — every applicable layer must pass.

Environmental failures (Langfuse unreachable, DB connection failed) mark
the relevant layer ``status="error"`` and still flip the metric to FAIL —
"unable to verify" is treated as not-passing, not as an exception.

Replaces the old trio (``CompletedMetric`` + ``DatabaseStatusMetric`` +
``RunCompletionMetric``) which split this same question across three
pytest items and made it impossible to read "did this session finish"
off a report at a glance.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from deepeval.metrics import BaseConversationalMetric

from ._base import get_conv_id
from ._db import fetch_messages, get_conn
from ._langfuse import fetch_session_traces, hydrate_trace


# Terminal states allowed on the last `parts[*]` of an assistant message.
# Mirrors the validation in Mira_Validation's databaseStatusEvaluator.
_VALID_LAST_PART_TYPES = {"tool-clarify_question", "tool-confirm"}

# Substrings in MiraSession.warnings that we treat as hard failures.
# Keep this list small and explicit; other warnings are informational.
_FATAL_WARNING_SUBSTRS: tuple[str, ...] = ("stream_truncated",)


# One-shot probe result, populated on first persistence-layer call.
# (available: bool, reason: str | None)
_DB_PROBE: tuple[bool, str | None] | None = None


def _persistence_available() -> tuple[bool, str | None]:
    """Probe ``TEST_DATABASE_URL`` once and cache the verdict.

    Decision matrix:

      - URL not set / blank   → (False, "not configured")
      - URL set but ``SELECT 1`` fails (DNS, auth, IP-whitelist, etc.)
                              → (False, "unreachable: <err>")
      - URL set and reachable → (True, None)

    Cached for the process lifetime — if you ``unset TEST_DATABASE_URL``
    or fix a firewall rule mid-run, restart pytest to re-probe. The
    caller (``_check_persistence``) turns ``(False, why)`` into a
    skipped layer so the metric is not penalised when running against
    an environment that simply has no PG to check.
    """
    global _DB_PROBE
    if _DB_PROBE is not None:
        return _DB_PROBE

    url = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    if not url:
        _DB_PROBE = (False, "TEST_DATABASE_URL not configured")
        return _DB_PROBE

    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        _DB_PROBE = (True, None)
    except Exception as e:  # noqa: BLE001 — any connect/auth/network failure
        _DB_PROBE = (False, f"DB unreachable: {type(e).__name__}: {str(e)[:120]}")
    return _DB_PROBE


@dataclass
class _CheckResult:
    """One layer's verdict.

    status:
      - "pass"    layer's checks all succeeded
      - "fail"    layer's data was readable and at least one check failed
      - "error"   couldn't read the data (Langfuse unreachable, DB down)
      - "skipped" layer didn't apply (e.g. no conv_id available)
    """
    layer: str
    status: str
    detail: str = ""
    issues: list[str] = field(default_factory=list)


# ── per-layer checks ─────────────────────────────────────────────────────────

def _check_client(test_case: Any) -> _CheckResult:
    """In-memory signals from MiraSession (read via test_case.metadata)."""
    try:
        meta = getattr(test_case, "metadata", None) or {}
        issues: list[str] = []
        for w in (meta.get("mira_warnings") or []):
            if any(s in w for s in _FATAL_WARNING_SUBSTRS):
                issues.append(f"stream warning: {w[:120]}")
        for terr in (meta.get("mira_tool_errors") or []):
            issues.append(f"tool error: {terr[:100]}")
        turns = getattr(test_case, "turns", None) or []
        for i, turn in enumerate(turns):
            if getattr(turn, "role", None) == "assistant":
                content = getattr(turn, "content", "") or ""
                if not content.strip():
                    issues.append(f"empty assistant turn @ idx {i}")
        if issues:
            return _CheckResult("client", "fail", f"{len(issues)} issue(s)", issues)
        return _CheckResult("client", "pass", "ok")
    except Exception as e:  # noqa: BLE001 — surface as layer error, never propagate
        msg = f"{type(e).__name__}: {e}"
        return _CheckResult("client", "error", msg, [msg])


def _retry_call(fn, *, attempts: int = 3, base_delay: float = 0.5, op: str = "call"):
    """Small retry wrapper for transient Langfuse / TLS flakes.

    We've observed ``[SSL: UNEXPECTED_EOF_WHILE_READING]`` and stray
    ``ReadTimeout`` on otherwise healthy runs. 3 attempts with 0.5s / 1s
    backoff (≈ 1.5s ceiling between calls) absorbs these without masking
    a real outage. Each individual call's network timeout is governed by
    the Langfuse client (60s in _langfuse.py).
    """
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — propagated after final attempt
            last_exc = e
            if i < attempts - 1:
                time.sleep(base_delay * (2 ** i))
    assert last_exc is not None
    raise last_exc


def _check_trace(test_case: Any) -> _CheckResult:
    """Langfuse trace + last-assistant-content check.

    Splits failure modes clearly:
      - ``fail``   data was fetched but reveals a real problem (no trace
                   for this session_id, no clean end_time, empty content).
      - ``error``  couldn't fetch — Langfuse exception bubbled past retry,
                   or hydrate threw. The metric is unable to verify.
    """
    conv_id = get_conv_id(test_case)
    if not conv_id:
        return _CheckResult("trace", "skipped", "no conv_id on metadata")

    # Last assistant turn content non-empty (multi-turn path).
    # Fall back to actual_output for LLMTestCase shape. No network here.
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
        return _CheckResult("trace", "fail", empty_reason, [empty_reason])

    # Network call: retry transient TLS / connection flakes before declaring error.
    try:
        traces = _retry_call(lambda: fetch_session_traces(conv_id),
                             op="fetch_session_traces")
    except Exception as e:  # noqa: BLE001 — terminal network failure → error
        msg = f"fetch_session_traces: {type(e).__name__}: {e}"
        return _CheckResult("trace", "error", msg, [msg])

    if not traces:
        # Successful query, just no data — this is a real fail (session
        # never registered), not an environmental error.
        msg = f"no Langfuse traces for session_id={conv_id}"
        return _CheckResult("trace", "fail", msg, [msg])

    # At least one trace must have a valid end_time AND non-ERROR level.
    # Hydrate also gets retry-wrapped — ReadTimeout on this endpoint is
    # the second most common Langfuse flake after fetch_session_traces.
    try:
        ok_any = False
        for short in traces:
            full = _retry_call(lambda s=short: hydrate_trace(s),
                               op="hydrate_trace")
            end_t = getattr(full, "end_time", None) or getattr(full, "endTime", None)
            mira_agent = [
                o for o in (full.observations or [])
                if getattr(o, "name", None) == "mira-agent"
            ]
            agent_done = bool(mira_agent) and all(
                (getattr(o, "end_time", None) or getattr(o, "endTime", None))
                for o in mira_agent
            )
            level = getattr(full, "level", None) or "DEFAULT"
            if (end_t or agent_done) and level == "DEFAULT":
                ok_any = True
                break
    except Exception as e:  # noqa: BLE001 — hydrate network exception
        msg = f"hydrate_trace: {type(e).__name__}: {e}"
        return _CheckResult("trace", "error", msg, [msg])

    if not ok_any:
        msg = "no trace has end_time + level=DEFAULT"
        return _CheckResult("trace", "fail", msg, [msg])
    return _CheckResult("trace", "pass", f"{len(traces)} trace(s) end clean")


def _validate_messages(rows: list[dict]) -> tuple[list[str], int]:
    """Inner pair + parts validation. Returns (issues, pair_count).

    Pure function over the rows we just queried — no I/O, safe to call from
    retry loop.
    """
    issues: list[str] = []

    # 1) pairing check
    pending_user = False
    pair_count = 0
    user_count = 0
    for r in rows:
        role = r.get("role")
        if role == "user":
            user_count += 1
            if pending_user:
                issues.append("two consecutive user messages")
                break
            pending_user = True
        elif role == "assistant":
            if not pending_user:
                issues.append("assistant before user")
                break
            pair_count += 1
            pending_user = False
    if not issues:
        if pending_user:
            issues.append(f"trailing user without assistant (turns={user_count})")
        elif user_count > 0 and pair_count != user_count:
            issues.append(
                f"pair count mismatch: turns={user_count} pairs={pair_count}"
            )

    # 2) + 3) per-assistant parts validation (skip if pairing already failed)
    if not issues:
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
                    issues.append(f"seq={seq} parts not JSON")
                    break
            if not isinstance(parts, list) or not parts:
                issues.append(f"seq={seq} parts empty")
                break

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
                issues.append(
                    f"seq={seq} invalid last part type={last_type} state={last_state}"
                )
                break

            bad_part = False
            for i, p in enumerate(parts):
                if not isinstance(p, dict):
                    continue
                ptype = p.get("type")
                pstate = p.get("state")
                if ptype == "text" and pstate != "done":
                    issues.append(
                        f"seq={seq} text part idx={i} state={pstate} (not done)"
                    )
                    bad_part = True
                    break
                if isinstance(ptype, str) and ptype.startswith("tool-"):
                    if pstate in ("input-streaming", "output-error"):
                        issues.append(
                            f"seq={seq} tool part idx={i} type={ptype} state={pstate}"
                        )
                        bad_part = True
                        break
            if bad_part:
                break

    return issues, pair_count


def _check_persistence(
    test_case: Any,
    attempts: int = 3,
    base_delay: float = 1.5,
) -> _CheckResult:
    """Mira Postgres `messages` table check, with DB-lag-tolerant retry.

    Mira BFF writes the user row at request time but the assistant row
    only after the response is fully generated. If our metric runs the
    instant Mira's HTTP stream ends (especially when the stream was
    truncated and we proceeded fast), the assistant row may not be in PG
    yet → we'd falsely report "trailing user without assistant".

    Retry: up to ``attempts`` validations, ``base_delay`` × 2^i seconds
    between (default 1.5s, 3s, 6s ≈ 10s total upper bound). Returns as
    soon as a pass occurs.

    Skips (rather than fails) when ``TEST_DATABASE_URL`` is unset or
    unreachable from this host — see ``_persistence_available``.
    """
    conv_id = get_conv_id(test_case)
    if not conv_id:
        return _CheckResult("persistence", "skipped", "no conv_id on metadata")

    ok, why = _persistence_available()
    if not ok:
        return _CheckResult("persistence", "skipped", why or "DB not configured")

    last_issues: list[str] = []
    last_pair_count = 0

    for i in range(attempts):
        try:
            rows = fetch_messages(conv_id)
        except Exception as e:  # noqa: BLE001 — DB connection failure is env
            msg = f"DB query failed: {e!s}"
            return _CheckResult("persistence", "error", msg, [msg])

        if not rows:
            last_issues = [f"no messages in DB for chat_id={conv_id}"]
        else:
            last_issues, last_pair_count = _validate_messages(rows)
            if not last_issues:
                return _CheckResult(
                    "persistence",
                    "pass",
                    f"{last_pair_count} user/assistant pair(s) validated"
                    + (f" (after {i+1} attempt{'s' if i else ''})" if i else ""),
                )

        if i < attempts - 1:
            time.sleep(base_delay * (2 ** i))

    return _CheckResult(
        "persistence",
        "fail",
        "; ".join(last_issues)[:200] + f" (after {attempts} attempts)",
        last_issues,
    )


# ── public metric ────────────────────────────────────────────────────────────

class SessionHealthMetric(BaseConversationalMetric):
    """Unified session-completion gate across 3 independent layers.

    Binary 1.0/0.0 — every applicable (non-skipped) layer must pass. When this
    metric fails, the quality scores downstream are not trustworthy.

    The full per-layer breakdown is exposed in two places:

      - ``self.reason`` — multi-line human-readable summary that the report
        renders directly. Each layer shows pass/fail/error/skipped + detail.
      - ``self.checks`` — structured ``list[_CheckResult]`` for callers that
        want to inspect individual layers (e.g. custom dashboards).
    """

    def __init__(self, threshold: float = 1.0, async_mode: bool = False):
        self.threshold = threshold
        self.async_mode = async_mode
        self.strict_mode = False
        self.verbose_mode = False
        self.include_reason = True
        self.score: float | None = None
        self.success: bool | None = None
        self.reason: str | None = None
        self.error: str | None = None
        self.evaluation_cost = 0
        self.checks: list[_CheckResult] = []

    @property
    def __name__(self) -> str:  # noqa: D401 — match deepeval's other metrics
        return "SessionHealth"

    def measure(self, test_case: Any, *_: Any, **__: Any) -> float:
        # Run every layer; never short-circuit, so the report can show full picture.
        self.checks = [
            _check_client(test_case),
            _check_trace(test_case),
            _check_persistence(test_case),
        ]

        active = [c for c in self.checks if c.status != "skipped"]
        pass_count = sum(1 for c in active if c.status == "pass")
        skip_count = len(self.checks) - len(active)
        all_pass = bool(active) and all(c.status == "pass" for c in active)

        self.score = 1.0 if all_pass else 0.0
        self.success = all_pass
        self.reason = self._format_reason(pass_count, len(active), skip_count)

        errs = [f"{c.layer}: {c.detail}" for c in self.checks if c.status == "error"]
        self.error = "; ".join(errs)[:300] if errs else None
        return self.score

    async def a_measure(self, test_case: Any, *args: Any, **kwargs: Any) -> float:
        return self.measure(test_case, *args, **kwargs)

    def is_successful(self) -> bool:
        return bool(self.score is not None and self.score >= self.threshold)

    def _format_reason(self, pass_count: int, active: int, skipped: int) -> str:
        if active == 0:
            return "INCONCLUSIVE — all layers skipped (no conv_id)"
        verdict = "PASS" if pass_count == active else "FAIL"
        head = f"{verdict} ({pass_count}/{active} layers"
        if skipped:
            head += f", {skipped} skipped"
        head += ")"
        icon = {"pass": "✓", "fail": "✗", "error": "🚨", "skipped": "•"}
        lines = [head]
        for c in self.checks:
            # Prefer the first issue text over the bare "N issue(s)" summary
            # when status is fail/error — much more useful in reports.
            if c.status in ("fail", "error") and c.issues:
                detail_text = c.issues[0]
                if len(c.issues) > 1:
                    detail_text += f" (+{len(c.issues) - 1} more)"
            else:
                detail_text = c.detail
            lines.append(f"  {icon[c.status]} {c.layer:<11} — {detail_text}")
        return "\n".join(lines)
