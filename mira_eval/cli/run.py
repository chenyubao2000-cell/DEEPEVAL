"""`mira-eval-run` — multi-golden × N-metric evaluation entry point.

Filtering options (all combinable; AND semantics):
  --category crm,voice,ci_email,ci_dingding   _category field
  --tier light|heavy                           _tier field
  --index 10,11,12                             0-based positions in goldens.json
  --scenario 上海                              substring match against scenario

If no filter is given, runs the 4 default categories (crm/voice/ci_email/ci_dingding).
"""
from __future__ import annotations

import argparse
import os
import sys

from ..evaluate import pipeline


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="mira-eval-run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
    )
    ap.add_argument("--env", help="environment name (default: $MIRA_ENV or 'preview'); selects .env.<name>")
    ap.add_argument("--category", help="comma-separated _category filter (e.g. crm,voice)")
    ap.add_argument("--tier", choices=["light", "heavy"], help="filter by _tier")
    ap.add_argument("--index", help="comma-separated 0-based indices")
    ap.add_argument("--scenario", help="substring match on scenario")
    ap.add_argument("--all", action="store_true", help="run all goldens (overrides default-category filter)")
    ap.add_argument("--out", default="reports/report", help="output basename (will write .json + .md)")
    ap.add_argument("--refresh-tools", action="store_true",
                    help="force a Langfuse refresh of the tool-registry cache after the first golden")
    ap.add_argument("--no-langfuse-refresh", action="store_true",
                    help="never query Langfuse; use cache only (offline / CI)")
    ap.add_argument("--golden-concurrency", type=int,
                    default=int(os.environ.get("MIRA_GOLDEN_CONCURRENCY", "4")),
                    help="max goldens to run in parallel after the first one")
    args = ap.parse_args()
    sys.exit(pipeline.run(args))


if __name__ == "__main__":
    main()
