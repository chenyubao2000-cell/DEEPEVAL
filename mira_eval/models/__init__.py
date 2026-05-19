"""Adapter to the judge LLM.

Mirrors deepeval's `models/` package: one file per LLM backend implementing
DeepEval's `DeepEvalBaseLLM` interface. Only Claude CLI today; add more here
if you need to A/B a different judge.
"""
from .claude_cli import ClaudeCliJudge

__all__ = ["ClaudeCliJudge"]
