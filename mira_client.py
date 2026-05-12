"""Thin client for Mira's BFF chat endpoint.

Talks to `POST /api/task` with a better-auth session cookie, parses the
Vercel AI SDK v6 SSE stream, and returns the assistant's final text and
the structured tool-call trace.

Multi-turn: pass the same `conversation_id` to `MiraSession.send()` — the
server uses `getOrCreateTask` so subsequent messages append to the same task.

File attachments: pass `attachments=[Path]` to `MiraSession.send()`. The client
uploads each file via `POST /api/files/upload` → `PUT` to the signed R2 URL,
then injects `[Uploaded File: /mnt/task/upload/<name>|<size>|<r2Key>]` markers
into the user message text. The BFF's file-preprocessor extracts these markers
and signs them back into FileUIParts for the model. /api/task itself does NOT
accept multipart — text+marker is the only supported transport.

SSE events captured (Vercel AI SDK v6):
- start                       : message id
- text-start / text-delta / text-end : assistant prose chunks
- tool-input-start            : toolCallId + toolName announced
- tool-input-delta            : streaming JSON input chunks
- tool-input-available        : final input dict for the tool call
- tool-approval-request       : tool paused, approvalId issued (HITL gate)
- tool-output-available       : tool result dict
- start-step / finish-step    : step boundaries within one assistant turn
- finish                      : finishReason
- [DONE]                      : end of stream

HITL auto-approval: Mira's voice / dangerous-action flows pause on the
built-in `confirm` tool (or any tool that emits `tool-approval-request`).
The stream ends with `finishReason=tool-calls` and the tool sits in
`approval-requested` state. By default `MiraSession.send()` auto-approves
each gate by POSTing a `toolInvocations` body with `state="approval-responded"`
and `approval.approved=true`, then resumes the stream — up to
`MAX_APPROVAL_ROUNDS` rounds per turn. Disable by passing `auto_approve=False`.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

import httpx
from dotenv import load_dotenv


load_dotenv(Path(__file__).parent / ".env")

BFF_URL = os.environ.get("MIRA_BFF_URL", "https://mira-bff-preview.up.railway.app").rstrip("/")
SESSION_TOKEN = os.environ["MIRA_SESSION_TOKEN"]
COOKIE_NAME = os.environ.get("MIRA_COOKIE_NAME", "__Secure-better-auth.session_token")
REQUEST_TIMEOUT_S = int(os.environ.get("MIRA_TIMEOUT_S", "1800"))
UPLOAD_TIMEOUT_S = int(os.environ.get("MIRA_UPLOAD_TIMEOUT_S", "300"))
MAX_APPROVAL_ROUNDS = int(os.environ.get("MIRA_MAX_APPROVAL_ROUNDS", "8"))

# Tools that block on HITL by emitting an input-available state and no execute()
# in the AI SDK tool registration. Frontend resolves by calling
# `addToolOutput(toolCallId, <output>)` — see
# apps/mira-work/shared/tools/base/ask-for-confirmation.tsx (string output)
# and apps/mira-work/shared/tools/base/clarify-question-form.tsx (dict output
# built from field defaults). Listed by toolName (not type prefix).
AUTO_CONFIRM_TOOLS: set[str] = {
    "confirm",                       # built-in confirm-tool.ts → "Yes, confirmed."
    "bua_confirm_sensitive_action",  # BUA destructive-action gate → same string
    "clarify_question",              # built-in clarify-question.ts → {field:default}
}


def _clarify_question_default_output(tool_input: dict | None) -> dict:
    """Build the auto-submit payload for a `clarify_question` form by walking
    its fields and picking each field's `defaultValue` (or locked options).

    Mira pre-fills the form with sane defaults derived from the user's prompt
    (e.g. 推荐公司=美团, 执行时间=2026-05-12 11:00:00). Submitting those
    defaults verbatim is the same thing a sane user would do — not cheating.
    """
    out: dict[str, object] = {}
    fields = (tool_input or {}).get("fields") or []
    for f in fields:
        if not isinstance(f, dict):
            continue
        fid = f.get("id")
        if not fid:
            continue
        ftype = f.get("type")
        default = f.get("defaultValue")
        if default is not None:
            out[fid] = default
            continue
        if ftype == "checkbox":
            opts = f.get("options") or []
            out[fid] = [o.get("value") for o in opts if isinstance(o, dict) and o.get("locked")]
        elif ftype in ("select", "radio"):
            opts = f.get("options") or []
            locked = next((o.get("value") for o in opts if isinstance(o, dict) and o.get("locked")), None)
            if locked is not None:
                out[fid] = locked
            elif opts and isinstance(opts[0], dict):
                out[fid] = opts[0].get("value", "")
            else:
                out[fid] = ""
        else:
            out[fid] = ""
    return out

# Mirrors apps/mira-work/lib/utils/file-utils.ts:inferMediaTypeFromFilename.
# Server validates content-type against TASK_UPLOAD_SERVER_ALLOWED_MIME_TYPES;
# octet-stream falls through, so prefer the canonical type for office/csv/etc.
_MIME_BY_EXT: dict[str, str] = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".csv": "text/csv",
    ".json": "application/json",
}


def _guess_content_type(path: Path) -> str:
    ext = path.suffix.lower()
    return _MIME_BY_EXT.get(ext) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _safe_r2_name(name: str) -> str:
    """Replicate r2-utils.ts:getSignedUploadUrl's name sanitiser so the local
    marker uses the same path the server signs."""
    return re.sub(r"[^a-zA-Z0-9._\-一-龥]", "_", name)


class MiraError(RuntimeError):
    pass


def _parse_sse_event(line: str) -> dict | None:
    if not line.startswith("data:"):
        return None
    payload = line[len("data:"):].strip()
    if not payload or payload == "[DONE]":
        return {"_done": payload == "[DONE]"}
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


@dataclass
class AssistantStreamResult:
    text: str
    message_id: str | None
    tool_calls: list[dict]  # ordered as observed in the stream
    error_marker: str | None


def _consume_stream(lines: Iterator[str]) -> AssistantStreamResult:
    """Consume SSE lines, return text + tool calls + meta.

    Tool calls are dicts with keys: tool_call_id, tool_name, input, output,
    status ('ok' | 'no_output' | 'error'), input_text_buffer (raw delta join
    when input dict not yet assembled).
    """
    chunks: list[str] = []
    message_id: str | None = None
    finish_reason: str | None = None
    error_marker: str | None = None
    # Tool calls keyed by toolCallId to allow out-of-order input/output events.
    tc_index: dict[str, dict[str, Any]] = {}
    tc_order: list[str] = []

    def _ensure(tc_id: str) -> dict:
        if tc_id not in tc_index:
            tc_index[tc_id] = {
                "tool_call_id": tc_id,
                "tool_name": None,
                "dynamic": False,
                "input": None,
                "input_text_buffer": "",
                "output": None,
                "status": "pending",
                "approval_id": None,
            }
            tc_order.append(tc_id)
        return tc_index[tc_id]

    try:
        for raw in lines:
            if not raw:
                continue
            evt = _parse_sse_event(raw)
            if evt is None:
                continue
            if evt.get("_done"):
                break
            etype = evt.get("type")

            if etype == "start" and "messageId" in evt:
                message_id = evt["messageId"]
            elif etype == "text-delta":
                chunks.append(evt.get("delta", ""))
            elif etype == "finish":
                finish_reason = evt.get("finishReason")

            elif etype == "tool-input-start":
                tc = _ensure(evt["toolCallId"])
                tc["tool_name"] = evt.get("toolName") or tc["tool_name"]
                if evt.get("dynamic"):
                    tc["dynamic"] = True
            elif etype == "tool-input-delta":
                tc = _ensure(evt["toolCallId"])
                tc["input_text_buffer"] += evt.get("inputTextDelta", "")
            elif etype == "tool-input-available":
                tc = _ensure(evt["toolCallId"])
                tc["tool_name"] = evt.get("toolName") or tc["tool_name"]
                tc["input"] = evt.get("input")
                if evt.get("dynamic"):
                    tc["dynamic"] = True
            elif etype == "tool-approval-request":
                tc = _ensure(evt["toolCallId"])
                tc["approval_id"] = evt.get("approvalId")
                tc["status"] = "awaiting_approval"
            elif etype == "tool-output-available":
                tc = _ensure(evt["toolCallId"])
                tc["output"] = evt.get("output")
                tc["status"] = "ok"
            elif etype == "tool-output-error":
                tc = _ensure(evt["toolCallId"])
                tc["output"] = evt.get("error")
                tc["status"] = "error"
    except httpx.RemoteProtocolError as e:
        error_marker = f"stream_truncated: {e}"
    except httpx.ReadTimeout as e:
        error_marker = f"stream_read_timeout: {e}"

    text = "".join(chunks)
    if finish_reason and finish_reason not in ("stop", "end_turn"):
        text = f"{text}\n\n[finish_reason={finish_reason}]"

    # Mark tool calls that never produced an output (truncated stream, etc.)
    tool_calls: list[dict] = []
    for tc_id in tc_order:
        tc = tc_index[tc_id]
        if tc["status"] == "pending" and tc["output"] is None:
            tc["status"] = "no_output"
        # If input dict was never assembled but we have buffered deltas, try to
        # parse the buffer (it's usually a single JSON object spread across deltas).
        if tc["input"] is None and tc["input_text_buffer"]:
            try:
                tc["input"] = json.loads(tc["input_text_buffer"])
            except json.JSONDecodeError:
                pass
        # Drop the buffer from the public record.
        tc.pop("input_text_buffer", None)
        tool_calls.append(tc)

    return AssistantStreamResult(
        text=text, message_id=message_id, tool_calls=tool_calls, error_marker=error_marker,
    )


@dataclass
class MiraSession:
    conversation_id: str = field(default_factory=lambda: f"deepeval-{uuid.uuid4().hex[:12]}")
    history: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    model_override: str | None = None

    def upload_file(self, path: str | Path) -> dict:
        """Upload a local file via the BFF's signed-URL flow.

        Two-step (matches features/task/components/task-input.tsx):
          1. POST /api/files/upload {fileName, contentType, taskId, fileSize}
             → {uploadUrl, key}
          2. PUT bytes to uploadUrl with the same Content-Type header.

        Returns {"path": "/mnt/task/upload/<safe>", "size": N, "r2Key": "..."}
        — the dict you embed via `[Uploaded File: ...]` in user_message text.
        """
        p = Path(path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"attachment not found: {p}")
        safe_name = _safe_r2_name(p.name)
        content_type = _guess_content_type(p)
        size = p.stat().st_size

        headers = {"Content-Type": "application/json"}
        cookies = {COOKIE_NAME: SESSION_TOKEN}
        body = {
            "fileName": safe_name,
            "contentType": content_type,
            "taskId": self.conversation_id,
            "fileSize": size,
        }

        with httpx.Client(timeout=httpx.Timeout(UPLOAD_TIMEOUT_S)) as client:
            sign = client.post(f"{BFF_URL}/api/files/upload", json=body, headers=headers, cookies=cookies)
            if sign.status_code != 200:
                raise MiraError(f"sign upload failed ({sign.status_code}): {sign.text[:300]}")
            signed = sign.json()
            upload_url = signed["uploadUrl"]
            r2_key = signed["key"]

            with p.open("rb") as f:
                put = client.put(upload_url, content=f.read(), headers={"Content-Type": content_type})
            if put.status_code not in (200, 201):
                raise MiraError(f"R2 PUT failed ({put.status_code}): {put.text[:300]}")

        # R2 key shape: {userId}/task/{taskId}/upload/{safeName}
        # Marker path mirrors task-input.tsx — strip everything up to and including
        # `task/{taskId}/`, prefix with `/mnt/task/`.
        seg = f"task/{self.conversation_id}/"
        idx = r2_key.find(seg)
        if idx < 0:
            raise MiraError(f"unexpected r2 key shape: {r2_key}")
        marker_path = f"/mnt/task/{r2_key[idx + len(seg):]}"
        return {"path": marker_path, "size": size, "r2Key": r2_key}

    def send(
        self,
        user_message: str,
        max_attempts: int = 2,
        attachments: Iterable[str | Path] | None = None,
        auto_approve: bool = True,
    ) -> str:
        """Send `user_message`, return the assistant's final text.

        If `attachments` is given, each file is uploaded to R2 first and an
        `[Uploaded File: ...]` marker is appended to the user message text.

        If `auto_approve` (default), HITL approval gates (tools that emit
        `tool-approval-request`) are automatically approved and the stream is
        resumed via a `toolInvocations` POST, up to `MAX_APPROVAL_ROUNDS`.

        Side effect: appends user + assistant entries to `self.history`. The
        assistant entry is a dict with keys: role, content, tool_calls,
        message_id, approval_rounds. Read it to build a Turn.
        """
        attached: list[dict] = []
        for attachment in attachments or ():
            attached.append(self.upload_file(attachment))

        if attached:
            markers = "\n".join(
                f"[Uploaded File: {a['path']}|{a['size']}|{a['r2Key']}]"
                for a in attached
            )
            user_message = f"{user_message}\n\n{markers}" if user_message else markers

        self.history.append({
            "role": "user",
            "content": user_message,
            "attachments": attached or None,
        })

        # 1) initial user-message POST (with simple retry on empty stream)
        last_marker: str | None = None
        result = AssistantStreamResult(text="", message_id=None, tool_calls=[], error_marker=None)
        for _ in range(1, max_attempts + 1):
            result = self._post_user_message(user_message)
            last_marker = result.error_marker
            if result.text.strip() and not result.error_marker:
                break
            if result.text.strip() and result.error_marker:
                break  # partial text; don't retry (would duplicate server-side)

        # 2) drain HITL gates: confirm-style tools end the stream with
        #    `state=input-available` and no output. POST a toolInvocations body
        #    that supplies a synthetic output and resume the stream. Repeat
        #    until no more gates or MAX_APPROVAL_ROUNDS reached.
        approval_rounds = 0
        merged_text = result.text
        merged_tool_calls: list[dict] = list(result.tool_calls)
        message_id = result.message_id

        if auto_approve and result.message_id:
            hit_cap = False
            while approval_rounds < MAX_APPROVAL_ROUNDS:
                pending = self._collect_pending_gates(merged_tool_calls)
                if not pending:
                    break
                approval_rounds += 1
                self.warnings.append(
                    f"auto_approve round {approval_rounds}: "
                    + ", ".join(f"{tc['tool_name']}({tc['status']})" for tc in pending)
                )
                resumed = self._post_tool_outputs(pending, message_id)
                if resumed.error_marker:
                    last_marker = resumed.error_marker
                if resumed.text:
                    merged_text = (merged_text + "\n" + resumed.text).strip()
                # Resumed stream's tool_calls update existing entries (same toolCallId)
                # or extend with brand-new ones.
                by_id = {tc["tool_call_id"]: tc for tc in merged_tool_calls}
                for tc in resumed.tool_calls:
                    existing = by_id.get(tc["tool_call_id"])
                    if existing is None:
                        merged_tool_calls.append(tc)
                        by_id[tc["tool_call_id"]] = tc
                    else:
                        if tc.get("output") is not None:
                            existing["output"] = tc["output"]
                        if tc.get("status") and tc["status"] != "pending":
                            existing["status"] = tc["status"]
                        if tc.get("approval_id"):
                            existing["approval_id"] = tc["approval_id"]
                # Mark the gates we just resolved so we don't loop forever if
                # the resumed stream didn't echo back an explicit output event
                # for them (server has already persisted our supplied output).
                for tc in pending:
                    if tc["status"] in ("no_output", "awaiting_approval"):
                        tc["status"] = "auto_resolved"
            else:
                hit_cap = True
            if hit_cap:
                self.warnings.append(
                    f"hit MAX_APPROVAL_ROUNDS={MAX_APPROVAL_ROUNDS}; remaining gates abandoned"
                )

        text = merged_text.strip() or "[empty assistant response]"
        if last_marker:
            self.warnings.append(last_marker)
        self.history.append({
            "role": "assistant",
            "content": text,
            "tool_calls": merged_tool_calls,
            "message_id": message_id,
            "approval_rounds": approval_rounds,
        })
        return text

    def _post_user_message(self, user_message: str) -> AssistantStreamResult:
        msg_id = f"msg-{uuid.uuid4().hex[:12]}"
        payload: dict = {
            "id": self.conversation_id,
            "message": {
                "id": msg_id,
                "role": "user",
                "parts": [{"type": "text", "text": user_message}],
            },
        }
        if self.model_override:
            payload["model"] = self.model_override
        return self._stream_task(payload)

    @staticmethod
    def _collect_pending_gates(tool_calls: list[dict]) -> list[dict]:
        """Tools that need a synthetic output to resume the stream.

        Two flavours:
          * confirm-style (no execute fn) → status=='no_output' and tool_name
            in AUTO_CONFIRM_TOOLS; resolve via state='output-available'.
          * AI SDK approval flow → status=='awaiting_approval' and approval_id
            set; resolve via state='approval-responded'.
        """
        out: list[dict] = []
        for tc in tool_calls:
            status = tc.get("status")
            name = tc.get("tool_name") or ""
            if status == "no_output" and name in AUTO_CONFIRM_TOOLS:
                out.append(tc)
            elif status == "awaiting_approval" and tc.get("approval_id"):
                out.append(tc)
        return out

    def _post_tool_outputs(
        self,
        pending: list[dict],
        assistant_message_id: str,
        approved: bool = True,
    ) -> AssistantStreamResult:
        """Resume Mira's stream by submitting tool outputs / approvals.

        Endpoint is POST /api/task with a {id, messageId, toolInvocations}
        payload (lib/schemas/message.schema.ts:toolInvocationsSchema). Two
        shapes are emitted based on the gate kind — see `_collect_pending_gates`.
        """
        invocations: list[dict] = []
        for tc in pending:
            base: dict = {
                "toolCallId": tc["tool_call_id"],
                "input": tc.get("input"),
            }
            if tc.get("dynamic"):
                base["type"] = "dynamic-tool"
                base["toolName"] = tc.get("tool_name") or ""
            else:
                base["type"] = f"tool-{tc.get('tool_name') or 'unknown'}"

            if tc.get("status") == "awaiting_approval" and tc.get("approval_id"):
                base["state"] = "approval-responded"
                base["approval"] = {
                    "id": tc["approval_id"],
                    "approved": approved,
                    "reason": "auto-approved by eval driver",
                }
            else:
                base["state"] = "output-available"
                tname = tc.get("tool_name") or ""
                if tname == "clarify_question":
                    base["output"] = _clarify_question_default_output(tc.get("input")) if approved else {"_eval_denied": True}
                else:
                    # ask-for-confirmation.tsx submits a plain string; `isConfirmed`
                    # in the frontend keys off "confirmed" / "yes" substring.
                    base["output"] = "Yes, confirmed." if approved else "No, denied."
            invocations.append(base)

        payload = {
            "id": self.conversation_id,
            "messageId": assistant_message_id,
            "toolInvocations": invocations,
        }
        if self.model_override:
            payload["model"] = self.model_override
        return self._stream_task(payload)

    def _stream_task(self, payload: dict) -> AssistantStreamResult:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        cookies = {COOKIE_NAME: SESSION_TOKEN}
        try:
            with httpx.stream(
                "POST",
                f"{BFF_URL}/api/task",
                json=payload,
                headers=headers,
                cookies=cookies,
                timeout=httpx.Timeout(REQUEST_TIMEOUT_S, read=REQUEST_TIMEOUT_S),
            ) as r:
                if r.status_code != 200:
                    body = r.read().decode(errors="replace")[:500]
                    return AssistantStreamResult(
                        text="", message_id=None, tool_calls=[],
                        error_marker=f"http_{r.status_code}: {body}",
                    )
                return _consume_stream(r.iter_lines())
        except httpx.RemoteProtocolError as e:
            return AssistantStreamResult("", None, [], f"connect_truncated: {e}")
        except httpx.HTTPError as e:
            return AssistantStreamResult("", None, [], f"http_error: {e}")


# Backwards-compat shim
ChatbotSession = MiraSession


if __name__ == "__main__":
    s = MiraSession()
    print(f"conv_id={s.conversation_id}")
    print("USER : Use web search to find the official website of Anker Innovations. Just give me the URL.")
    reply = s.send("Use web search to find the official website of Anker Innovations. Just give me the URL.")
    print("MIRA :", reply[:200], "...")
    print()
    last = s.history[-1]
    print(f"captured {len(last['tool_calls'])} tool call(s):")
    for tc in last["tool_calls"]:
        print(f"  - tool={tc['tool_name']!r} status={tc['status']} input={json.dumps(tc.get('input'), ensure_ascii=False)[:200]}")
        out = tc.get("output")
        if isinstance(out, dict):
            print(f"      output keys: {list(out.keys())[:8]}")
