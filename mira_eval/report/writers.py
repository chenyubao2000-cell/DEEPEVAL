"""JSON + Markdown report writers.

Pure I/O — receives ``GoldenResult`` instances and ``meta`` dict from
``evaluate.pipeline`` and writes to disk. All audit / verdict logic comes
from ``evaluate.audit``.

Splitting writer from pipeline lets:
  - replay.py reuse write_json / write_markdown without re-running the pipeline
  - tests of rendering not pull in the full evaluation chain (no judge / driver)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..evaluate.audit import METRIC_PROFILE, broken_metric_keys, verdict

if TYPE_CHECKING:
    from ..evaluate.pipeline import GoldenResult, MetricResult


# ─────────────────────────────────────────────────────────────────────────────
# Reader-facing metric explanations
# ─────────────────────────────────────────────────────────────────────────────
# What each metric is actually testing, in 1 sentence — surfaced in the
# report so non-engineer reviewers can read it without context.
_METRIC_EXPLANATIONS: dict[str, str] = {
    # ops — health + dependency
    "SessionHealthMetric":
        "这次会话是否干净跑完：client（SSE/工具错误）+ trace（Langfuse 落盘）"
        "+ persistence（Postgres 消息表）三层都通过才给 1.0，任何一层失败即 FAIL。",
    "ToolDependencyMetric":
        "工具调用顺序是否违反硬约束（例如 people_search 之后必须 complete 收尾、"
        "generate 之前必须先 search）。LLM-as-judge 评分。",
    # ops — informational (tokens / cost / latency)
    "TokensMetric":
        "整段会话 token 总用量（input + output）。仅记录，不计入 PASS 率。",
    "SessionCostMetric":
        "整段会话的 LLM 调用总成本（USD）。仅记录。",
    "TimeToFirstTokenMetric":
        "首个 token 的最短到达时间（秒）。仅记录，用于看延迟趋势。",
    "SessionDurationMetric":
        "整段 trace 的总耗时（秒）。仅记录。",
    # e2e — multi-turn quality
    "RoleAdherenceMetric":
        "助手回复是否始终保持 Mira 的专业 AI 代理角色，没有破人设或跑题。",
    "GoalAccuracyMetric":
        "助手的整体行为（计划 + 执行）是否真的完成了用户在 scenario 里要的目标。",
    # tooluse
    "ToolUseMetric":
        "针对任务，助手选用的工具集合是否合理（选对了工具吗）。",
    "ArgumentCorrectnessMetric":
        "每次工具调用的参数是否填对了（参数与输入需求是否匹配）。",
    # custom GEval rubric
    "GEval/DeliverableMatchesRequest":
        "用户在 scenario 里要的具体交付物（PPT/Excel/候选人名单/报告等）"
        "是否真的产出了，而不只是口头描述。",
    # noisy
    "TopicAdherenceMetric":
        "对话是否始终围绕给定的话题列表。Mira 场景下判官抖动大，列为 noisy。",
    "KnowledgeRetentionMetric":
        "助手是否记住了多轮上下文里的关键信息。单轮 golden 上恒为 0，列为 noisy。",
}

# 一句话的极简说明，用在「按指标汇总」表的「说明」列，保持列宽。
_METRIC_TAGLINES: dict[str, str] = {
    "SessionHealthMetric":             "会话是否干净跑完",
    "ToolDependencyMetric":            "工具调用顺序是否违规",
    "TokensMetric":                    "token 总用量",
    "SessionCostMetric":               "会话总成本（USD）",
    "TimeToFirstTokenMetric":          "首 token 延迟",
    "SessionDurationMetric":           "会话总耗时",
    "RoleAdherenceMetric":             "是否保持 Mira 角色",
    "GoalAccuracyMetric":              "是否完成用户目标",
    "ToolUseMetric":                   "工具选择是否合理",
    "ArgumentCorrectnessMetric":       "工具参数是否正确",
    "GEval/DeliverableMatchesRequest": "交付物是否真的产出",
    "TopicAdherenceMetric":            "是否扣住给定话题",
    "KnowledgeRetentionMetric":        "是否记住上下文",
}


def _explanation_for(metric_name: str) -> str:
    """Look up the long explanation for a metric label.

    Handles the "GEval/Rubric [Conversational GEval]" suffix that
    ConversationalGEval metrics carry in their display name.
    """
    key = metric_name.split(" [")[0]
    return _METRIC_EXPLANATIONS.get(key, "")


def _tagline_for(metric_name: str) -> str:
    key = metric_name.split(" [")[0]
    return _METRIC_TAGLINES.get(key, "—")


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_score(s: float | None) -> str:
    return f"{s:.2f}" if isinstance(s, (int, float)) else "—"


def _fmt_threshold(thr: float | None, informational: bool = False) -> str:
    """Render a threshold cell — empty for informational / inf / None.

    Informational metrics inherit a numeric default threshold from their
    subclass (e.g. TokensMetric defaults to 200_000), but the value is
    meaningless when the metric never gates — render as "—".
    """
    if informational:
        return "—"
    if thr is None:
        return "—"
    if isinstance(thr, float) and (thr == float("inf") or thr != thr):  # NaN check
        return "—"
    return f"≥{thr:.2f}"


def _task_url(base: str, conv_id: str) -> str:
    if not base or not conv_id:
        return ""
    return f"{base}/task/{conv_id}"


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation
# ─────────────────────────────────────────────────────────────────────────────
# Metric buckets whose `score` is NOT a 0-1 quality measure — these get a
# PASS/FAIL count but are excluded from the category-level `avg_score` rollup
# (mixing tokens=12345 with relevancy=0.87 would be nonsense).
_NON_QUALITY_BUCKETS: frozenset[str] = frozenset({"ops"})


def _aggregate_metric(results: list["GoldenResult"]) -> list[dict]:
    """Per-metric aggregate across all goldens."""
    by_metric: dict[str, dict] = {}
    for gr in results:
        for mr in gr.metric_results:
            key = f"{mr.file}/{mr.metric}"
            d = by_metric.setdefault(key, {
                "file": mr.file, "metric": mr.metric, "cls": mr.cls,
                "scores": [], "pass": 0, "fail": 0, "error": 0, "none": 0, "info": 0,
                "threshold": mr.threshold,
                "informational": mr.informational,
            })
            v = verdict(mr)
            d[v.lower()] = d.get(v.lower(), 0) + 1
            if mr.score is not None:
                d["scores"].append(mr.score)
    rows = []
    for d in by_metric.values():
        avg = (sum(d["scores"]) / len(d["scores"])) if d["scores"] else None
        rows.append({
            "file": d["file"], "metric": d["metric"], "cls": d["cls"],
            "avg_score": avg, "threshold": d["threshold"],
            "informational": d["informational"],
            "pass": d.get("pass", 0), "fail": d.get("fail", 0),
            "error": d.get("error", 0), "none": d.get("none", 0),
            "info": d.get("info", 0),
        })
    rows.sort(key=lambda r: (
        r["informational"],
        -(r["fail"] + r["error"]),
        r["file"],
        r["metric"],
    ))
    return rows


def _aggregate_category(results: list["GoldenResult"]) -> list[dict]:
    by_cat: dict[str, dict] = {}
    for gr in results:
        cat = gr.category or "(uncat)"
        d = by_cat.setdefault(cat, {
            "category": cat, "n_goldens": 0,
            "pass": 0, "fail": 0, "error": 0, "none": 0, "info": 0,
            "scores": [],
        })
        d["n_goldens"] += 1
        for mr in gr.metric_results:
            v = verdict(mr).lower()
            d[v] = d.get(v, 0) + 1
            if mr.score is not None and mr.file not in _NON_QUALITY_BUCKETS:
                d["scores"].append(mr.score)
    rows = []
    for d in by_cat.values():
        d["avg_score"] = (sum(d["scores"]) / len(d["scores"])) if d["scores"] else None
        d.pop("scores", None)
        rows.append(d)
    rows.sort(key=lambda r: r["category"])
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# JSON writer
# ─────────────────────────────────────────────────────────────────────────────

def write_json(results: list["GoldenResult"], out_path: Path, meta: dict) -> None:
    payload = {
        "meta": meta,
        "results": [
            {
                "index": gr.index,
                "scenario": gr.scenario,
                "tier": gr.tier,
                "category": gr.category,
                "mira_elapsed_s": round(gr.mira_elapsed_s, 1),
                "n_user_turns": gr.n_user_turns,
                "n_assistant_turns": gr.n_assistant_turns,
                "n_tool_calls": gr.n_tool_calls,
                "tools_observed": gr.tools_observed,
                "conv_id": gr.conv_id,
                "task_url": _task_url(meta.get("task_url_base", ""), gr.conv_id),
                "share_url": gr.share_url,
                "session_warnings": gr.session_warnings,
                "drive_error": gr.drive_error,
                "metrics": [
                    {
                        "file": mr.file,
                        "scope": mr.scope,
                        "metric": mr.metric,
                        "cls": mr.cls,
                        "profile": mr.profile,
                        "informational": mr.informational,
                        "score": (float(mr.score) if mr.score is not None else None),
                        "threshold": (
                            None
                            if mr.informational
                            or mr.threshold is None
                            or mr.threshold == float("inf")
                            else float(mr.threshold)
                        ),
                        "success": mr.success,
                        "verdict": verdict(mr),
                        "audit_warning": mr.audit_warning,
                        "reason": mr.reason,
                        "error": mr.error,
                        "elapsed_s": round(mr.elapsed_s, 1),
                        "n_cases": mr.n_cases,
                    }
                    for mr in gr.metric_results
                ],
            }
            for gr in results
        ],
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Markdown writer
# ─────────────────────────────────────────────────────────────────────────────

def write_markdown(results: list["GoldenResult"], out_path: Path, meta: dict) -> None:
    lines: list[str] = []
    add = lines.append

    # ── HEADER ──────────────────────────────────────────────────────────────
    add(f"# Mira 评测报告")
    add("")
    add(f"- 生成时间：`{meta['generated_at']}`")
    add(f"- 环境：`{meta['env']}`")
    add(f"- BFF：`{meta['bff_url']}`")
    add(f"- Langfuse：`{meta['langfuse_host']}`")
    add(f"- 工具清单：{meta['n_available_tools']} 个（来源：`{meta['tools_source']}`）")
    add(f"- 命令：`{meta['invocation']}`")
    add(f"- Goldens：{meta['n_goldens']} 条  ·  指标：{meta['n_metrics']} 项 / golden  ·  总评估次数：{meta['n_goldens'] * meta['n_metrics']}")
    add(f"- 总耗时：**{meta['total_elapsed_s']:.0f}s**（Mira 驱动 {meta['mira_total_s']:.0f}s + Judge 评分 {meta['judge_total_s']:.0f}s）")
    add("")

    # ── TOP-LEVEL VERDICT ──────────────────────────────────────────────────
    # PASS 率只统计 signal 类指标。noisy 仅作辅助参考。broken 已 skip。
    def _tally(predicate) -> dict:
        d = {"PASS": 0, "FAIL": 0, "ERROR": 0, "NONE": 0, "INCONCLUSIVE": 0, "INFO": 0}
        for gr in results:
            for mr in gr.metric_results:
                if not predicate(mr):
                    continue
                v = verdict(mr)
                d[v] = d.get(v, 0) + 1
        d["TOTAL"] = sum(d.values())
        denom = d["PASS"] + d["FAIL"]
        d["RATE"] = (d["PASS"] / denom * 100) if denom else 0.0
        return d

    signal_t = _tally(lambda mr: mr.profile == "signal")
    noisy_t  = _tally(lambda mr: mr.profile == "noisy")
    broken_metrics = broken_metric_keys()

    add(f"## 总评 — 仅信号指标（signal）")
    add("")
    add(f"> 信号指标 = 11 个可信、有判别力的 metric；这一行才是 Mira 真实表现的决策依据。")
    add(f"> Noisy / Broken 的分布看下面两节。`ℹ INFO` 是 `informational=True` 的 ops 指标（token/cost/latency），仅记录、不进 PASS 率分母。")
    add("")
    add(f"| 通过 ✓ | 失败 ✗ | 错误 ! | 缺失 · | 自相矛盾 ? | 仅记录 ℹ | 通过率（PASS / (PASS+FAIL)）|")
    add(f"|---:|---:|---:|---:|---:|---:|---:|")
    add(
        f"| **{signal_t['PASS']}** | **{signal_t['FAIL']}** | **{signal_t['ERROR']}** | "
        f"**{signal_t['NONE']}** | **{signal_t['INCONCLUSIVE']}** | **{signal_t['INFO']}** | "
        f"**{signal_t['RATE']:.1f}%** |"
    )
    add("")

    add(f"## 辅助参考 — noisy 指标（不进 PASS 率，仅供观察）")
    add("")
    if noisy_t["TOTAL"] == 0:
        add("（本次跑未产生 noisy 指标结果。）")
    else:
        add(f"| 通过 ✓ | 失败 ✗ | 错误 ! | 缺失 · | 自相矛盾 ? | 仅记录 ℹ |")
        add(f"|---:|---:|---:|---:|---:|---:|")
        add(
            f"| {noisy_t['PASS']} | {noisy_t['FAIL']} | {noisy_t['ERROR']} | "
            f"{noisy_t['NONE']} | {noisy_t['INCONCLUSIVE']} | {noisy_t['INFO']} |"
        )
        add("")
        add(f"> 这些 metric 在我们场景下判官抖动大或结构性假阳/假阴。不计入总评。")
    add("")

    add(f"## 全局 skip — broken 指标")
    add("")
    add(f"以下 metric 在 DeepEval 4.0 上对 Mira 场景被判定不可信，**所有 golden 一律跳过**：")
    add("")
    for k in broken_metrics:
        add(f"- `{k}` — METRIC_PROFILE 标记为 broken")
    add("")

    # ── METRIC EXPLANATIONS ────────────────────────────────────────────────
    seen_metrics: dict[str, dict] = {}
    for gr in results:
        for mr in gr.metric_results:
            seen_metrics.setdefault(
                mr.metric,
                {"file": mr.file, "informational": mr.informational},
            )
    if seen_metrics:
        add(f"## 指标说明")
        add("")
        add(f"> 下面每个指标分别在测什么 —— 看后面的 PASS/FAIL 时对照本节。"
            f"标 ℹ 的是 informational 指标，仅记录、不进 PASS 率。")
        add("")
        add("| 文件 | 指标 | 这个指标在测什么 |")
        add("|---|---|---|")
        for name in sorted(seen_metrics, key=lambda n: (seen_metrics[n]["file"], n)):
            info = seen_metrics[name]
            desc = _explanation_for(name) or "—"
            tag = " ℹ" if info["informational"] else ""
            add(f"| `{info['file']}` | `{name}`{tag} | {desc} |")
        add("")

    # ── PER-CATEGORY ROLLUP ────────────────────────────────────────────────
    cat_rows = _aggregate_category(results)
    add(f"## 按类别汇总")
    add("")
    add("| 类别 | Goldens | 平均分 | PASS | FAIL | ERROR | NONE | INFO |")
    add("|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in cat_rows:
        add(
            f"| `{r['category']}` | {r['n_goldens']} | {_fmt_score(r['avg_score'])} | "
            f"{r.get('pass',0)} | {r.get('fail',0)} | {r.get('error',0)} | "
            f"{r.get('none',0)} | {r.get('info',0)} |"
        )
    add("")

    # ── PER-GOLDEN ROLLUP ──────────────────────────────────────────────────
    add(f"## 按用例汇总")
    add("")
    add("| # | 类别 | Tier | 场景 | Mira 耗时 | 工具调用 | PASS / FAIL / ERR / INFO |")
    add("|---:|:---:|:---:|---|---:|---:|---|")
    for gr in results:
        p = sum(1 for mr in gr.metric_results if verdict(mr) == "PASS")
        f = sum(1 for mr in gr.metric_results if verdict(mr) == "FAIL")
        e = sum(1 for mr in gr.metric_results if verdict(mr) == "ERROR")
        i = sum(1 for mr in gr.metric_results if verdict(mr) == "INFO")
        cat = gr.category or "—"
        scenario_short = (gr.scenario or "")[:60]
        add(f"| {gr.index} | `{cat}` | {gr.tier or '—'} | {scenario_short} | {gr.mira_elapsed_s:.0f}s | {gr.n_tool_calls} | {p} / {f} / {e} / {i} |")
    add("")

    # ── PER-METRIC ROLLUP ──────────────────────────────────────────────────
    metric_rows = _aggregate_metric(results)
    add(f"## 按指标汇总")
    add("")
    add("| 文件 | 指标 | 说明 | 类型 | 平均值 | 阈值 | PASS | FAIL | ERROR | NONE | INFO |")
    add("|---|---|---|:---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in metric_rows:
        kind = "ℹ" if r["informational"] else "gate"
        thr = _fmt_threshold(r["threshold"], r["informational"])
        desc = _tagline_for(r["metric"])
        add(
            f"| `{r['file']}` | `{r['metric']}` | {desc} | {kind} | {_fmt_score(r['avg_score'])} | "
            f"{thr} | {r['pass']} | {r['fail']} | {r['error']} | {r['none']} | {r['info']} |"
        )
    add("")

    # ── DETAILED PER-GOLDEN BLOCKS ─────────────────────────────────────────
    add(f"## 每条用例详情")
    add("")
    url_base = meta.get("task_url_base") or ""
    for gr in results:
        add(f"### [{gr.index}] {gr.scenario}")
        add("")
        meta_bits = [
            f"`tier={gr.tier or '—'}`",
            f"`category={gr.category or '—'}`",
            f"`mira={gr.mira_elapsed_s:.1f}s`",
            f"`turns={gr.n_user_turns}U/{gr.n_assistant_turns}A`",
            f"`tools={gr.n_tool_calls}`",
            f"`conv={gr.conv_id}`",
        ]
        add(" · ".join(meta_bits))
        add("")
        share = (gr.share_url or "").strip()
        task = _task_url(url_base, gr.conv_id)
        if share:
            add(f"🔗 分享链接（公开访问）：<{share}>")
            add("")
        elif task:
            add(f"🔗 任务页面（需登录）：<{task}>")
            add("")
        url = share or task
        if gr.session_warnings:
            add(f"> ⚠ session warnings: `{gr.session_warnings}`")
            add("")
        if gr.tools_observed:
            from collections import Counter
            tc = Counter(gr.tools_observed)
            add("**工具调用：** " + ", ".join(f"`{k}`×{v}" for k, v in tc.most_common()))
            add("")
        if gr.drive_error:
            add(f"> 🛑 **Mira 驱动失败：** ```{gr.drive_error[:400]}```")
            add("")
            continue

        add("| 文件 | 指标 | profile | 分数 | 阈值 | 判定 | 耗时 |")
        add("|---|---|:---:|---:|---:|:---:|---:|")
        for mr in gr.metric_results:
            v = verdict(mr)
            badge = {
                "PASS": "✅ PASS", "FAIL": "❌ FAIL", "ERROR": "🚨 ERR",
                "NONE": "· NONE", "INCONCLUSIVE": "🟡 INCONCLUSIVE",
            }.get(v, v)
            thr = f"≥{mr.threshold:.2f}" if mr.threshold is not None else "—"
            score = _fmt_score(mr.score)
            prof_badge = {"signal": "🟢 signal", "noisy": "🟠 noisy"}.get(mr.profile, mr.profile)
            add(f"| `{mr.file}` | `{mr.metric}` | {prof_badge} | {score} | {thr} | {badge} | {mr.elapsed_s:.1f}s |")
        add("")

        # gate-class reasons (PASS items expanded too — without judge's explanation
        # readers can't see why a score is what it is). INFO has its own block.
        gating = [mr for mr in gr.metric_results if verdict(mr) != "INFO"]
        if gating:
            add("<details><summary>所有指标的 reason / error / audit（点击展开）</summary>")
            add("")
            if url:
                label = "分享链接（公开访问）" if share else "任务页面（需登录）"
                add(f"🔗 {label}：<{url}>")
                add("")
            for mr in gating:
                v = verdict(mr)
                add(f"- **`{mr.metric}` ({v}, profile={mr.profile})** — score={_fmt_score(mr.score)} thr={_fmt_score(mr.threshold)}")
                if mr.audit_warning:
                    add(f"  - 🟡 audit: {mr.audit_warning}")
                if mr.error:
                    add(f"  - 🛑 error: `{mr.error[:300]}`")
                if mr.reason:
                    reason = mr.reason.replace("\n", " ").strip()[:600]
                    add(f"  - 💬 reason: {reason}")
            add("")
            add("</details>")
            add("")

        info_rows = [mr for mr in gr.metric_results if verdict(mr) == "INFO"]
        if info_rows:
            add("<details><summary>ℹ 仅记录的指标（不参与判定）</summary>")
            add("")
            for mr in info_rows:
                score = _fmt_score(mr.score)
                bits = [f"- **`{mr.metric}`** = {score}"]
                if mr.reason:
                    bits.append(f"_{mr.reason.replace(chr(10), ' ').strip()[:300]}_")
                if mr.error:
                    bits.append(f"⚠ {mr.error[:200]}")
                add("  ".join(bits))
            add("")
            add("</details>")
            add("")

    # ── FOOTER ─────────────────────────────────────────────────────────────
    add("---")
    add("")
    add(f"完整 JSON 详情见 `{meta['json_path']}`。")

    out_path.write_text("\n".join(lines), encoding="utf-8")
