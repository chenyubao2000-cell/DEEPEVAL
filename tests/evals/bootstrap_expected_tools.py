"""Bootstrap _expected_tools per golden via one-shot LLM judge call.

Run once after adding new goldens or after major Mira tool-set changes:

    python tests/evals/bootstrap_expected_tools.py
    python tests/evals/bootstrap_expected_tools.py --force          # overwrite existing
    python tests/evals/bootstrap_expected_tools.py --index 10       # only golden 10

For each golden without an `_expected_tools` field (or all when --force), asks
the judge to propose an optimal 3-8 step tool-call sequence given the user
input + full Mira tool catalog (name + description only — schemas not
included; LLM makes educated guesses on input_parameters key names, which the
runtime ExpectedToolPathGEval judge tolerates via semantic equivalence).

Writes back to tests/evals/.dataset.json. **Review with `git diff` before
committing.** The bootstrap is best-effort; humans are the final arbiter on
what "optimal path" means for each scenario.

Cost: ~$0.10 / golden × N goldens. One-shot.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel, Field

# Make tests/evals + project root importable so _env / _langfuse_tools /
# claude_cli_judge resolve regardless of how this script is launched.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))

from _env import load_env  # noqa: E402
from _langfuse_tools import available_tool_registry  # noqa: E402
from claude_cli_judge import ClaudeCliJudge  # noqa: E402

DATASET_PATH = _ROOT / "tests" / "evals" / ".dataset.json"


class _Step(BaseModel):
    name: str = Field(..., description="Tool name as it appears in the catalog")
    input_parameters: dict = Field(default_factory=dict, description="Key fields driving this step")
    rationale: str = Field(default="", description="≤30字中文 — why this step (for human review)")


class _Plan(BaseModel):
    expected_tools: list[_Step]


def _build_prompt(user_input: str, catalog: dict[str, dict]) -> str:
    tool_list = "\n".join(
        f"- `{name}`: {(meta.get('description') or '').strip()[:240]}"
        for name, meta in sorted(catalog.items())
    )
    return f"""你是 Mira agent 工具路径规划专家。给定用户请求和可用工具清单，规划出**理想的最优工具调用序列**（3-8 步），用于后续作为"参考答案"对比 Mira 实际执行情况。

## 用户请求
{user_input}

## 可用工具（共 {len(catalog)} 个，name + description）
{tool_list}

## 规划原则
1. **步数 3-15**：覆盖核心功能但不冗余
2. **每一步给 name 和关键 input_parameters**：只列驱动这一步的 1-2 个关键字段（用语义合理的值，不需要给出所有可选字段，runtime judge 会容忍措辞差异）
3. **序列要完整**：通常以 `complete` 之类的收尾工具结尾
4. **不要选无关工具**：每一步都必须直接服务用户请求
5. **rationale 30 字以内中文**：解释为什么需要这一步，方便人工审

## 输出
严格只返回一个合法 JSON 对象，无 markdown 围栏，无解释性 prose。"""


def bootstrap_one(golden: dict, judge: ClaudeCliJudge, catalog: dict[str, dict]) -> list[dict]:
    user_inputs = golden.get("user_inputs") or []
    if isinstance(user_inputs, list):
        user_input = "\n\n".join(str(u) for u in user_inputs)
    else:
        user_input = str(user_inputs)
    if not user_input.strip():
        raise ValueError("empty user_inputs")
    prompt = _build_prompt(user_input, catalog)
    plan: _Plan = judge.generate(prompt, schema=_Plan)
    return [step.model_dump() for step in plan.expected_tools]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="Re-generate even if golden already has _expected_tools")
    ap.add_argument("--index", type=int, help="Only process this 0-based golden index")
    ap.add_argument("--dry-run", action="store_true", help="Print proposed plans, do not write dataset")
    args = ap.parse_args()

    load_env()
    judge = ClaudeCliJudge()
    catalog = available_tool_registry()
    if not catalog:
        print("[bootstrap] ERROR: tool catalog is empty — run report.py once first to populate .cache/tools-<env>.json")
        sys.exit(2)
    print(f"[bootstrap] env loaded, judge ready, catalog has {len(catalog)} tools")

    data = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    goldens = data["goldens"]

    targets = [
        (i, g) for i, g in enumerate(goldens)
        if (args.index is None or i == args.index)
        and (args.force or not g.get("_expected_tools"))
    ]
    if not targets:
        print(f"[bootstrap] nothing to do — {len(goldens)} goldens, all already populated (use --force to overwrite)")
        return

    print(f"[bootstrap] processing {len(targets)} / {len(goldens)} golden(s)")
    n_ok = n_fail = 0
    for i, g in targets:
        scenario = (g.get("scenario") or "(no scenario)").strip().splitlines()[0][:70]
        print(f"\n  [{i:>2}] {scenario}")
        try:
            steps = bootstrap_one(g, judge, catalog)
            g["_expected_tools"] = steps
            for s in steps:
                key_args = ", ".join(f"{k}={v!r}"[:40] for k, v in (s.get("input_parameters") or {}).items())
                print(f"        - {s['name']:<28} ({key_args})  // {s.get('rationale','')[:40]}")
            n_ok += 1
        except Exception as e:  # noqa: BLE001 — surface judge failures cleanly
            print(f"        ✗ FAILED: {e!s}")
            n_fail += 1

    print(f"\n[bootstrap] done: {n_ok} ok, {n_fail} failed")

    if args.dry_run:
        print("[bootstrap] --dry-run: dataset NOT modified")
        return

    DATASET_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    rel = DATASET_PATH.relative_to(_ROOT)
    print(f"[bootstrap] wrote {rel}")
    print(f"[bootstrap] REVIEW: git diff -- {rel}")
    print(f"[bootstrap] If wrong: edit by hand or re-run with --force --index <N>")


if __name__ == "__main__":
    main()
