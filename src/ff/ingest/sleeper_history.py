"""Our own injury-label history: Sleeper keeps only the CURRENT label (no history API), so we record every change.

Each daily run appends a row to ``data/history/sleeper_injuries.csv`` only when a player's label CHANGES
(status, body part or notes), plus a ``status = null`` row when a player disappears from Sleeper's injured list
(= cleared). The file is tiny, diffable in git and committed by the workflow, so it grows by itself from the day it
starts (nothing before that can be recovered; for the past we have nflverse's weekly reports).

``crosscheck`` compares today's Sleeper label with the NFL's official report for the current week.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

HISTORY_SCHEMA = {
    "sleeper_id": pl.String, "observed": pl.String, "sl_status": pl.String, "sl_part": pl.String,
    "sl_notes": pl.String, "sl_news": pl.Int64,
}
_KEY = ["sl_status", "sl_part", "sl_notes"]


def read_history(path: Path) -> pl.DataFrame:
    return pl.read_csv(path, schema=HISTORY_SCHEMA) if path.exists() else pl.DataFrame(schema=HISTORY_SCHEMA)


def update_history(history: pl.DataFrame, snapshot: pl.DataFrame, observed: str) -> tuple[pl.DataFrame, int]:
    """Append the rows that differ from each player's latest recorded label. Returns (history, rows added)."""
    last = (
        history.with_row_index("_i").sort("_i").group_by("sleeper_id", maintain_order=True).last().drop("_i")
        if history.height else history
    )
    cur = snapshot.select("sleeper_id", *_KEY, "sl_news").with_columns(observed=pl.lit(observed))
    # new or changed labels (null-safe comparison: eq_missing)
    joined = cur.join(last.select("sleeper_id", **{f"p_{c}": c for c in _KEY}), on="sleeper_id", how="left")
    changed = joined.filter(
        pl.col("p_sl_status").is_null() & pl.col("sl_status").is_not_null()
        | pl.any_horizontal(*[~pl.col(c).eq_missing(pl.col(f"p_{c}")) for c in _KEY])
    ).select(history.columns)
    # cleared: had a status at the last observation and is no longer listed
    cleared = (
        last.filter(pl.col("sl_status").is_not_null() & ~pl.col("sleeper_id").is_in(snapshot["sleeper_id"].implode()))
        .with_columns(observed=pl.lit(observed), sl_status=pl.lit(None, dtype=pl.String),
                      sl_part=pl.lit(None, dtype=pl.String), sl_notes=pl.lit(None, dtype=pl.String),
                      sl_news=pl.lit(None, dtype=pl.Int64))
        .select(history.columns)
    ) if history.height else pl.DataFrame(schema=HISTORY_SCHEMA)
    added = pl.concat([changed, cleared])
    return pl.concat([history, added]), added.height


def save_history(history: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    history.sort("observed", "sleeper_id").write_csv(path)


def crosscheck(sleeper: pl.DataFrame, nfl_week: pl.DataFrame, ids: pl.DataFrame) -> pl.DataFrame:
    """Sleeper label vs the official NFL report of the same week, one row per player on either list.

    ``nfl_week``: injuries for ONE (season, week) with ``gsis_id`` and ``report_status``; ``ids``: gsis_id <-> sleeper_id.
    ``agreement``: same / differ / sleeper_only (Sleeper lists him, the NFL report does not) / nfl_only.
    """
    ids = ids.select("gsis_id", pl.col("sleeper_id").cast(pl.String)).drop_nulls().unique("gsis_id")
    sl = sleeper.select("sleeper_id", "sl_status", "sl_part", "sl_notes").join(ids, on="sleeper_id", how="left")
    nfl = nfl_week.filter(pl.col("report_status").is_not_null()).select("gsis_id", nfl_status="report_status", nfl_part="report_primary_injury").unique("gsis_id")
    both = sl.join(nfl, on="gsis_id", how="full", coalesce=True)
    return both.with_columns(
        agreement=pl.when(pl.col("nfl_status").is_null()).then(pl.lit("sleeper_only"))
        .when(pl.col("sl_status").is_null()).then(pl.lit("nfl_only"))
        .when(pl.col("sl_status").str.to_lowercase() == pl.col("nfl_status").str.to_lowercase()).then(pl.lit("same"))
        .otherwise(pl.lit("differ"))
    )
