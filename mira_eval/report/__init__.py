"""Report rendering — JSON / Markdown / HTML writers.

  - writers.py — `write_json` + `write_markdown` for evaluate.pipeline output
  - html.py    — Markdown → styled HTML standalone renderer (was md_to_html.py)

Audit / verdict logic does NOT live here — that's in `evaluate/audit.py`.
This layer only does formatting + I/O.
"""
