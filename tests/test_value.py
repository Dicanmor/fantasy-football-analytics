import polars as pl
import pytest

from ff.league import League
from ff.models.value import expected_remaining_absence, load_adjustments, project_ppg, remaining_games


def test_season_boost_ramps_up_over_a_players_first_games_this_season():
    """A single current-season game should not get the full boost (it swung real projections wildly:
    one bad Week 1 game at full 3x weight briefly dropped a normally-strong veteran QB below replacement)."""
    log = pl.DataFrame(
        {
            "gsis_id": ["p"] * 5, "season": [2025, 2025, 2025, 2026, 2026], "week": [10, 14, 18, 1, 2],
            "key": [202510, 202514, 202518, 202601, 202602], "ppr": [20.0, 20.0, 20.0, 0.0, 20.0],
            "offense_pct": [1.0] * 5,
        }
    )
    directory = pl.DataFrame({"gsis_id": ["p"]})
    active = pl.DataFrame({"gsis_id": ["p"], "position": ["QB"]})
    league = League(teams=1, slots=("QB",))
    ramped, _ = project_ppg(log, directory, active, league, (2026, 3), boost_ramp_games=3)
    full, _ = project_ppg(log, directory, active, league, (2026, 3), boost_ramp_games=0)
    # with the ramp, one bad game (week 1) counts less than with the old always-full boost
    assert ramped["raw_ppg"][0] > full["raw_ppg"][0]


def test_replacement_rank_depends_on_league_size_and_flex():
    assert League(teams=12).replacement_rank("RB") == round(12 * (2 + 0.45 + 1.8))   # default has 1 FLEX
    no_flex = League(teams=12, slots=("QB", "RB", "RB", "WR", "WR", "TE", "K", "DEF"))
    assert no_flex.replacement_rank("RB") == round(12 * (2 + 1.8))
    assert League(teams=10).replacement_rank("QB") == round(10 * (1 + 0.4))
    assert League(teams=10).replacement_rank("K") == round(10 * 1.5)


def test_remaining_games_skips_the_bye():
    sched = pl.DataFrame({"season": [2025] * 4, "week": [2, 4, 5, 1], "game_type": ["REG"] * 4,
                          "home_team": ["AAA"] * 4, "away_team": ["ZZZ"] * 4})
    r = remaining_games(sched, (2025, 2))
    assert r.filter(pl.col("team") == "AAA")["remaining"].item() == 3  # weeks 2, 4, 5 (week 3 = bye, week 1 played)


def _log(rows):
    return pl.DataFrame(rows, orient="row", schema={"gsis_id": pl.String, "season": pl.Int32, "week": pl.Int32,
                                      "offense_pct": pl.Float64, "ppr": pl.Float64}).with_columns(
        key=pl.col("season") * 100 + pl.col("week"))


def test_projection_replacement_shrinkage_and_no_leakage():
    ppgs = [20, 18, 16, 14, 12, 10, 8, 6]
    rows = [(f"p{i}", 2025, w, 0.7, float(v)) for i, v in enumerate(ppgs) for w in range(1, 9)]
    rows.append(("p0", 2025, 9, 0.7, 500.0))  # the as-of week itself: must be ignored (no leakage)
    active = pl.DataFrame({"gsis_id": [f"p{i}" for i in range(8)] + ["rookie"], "position": ["RB"] * 9})
    proj, repl = project_ppg(_log(rows), pl.DataFrame(), active, League(teams=1), (2025, 9))

    assert repl["RB"] == pytest.approx(14.0)   # RB rank round(1 * (2 + 1.8)) = 4 -> 4th best raw PPG
    p0 = proj.filter(pl.col("gsis_id") == "p0").row(0, named=True)
    assert p0["raw_ppg"] == pytest.approx(20.0)                # week 9 (500 pts) not used
    assert 14.0 < p0["proj_ppg"] < 20.0                        # shrunk toward replacement, not equal to raw
    rookie = proj.filter(pl.col("gsis_id") == "rookie").row(0, named=True)
    assert rookie["proj_ppg"] == pytest.approx(14.0)           # no evidence -> replacement level


def test_low_snap_games_are_ignored():
    rows = [("a", 2025, w, 0.7, 10.0) for w in range(1, 8)] + [("a", 2025, 8, 0.05, 0.0)]  # got hurt early
    active = pl.DataFrame({"gsis_id": ["a"], "position": ["RB"]})
    proj, _ = project_ppg(_log(rows), pl.DataFrame(), active, League(teams=1), (2025, 9))
    assert proj["raw_ppg"].item() == pytest.approx(10.0)


def test_expected_remaining_absence():
    eps = pl.DataFrame({"season": [2024] * 4, "games_missed": [1, 1, 2, 5], "ir_games": [0, 0, 0, 0],
                        "starts_on_ir": [False] * 4})
    # k=2: episodes with >=2 games are [2, 5] -> games still to miss = (2-2+1, 5-2+1) = (1, 4) -> 2.5
    assert expected_remaining_absence(eps, 2, 2026) == pytest.approx(2.5)


def test_manual_adjustments_file(tmp_path):
    f = tmp_path / "adj.csv"
    f.write_text("gsis_id,name,ppg_delta,note\n,Foo Bar,1.5,rookie usage\n00-1,,-2,\n")
    a = load_adjustments(f)
    assert a["ppg_delta"].to_list() == [1.5, -2.0]
    assert load_adjustments(tmp_path / "missing.csv").height == 0
