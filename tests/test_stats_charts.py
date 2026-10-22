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
        "yardline_100": [15.0, 60.0, 5.0, 40.0, 10.0, 30.0, 50.0, 10.0],
        "complete_pass": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 1.0, 1.0],
        "yards_gained": [12.0, 0.0, 3.0, 4.0, 8.0, 2.0, 9.0, 99.0],
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
    assert w1["pos"] == "WR" and next(r for r in s["rec"] if r["id"] == "w2")["pos"] == "TE"
    # AAA threw 3 passes in weeks 1-2 (rows 0, 1, 4): w1 got all 3 -> 100% share; 2 of them were in the red zone (15, 10 yds out), and so were the team's 2
    assert w1["ypt"] == pytest.approx((12 + 0 + 8) / 3, abs=0.01) and w1["tgt_share"] == pytest.approx(1.0)
    assert w1["rz_share"] == pytest.approx(1.0)
    aaa = next(t for t in s["team"] if t["team"] == "AAA")
    assert aaa["off_pass"] == pytest.approx(1 / 3, abs=1e-3) and aaa["def_pass"] == pytest.approx(0.2)


def test_export_logos_saves_files_skips_existing_and_survives_failed_downloads(tmp_path):
    from ff.models.stats_charts import export_logos

    def fetch(url):
        if "bad" in url:
            raise OSError("boom")
        return b"PNG"

    d = tmp_path / "logos"
    assert export_logos({"KC": "https://x/kc.png", "BAD": "https://x/bad.png"}, d, fetch=fetch) == 1
    assert (d / "KC.png").read_bytes() == b"PNG" and not (d / "BAD.png").exists()
    assert export_logos({"KC": "https://x/kc.png"}, d, fetch=fetch) == 0  # already there


def test_xfp_and_separation_tables_use_completed_weeks_and_wr_te_only():
    opp = pl.DataFrame({
        "season": ["2026"] * 4, "week": [1.0, 2.0, 6.0, 1.0], "player_id": ["w1", "w1", "w1", "q1"], "full_name": ["W One"] * 3 + ["Q One"],
        "position": ["WR", "WR", "WR", "QB"], "posteam": ["AAA"] * 4, "rec_attempt": [5.0, 7.0, 9.0, 0.0],
        "total_fantasy_points_exp": [10.0, 12.0, 99.0, 20.0], "total_fantasy_points": [8.0, 20.0, 99.0, 25.0],
    })
    ngs = pl.DataFrame({
        "season": [2026, 2026, 2026], "season_type": ["REG"] * 3, "week": [0, 3, 0], "player_gsis_id": ["w1", "w1", "q1"],
        "player_display_name": ["W One", "W One", "Q One"], "team_abbr": ["AAA"] * 3, "player_position": ["WR", "WR", "QB"],
        "targets": [30, 9, 1], "avg_separation": [3.1, 9.9, 1.0], "avg_yac_above_expectation": [0.5, 9.9, 0.0],
    })
    s = build_stats(_pbp(), _players(), (2026, 3), opportunity=opp, ngs=ngs)
    assert s["xfp"] == [{"id": "w1", "name": "W One", "team": "AAA", "n": 12, "pos": "WR", "xfp": 22.0, "fp": 28.0}]
    assert s["sep"] == [{"id": "w1", "name": "W One", "team": "AAA", "n": 30, "pos": "WR", "sep": 3.1, "yacoe": 0.5}]
    assert build_stats(_pbp(), _players(), (2026, 3))["xfp"] == []  # optional inputs
