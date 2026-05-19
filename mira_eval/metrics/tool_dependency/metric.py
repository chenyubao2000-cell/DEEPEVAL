"""Tool-call path compliance metric (LLM-as-judge).

Ports Mira_Validation's ``toolDependencyEvaluator``: feeds the local claude
CLI a catalog of tool-sequence constraints plus the actual tool calls Mira
made in the session, asks it which constraints were violated, scores by
step-hit-rate.

Constraint catalog lives at ``data/tool_dependencies.json`` (the project root
data/ directory). Override via ``constraints_path=`` constructor arg.

Scoring (normalised 0-1 unlike Mira_Validation's 0-100):
- No tool calls → 1.0 (skip)
- No violations → 1.0
- ``score = (totalSteps - violatingSteps) / totalSteps``
  ``violatingSteps`` = de-duplicated set of step numbers appearing in any
  violation's ``where`` field. Lenient toward long paths — one violation
  doesn't tank the whole sequence.

Cost: 1 claude CLI call per measure() (~30-60s). The CLI caps concurrency
at 4 via a semaphore in models.claude_cli, so this metric is safe to run
under DeepEval's async gathering.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Optional

from deepeval.metrics import BaseMetric

from ...config import ROOT
from ...models import ClaudeCliJudge
from ...tracing.langfuse import (
    fetch_session_traces,
    llm_generations,
    merge_observations,
)
from .._base import get_conv_id


_DEFAULT_CONSTRAINTS = ROOT / "data" / "tool_dependencies.json"

# Args worth surfacing to the judge — match the TS implementation's whitelist.
_INTERESTING_ARG_KEYS = (
    "filePath",
    "targetFile",
    "command",
    "url",
    "selector",
    "uid",
    "query",
)

_STEP_RE = re.compile(r"step\s+(\d+)", re.IGNORECASE)


def _load_constraints(path: Path) -> list[dict]:
    """Load and validate the constraint catalog."""
    if not path.is_file():
        raise FileNotFoundError(f"constraints catalog not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("constraints"), list):
        raise ValueError(f"{path}: expected {{'constraints': [...]}}")
    return payload["constraints"]


def _compact_args(args: Any) -> Optional[str]:
    """Reduce a tool call's input dict to a short whitelist of args."""
    if not isinstance(args, dict):
        return None
    parts: list[str] = []
    for k in _INTERESTING_ARG_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v:
            shown = v if len(v) <= 60 else v[:60] + "…"
            parts.append(f"{k}={shown}")
    return " ".join(parts) if parts else None


def _extract_tool_calls(generations: list[Any]) -> list[dict]:
    """Walk the LATEST doStream observation's input.messages, pull every
    ``role=assistant`` ``type=tool-call`` content item."""
    if not generations:
        return []

    def _start_ts(o: Any) -> float:
        ts = getattr(o, "start_time", None) or getattr(o, "startTime", None)
        if hasattr(ts, "timestamp"):
            return ts.timestamp()
        if isinstance(ts, (int, float)):
            return float(ts)
        return 0.0

    last = sorted(generations, key=_start_ts)[-1]
    raw_input = getattr(last, "input", None)
    if isinstance(raw_input, str):
        try:
            raw_input = json.loads(raw_input)
        except json.JSONDecodeError:
            return []

    if isinstance(raw_input, list):
        messages = raw_input
    elif isinstance(raw_input, dict):
        messages = raw_input.get("messages") or []
    else:
        return []

    out: list[dict] = []
    step = 0
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, dict):
            content = [content]
        if not isinstance(content, list):
            continue
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool-call":
                step += 1
                out.append({
                    "step": step,
                    "toolName": c.get("toolName") or "unknown",
                    "argsHint": _compact_args(c.get("input")),
                })
    return out


def _build_prompt(question: str, constraints: list[dict], tool_calls: list[dict]) -> str:
    constraints_block = "\n\n".join(
        "\n".join([
            f"### {c['id']} [severity={c.get('severity', 'soft')}]",
            f"source: {c.get('source', '')}",
            f"rule: {c.get('rule', '')}",
            f"detect: {c.get('detect', '')}",
        ])
        for c in constraints
    )
    calls_block = "\n".join(
        f"{c['step']}. {c['toolName']}" + (f"  // {c['argsHint']}" if c.get("argsHint") else "")
        for c in tool_calls
    ) or "(无工具调用)"

    return "\n".join([
        "你是 Mira Agent 工具调用约束审查员。任务：仅根据下面的『约束目录』和『实际工具调用序列』，判定路径违反了哪几条约束。",
        "",
        "## 约束目录",
        constraints_block,
        "",
        "## 用户问题",
        question or "(无)",
        "",
        "## 实际工具调用序列（按时序）",
        calls_block,
        "",
        "## 输出要求",
        "- 严格只输出 JSON，不要任何解释、代码块、前后空行。",
        "- 只标『约束目录里』明确列出的违反，不要凭印象自创规则。",
        "- 如果某条约束的 detect 描述无法从仅有路径中确定（如 sb_command_execute 是否危险需看参数），不要标违反。",
        '- where 用 "step N" 或 "step N→N" 形式定位。',
        "- evidence 用一句话说为什么违反（≤60 字）。",
        "",
        "## 输出 schema",
        "{",
        '  "violations": [',
        '    { "id": "<约束 id>", "severity": "hard"|"soft", "where": "step N", "evidence": "..." }',
        "  ],",
        '  "summary": "1-2 句中文总结，无违反则写 \'通过\'"',
        "}",
    ])


def _extract_violating_steps(violations: list[dict], total_steps: int) -> set[int]:
    out: set[int] = set()
    for v in violations:
        where = v.get("where") or ""
        for m in _STEP_RE.finditer(where):
            try:
                n = int(m.group(1))
            except ValueError:
                continue
            if 1 <= n <= total_steps:
                out.add(n)
    return out


class ToolDependencyMetric(BaseMetric):
    """LLM-as-judge tool path compliance audit.

    Loads a constraints catalog (JSON), extracts the actual tool-call sequence
    from Langfuse, asks the local claude CLI to find violations, scores by
    step-hit-rate (0-1, higher better).

    Parameters
    ----------
    threshold : float
        0-1 minimum score to pass. Default 0.8 = at most 20% of steps may
        violate.
    constraints_path : Path | None
        Override the default catalog. Defaults to
        ``data/tool_dependencies.json``.
    model : ClaudeCliJudge | None
        Judge for the LLM call. Defaults to a fresh ``ClaudeCliJudge()``.
    """

    def __init__(
        self,
        threshold: float = 0.8,
        *,
        constraints_path: Path | str | None = None,
        model: ClaudeCliJudge | None = None,
        verbose_mode: bool = False,
    ) -> None:
        self.threshold = threshold
        self.async_mode = True  # claude CLI judge allows 4 concurrent calls
        self.strict_mode = False
        self.verbose_mode = verbose_mode
        self.include_reason = True
        self.score: float | None = None
        self.reason: str | None = None
        self.success: bool | None = None
        self.error: str | None = None
        self.evaluation_cost = None

        self._constraints_path = Path(constraints_path) if constraints_path else _DEFAULT_CONSTRAINTS
        self._constraints: list[dict] = _load_constraints(self._constraints_path)
        self._model = model or ClaudeCliJudge()

    @property
    def __name__(self) -> str:  # noqa: D401
        return "ToolDependency"

    def is_successful(self) -> bool:
        if self.error is not None or self.score is None:
            return False
        self.success = self.score >= self.threshold
        return self.success

    def _bail(self, reason: str) -> float:
        self.score = 0.0
        self.reason = reason
        self.error = reason
        self.success = False
        return 0.0

    def measure(self, test_case: Any, *_: Any, **__: Any) -> float:
        conv_id = get_conv_id(test_case)
        if not conv_id:
            return self._bail("no conv_id on test_case.metadata")

        traces = fetch_session_traces(conv_id)
        if not traces:
            return self._bail(f"no Langfuse traces for session_id={conv_id}")

        gens = llm_generations(merge_observations(traces))
        tool_calls = _extract_tool_calls(gens)
        if not tool_calls:
            # Empty path → full score, skip judge.
            self.score = 1.0
            self.reason = json.dumps({
                "violations": [],
                "summary": "无工具调用，跳过依赖检查",
                "totalConstraints": len(self._constraints),
                "totalToolCalls": 0,
            }, ensure_ascii=False)
            self.success = True
            return 1.0

        question = getattr(test_case, "input", None) or ""
        prompt = _build_prompt(question, self._constraints, tool_calls)

        try:
            raw = self._model.generate(prompt)
        except Exception as e:  # noqa: BLE001 — surface judge failures cleanly
            return self._bail(f"claude CLI failed: {e!s}")

        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE).strip()
        try:
            judge: dict = json.loads(cleaned)
        except json.JSONDecodeError as e:
            return self._bail(f"judge returned non-JSON: {e!s}; raw[:200]={raw[:200]!r}")

        known = {c["id"]: c for c in self._constraints}
        raw_violations = judge.get("violations") if isinstance(judge.get("violations"), list) else []
        clean_violations: list[dict] = []
        for v in raw_violations:
            if not isinstance(v, dict):
                continue
            vid = v.get("id")
            if not isinstance(vid, str) or vid not in known:
                continue
            clean_violations.append({
                "id": vid,
                "severity": known[vid].get("severity", "soft"),
                "where": v.get("where") if isinstance(v.get("where"), str) else "?",
                "evidence": v.get("evidence") if isinstance(v.get("evidence"), str) else "",
            })

        total = len(tool_calls)
        violating_steps = _extract_violating_steps(clean_violations, total)
        safe = max(0, total - len(violating_steps))
        score = 1.0 if not clean_violations else (safe / total if total else 1.0)

        summary = judge.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            summary = (
                "通过" if not clean_violations
                else f"{len(clean_violations)} 处违反 ({len(violating_steps)}/{total} 步命中)"
            )

        self.score = round(score, 4)
        self.reason = json.dumps({
            "violations": clean_violations,
            "summary": summary.strip(),
            "totalConstraints": len(self._constraints),
            "totalToolCalls": total,
        }, ensure_ascii=False)
        self.is_successful()
        return self.score

    async def a_measure(self, test_case: Any, *args: Any, **kwargs: Any) -> float:
        # Offload the (still blocking) measure() to a thread so DeepEval's
        # asyncio.gather can actually parallelize across test cases.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.measure(test_case, *args, **kwargs))
