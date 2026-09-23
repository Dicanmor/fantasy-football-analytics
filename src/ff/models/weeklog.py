"""Weekly game log for the player-card "Stats" tab: past weeks' actual box score, future weeks' opponent.

This is a display-only table, separate from ``player_values.json`` so that file stays small. Only the current
season is exported (a full multi-season history would make the file much larger for a feature that is about
"how has he looked lately", not deep research).

The PROJ column is the player's current rest-of-season PPG (same number every future week) shown for context next
to actual results, NOT a week-specific, opponent-adjusted projection — building a real week-by-week projection
(home/away, Vegas lines, etc.) is a separate, bigger model this project does not have yet (see README, roadmap).
"""
from __future__ import annotations

import json

import polars as pl

from ff.models.value_common import WEEKS


def _opponent_by_team_week(schedules: pl.DataFrame, season: int) -> pl.DataFrame:
    sched = schedules.filter((pl.col("season") == season) & (pl.col("game_type") == "REG"))
    return pl.concat(
        [
            sched.select("week", team="home_team", opp="away_team"),
            sched.select("week", team="away_team", opp="home_team"),
        ]
    )


def _box_score(player_stats: pl.DataFrame, season: int) -> pl.DataFrame:
    f = lambda c: pl.col(c).fill_null(0)  # noqa: E731
    return (
        player_stats.filter((pl.col("season") == season) & (pl.col("season_type") == "REG"))
        .select(
            gsis_id="player_id", week="week",
            rush_att="carries", rush_yd="rushing_yards", rush_td="rushing_tds",
            tar="targets", rec="receptions", rec_yd="receiving_yards", rec_td="receiving_tds",
            fum=f("rushing_fumbles") + f("receiving_fumbles") + f("sack_fumbles"),
            fum_lost=f("rushing_fumbles_lost") + f("receiving_fumbles_lost") + f("sack_fumbles_lost"),
            kr="kickoff_returns", kyd="kickoff_return_yards", pr="punt_returns",
            sp_td=f("pt_return_tds"),
        )
    )


def build_weekly_log(
    active: pl.DataFrame, log: pl.DataFrame, schedules: pl.DataFrame, player_stats: pl.DataFrame, as_of: tuple[int, int]
) -> dict[str, list[dict]]:
    """``active``: current_roster() output. ``log``: build_all()["log"] (gsis_id, season, week, offense_pct, ppr).
    ``schedules``/``player_stats``: load_raw() output, passed in (not loaded here) so this stays a pure,
    easily-tested function. Returns {sleeper_id: [{wk, opp, fpts, snap, ra, ry, rt, tg, rc, cy, ct, fm, fl, kr,
    kyd, pr, spt}, ...]}."""
    season, week = as_of
    opp = _opponent_by_team_week(schedules, season)
    box = _box_score(player_stats, season)
    snaps = log.filter(pl.col("season") == season).select("gsis_id", "week", "offense_pct", "ppr")

    weeks = pl.DataFrame({"week": list(range(1, WEEKS + 1))})
    out: dict[str, list[dict]] = {}
    for row in active.iter_rows(named=True):
        if not row["sleeper_id"]:
            continue
        sched_rows = opp.filter(pl.col("team") == row["team"]).join(weeks, on="week", how="right").sort("week")
        s = snaps.filter(pl.col("gsis_id") == row["gsis_id"])
        b = box.filter(pl.col("gsis_id") == row["gsis_id"])
        merged = sched_rows.join(s, on="week", how="left").join(b, on="week", how="left", suffix="_b")
        games = []
        for r in merged.iter_rows(named=True):
            played = r["week"] < week and r["ppr"] is not None
            games.append(
                {
                    "wk": r["week"], "opp": r["opp"],
                    "fpts": round(r["ppr"], 2) if played else None,
                    "snap": round(r["offense_pct"] * 100) if played and r["offense_pct"] is not None else None,
                    "ra": r["rush_att"] if played else None, "ry": r["rush_yd"] if played else None,
                    "rt": r["rush_td"] if played else None,
                    "tg": r["tar"] if played else None, "rc": r["rec"] if played else None,
                    "cy": r["rec_yd"] if played else None, "ct": r["rec_td"] if played else None,
                    "fm": r["fum"] if played else None, "fl": r["fum_lost"] if played else None,
                    "kr": r["kr"] if played else None, "kyd": r["kyd"] if played else None,
                    "pr": r["pr"] if played else None, "spt": r["sp_td"] if played else None,
                }
            )
        if any(g["opp"] is not None for g in games):
            out[row["sleeper_id"]] = games
    return out


def export_weekly_log(
    active: pl.DataFrame, log: pl.DataFrame, schedules: pl.DataFrame, player_stats: pl.DataFrame,
    as_of: tuple[int, int], proj_by_sleeper_id: dict[str, float], directory,
) -> None:
    weeklog = build_weekly_log(active, log, schedules, player_stats, as_of)
    for sid, games in weeklog.items():
        p = proj_by_sleeper_id.get(sid)
        for g in games:
            g["proj"] = round(p, 1) if p is not None and g["fpts"] is None and g["opp"] is not None else None
    text = json.dumps(weeklog, separators=(",", ":"))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "player_weeks.json").write_text(text, encoding="utf-8")
