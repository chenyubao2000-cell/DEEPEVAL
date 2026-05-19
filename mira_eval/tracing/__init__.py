"""Observability adapters — Langfuse trace pull + per-env tool registry.

  - langfuse.py      — fetch traces / observations / usage / cost
  - tool_registry.py — pull Mira's available-tools registry from traces,
                       cache on disk under .cache/tools-<env>.json
"""
