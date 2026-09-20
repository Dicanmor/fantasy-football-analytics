"""Helpers to read the raw Parquet files written by ``ff-download``."""
from __future__ import annotations

import re
from pathlib import Path

import polars as pl

from ff.config import RAW_DIR


def load_raw(name: str, seasons: list[int] | None = None, raw_dir: Path | None = None) -> pl.DataFrame:
    """Load a dataset from ``data/raw`` as one DataFrame.

    Seasonal datasets are concatenated across seasons. nflverse changes schemas between seasons
    (e.g. ``injuries`` gained ``season_type`` and lost ``date_modified`` in 2025; ``rosters.height``
    switched from float to int), so files are combined with ``diagonal_relaxed``: missing columns
    become nulls and types are upcast instead of raising an error.
    """
    raw_dir = raw_dir or RAW_DIR
    flat = raw_dir / f"{name}.parquet"
    if flat.exists():
        return pl.read_parquet(flat)

    files = sorted((raw_dir / name).glob(f"{name}_*.parquet"))
    if seasons is not None:
        wanted = {int(s) for s in seasons}
        files = [f for f in files if (m := re.search(r"_(\d{4})\.parquet$", f.name)) and int(m.group(1)) in wanted]
    if not files:
        raise FileNotFoundError(f"No files for '{name}' in {raw_dir}. Run `ff-download` first.")
    return pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")
