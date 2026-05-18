"""Custom DeepEval judge model that proxies to the local `claude` CLI.

Lets you run metrics like ConversationalGEval / TurnRelevancyMetric without
giving DeepEval a separate ANTHROPIC_API_KEY — it just shells out to the same
Claude Code session you're already authenticated with.

Trade-offs:
  - Each judge call spawns a subprocess (~3-5s round-trip), so eval suites run
    slower than a direct API client. Acceptable for small datasets.
  - The CLI returns plain text. For structured-output metrics we ask Claude
    to return JSON and parse it ourselves.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional, Type

from pydantic import BaseModel

from deepeval.models.base_model import DeepEvalBaseLLM


CLAUDE_BIN = shutil.which("claude") or "claude"
CLI_TIMEOUT_S = 240
MAX_RETRIES = 5
RETRY_BACKOFF_S = 2.0

# Cap concurrent `claude -p` at this many. This is a *hard* upper bound on
# in-flight CLI subprocesses regardless of how many callers exist —
# parallel goldens in report.py don't multiply this number, they queue
# against the same semaphore. Probed safe up to N=10 (no rc=1, no JSON
# corruption), but past ~6 the API rate-limits individual calls from ~6s
# to ~30s and wall-clock stops improving. N=4 is the sweet spot.
# Override with CLAUDE_JUDGE_CONCURRENCY env var.
# BoundedSemaphore for sync so an over-release would raise loudly; async
# semaphore is lazily created since it needs a running loop.
_CLI_CONCURRENCY = int(os.environ.get("CLAUDE_JUDGE_CONCURRENCY", "4"))
_CLI_SEM = threading.BoundedSemaphore(_CLI_CONCURRENCY)
_CLI_ASEMAPHORE: Optional[asyncio.Semaphore] = None  # lazily created per loop

DEBUG_DIR = Path(os.environ.get("CLAUDE_JUDGE_DEBUG_DIR", "/tmp/claude_judge_debug"))
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

# Global language directive appended to every judge prompt. The DeepEval
# templates are hardcoded English, so without this judges return English-only
# reason text — fine technically but unfriendly for our Chinese users. We
# append a single Chinese language preference instead of forking templates.
#
# Critical caveats:
#   - JSON keys (`score`, `verdict`, `reason`, `verdicts`, ...) MUST stay
#     unchanged English or DeepEval's schema parser breaks.
#   - JSON enum values (`"yes"` / `"no"` for verdicts) MUST stay English for
#     the same reason — DeepEval matches them with `verdict.strip().lower()`.
#   - Only the free-text `reason` value should be in Chinese.
#
# Override with JUDGE_LANG=en (or unset CLAUDE_JUDGE_REASON_LANG_INJECTION=0)
# if you ever need raw English reasons (e.g. comparing to upstream defaults).
_REASON_LANG = os.environ.get("JUDGE_LANG", "zh").lower()
_INJECT_LANG = os.environ.get("CLAUDE_JUDGE_REASON_LANG_INJECTION", "1") == "1"

_LANG_DIRECTIVE_ZH = (
    "\n\n---\n"
    "OUTPUT LANGUAGE: 请用简体中文撰写所有自由文本字段（特别是 `reason` 字段）。\n"
    "硬性约束：\n"
    "  - JSON 键名（如 score / verdict / reason / verdicts / statements）必须保持英文，原样不变；\n"
    "  - JSON 枚举值（如 verdict 的 'yes'/'no'、'PASS'/'FAIL'）必须保持英文，原样不变；\n"
    "  - 只把 `reason` / 解释性 string value 翻译成简体中文；\n"
    "  - 不输出英文 reason，不输出多语言混杂的 reason；\n"
    "  - 仍然只返回一个合法 JSON 对象，无 prose、无 markdown 代码围栏。\n"
)


def _maybe_inject_lang(prompt: str) -> str:
    if _INJECT_LANG and _REASON_LANG.startswith("zh"):
        return prompt + _LANG_DIRECTIVE_ZH
    return prompt


def _build_schema_prompt(prompt: str, schema: Type[BaseModel]) -> str:
    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    base = (
        f"{prompt}\n\n"
        "---\n"
        "Respond with ONLY a single JSON object that conforms to this JSON Schema. "
        "No prose, no markdown fences, no explanation outside the JSON.\n\n"
        f"JSON Schema:\n{schema_json}\n"
    )
    return _maybe_inject_lang(base)


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _dump_debug(prompt: str, stdout: str, stderr: str, err: str, attempt: int) -> None:
    fname = DEBUG_DIR / f"fail-{int(time.time())}-{uuid.uuid4().hex[:6]}-att{attempt}.log"
    try:
        fname.write_text(
            f"=== ERROR ===\n{err}\n\n"
            f"=== STDOUT ({len(stdout)} chars) ===\n{stdout}\n\n"
            f"=== STDERR ({len(stderr)} chars) ===\n{stderr}\n\n"
            f"=== PROMPT ({len(prompt)} chars) ===\n{prompt}\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def _extract_json(text: str) -> str:
    text = _FENCE_RE.sub("", text).strip()
    # Best-effort: find the first {...} block if Claude prepended/appended prose.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


# ── token-audit hook ────────────────────────────────────────────────────────
# When CLAUDE_JUDGE_LOG_PATH is set, every CLI call is routed through
# `claude -p --output-format json` and the resulting usage block is appended
# (as one JSONL row) to that path. Each row is attributed to the calling
# metric via stack walk (looks for the nearest frame whose `self` has the
# DeepEval BaseMetric duck-type — `.threshold` + `.score`).
def _audit_log_path() -> Optional[str]:
    return os.environ.get("CLAUDE_JUDGE_LOG_PATH") or None


def _audit_argv() -> list[str]:
    if _audit_log_path():
        return [CLAUDE_BIN, "-p", "--output-format", "json"]
    return [CLAUDE_BIN, "-p"]


def _find_calling_metric() -> str:
    frame = inspect.currentframe()
    try:
        depth = 0
        while frame is not None and depth < 30:
            obj = frame.f_locals.get("self")
            if obj is not None:
                cls_name = type(obj).__name__
                if cls_name != "ClaudeCliJudge" and hasattr(obj, "threshold") and hasattr(obj, "score"):
                    name = getattr(obj, "name", None)
                    if cls_name == "ConversationalGEval" and name:
                        return f"GEval/{name}"
                    return cls_name
            frame = frame.f_back
            depth += 1
    finally:
        del frame
    return "?"


def _audit_unwrap(stdout: str, prompt: str, attempt: int, schema_mode: bool) -> str:
    """In audit mode: parse JSON output, log usage, return the model response text.
    Outside audit mode: return stdout unchanged."""
    path = _audit_log_path()
    if not path:
        return stdout
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    text = parsed.get("result", "") or ""
    usage = parsed.get("usage") or {}
    record = {
        "ts": time.time(),
        "metric": _find_calling_metric(),
        "attempt": attempt,
        "schema_mode": schema_mode,
        "prompt_chars": len(prompt),
        "prompt_bytes": len(prompt.encode("utf-8")),
        "response_chars": len(text),
        "response_bytes": len(text.encode("utf-8")),
        "duration_ms": parsed.get("duration_ms"),
        "duration_api_ms": parsed.get("duration_api_ms"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "total_cost_usd": parsed.get("total_cost_usd"),
    }
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return text


class ClaudeCliJudge(DeepEvalBaseLLM):
    def __init__(self, model_label: str = "claude-code-cli"):
        self._label = model_label
        super().__init__(model=model_label)

    def load_model(self):
        return CLAUDE_BIN

    def get_model_name(self) -> str:
        return self._label

    # ---- sync ----------------------------------------------------------------
    def generate(self, prompt: str, schema: Optional[Type[BaseModel]] = None, **_: Any):
        full_prompt = _build_schema_prompt(prompt, schema) if schema else _maybe_inject_lang(prompt)
        last_err: Optional[str] = None
        last_stdout = ""
        for attempt in range(1, MAX_RETRIES + 1):
            with _CLI_SEM:
                # Pipe prompt via stdin, not argv. Tool-rich prompts (78 mira
                # tools × ~1KB description each) easily exceed Windows's ~32KB
                # CreateProcess command-line limit otherwise.
                result = subprocess.run(
                    _audit_argv(),
                    input=full_prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=CLI_TIMEOUT_S,
                    check=False,
                )
            last_stdout = result.stdout
            if result.returncode == 0:
                out = _audit_unwrap(result.stdout, full_prompt, attempt, schema is not None).strip()
                if schema is None:
                    return out
                try:
                    return schema.model_validate_json(_extract_json(out))
                except Exception as parse_err:
                    last_err = f"schema parse error: {parse_err}; raw: {out[:300]}"
                    _dump_debug(full_prompt, result.stdout, result.stderr, last_err, attempt)
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_BACKOFF_S)
                        continue
                    raise RuntimeError(last_err)
            last_err = f"claude CLI rc={result.returncode}; stderr={result.stderr.strip()[:500]!r}; stdout={result.stdout.strip()[:300]!r}"
            _dump_debug(full_prompt, result.stdout, result.stderr, last_err, attempt)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_S)
                continue
            raise RuntimeError(last_err)
        raise RuntimeError(last_err or "claude CLI: unknown failure")

    # ---- async ---------------------------------------------------------------
    async def a_generate(self, prompt: str, schema: Optional[Type[BaseModel]] = None, **_: Any):
        full_prompt = _build_schema_prompt(prompt, schema) if schema else _maybe_inject_lang(prompt)
        global _CLI_ASEMAPHORE
        if _CLI_ASEMAPHORE is None:
            _CLI_ASEMAPHORE = asyncio.Semaphore(_CLI_CONCURRENCY)
        last_err: Optional[str] = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with _CLI_ASEMAPHORE:
                # See sync path: pipe prompt via stdin to avoid Windows argv
                # length limit when tool descriptions inflate the prompt.
                proc = await asyncio.create_subprocess_exec(
                    *_audit_argv(),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(input=full_prompt.encode("utf-8")),
                        timeout=CLI_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    raise
            if proc.returncode == 0:
                out = _audit_unwrap(stdout.decode("utf-8", errors="replace"), full_prompt, attempt, schema is not None).strip()
                if schema is None:
                    return out
                try:
                    return schema.model_validate_json(_extract_json(out))
                except Exception as parse_err:
                    last_err = f"schema parse error: {parse_err}; raw: {out[:300]}"
                    _dump_debug(full_prompt, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace"), last_err, attempt)
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(RETRY_BACKOFF_S)
                        continue
                    raise RuntimeError(last_err)
            last_err = (
                f"claude CLI rc={proc.returncode}; "
                f"stderr={stderr.decode("utf-8", errors="replace").strip()[:500]!r}; "
                f"stdout={stdout.decode("utf-8", errors="replace").strip()[:300]!r}"
            )
            _dump_debug(full_prompt, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace"), last_err, attempt)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF_S)
                continue
            raise RuntimeError(last_err)
        raise RuntimeError(last_err or "claude CLI: unknown failure")

    # ---- capability hints ----------------------------------------------------
    def supports_structured_outputs(self) -> bool:
        return True

    def supports_json_mode(self) -> bool:
        return True


if __name__ == "__main__":
    judge = ClaudeCliJudge()
    print("plain:", judge.generate("Reply with exactly: OK"))

    class Score(BaseModel):
        score: float
        reason: str

    print("schema:", judge.generate(
        "Rate the helpfulness of this answer on a 0-1 scale: 'The capital of France is Paris.'",
        schema=Score,
    ))
