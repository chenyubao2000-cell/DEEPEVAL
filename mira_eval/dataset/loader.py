"""Golden dataset loader.

Goldens live in ``data/goldens.json`` at repo root. Old location was
``tests/evals/.dataset.json`` (hidden file in a tests dir); v2 moved it
to a top-level ``data/`` so editing the dataset doesn't require touching
a test package.

The JSON shape is unchanged:

    {
        "goldens": [
            {"scenario": ..., "user_inputs": [...], "expected_outcome": ...,
             "_category": ..., "_tier": "light"|"heavy", "_expected_tools": [...],
             ...},
            ...
        ]
    }
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..config import ROOT

DATASET_PATH = ROOT / "data" / "goldens.json"


def load_goldens() -> list[dict]:
    """Load the shared dataset; honour MIRA_GOLDEN_TIER filter (light/heavy/all)."""
    with DATASET_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    goldens = data["goldens"]
    tier = os.environ.get("MIRA_GOLDEN_TIER", "all").lower()
    if tier in ("light", "heavy"):
        goldens = [g for g in goldens if g.get("_tier") == tier]
    return goldens


def golden_id(g: dict) -> str:
    """Short identifier for pytest parametrize ids / report headings."""
    return (g.get("scenario") or "case")[:60]
