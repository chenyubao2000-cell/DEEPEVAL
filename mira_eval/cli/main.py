"""`mira-eval` top-level dispatcher.

Dispatches to one of:

    mira-eval run [...]            # full multi-golden evaluation
    mira-eval healthcheck [...]    # single golden, every metric
    mira-eval compare [...]        # two JSON reports → side-by-side HTML
    mira-eval html <file.md>       # Markdown → styled HTML
    mira-eval replay <file.json>   # re-audit historical JSON report
    mira-eval bootstrap [...]      # propose _expected_tools for goldens
    mira-eval report [...]         # regenerate a report from the results DB
    mira-eval trend [...]          # cross-run trend for one metric
    mira-eval regression [...]     # diff two DB runs (PASS↔FAIL flips, drift)

Each subcommand is also exposed as its own console_script (e.g. `mira-eval-run`)
if you prefer flat invocation.
"""
from __future__ import annotations

import sys


_COMMANDS = {
    "run":          "mira_eval.cli.run",
    "healthcheck":  "mira_eval.cli.healthcheck",
    "compare":      "mira_eval.cli.compare",
    "html":         "mira_eval.cli.html",
    "replay":       "mira_eval.cli.replay",
    "bootstrap":    "mira_eval.cli.bootstrap",
    "report":       "mira_eval.cli.report",
    "trend":        "mira_eval.cli.trend",
    "regression":   "mira_eval.cli.regression",
}


def _usage() -> None:
    print("usage: mira-eval <command> [args...]", file=sys.stderr)
    print("\nCommands:", file=sys.stderr)
    for c in _COMMANDS:
        print(f"  {c}", file=sys.stderr)
    print("\nRun `mira-eval <command> --help` for command-specific help.", file=sys.stderr)


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        _usage()
        sys.exit(0 if len(sys.argv) >= 2 else 2)

    cmd = sys.argv[1]
    module_name = _COMMANDS.get(cmd)
    if module_name is None:
        print(f"unknown command: {cmd!r}", file=sys.stderr)
        _usage()
        sys.exit(2)

    # Shift argv so the subcommand's argparse sees its own args
    sys.argv = [f"mira-eval {cmd}", *sys.argv[2:]]

    import importlib
    mod = importlib.import_module(module_name)
    mod.main()


if __name__ == "__main__":
    main()
