"""Matchup guide: how much does each defense allow, and who has the easiest schedule this week?

Pipeline
  1. ``build_game_metrics``  player-level weekly stats -> one row per (game, defense, metric)
       metrics: fantasy points allowed to QB / RB / WR / TE, passing yards, rushing yards
  2. ``matchup_ratings``     recency-weighted average per defense, relative to the league
  3. ``weekly_guide``        attach the ratings to a week's schedule (team vs. opponent)

Two design decisions worth knowing:
  * No leakage: ratings "as of" week W only use games played strictly before W, so the same code
    powers the live guide and honest backtests.
  * Short memory: only the last ``seasons_back`` seasons (default 2) count, and within them recent
    games weigh more (exponential decay, half-life in games) because rosters and schemes change.

Run it::

    python -m ff.features.defense                       # current season/week, PPR
    python -m ff.features.defense --season 2025 --week 10 --scoring half
"""
from __future__ import annotations

import argparse

import polars as pl

from ff.config import PROCESSED_DIR
from ff.data import load_raw

POSITIONS = ("QB", "RB", "WR", "TE")
METRICS = [f"pts_{p}" for p in POSITIONS] + ["pass_yds", "rush_yds"]


def _points_expr(scoring: str) -> pl.Expr:
    ppr, std = pl.col("fantasy_points_ppr"), pl.col("fantasy_points")
    return {"ppr": ppr, "std": std, "half": (ppr + std) / 2}[scoring]


def build_game_metrics(stats: pl.DataFrame, scoring: str = "ppr") -> pl.DataFrame:
    """Long table: season, week, defense, metric, value (regular season only).

    Every (game, defense, metric) exists, even when the value is 0 (e.g. a defense that allowed no
    TE production), so averages are not inflated by missing rows.
    """
    reg = stats.filter(
        (pl.col("season_type") == "REG") & pl.col("opponent_team").is_not_null()
    ).rename({"opponent_team": "defense"})

    games = reg.select("season", "week", "defense").unique()

    pts = (
        reg.filter(pl.col("position").is_in(POSITIONS))
        .group_by("season", "week", "defense", "position")
        .agg(_points_expr(scoring).sum().cast(pl.Float64).alias("value"))
        .with_columns(("pts_" + pl.col("position")).alias("metric"))
        .select("season", "week", "defense", "metric", "value")
    )
    yds = (
        reg.group_by("season", "week", "defense")
        .agg(
            pl.col("passing_yards").fill_null(0).sum().alias("pass_yds"),
            pl.col("rushing_yards").fill_null(0).sum().alias("rush_yds"),
        )
        .unpivot(index=["season", "week", "defense"], variable_name="metric", value_name="value")
    )
    observed = pl.concat([pts, yds.with_columns(pl.col("value").cast(pl.Float64))])

    # Fill the zeros: full grid of games x metrics, then left-join what we observed.
    grid = games.join(pl.DataFrame({"metric": METRICS}), how="cross")
    return (
        grid.join(observed, on=["season", "week", "defense", "metric"], how="left")
        .with_columns(pl.col("value").fill_null(0.0))
        .sort("season", "week", "defense", "metric")
    )


def matchup_ratings(
    metrics: pl.DataFrame,
    as_of: tuple[int, int],
    seasons_back: int = 2,
    half_life: float = 8.0,
) -> pl.DataFrame:
    """Recency-weighted allowed-per-game for each defense/metric, as of (season, week).

    ``score`` is the weighted average divided by the league average (1.10 = allows 10% more than
    a typical defense = better matchup for the offense). ``rank`` 1 = most allowed = easiest.
    """
    season, week = as_of
    hist = metrics.filter(
        (pl.col("season") >= season - seasons_back + 1)
        & ((pl.col("season") < season) | ((pl.col("season") == season) & (pl.col("week") < week)))
    )
    weighted = (
        hist.sort("season", "week", descending=True)
        .with_columns(games_ago=pl.int_range(pl.len()).over("defense", "metric"))
        .with_columns(w=0.5 ** (pl.col("games_ago") / half_life))
        .group_by("defense", "metric")
        .agg(
            pl.len().alias("games"),
            ((pl.col("value") * pl.col("w")).sum() / pl.col("w").sum()).alias("allowed_per_game"),
        )
    )
    return (
        weighted.with_columns(
            league_avg=pl.col("allowed_per_game").mean().over("metric"),
        )
        .with_columns(
            score=pl.col("allowed_per_game") / pl.col("league_avg"),
            rank=pl.col("allowed_per_game").rank("min", descending=True).over("metric").cast(pl.Int32),
        )
        .sort("metric", "rank")
    )


def weekly_guide(ratings: pl.DataFrame, schedules: pl.DataFrame, season: int, week: int) -> pl.DataFrame:
    """One row per team for the given week: opponent plus that opponent's score/rank per metric."""
    games = schedules.filter(
        (pl.col("season") == season) & (pl.col("week") == week) & (pl.col("game_type") == "REG")
    )
    sides = pl.concat(
        [
            games.select(team="home_team", opponent="away_team", home=pl.lit(True)),
            games.select(team="away_team", opponent="home_team", home=pl.lit(False)),
        ]
    )
    wide = ratings.pivot(on="metric", index="defense", values=["score", "rank"], separator="_")
    # pivot names columns like "score_pts_QB" / "rank_pts_QB"; reorder to "<metric>_score"
    wide = wide.rename({c: f"{c.split('_', 1)[1]}_{c.split('_', 1)[0]}" for c in wide.columns if c != "defense"})
    out = sides.join(wide, left_on="opponent", right_on="defense", how="left").sort("team")
    score_cols = [c for c in out.columns if c.endswith("_score")]
    return out.with_columns(pl.col(score_cols).round(2))


def main(argv: list[str] | None = None) -> None:
    import nflreadpy as nfl

    p = argparse.ArgumentParser(description="Build defense ratings and the weekly matchup guide.")
    p.add_argument("--season", type=int, default=None)
    p.add_argument("--week", type=int, default=None)
    p.add_argument("--scoring", choices=["ppr", "half", "std"], default="ppr")
    p.add_argument("--seasons-back", type=int, default=2)
    p.add_argument("--half-life", type=float, default=8.0, help="in games")
    a = p.parse_args(argv)

    season = a.season or nfl.get_current_season()
    week = a.week or nfl.get_current_week()

    stats = load_raw("player_stats", seasons=list(range(season - a.seasons_back, season + 1)))
    metrics = build_game_metrics(stats, a.scoring)
    ratings = matchup_ratings(metrics, (season, week), a.seasons_back, a.half_life)
    guide = weekly_guide(ratings, load_raw("schedules", seasons=[season]), season, week)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    ratings.write_csv(PROCESSED_DIR / "defense_ratings.csv", float_precision=3)
    guide.write_csv(PROCESSED_DIR / "matchup_guide.csv")
    print(f"Ratings as of {season} week {week} ({a.scoring.upper()}), based on {ratings['games'].max()} games max")
    print(f"Wrote {PROCESSED_DIR / 'defense_ratings.csv'} and {PROCESSED_DIR / 'matchup_guide.csv'}")
    print(guide.select("team", "opponent", "pts_QB_score", "pts_RB_score", "pts_WR_score", "pts_TE_score").head(16))


if __name__ == "__main__":
    main()
