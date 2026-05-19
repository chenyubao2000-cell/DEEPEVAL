"""Tool-call path verification — ConversationalGEval rubric.

Compares Mira's actual tool calls against the bootstrapped reference path
stored on each golden as ``_expected_tools`` (carried into metric via
``MultiTurnParams.METADATA``). Replaces the old ToolUseMetric chain — see
`reports/token_audit_report.md` for the savings analysis.
"""
from .rubric import EXPECTED_TOOL_PATH_METRIC

__all__ = ["EXPECTED_TOOL_PATH_METRIC"]
