"""Dataset loading — Mira goldens.

Mirrors deepeval's `dataset/` package shape. Today only holds the loader
for `data/goldens.json`; expand here when adding synthesizer / fixtures.
"""
from .loader import DATASET_PATH, golden_id, load_goldens

__all__ = ["DATASET_PATH", "golden_id", "load_goldens"]
