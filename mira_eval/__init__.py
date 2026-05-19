"""Mira evaluation harness — DeepEval × Mira BFF.

Package layout (concept-based, modelled on deepeval's own structure):

    mira_eval/
      config.py        — env / .env.<name> loader (small, kept flat)
      cli/             — thin argparse entry points (one console_script each)
      dataset/         — golden loading + tier filter
      client/          — adapter to the system under test (Mira BFF)
      models/          — adapter to the judge LLM (Claude CLI)
      tracing/         — observability adapters (Langfuse traces, tool registry)
      persistence/     — adapter to Mira's Postgres (SessionHealth verifies)
      evaluate/        — main runner: pipeline / driver / audit / compare / replay
      metrics/         — registry + builtins wrapper + one subpackage per metric
      report/          — JSON / Markdown / HTML writers

Data assets live in `data/` (goldens, tool-dependency constraints).
Outputs go to `reports/`. Runtime caches to `.cache/`.

The 6-stage mental model (user → data prep → agent invoke → evidence → eval
→ report) lives in `evaluate/pipeline.py`'s main flow, not in directory
names — the structure stays stable as the suite grows.
"""

__version__ = "0.2.0"
