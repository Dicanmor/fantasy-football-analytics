"""Project-wide paths."""
from pathlib import Path

# src/ff/config.py -> parents[2] is the repo root
ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"
