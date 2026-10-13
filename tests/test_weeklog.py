import polars as pl

from ff.models.weeklog import build_weekly_log


def _frames():
    """Shared synthetic fixture: one player, 2 games played, a bye week, and a future week.
    No load_raw() calls here (schedules/player_stats are passed in), so this runs fine even before
    `ff-download` has ever put anything in data/raw — e.g. in CI, where pytest runs before the download step."""
    active = pl.DataFrame(
        {"gsis_id": ["p1"], "sleeper_id": ["100"], "full_name": ["Test Player"], "position": ["RB"], "team": ["AAA"]}
    )
    log = pl.DataFrame({"gsis_id": ["p1", "p1"], "season": [2026, 2026], "week": [1, 2], "offense_pct": [0.8, 0.7], "ppr": [20.0, 15.0]})
    schedules = pl.DataFrame(
        {"season": [2026, 2026, 2026], "week": [1, 2, 4], "game_type": ["REG"] * 3,
         "home_team": ["AAA", "AAA", "AAA"], "away_team": ["BBB", "CCC", "DDD"]}  # week 3 is a bye
    )
    player_stats = pl.DataFrame(
        {"player_id": pl.Series([], dtype=pl.String), "season": pl.Series([], dtype=pl.Int64), "week": pl.Series([], dtype=pl.Int64),
         "season_type": pl.Series([], dtype=pl.String), "attempts": pl.Series([], dtype=pl.Int64), "completions": pl.Series([], dtype=pl.Int64),
         "passing_yards": pl.Series([], dtype=pl.Int64), "passing_tds": pl.Series([], dtype=pl.Int64), "passing_interceptions": pl.Series([], dtype=pl.Int64),
         "sacks_suffered": pl.Series([], dtype=pl.Int64), "sack_yards_lost": pl.Series([], dtype=pl.Int64),
         "carries": pl.Series([], dtype=pl.Int64), "rushing_yards": pl.Series([], dtype=pl.Int64),
         "rushing_tds": pl.Series([], dtype=pl.Int64), "targets": pl.Series([], dtype=pl.Int64), "receptions": pl.Series([], dtype=pl.Int64),
         "receiving_yards": pl.Series([], dtype=pl.Int64), "receiving_tds": pl.Series([], dtype=pl.Int64), "rushing_fumbles": pl.Series([], dtype=pl.Int64),
         "receiving_fumbles": pl.Series([], dtype=pl.Int64), "sack_fumbles": pl.Series([], dtype=pl.Int64), "rushing_fumbles_lost": pl.Series([], dtype=pl.Int64),
         "receiving_fumbles_lost": pl.Series([], dtype=pl.Int64), "sack_fumbles_lost": pl.Series([], dtype=pl.Int64), "kickoff_returns": pl.Series([], dtype=pl.Int64),
         "kickoff_return_yards": pl.Series([], dtype=pl.Int64), "punt_returns": pl.Series([], dtype=pl.Int64), "pt_return_tds": pl.Series([], dtype=pl.Int64)}
    )
    return active, log, schedules, player_stats


def test_past_weeks_get_actuals_future_weeks_get_none_and_bye_has_no_opponent():
    active, log, schedules, player_stats = _frames()
    out = build_weekly_log(active, log, schedules, player_stats, (2026, 3))

    games = {g["wk"]: g for g in out["100"]}
    assert games[1]["fpts"] == 20.0 and games[1]["opp"] == "BBB"
    assert games[3]["opp"] is None  # bye week, not in the schedule join
    assert games[4]["fpts"] is None and games[4]["opp"] == "DDD"  # future week: no stat line yet
    assert games[1]["inj"] is None  # no injuries table passed in -> no crash, just no injury column


def test_injury_report_shows_up_as_a_short_label_per_week():
    """The real report text (status + body part) should show up next to the week it happened — and
    disappear once he is no longer listed (that disappearance IS the signal that a long absence is
    finally over)."""
    active, log, schedules, player_stats = _frames()
    injuries = pl.DataFrame(
        {
            "season": [2026, 2026, 2026], "week": [1, 2, 4], "game_type": ["REG"] * 3, "gsis_id": ["p1"] * 3,
            "report_status": ["Questionable", "Out", None],
            "report_primary_injury": ["Hamstring", "Hamstring", None],
            "practice_status": ["Limited Participation in Practice", "Did Not Participate In Practice", "Full Participation in Practice"],
            "practice_primary_injury": ["Hamstring", "Hamstring", None],
        }
    )
    out = build_weekly_log(active, log, schedules, player_stats, (2026, 3), injuries=injuries)
    games = {g["wk"]: g for g in out["100"]}
    assert games[1]["inj"] == "Questionable - Hamstring"
    assert games[2]["inj"] == "Out - Hamstring"
    assert games[4]["inj"] is None  # fully off the report by week 4: he has recovered
