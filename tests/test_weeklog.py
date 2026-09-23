import polars as pl

from ff.models.weeklog import build_weekly_log


def test_past_weeks_get_actuals_future_weeks_get_none_and_bye_has_no_opponent():
    active = pl.DataFrame(
        {"gsis_id": ["p1"], "sleeper_id": ["100"], "full_name": ["Test Player"], "position": ["RB"], "team": ["AAA"]}
    )
    log = pl.DataFrame({"gsis_id": ["p1", "p1"], "season": [2026, 2026], "week": [1, 2], "offense_pct": [0.8, 0.7], "ppr": [20.0, 15.0]})
    import ff.models.weeklog as wl

    orig = wl._opponent_by_team_week
    wl._opponent_by_team_week = lambda schedules, season: pl.DataFrame(
        {"week": [1, 2, 4], "team": ["AAA", "AAA", "AAA"], "opp": ["BBB", "CCC", "DDD"]}  # week 3 is a bye
    )
    try:
        out = build_weekly_log(active, log, (2026, 3))
    finally:
        wl._opponent_by_team_week = orig

    games = {g["wk"]: g for g in out["100"]}
    assert games[1]["fpts"] == 20.0 and games[1]["opp"] == "BBB"
    assert games[3]["opp"] is None  # bye week, not in the schedule join
    assert games[4]["fpts"] is None and games[4]["opp"] == "DDD"  # future week: no stat line yet
