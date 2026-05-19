"""Custom ConversationalGEval rubrics — 3 judges:

  - ProfessionalNoFabrication  — tone + no invented context
  - DeliverableMatchesRequest  — does the agent actually produce the asked artifact
  - GroundedNoFabrication      — every factual claim has source or disclaimer

The package name is `deliverable_match` because that's the load-bearing
metric of the three. All three share the package because they are all
"text-quality GEvals" that don't need their own tracing / persistence wiring.
"""
from .rubric import GEVAL_METRICS

__all__ = ["GEVAL_METRICS"]
