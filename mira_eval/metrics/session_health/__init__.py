"""Session-health gate: did the Mira run complete cleanly across 3 layers?

Aggregates client (in-memory) + trace (Langfuse) + persistence (Postgres)
checks into a single 0/1 score.
"""
from .metric import SessionHealthMetric

__all__ = ["SessionHealthMetric"]
