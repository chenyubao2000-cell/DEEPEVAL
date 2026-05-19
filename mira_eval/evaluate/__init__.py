"""Main evaluation runner — orchestration, audit, side-tools.

Mirrors deepeval's `evaluate/` package shape:

  - pipeline.py  — multi-golden × N-metric orchestration (was report.py)
  - driver.py    — golden → ConversationalTestCase (was _driver.py)
  - audit.py     — verdict + judge-consistency check (was _verdict /
                   _audit_judge_consistency / _profile_for in report.py)
  - compare.py   — two reports → side-by-side HTML
  - replay.py    — re-audit JSON without re-judging

The 6-stage mental model (① user → ② data prep → ③ agent → ④ evidence →
⑤ eval → ⑥ report) is materialised in `pipeline.py`'s main flow.
"""
