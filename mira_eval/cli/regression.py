"""`mira-eval-regression` — diff two DB-stored runs at the (golden, metric) level.

Surfaces:
  - Items that flipped PASS → FAIL (regressions — the headline question)
  - Items that flipped FAIL → PASS (improvements)
  - Items that stayed PASS but dropped ≥ 0.10 in score (silent drift)
  - Items that stayed PASS and gained ≥ 0.10 (silent wins)
  - Items missing from either side (dataset additions/removals)

Example:
    # Compare the latest preview run to the one before it
    mira-eval-regression --env preview --against-prev

    # Explicit pair
    mira-eval-regression --a <uuid_a> --b <uuid_b>
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import desc, select

from ..config import load_env
from ..persistence import results_db


def _pick_two(engine, env, args) -> tuple[str, str]:
    if args.a and args.b:
        return args.a, args.b

    if args.against_prev:
        # Two most recent runs for env (newest = b, prev = a).
        with engine.connect() as conn:
            stmt = (
                select(results_db.eval_runs.c.run_uuid)
                .order_by(desc(results_db.eval_runs.c.generated_at))
                .limit(2)
            )
            if env:
                stmt = stmt.where(results_db.eval_runs.c.env == env)
            uuids = [r[0] for r in conn.execute(stmt).all()]
        if len(uuids) < 2:
            print("ERROR: need at least 2 runs to diff"
                  + (f" for env={env}" if env else ""), file=sys.stderr)
            sys.exit(2)
        return uuids[1], uuids[0]   # older first, newer second

    print("ERROR: pass --a and --b, OR --against-prev (with optional --env)", file=sys.stderr)
    sys.exit(2)


def _short(uuid: str) -> str:
    return uuid[:8]


def _fmt_score(s):
    return f"{s:.2f}" if isinstance(s, (int, float)) else "—"


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="mira-eval-regression",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
    )
    ap.add_argument("--a", help="older / baseline run_uuid")
    ap.add_argument("--b", help="newer / current run_uuid")
    ap.add_argument("--against-prev", action="store_true",
                    help="auto-pick the two most recent runs (combine with --env)")
    ap.add_argument("--env", help="filter when using --against-prev")
    ap.add_argument("--db-url", default=None)
    ap.add_argument("--load-env", default=None)
    args = ap.parse_args()

    if args.load_env:
        load_env(args.load_env)

    db_url = args.db_url or os.environ.get("MIRA_RESULTS_DB_URL")
    if not db_url:
        print("ERROR: no DB URL. Pass --db-url or set MIRA_RESULTS_DB_URL.", file=sys.stderr)
        sys.exit(2)
    engine = results_db.get_engine(db_url)

    a_uuid, b_uuid = _pick_two(engine, args.env, args)
    diff = results_db.query_regression(engine, a_uuid, b_uuid)

    print(f"A (baseline): {a_uuid}")
    print(f"B (current):  {b_uuid}")
    print()

    def _section(title, rows, body):
        print(f"━━ {title}  ({len(rows)} item(s)) " + "━" * max(0, 60 - len(title)))
        if not rows:
            print("  (none)")
        else:
            for r in rows:
                body(r)
        print()

    _section(
        "🔻 REGRESSIONS — PASS → FAIL",
        diff["regressed"],
        lambda r: print(
            f"  [{r['scenario'][:50]:<50}] {r['metric']:<48} "
            f"A={_fmt_score(r['a']['score'])} → B={_fmt_score(r['b']['score'])}"
        ),
    )
    _section(
        "🟢 IMPROVEMENTS — FAIL → PASS",
        diff["improved"],
        lambda r: print(
            f"  [{r['scenario'][:50]:<50}] {r['metric']:<48} "
            f"A={_fmt_score(r['a']['score'])} → B={_fmt_score(r['b']['score'])}"
        ),
    )
    _section(
        "📉 SILENT DRIFT — both PASS but B score lower by ≥0.10",
        diff["score_drop"],
        lambda r: print(
            f"  [{r['scenario'][:50]:<50}] {r['metric']:<48} "
            f"A={_fmt_score(r['a']['score'])} → B={_fmt_score(r['b']['score'])}  Δ={r['delta']}"
        ),
    )
    _section(
        "📈 SILENT GAINS — both PASS but B score higher by ≥0.10",
        diff["score_gain"],
        lambda r: print(
            f"  [{r['scenario'][:50]:<50}] {r['metric']:<48} "
            f"A={_fmt_score(r['a']['score'])} → B={_fmt_score(r['b']['score'])}  Δ=+{r['delta']}"
        ),
    )
    _section(
        f"Only in A ({_short(a_uuid)})",
        diff["only_in_a"],
        lambda r: print(f"  [{r['scenario'][:50]:<50}] {r['metric']}"),
    )
    _section(
        f"Only in B ({_short(b_uuid)})",
        diff["only_in_b"],
        lambda r: print(f"  [{r['scenario'][:50]:<50}] {r['metric']}"),
    )


if __name__ == "__main__":
    main()
