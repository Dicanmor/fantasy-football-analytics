"""Kicker and team-defense (DEF) projections, in points per game.

K and DEF are streamed in most leagues, so they matter little for trades, but they are part of the lineup and
of the bench, so the site needs a number for them. Scoring below is a common default (it is NOT read from
your league); change the constants if your league differs.

  Kicker:  FG 0-39 yds = 3, 40-49 = 4, 50+ = 5, PAT = 1
  DEF:     sack 1, interception 2, fumble recovery 2, defensive TD 6, safety 2, blocked kick 2,
           points allowed: 0 -> 10, 1-6 -> 7, 7-13 -> 4, 14-20 -> 1, 21-27 -> 0, 28-34 -> -1, 35+ -> -4
"""
from __future__ import annotations

import polars as pl

from ff.models.value_common import WEEKS

# nflverse uses LA for the Rams; Sleeper's DEF id is LAR.
SLEEPER_TEAM = {"LA": "LAR"}


def kicker_games(player_stats: pl.DataFrame) -> pl.DataFrame:
    f = lambda c: pl.col(c).fill_null(0)  # noqa: E731
    return (
        player_stats.filter((pl.col("season_type") == "REG") & (pl.col("position") == "K"))
        .select(
            gsis_id="player_id",
            season=pl.col("season").cast(pl.Int32),
            week=pl.col("week").cast(pl.Int32),
            pts=3 * (f("fg_made_0_19") + f("fg_made_20_29") + f("fg_made_30_39"))
            + 4 * f("fg_made_40_49") + 5 * (f("fg_made_50_59") + f("fg_made_60_")) + f("pat_made"),
        )
        .with_columns(pl.col("pts").cast(pl.Float64))
    )


def _points_allowed_score(pa: pl.Expr) -> pl.Expr:
    return (
        pl.when(pa == 0).then(10).when(pa <= 6).then(7).when(pa <= 13).then(4).when(pa <= 20).then(1)
        .when(pa <= 27).then(0).when(pa <= 34).then(-1).otherwise(-4)
    )


def dst_games(team_stats: pl.DataFrame, schedules: pl.DataFrame) -> pl.DataFrame:
    """Weekly DEF fantasy points per team (regular season)."""
    sched = schedules.filter(pl.col("game_type") == "REG")
    allowed = pl.concat(
        [
            sched.select("season", "week", team="home_team", pa="away_score"),
            sched.select("season", "week", team="away_team", pa="home_score"),
        ]
    ).drop_nulls("pa").with_columns(pl.col("season").cast(pl.Int32), pl.col("week").cast(pl.Int32))
    f = lambda c: pl.col(c).fill_null(0)  # noqa: E731
    stats = team_stats.filter(pl.col("season_type") == "REG").with_columns(
        pl.col("season").cast(pl.Int32), pl.col("week").cast(pl.Int32)
    )
    return (
        stats.join(allowed, on=["season", "week", "team"], how="inner")
        .select(
            "team", "season", "week",
            pts=(
                f("def_sacks") + 2 * f("def_interceptions") + 2 * f("fumble_recovery_opp") + 6 * f("def_tds")
                + 2 * f("def_safeties") + 2 * (f("def_fg_blocks") + f("def_punt_blocks") + f("def_pat_blocks"))
                + _points_allowed_score(pl.col("pa"))
            ).cast(pl.Float64),
        )
    )


def project_special(
    games: pl.DataFrame,
    key: str,
    active: pl.DataFrame,
    as_of: tuple[int, int],
    half_life: float = 17.0,
    season_boost: float = 3.0,
    prior_k: float = 4.0,
    window_seasons: int = 3,
) -> pl.DataFrame:
    """Recency-weighted PPG per K / DEF, shrunk toward the average of the group (no evidence = average)."""
    season, week = as_of
    now_t = season * WEEKS + week
    h = games.filter(
        (pl.col("season") * 100 + pl.col("week") < season * 100 + week) & (pl.col("season") > season - window_seasons)
    ).with_columns(
        w=0.5 ** ((now_t - (pl.col("season") * WEEKS + pl.col("week"))) / half_life)
        * pl.when(pl.col("season") == season).then(season_boost).otherwise(1.0)
    )
    agg = h.group_by(key).agg(pl.col("w").sum().alias("sw"), (pl.col("w") * pl.col("pts")).sum().alias("wp"),
                              pl.len().alias("games"))
    table = active.join(agg, on=key, how="left").with_columns(
        pl.col("games").fill_null(0), pl.col("sw").fill_null(0.0), pl.col("wp").fill_null(0.0)
    )
    mean = float((table["wp"].sum() / table["sw"].sum())) if table["sw"].sum() > 0 else 0.0
    return table.with_columns(
        raw_ppg=pl.when(pl.col("sw") > 0).then(pl.col("wp") / pl.col("sw")),
        proj_ppg=(pl.col("wp") + prior_k * mean) / (pl.col("sw") + prior_k),
    )
