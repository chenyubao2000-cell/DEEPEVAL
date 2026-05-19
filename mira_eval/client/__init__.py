"""Adapter to the system under test — Mira BFF.

This is the only module allowed to import `httpx` and speak SSE. Other code
goes through `MiraSession` from `client.mira`.
"""
from .mira import MiraSession

__all__ = ["MiraSession"]
