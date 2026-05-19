"""`mira-eval-report` — regenerate a Markdown/JSON report from a DB-stored run.

Three lookup modes:
  --run-uuid <uuid>       explicit run
  --env <env> --latest    most-recent run for that env
  --list                  print the 20 most recent runs (newest first) and exit

The resulting files are written next to the same `--out` basename pattern used
by `mira-eval-run` (default `reports/report-<short-uuid>.{json,md}`).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..config import load_env
from ..persistence import results_db
from ..report import writers


def _print_run_list(rows: list[dict]) -> None:
    if not rows:
        print("(no runs found)")
        return
    print(f"{'generated_at':<20}  {'env':<10}  {'goldens':>7}  {'pass':>5} {'fail':>5} {'err':>4} {'info':>5}  {'rate':>6}  run_uuid")
    print("-" * 110)
    for r in rows:
        gen = r["generated_at"].strftime("%Y-%m-%d %H:%M:%S") if r["generated_at"] else "?"
        rate = f"{r['pass_rate']*100:.1f}%" if r["pass_rate"] is not None else "  —  "
        print(
            f"{gen:<20}  {r['env']:<10}  {r['n_goldens']:>7}  "
            f"{r['pass']:>5} {r['fail']:>5} {r['error']:>4} {r['info']:>5}  "
            f"{rate:>6}  {r['run_uuid']}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="mira-eval-report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
    )
    ap.add_argument("--run-uuid", help="explicit run to regenerate")
    ap.add_argument("--env", help="env to filter by when using --latest / --list")
    ap.add_argument("--latest", action="store_true",
                    help="pick the most recent run (combine with --env to filter)")
    ap.add_argument("--list", dest="do_list", action="store_true",
                    help="list the 20 most recent runs and exit")
    ap.add_argument("--limit", type=int, default=20,
                    help="how many runs to show with --list (default 20)")
    ap.add_argument("--out", default=None,
                    help="output basename (default: reports/report-<short-uuid>)")
    ap.add_argument("--db-url", default=None,
                    help="MySQL URL (default: $MIRA_RESULTS_DB_URL)")
    ap.add_argument("--load-env", default=None,
                    help="also load .env.<name> first (useful when DB URL is in .env)")
    args = ap.parse_args()

    # Optional: load .env.<name> so MIRA_RESULTS_DB_URL can live there too.
    if args.load_env:
        load_env(args.load_env)

    db_url = args.db_url or os.environ.get("MIRA_RESULTS_DB_URL")
    if not db_url:
        print("ERROR: no DB URL. Pass --db-url or set MIRA_RESULTS_DB_URL.", file=sys.stderr)
        sys.exit(2)

    engine = results_db.get_engine(db_url)

    if args.do_list:
        rows = results_db.list_recent_runs(engine, env=args.env, limit=args.limit)
        _print_run_list(rows)
        sys.exit(0)

    run_uuid = args.run_uuid
    if not run_uuid and args.latest:
        run_uuid = results_db.find_latest_run(engine, env=args.env)
        if not run_uuid:
            print("no runs found"
                  + (f" for env={args.env}" if args.env else ""), file=sys.stderr)
            sys.exit(2)
        print(f"(latest run: {run_uuid})")

    if not run_uuid:
        ap.print_help(sys.stderr)
        sys.exit(2)

    try:
        results, meta = results_db.load_run(engine, run_uuid)
    except LookupError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    out_base = Path(args.out) if args.out else Path("reports") / f"report-{run_uuid[:8]}"
    out_base.parent.mkdir(parents=True, exist_ok=True)
    json_path = out_base.with_suffix(".json")
    md_path = out_base.with_suffix(".md")

    meta = dict(meta)
    meta["json_path"] = json_path.name

    writers.write_json(results, json_path, meta)
    writers.write_markdown(results, md_path, meta)

    print(f"📊 Markdown report : {md_path}")
    print(f"🧾 JSON details    : {json_path}")


if __name__ == "__main__":
    main()
