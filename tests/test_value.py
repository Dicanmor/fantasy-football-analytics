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


def test_low_snap_games_count_so_backups_are_not_flattered():
    rows = [("a", 2025, w, 0.7, 10.0) for w in range(1, 8)] + [("a", 2025, 8, 0.05, 0.0)]  # a starter hurt early in week 8
    active = pl.DataFrame({"gsis_id": ["a"], "position": ["RB"]})
    proj, _ = project_ppg(_log(rows), pl.DataFrame(), active, League(teams=1), (2025, 9))
    assert proj["raw_ppg"].item() < 10.0  # the bad low-snap game is in the average (it used to be dropped)


def test_role_prior_pulls_a_part_timer_below_replacement_but_not_a_starter():
    # starters (70% of snaps) score ~15, part-timers (20%) ~4: the fitted line tells them apart
    rows = [(f"s{i}", 2025, w, 0.7, 15.0) for i in range(6) for w in range(1, 9)]
    rows += [(f"b{i}", 2025, w, 0.2, 4.0) for i in range(6) for w in range(1, 9)]
    rows += [("new_b", 2025, 8, 0.2, 14.0)]  # one lucky game, 20% of the snaps
    active = pl.DataFrame({"gsis_id": [f"s{i}" for i in range(6)] + [f"b{i}" for i in range(6)] + ["new_b"], "position": ["RB"] * 13})
    proj, repl = project_ppg(_log(rows), pl.DataFrame(), active, League(teams=1), (2025, 9))
    ppg = {r["gsis_id"]: r["proj_ppg"] for r in proj.iter_rows(named=True)}
    assert ppg["s0"] == pytest.approx(15.0, abs=0.3)
    assert ppg["new_b"] < repl["RB"] - 1   # one 14-point game at 20% of the snaps does not make him a replacement-level back


def test_expected_remaining_absence():
    eps = pl.DataFrame({"season": [2024] * 4, "games_missed": [1, 1, 2, 5], "ir_games": [0, 0, 0, 0],
                        "starts_on_ir": [False] * 4, "injury": ["X"] * 4})
    # k=2: episodes with >=2 games are [2, 5] -> games still to miss = (2-2+1, 5-2+1) = (1, 4) -> 2.5
    assert expected_remaining_absence(eps, 2, 2026) == pytest.approx(2.5)


def test_expected_remaining_absence_blends_toward_injury_specific_history():
    """A hamstring that has already cost 2 games historically clears up fast (1 more game); an unrelated
    pile of OTHER injuries lasting much longer should not drag a well-sampled hamstring estimate toward
    them, but a THIN hamstring sample (below prior_episodes) should still lean mostly on the generic rate."""
    hamstring = [("Hamstring", 2)] * 20  # 20 past hamstring episodes, every one exactly 2 games
    other = [("Knee", 6)] * 20           # 20 unrelated, much longer knee episodes
    eps = pl.DataFrame(
        {
            "season": [2024] * 40, "games_missed": [g for _, g in hamstring + other],
            "ir_games": [0] * 40, "starts_on_ir": [False] * 40,
            "injury": [i for i, _ in hamstring + other],
        }
    )
    # well-sampled hamstring history (20 episodes >> prior_episodes=8) should pull the estimate well below
    # the knee-polluted generic average (3.0), toward the hamstring-only rate (1.0): w = 20/(20+8) = 0.714
    # -> 0.714*1.0 + 0.286*3.0 = 1.57.
    out = expected_remaining_absence(eps, 2, 2026, injury="Hamstring")
    generic = expected_remaining_absence(eps, 2, 2026, injury=None)
    assert generic == pytest.approx(3.0)
    assert out == pytest.approx(1.571, abs=0.01)
    assert generic > out + 1  # the generic (knee-polluted) estimate is clearly higher

    # a single past hamstring episode (way below prior_episodes) should barely move the estimate off generic
    thin = eps.vstack(pl.DataFrame(
        {"season": [2024], "games_missed": [2], "ir_games": [0], "starts_on_ir": [False], "injury": ["ACL"]}
    ))
    thin_out = expected_remaining_absence(thin, 2, 2026, injury="ACL")
    assert thin_out == pytest.approx(generic, abs=0.5)  # 1 matching episode -> mostly the generic rate


def test_manual_adjustments_file(tmp_path):
    f = tmp_path / "adj.csv"
    f.write_text("gsis_id,name,ppg_delta,note\n,Foo Bar,1.5,rookie usage\n00-1,,-2,\n")
    a = load_adjustments(f)
    assert a["ppg_delta"].to_list() == [1.5, -2.0]
    assert load_adjustments(tmp_path / "missing.csv").height == 0
