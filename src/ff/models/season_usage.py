"""Season-to-date usage stats for the website's "Season stats" tab: pick a team, pick a position, see how touches
and targets are actually being split (snap share, rush share, target share, target quality).

**Routes run and targets-per-route-run (TPRR) are NOT included.** nflverse's play-participation data (the only
public source for "who ran a route on this play") lags a full season — as of writing it only goes through the
2025 season, not the in-progress one — so there is no way to compute a real, current-season route count. Rather
than fake it (e.g. from offensive snaps, which mixes route-runners with pass-blockers), those columns are left out
until that data actually exists for the current season. Everything else here uses in-season data:
  - Rush attempt share, target share: `player_stats` (already current-season).
  - ADOT, air yards share: NextGen Stats receiving (`load_nextgen_stats`), updated during the season.
  - Catchable target share: FTN charting's `is_catchable_ball`, joined to `pbp` by game/play id, updated weekly.
  - End-zone / 3rd-4th-down target share: `pbp`, updated weekly.

Every function below takes its data as a parameter rather than loading it internally (all callers get theirs via
``ff.data.load_raw``), so this module can be unit-tested with synthetic frames — no downloaded data required.
That matters in CI: the test suite runs before ``ff-download``, so any function that calls ``load_raw`` itself
fails there even though it works fine on a machine that already has data on disk.
"""
from __future__ import annotations

import json

import polars as pl

USAGE_POSITIONS = ("RB", "WR", "TE", "FB")


def _pfr_crosswalk(players: pl.DataFrame) -> pl.DataFrame:
    return players.select("gsis_id", "pfr_id").filter(pl.col("pfr_id").is_not_null())


def _snap_pct(snap_counts: pl.DataFrame, season: int, week: int, crosswalk: pl.DataFrame) -> pl.DataFrame:
    sc = snap_counts.filter((pl.col("season") == season) & (pl.col("week") < week))
    return (
        sc.join(crosswalk, left_on="pfr_player_id", right_on="pfr_id", how="inner")
        .group_by("gsis_id")
        .agg(pl.col("offense_pct").mean().alias("snap_pct"))
    )


def _volume_shares(player_stats: pl.DataFrame) -> pl.DataFrame:
    """Rush attempt share and target share, both as % of the player's OWN team's season total."""
    team_totals = player_stats.group_by("team").agg(
        pl.col("carries").sum().alias("team_rush_att"), pl.col("targets").sum().alias("team_targets")
    )
    per_player = player_stats.group_by("player_id", "team").agg(
        pl.col("carries").sum().alias("rush_att"), pl.col("targets").sum().alias("targets"),
        pl.col("week").n_unique().alias("gms"),
    )
    return per_player.join(team_totals, on="team").with_columns(
        rush_att_pct=pl.when(pl.col("team_rush_att") > 0).then(pl.col("rush_att") / pl.col("team_rush_att")),
        targets_pct=pl.when(pl.col("team_targets") > 0).then(pl.col("targets") / pl.col("team_targets")),
    ).rename({"player_id": "gsis_id"})


def _adot_and_air_yards_share(ngs_receiving: pl.DataFrame, season: int, week: int) -> pl.DataFrame:
    ngs = ngs_receiving.filter((pl.col("week") < week) & (pl.col("week") > 0))
    return (
        ngs.group_by("player_gsis_id")
        .agg(
            (pl.col("avg_intended_air_yards") * pl.col("targets")).sum().alias("_ay"),
            (pl.col("percent_share_of_intended_air_yards") * pl.col("targets")).sum().alias("_ays"),
            pl.col("targets").sum().alias("_t"),
        )
        .with_columns(
            adot=pl.when(pl.col("_t") > 0).then(pl.col("_ay") / pl.col("_t")),
            air_yards_pct=pl.when(pl.col("_t") > 0).then(pl.col("_ays") / pl.col("_t") / 100),
        )
        .select(gsis_id="player_gsis_id", adot="adot", air_yards_pct="air_yards_pct")
    )


def _target_quality(pbp: pl.DataFrame, ftn_charting: pl.DataFrame, season: int, week: int) -> pl.DataFrame:
    """Catchable / end-zone / 3rd-4th-down share of a player's OWN targets (not team share)."""
    p = (
        pbp.filter((pl.col("season") == season) & (pl.col("week") < week) & (pl.col("season_type") == "REG") & (pl.col("pass_attempt") == 1))
        .select("game_id", "play_id", "down", "air_yards", "yardline_100", "receiver_player_id")
        .filter(pl.col("receiver_player_id").is_not_null())
        .with_columns(pl.col("play_id").cast(pl.Int32))
    )
    ftn = ftn_charting.select("nflverse_game_id", "nflverse_play_id", "is_catchable_ball")
    j = p.join(ftn, left_on=["game_id", "play_id"], right_on=["nflverse_game_id", "nflverse_play_id"], how="left")
    return j.group_by(gsis_id="receiver_player_id").agg(
        pl.len().alias("_targets"),
        pl.col("is_catchable_ball").sum().alias("_catchable"),
        (pl.col("air_yards") >= pl.col("yardline_100")).sum().alias("_ez"),
        pl.col("down").is_in([3, 4]).sum().alias("_d34"),
    ).with_columns(
        catchable_pct=pl.col("_catchable") / pl.col("_targets"),
        ez_tgt_pct=pl.col("_ez") / pl.col("_targets"),
        down34_tgt_pct=pl.col("_d34") / pl.col("_targets"),
    ).select("gsis_id", "catchable_pct", "ez_tgt_pct", "down34_tgt_pct")


def build_usage_table(
    active: pl.DataFrame, as_of: tuple[int, int], player_stats: pl.DataFrame, snap_counts: pl.DataFrame,
    players: pl.DataFrame, ngs_receiving: pl.DataFrame, pbp: pl.DataFrame, ftn_charting: pl.DataFrame,
) -> pl.DataFrame:
    """``active``: current_roster() output, filtered to RB/WR/TE/FB. The rest are raw load_raw() outputs for the
    current season, passed in by the caller. Returns one row per player with the columns in the module docstring."""
    season, week = as_of
    ps = player_stats.filter((pl.col("season") == season) & (pl.col("week") < week) & (pl.col("season_type") == "REG"))
    crosswalk = _pfr_crosswalk(players)
    return (
        active.join(_snap_pct(snap_counts, season, week, crosswalk), on="gsis_id", how="left")
        .join(_volume_shares(ps).select("gsis_id", "gms", "rush_att_pct", "targets_pct"), on="gsis_id", how="left")
        .join(_adot_and_air_yards_share(ngs_receiving, season, week), on="gsis_id", how="left")
        .join(_target_quality(pbp, ftn_charting, season, week), on="gsis_id", how="left")
        .with_columns(pl.col("gms").fill_null(0))
    )


def export_usage(table: pl.DataFrame, directory) -> None:
    r = lambda v, d=3: None if v is None else round(v, d)  # noqa: E731
    by_team: dict[str, dict[str, list[dict]]] = {}
    for row in table.filter(pl.col("sleeper_id").is_not_null()).iter_rows(named=True):
        team, pos = row["team"], row["position"]
        by_team.setdefault(team, {}).setdefault(pos, []).append(
            {
                "id": row["sleeper_id"], "name": row["full_name"], "gms": row["gms"],
                "snap": r(row["snap_pct"], 3), "rush_att": r(row["rush_att_pct"], 3), "targets": r(row["targets_pct"], 3),
                "adot": r(row["adot"], 1), "air_yards": r(row["air_yards_pct"], 3),
                "catchable": r(row["catchable_pct"], 3), "ez": r(row["ez_tgt_pct"], 3), "d34": r(row["down34_tgt_pct"], 3),
            }
        )
    for team in by_team:
        for pos in by_team[team]:
            by_team[team][pos].sort(key=lambda p: -(p["snap"] or 0))
    text = json.dumps(by_team, separators=(",", ":"))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "team_usage.json").write_text(text, encoding="utf-8")
