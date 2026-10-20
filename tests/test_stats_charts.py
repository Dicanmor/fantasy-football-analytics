import polars as pl
import pytest

from ff.models.stats_charts import build_stats


def _pbp():
    # floats for flags/down-like columns, like real nflverse pbp (nulls on non-pass/rush plays)
    n = 8
    return pl.DataFrame({
        "season": [2026] * n, "week": [1, 1, 1, 1, 2, 2, 2, 6], "season_type": ["REG"] * n,
        "posteam": ["AAA", "AAA", "AAA", "AAA", "AAA", "AAA", "BBB", "AAA"],
        "defteam": ["BBB", "BBB", "BBB", "BBB", "BBB", "BBB", "AAA", "BBB"],
        "qb_dropback": [1.0, 1.0, 0.0, 0.0, 1.0, 0.0, 1.0, 1.0],
        "qb_scramble": [0.0, 0.0, None, 0.0, 0.0, None, 0.0, 0.0],
        "pass_attempt": [1.0, 1.0, 0.0, 0.0, 1.0, 0.0, 1.0, 1.0],
        "rush_attempt": [0.0, 0.0, 1.0, 1.0, 0.0, 1.0, 0.0, 0.0],
        "epa": [0.5, -0.5, 1.0, 0.0, 1.0, -1.0, 0.2, 99.0],
        "success": [1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 1.0],
        "cpoe": [10.0, -10.0, None, None, 20.0, None, 5.0, 99.0],
        "air_yards": [10.0, 20.0, None, None, 30.0, None, 8.0, 99.0],
        "passer_player_id": ["q1", "q1", None, None, "q1", None, "q2", "q1"],
        "rusher_player_id": [None, None, "r1", "r1", None, "r1", None, None],
        "receiver_player_id": ["w1", "w1", None, None, "w1", None, "w2", "w1"],
    })


def _players():
    return pl.DataFrame({"gsis_id": ["q1", "q2", "r1", "w1", "w2"], "display_name": ["Q One", "Q Two", "R One", "W One", "W Two"],
                         "position": ["QB", "QB", "RB", "WR", "TE"]})


def test_stats_use_only_completed_regular_season_weeks_before_as_of():
    s = build_stats(_pbp(), _players(), (2026, 3))  # week 6 row (epa 99) must be excluded
    q1 = next(r for r in s["qb"] if r["id"] == "q1")
    assert q1["n"] == 3 and q1["epa"] == pytest.approx((0.5 - 0.5 + 1.0) / 3, abs=1e-3)
    assert q1["cpoe"] == pytest.approx(20 / 3, abs=0.01)  # mean(10, -10, 20)
    assert s["as_of"]["through_week"] == 2


def test_rb_rec_and_team_tables():
    s = build_stats(_pbp(), _players(), (2026, 3))
    r1 = s["rb"][0]
    assert r1["name"] == "R One" and r1["n"] == 3 and r1["epa"] == pytest.approx((1.0 + 0.0 - 1.0) / 3, abs=1e-3)
    w1 = next(r for r in s["rec"] if r["id"] == "w1")
    assert w1["n"] == 3 and w1["adot"] == pytest.approx(20.0)
    assert {r["pos"] if "pos" in r else None for r in s["rec"]} == {None}  # no stray columns leak into JSON rows
    aaa = next(t for t in s["team"] if t["team"] == "AAA")
    assert aaa["off_pass"] == pytest.approx(1 / 3, abs=1e-3) and aaa["def_pass"] == pytest.approx(0.2)
