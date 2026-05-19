"""MySQL adapter — persist evaluation results, load runs back for re-rendering,
and run cross-run analytics queries.

Lifecycle:
  - ``pipeline.run()`` calls ``persist_run(engine, results, meta)`` at the end
    of a successful evaluation (only when ``MIRA_RESULTS_DB_URL`` is set).
  - Failures here are non-fatal — local JSON/Markdown still gets written so
    the eval run never depends on the DB being reachable.
  - ``mira-eval-report --run-uuid ...`` calls ``load_run(...)`` and pipes the
    reconstructed results back into ``report.writers.write_*`` for a faithful
    re-render that's byte-equal to the original (modulo timestamps).
  - Cross-run analytics live in ``query_*`` functions consumed by
    ``cli/trend.py`` etc.

Schema (3 tables, idempotently created by ``init_schema``):
  - ``eval_runs``          — one row per pipeline.run() invocation
  - ``eval_goldens``       — one row per golden inside a run
  - ``eval_metric_results``— one row per (golden × metric) measurement

Connection URL:
  ``mysql+pymysql://<user>:<urlencoded_pw>@<host>:<port>/mira_eval?charset=utf8mb4``
  We accept the legacy ``mysql://`` prefix and auto-rewrite it to
  ``mysql+pymysql://`` so users don't have to know which driver is wired in.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Table,
    Text,
    create_engine,
    desc,
    func,
    select,
    text,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.engine import Engine

# ─────────────────────────────────────────────────────────────────────────────
# Schema definition (SQLAlchemy Core, no ORM)
# ─────────────────────────────────────────────────────────────────────────────
metadata_obj = MetaData()


eval_runs = Table(
    "eval_runs",
    metadata_obj,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_uuid", String(36), nullable=False, unique=True),
    Column("env", String(32), nullable=False, index=True),
    Column("bff_url", String(255), nullable=False),
    Column("langfuse_host", String(255), nullable=False),
    Column("n_available_tools", SmallInteger, nullable=False),
    Column("tools_source", String(255)),
    Column("invocation", Text),
    Column("n_goldens", SmallInteger, nullable=False),
    Column("n_metrics", SmallInteger, nullable=False),
    Column("total_elapsed_s", Numeric(8, 2)),
    Column("mira_total_s", Numeric(8, 2)),
    Column("judge_total_s", Numeric(8, 2)),
    Column("generated_at", DateTime, nullable=False, index=True),
    Column("finished_at", DateTime),
    Column("json_path", String(255)),
    Column("md_path", String(255)),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
)


eval_goldens = Table(
    "eval_goldens",
    metadata_obj,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", BigInteger, ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("golden_index", SmallInteger, nullable=False),
    Column("scenario", String(255), nullable=False),
    Column("tier", String(16)),
    Column("category", String(32), index=True),
    Column("mira_elapsed_s", Numeric(8, 2)),
    Column("n_user_turns", SmallInteger),
    Column("n_assistant_turns", SmallInteger),
    Column("n_tool_calls", SmallInteger),
    Column("tools_observed", JSON),
    Column("conv_id", String(64), index=True),
    Column("task_url", String(500)),
    Column("share_url", String(500)),
    Column("session_warnings", JSON),
    Column("drive_error", Text),
    Column("created_at", DateTime, nullable=False),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
)


eval_metric_results = Table(
    "eval_metric_results",
    metadata_obj,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    # run_id is denormalized (also FK-derivable via golden_id → run_id) so
    # cross-run analytics queries don't need to JOIN through eval_goldens.
    Column("run_id", BigInteger, ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("golden_id", BigInteger, ForeignKey("eval_goldens.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("file_label", String(16), nullable=False),
    Column("scope", Enum("multi", "single", name="scope_enum"), nullable=False),
    Column("metric_name", String(128), nullable=False, index=True),
    Column("cls", String(64)),
    Column("profile", Enum("signal", "noisy", "broken", name="profile_enum")),
    Column("informational", Boolean, nullable=False, default=False),
    Column("score", Numeric(10, 4)),
    Column("threshold", Numeric(10, 4)),
    Column("success", Boolean),
    Column(
        "verdict",
        Enum("PASS", "FAIL", "ERROR", "NONE", "INCONCLUSIVE", "INFO", name="verdict_enum"),
        nullable=False,
        index=True,
    ),
    Column("audit_warning", Text),
    Column("reason", MEDIUMTEXT),
    Column("error", Text),
    Column("elapsed_s", Numeric(8, 2)),
    Column("n_cases", SmallInteger),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
)


# ─────────────────────────────────────────────────────────────────────────────
# Engine + schema helpers
# ─────────────────────────────────────────────────────────────────────────────

def _normalise_url(url: str) -> str:
    """Accept either ``mysql://`` or ``mysql+pymysql://``; we always need the
    explicit driver suffix for SQLAlchemy."""
    if url.startswith("mysql://"):
        return "mysql+pymysql://" + url[len("mysql://") :]
    return url


def get_engine(url: Optional[str] = None) -> Engine:
    """Return a SQLAlchemy engine for the eval results DB.

    Picks ``url`` arg first, falls back to ``MIRA_RESULTS_DB_URL`` env var.
    Raises ``RuntimeError`` if neither is set.
    """
    url = url or os.environ.get("MIRA_RESULTS_DB_URL")
    if not url:
        raise RuntimeError(
            "no DB URL: pass url= or set MIRA_RESULTS_DB_URL "
            "(e.g. mysql+pymysql://user:pw@host:3306/mira_eval?charset=utf8mb4)"
        )
    return create_engine(
        _normalise_url(url),
        pool_pre_ping=True,
        pool_recycle=3600,
        future=True,
    )


def init_schema(engine: Engine) -> None:
    """Idempotently create the 3 tables. Safe to call on every run."""
    metadata_obj.create_all(engine, checkfirst=True)


# ─────────────────────────────────────────────────────────────────────────────
# Write — persist a completed run
# ─────────────────────────────────────────────────────────────────────────────

def _verdict_for(mr) -> str:
    """Resolve a MetricResult's verdict. Inline copy of evaluate.audit.verdict
    semantics to avoid an import cycle (results_db is in persistence/, called
    from pipeline at end of run)."""
    if mr.informational:
        return "INFO"
    if mr.error:
        return "ERROR"
    if mr.audit_warning:
        return "INCONCLUSIVE"
    if mr.success is True:
        return "PASS"
    if mr.success is False:
        return "FAIL"
    if mr.score is None or mr.threshold is None:
        return "NONE"
    return "PASS" if float(mr.score) >= float(mr.threshold) else "FAIL"


def _to_decimal_or_none(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # MySQL DECIMAL won't store ±inf or NaN; treat them as "no threshold".
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def persist_run(engine: Engine, results: list, meta: dict) -> str:
    """Insert one eval run with its goldens + metric results.

    ``results`` is the list of ``pipeline.GoldenResult`` produced by run().
    ``meta`` is the dict passed to ``writers.write_json/markdown``.

    Returns the generated ``run_uuid``. Wraps everything in one transaction —
    either the full run lands or nothing does (no half-written DB state).
    """
    run_uuid = str(uuid.uuid4())
    generated_at = datetime.fromisoformat(meta["generated_at"])

    with engine.begin() as conn:
        run_pk = conn.execute(
            eval_runs.insert().values(
                run_uuid=run_uuid,
                env=meta["env"],
                bff_url=meta.get("bff_url", ""),
                langfuse_host=meta.get("langfuse_host", ""),
                n_available_tools=int(meta.get("n_available_tools", 0)),
                tools_source=meta.get("tools_source"),
                invocation=meta.get("invocation"),
                n_goldens=int(meta.get("n_goldens", len(results))),
                n_metrics=int(meta.get("n_metrics", 0)),
                total_elapsed_s=_to_decimal_or_none(meta.get("total_elapsed_s")),
                mira_total_s=_to_decimal_or_none(meta.get("mira_total_s")),
                judge_total_s=_to_decimal_or_none(meta.get("judge_total_s")),
                generated_at=generated_at,
                finished_at=datetime.now(),
                json_path=meta.get("json_path"),
                md_path=meta.get("md_path"),
            )
        ).inserted_primary_key[0]

        for gr in results:
            golden_pk = conn.execute(
                eval_goldens.insert().values(
                    run_id=run_pk,
                    golden_index=gr.index,
                    scenario=gr.scenario,
                    tier=gr.tier,
                    category=gr.category,
                    mira_elapsed_s=_to_decimal_or_none(gr.mira_elapsed_s),
                    n_user_turns=gr.n_user_turns,
                    n_assistant_turns=gr.n_assistant_turns,
                    n_tool_calls=gr.n_tool_calls,
                    tools_observed=list(gr.tools_observed) if gr.tools_observed else [],
                    conv_id=gr.conv_id,
                    task_url=meta.get("task_url_base", "") + f"/task/{gr.conv_id}" if gr.conv_id else None,
                    share_url=gr.share_url,
                    session_warnings=list(gr.session_warnings) if gr.session_warnings else [],
                    drive_error=gr.drive_error,
                    created_at=datetime.now(),
                )
            ).inserted_primary_key[0]

            if not gr.metric_results:
                continue

            conn.execute(
                eval_metric_results.insert(),
                [
                    {
                        "run_id": run_pk,
                        "golden_id": golden_pk,
                        "file_label": mr.file,
                        "scope": mr.scope,
                        "metric_name": mr.metric,
                        "cls": mr.cls,
                        "profile": mr.profile,
                        "informational": bool(mr.informational),
                        "score": _to_decimal_or_none(mr.score),
                        "threshold": _to_decimal_or_none(mr.threshold),
                        "success": mr.success,
                        "verdict": _verdict_for(mr),
                        "audit_warning": mr.audit_warning,
                        "reason": mr.reason,
                        "error": mr.error,
                        "elapsed_s": _to_decimal_or_none(mr.elapsed_s),
                        "n_cases": mr.n_cases,
                    }
                    for mr in gr.metric_results
                ],
            )

    return run_uuid


# ─────────────────────────────────────────────────────────────────────────────
# Read — reconstruct GoldenResult/MetricResult instances from a run_uuid so
# ``report.writers`` can re-render any historical run from the DB.
# ─────────────────────────────────────────────────────────────────────────────

def load_run(engine: Engine, run_uuid: str) -> tuple[list, dict]:
    """Pull one run out of the DB and rebuild (results, meta) tuples that
    match what ``pipeline.run()`` originally passed to writers. Output goes
    straight to ``writers.write_json/markdown`` for faithful re-rendering.

    Raises ``LookupError`` if ``run_uuid`` is not found.
    """
    # Local imports defer pipeline import until actually needed (results_db
    # itself should stay importable without pulling in deepeval).
    from ..evaluate.pipeline import GoldenResult, MetricResult

    with engine.connect() as conn:
        run_row = conn.execute(
            select(eval_runs).where(eval_runs.c.run_uuid == run_uuid)
        ).mappings().first()
        if run_row is None:
            raise LookupError(f"run_uuid not found: {run_uuid}")

        run_pk = run_row["id"]

        meta = {
            "generated_at": run_row["generated_at"].isoformat(timespec="seconds"),
            "env": run_row["env"],
            "bff_url": run_row["bff_url"],
            "langfuse_host": run_row["langfuse_host"],
            "n_available_tools": run_row["n_available_tools"],
            "tools_source": run_row["tools_source"],
            "invocation": run_row["invocation"],
            "n_goldens": run_row["n_goldens"],
            "n_metrics": run_row["n_metrics"],
            "total_elapsed_s": float(run_row["total_elapsed_s"] or 0),
            "mira_total_s": float(run_row["mira_total_s"] or 0),
            "judge_total_s": float(run_row["judge_total_s"] or 0),
            "json_path": run_row["json_path"],
            "task_url_base": run_row["bff_url"],  # best-effort reconstruction
        }

        golden_rows = conn.execute(
            select(eval_goldens)
            .where(eval_goldens.c.run_id == run_pk)
            .order_by(eval_goldens.c.golden_index)
        ).mappings().all()

        metric_rows_by_golden: dict[int, list] = {}
        if golden_rows:
            golden_ids = [g["id"] for g in golden_rows]
            metric_rows = conn.execute(
                select(eval_metric_results)
                .where(eval_metric_results.c.golden_id.in_(golden_ids))
                .order_by(eval_metric_results.c.id)
            ).mappings().all()
            for m in metric_rows:
                metric_rows_by_golden.setdefault(m["golden_id"], []).append(m)

    results: list = []
    for g in golden_rows:
        gr = GoldenResult(
            index=g["golden_index"],
            scenario=g["scenario"],
            tier=g["tier"],
            category=g["category"],
            mira_elapsed_s=float(g["mira_elapsed_s"] or 0),
            n_user_turns=g["n_user_turns"] or 0,
            n_assistant_turns=g["n_assistant_turns"] or 0,
            n_tool_calls=g["n_tool_calls"] or 0,
            tools_observed=list(g["tools_observed"] or []),
            conv_id=g["conv_id"] or "",
            session_warnings=list(g["session_warnings"] or []),
            drive_error=g["drive_error"],
            share_url=g["share_url"],
        )
        for m in metric_rows_by_golden.get(g["id"], []):
            gr.metric_results.append(
                MetricResult(
                    file=m["file_label"],
                    scope=m["scope"],
                    metric=m["metric_name"],
                    cls=m["cls"] or "",
                    score=float(m["score"]) if m["score"] is not None else None,
                    threshold=float(m["threshold"]) if m["threshold"] is not None else None,
                    success=m["success"],
                    reason=m["reason"],
                    error=m["error"],
                    elapsed_s=float(m["elapsed_s"] or 0),
                    n_cases=m["n_cases"] or 1,
                    informational=bool(m["informational"]),
                    profile=m["profile"] or "signal",
                    audit_warning=m["audit_warning"],
                )
            )
        results.append(gr)

    return results, meta


def list_recent_runs(
    engine: Engine,
    env: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """Return summary rows for the most recent runs (newest first).

    Each row carries run_uuid, env, generated_at, n_goldens, plus pre-computed
    PASS / FAIL / ERROR / INFO counts (one extra aggregate query, cheap).
    """
    with engine.connect() as conn:
        stmt = select(eval_runs).order_by(desc(eval_runs.c.generated_at)).limit(limit)
        if env:
            stmt = stmt.where(eval_runs.c.env == env)
        runs = conn.execute(stmt).mappings().all()

        if not runs:
            return []

        # Aggregate verdict counts per run in one query.
        run_ids = [r["id"] for r in runs]
        agg_rows = conn.execute(
            select(
                eval_metric_results.c.run_id,
                eval_metric_results.c.verdict,
                func.count().label("n"),
            )
            .where(eval_metric_results.c.run_id.in_(run_ids))
            .group_by(eval_metric_results.c.run_id, eval_metric_results.c.verdict)
        ).all()

    by_run: dict[int, dict[str, int]] = {}
    for run_id, v, n in agg_rows:
        by_run.setdefault(run_id, {})[v] = n

    out = []
    for r in runs:
        counts = by_run.get(r["id"], {})
        n_pass = counts.get("PASS", 0)
        n_fail = counts.get("FAIL", 0)
        denom = n_pass + n_fail
        out.append({
            "run_uuid": r["run_uuid"],
            "env": r["env"],
            "generated_at": r["generated_at"],
            "n_goldens": r["n_goldens"],
            "n_metrics": r["n_metrics"],
            "total_elapsed_s": float(r["total_elapsed_s"] or 0),
            "invocation": r["invocation"],
            "pass": n_pass,
            "fail": n_fail,
            "error": counts.get("ERROR", 0),
            "none": counts.get("NONE", 0),
            "inconclusive": counts.get("INCONCLUSIVE", 0),
            "info": counts.get("INFO", 0),
            "pass_rate": (n_pass / denom) if denom else None,
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Analytics — cross-run queries
# ─────────────────────────────────────────────────────────────────────────────

def query_metric_trend(
    engine: Engine,
    metric_name: str,
    env: Optional[str] = None,
    days: int = 30,
) -> list[dict]:
    """Per-run average score + pass rate for one metric over the last N days.

    Returns rows ordered oldest → newest so callers can plot a trend line.
    """
    with engine.connect() as conn:
        stmt = (
            select(
                eval_runs.c.run_uuid,
                eval_runs.c.env,
                eval_runs.c.generated_at,
                func.avg(eval_metric_results.c.score).label("avg_score"),
                func.sum(func.if_(eval_metric_results.c.verdict == "PASS", 1, 0)).label("n_pass"),
                func.sum(func.if_(eval_metric_results.c.verdict == "FAIL", 1, 0)).label("n_fail"),
                func.count().label("n_total"),
            )
            .join(eval_runs, eval_runs.c.id == eval_metric_results.c.run_id)
            .where(eval_metric_results.c.metric_name == metric_name)
            .where(eval_runs.c.generated_at >= func.date_sub(func.now(), text(f"INTERVAL {int(days)} DAY")))
            .group_by(eval_runs.c.id)
            .order_by(eval_runs.c.generated_at)
        )
        if env:
            stmt = stmt.where(eval_runs.c.env == env)
        rows = conn.execute(stmt).mappings().all()

    out = []
    for r in rows:
        n_pass = int(r["n_pass"] or 0)
        n_fail = int(r["n_fail"] or 0)
        out.append({
            "run_uuid": r["run_uuid"],
            "env": r["env"],
            "generated_at": r["generated_at"],
            "avg_score": float(r["avg_score"]) if r["avg_score"] is not None else None,
            "n_pass": n_pass,
            "n_fail": n_fail,
            "n_total": int(r["n_total"]),
            "pass_rate": (n_pass / (n_pass + n_fail)) if (n_pass + n_fail) else None,
        })
    return out


def query_regression(
    engine: Engine,
    run_uuid_a: str,
    run_uuid_b: str,
) -> dict:
    """Diff two runs at the (golden, metric) level.

    Pairs up rows by ``(scenario, metric_name)`` so it works across dataset
    re-orderings. Returns:

      - regressed:  rows where A passed but B failed
      - improved:   rows where A failed but B passed
      - score_drop: rows where both passed but B.score < A.score - 0.10
      - score_gain: mirror of score_drop
      - only_in_a / only_in_b: pairs missing from one side
    """
    def _flatten(run_uuid: str) -> dict[tuple[str, str], dict]:
        results, _ = load_run(engine, run_uuid)
        out: dict[tuple[str, str], dict] = {}
        for gr in results:
            for mr in gr.metric_results:
                out[(gr.scenario, mr.metric)] = {
                    "scenario": gr.scenario,
                    "metric": mr.metric,
                    "score": mr.score,
                    "verdict": _verdict_for(mr),
                    "reason": (mr.reason or "")[:300],
                }
        return out

    a = _flatten(run_uuid_a)
    b = _flatten(run_uuid_b)
    all_keys = set(a) | set(b)

    regressed, improved, score_drop, score_gain, only_in_a, only_in_b = [], [], [], [], [], []
    for k in all_keys:
        ra, rb = a.get(k), b.get(k)
        if ra is None:
            only_in_b.append(rb)
            continue
        if rb is None:
            only_in_a.append(ra)
            continue
        if ra["verdict"] == "PASS" and rb["verdict"] == "FAIL":
            regressed.append({"scenario": k[0], "metric": k[1], "a": ra, "b": rb})
        elif ra["verdict"] == "FAIL" and rb["verdict"] == "PASS":
            improved.append({"scenario": k[0], "metric": k[1], "a": ra, "b": rb})
        elif (
            ra["score"] is not None and rb["score"] is not None
            and ra["verdict"] == "PASS" and rb["verdict"] == "PASS"
        ):
            delta = rb["score"] - ra["score"]
            if delta <= -0.10:
                score_drop.append({"scenario": k[0], "metric": k[1], "delta": round(delta, 3), "a": ra, "b": rb})
            elif delta >= 0.10:
                score_gain.append({"scenario": k[0], "metric": k[1], "delta": round(delta, 3), "a": ra, "b": rb})

    return {
        "run_a": run_uuid_a,
        "run_b": run_uuid_b,
        "regressed": regressed,
        "improved": improved,
        "score_drop": sorted(score_drop, key=lambda r: r["delta"]),
        "score_gain": sorted(score_gain, key=lambda r: -r["delta"]),
        "only_in_a": only_in_a,
        "only_in_b": only_in_b,
    }


def find_latest_run(engine: Engine, env: Optional[str] = None) -> Optional[str]:
    """Return the run_uuid of the most recent run (optionally filtered by env)."""
    with engine.connect() as conn:
        stmt = select(eval_runs.c.run_uuid).order_by(desc(eval_runs.c.generated_at)).limit(1)
        if env:
            stmt = stmt.where(eval_runs.c.env == env)
        return conn.execute(stmt).scalar()
