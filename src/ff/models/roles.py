"""Who is actually in play this season? Tiers that keep unused backups from outranking real players.

A healthy backup with no snaps this season (a QB2 whose three old games were good) used to be valued as if he
would play every remaining game, which put him above real starters. Three tiers fix the ORDER and one factor fixes
the VALUE:

  tier 0  played a real share of the snaps this season (season average OR the last 3 weeks, so a backup who is
          taking over is not buried), OR is hurt now, OR missed time hurt earlier this season (Out/Doubtful on the
          report or on IR): a good player coming back from injury keeps the value his numbers say
  tier 1  healthy, never hurt this season, played a little (snap share under ``FEW``)
  tier 2  healthy, never hurt this season, no snaps at all

Tier 1/2 players are ranked after every tier-0 player (inside a tier, by value), and their expected games are
multiplied by how often such players really get on the field: ``PLAY_RATE`` is, per position, the share of the
remaining games a healthy player of that tier played (>= 20% of the snaps), RELATIVE to a regular player's rate.
Estimated on 2018-2025, as of weeks 4-12 (``value.evaluate_play_rates`` reproduces the table).
Until week 4 the snap shares are too noisy, so everybody stays in tier 0.
"""
from __future__ import annotations

import polars as pl

FEW = 0.2          # average snap share (over team games played) under which a healthy player is a "few snaps" player
MIN_WEEK = 4       # tiers start at this week
# position -> (no snaps this season, few snaps) play rate relative to a regular (>= FEW) player; see module docstring
PLAY_RATE = {
    "QB": (0.19, 0.27),
    "RB": (0.16, 0.32),
    "WR": (0.20, 0.38),
    "TE": (0.23, 0.42),
}


def season_snaps(log: pl.DataFrame, as_of: tuple[int, int]) -> pl.DataFrame:
    """gsis_id, cur_games (games with any offensive snap this season), cur_share (the larger of his season-average
    snap share and his last-3-weeks average; missed weeks count as 0)."""
    season, week = as_of
    cur = log.filter((pl.col("season") == season) & (pl.col("week") < week) & (pl.col("offense_pct") > 0))
    recent = pl.col("offense_pct").filter(pl.col("week") >= week - 3).sum() / min(3, max(week - 1, 1))
    return cur.group_by("gsis_id").agg(
        cur_games=pl.len(), cur_share=pl.max_horizontal(pl.col("offense_pct").sum() / max(week - 1, 1), recent)
    )


def hurt_ids(injuries: pl.DataFrame, rosters_weekly: pl.DataFrame, as_of: tuple[int, int]) -> pl.DataFrame:
    """gsis_id of everyone who missed time hurt this season before ``as_of``: Out/Doubtful on the report or on IR/PUP."""
    season, week = as_of
    rep = injuries.filter(
        (pl.col("season") == season) & (pl.col("game_type") == "REG") & (pl.col("week") <= week) & pl.col("report_status").is_in(["Out", "Doubtful"])
    ).select("gsis_id")
    ir = rosters_weekly.filter(
        (pl.col("season") == season) & (pl.col("game_type") == "REG") & (pl.col("week") <= week) & pl.col("status").is_in(["RES", "PUP"])
    ).select("gsis_id")
    return pl.concat([rep, ir]).drop_nulls().unique()


def add_roles(players: pl.DataFrame, log: pl.DataFrame, as_of: tuple[int, int], hurt: pl.DataFrame | None = None) -> pl.DataFrame:
    """Adds ``cur_games``, ``cur_share``, ``was_hurt``, ``tier`` (0/1/2) and ``play_rate`` (multiplier for expected games).

    ``hurt`` = ``hurt_ids(...)``. ``players`` needs ``position``, ``status``, ``report_status``, ``sl_status`` and ``absence_now`` (see ``availability``).
    """
    out = players.join(season_snaps(log, as_of), on="gsis_id", how="left").with_columns(
        pl.col("cur_games").fill_null(0), pl.col("cur_share").fill_null(0.0)
    )
    if hurt is not None:
        out = out.join(hurt.with_columns(was_hurt=pl.lit(True)), on="gsis_id", how="left").with_columns(pl.col("was_hurt").fill_null(False))
    else:
        out = out.with_columns(was_hurt=pl.lit(False))
    healthy = (
        (pl.col("absence_now").fill_null(0.0) <= 0) & pl.col("sl_status").is_null() & pl.col("report_status").is_null()
        & (pl.col("status") == "ACT") & ~pl.col("was_hurt")
    )
    tier = (
        pl.when(as_of[1] < MIN_WEEK).then(0)
        .when(~healthy).then(0)
        .when(pl.col("cur_share") >= FEW).then(0)
        .when(pl.col("cur_games") > 0).then(1)
        .otherwise(2)
    )
    out = out.with_columns(tier=tier.cast(pl.Int32))
    rate = pl.lit(1.0)
    for pos, (zero, few) in PLAY_RATE.items():
        rate = pl.when((pl.col("position") == pos) & (pl.col("tier") == 2)).then(zero).when((pl.col("position") == pos) & (pl.col("tier") == 1)).then(few).otherwise(rate)
    return out.with_columns(play_rate=rate)
