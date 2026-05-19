"""Tool-call path compliance metric.

LLM-as-judge audit against the constraint catalog at
``data/tool_dependencies.json``. See ``metric.py``.
"""
from .metric import ToolDependencyMetric

__all__ = ["ToolDependencyMetric"]
