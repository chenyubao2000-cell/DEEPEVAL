"""`mira-eval-trend` — show how one metric has trended over the last N days.

Useful for catching slow drift that single-run reports hide. Example:

    mira-eval-trend --metric GoalAccuracyMetric --env preview --days 30
"""
from __future__ import annotations

import argparse
import os
import sys

from ..config import load_env
from ..persistence import results_db


def main() -> None:
    ap = argparse.ArgumentParser(prog="mira-eval-trend", description=__doc__)
    ap.add_argument("--metric", required=True,
                    help='metric name as stored (e.g. "GoalAccuracyMetric" or "GEval/ExpectedToolPath [Conversational GEval]")')
    ap.add_argument("--env", help="filter by env (preview/mina/...)")
    ap.add_argument("--days", type=int, default=30)
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
    rows = results_db.query_metric_trend(engine, args.metric, env=args.env, days=args.days)

    if not rows:
        print(f"(no data for metric={args.metric!r} env={args.env or '*'} days={args.days})")
        return

    print(f"Trend for `{args.metric}` (env={args.env or '*'}, last {args.days}d, {len(rows)} runs)")
    print(f"{'generated_at':<20}  {'env':<10}  {'avg':>6}  {'pass':>5}  {'fail':>5}  {'rate':>6}  run_uuid")
    print("-" * 100)
    for r in rows:
        gen = r["generated_at"].strftime("%Y-%m-%d %H:%M:%S") if r["generated_at"] else "?"
        avg = f"{r['avg_score']:.3f}" if r["avg_score"] is not None else "  —  "
        rate = f"{r['pass_rate']*100:.0f}%" if r["pass_rate"] is not None else "  — "
        print(
            f"{gen:<20}  {r['env']:<10}  {avg:>6}  "
            f"{r['n_pass']:>5}  {r['n_fail']:>5}  {rate:>6}  {r['run_uuid']}"
        )


if __name__ == "__main__":
    main()
