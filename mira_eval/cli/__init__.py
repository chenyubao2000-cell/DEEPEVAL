"""Thin CLI entry points.

Each module is one console_script declared in `pyproject.toml`:

  - main.py        → `mira-eval` (top-level dispatcher with subcommands)
  - run.py         → `mira-eval-run` (multi-golden × N-metric report)
  - healthcheck.py → `mira-eval-healthcheck`
  - compare.py     → `mira-eval-compare`
  - html.py        → `mira-eval-html`
  - replay.py      → `mira-eval-replay`
  - bootstrap.py   → `mira-eval-bootstrap`

Every script parses args, calls `mira_eval.config.load_env`, then dispatches
to a single function in `evaluate/` or `report/`. CLI files stay <100 lines.
"""
