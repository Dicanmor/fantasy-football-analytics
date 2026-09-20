import polars as pl
import pytest

from ff.features.defense import build_game_metrics, matchup_ratings, weekly_guide


def _row(season, week, defense, position, ppr, passing=0, rushing=0, season_type="REG"):
    return dict(season=season, week=week, season_type=season_type, opponent_team=defense,
                position=position, fantasy_points=ppr, fantasy_points_ppr=ppr,
                passing_yards=passing, rushing_yards=rushing)


def _metric(df, defense, metric):
    return df.filter((pl.col("defense") == defense) & (pl.col("metric") == metric))


def test_missing_position_counts_as_zero():
    stats = pl.DataFrame([_row(2025, 1, "AAA", "QB", 20, passing=250)])
    m = build_game_metrics(stats)
    assert _metric(m, "AAA", "pts_TE")["value"].to_list() == [0.0]
    assert _metric(m, "AAA", "pass_yds")["value"].to_list() == [250.0]


def test_playoffs_are_excluded():
    stats = pl.DataFrame([_row(2025, 1, "AAA", "QB", 20), _row(2025, 19, "AAA", "QB", 99, season_type="POST")])
    assert build_game_metrics(stats)["week"].unique().to_list() == [1]


def test_no_leakage_and_recency_weighting():
    rows = []
    for d, vals in {"AAA": (10, 20, 999), "BBB": (10, 10, 10)}.items():
        for wk, v in enumerate(vals, start=1):
            rows.append(_row(2025, wk, d, "QB", v))
    m = build_game_metrics(pl.DataFrame(rows))

    r = matchup_ratings(m, as_of=(2025, 3), half_life=1.0)  # week 3 (the 999) must not be used
    aaa = _metric(r.rename({"defense": "defense"}), "AAA", "pts_QB")
    assert aaa["games"].item() == 2
    # weights: week 2 -> 1.0, week 1 -> 0.5  =>  (20*1 + 10*0.5) / 1.5
    assert aaa["allowed_per_game"].item() == pytest.approx(25 / 1.5)


def test_rank_one_is_the_easiest_matchup():
    rows = [_row(2025, 1, "EASY", "RB", 30), _row(2025, 1, "HARD", "RB", 10)]
    r = matchup_ratings(build_game_metrics(pl.DataFrame(rows)), as_of=(2025, 2))
    easy = _metric(r, "EASY", "pts_RB")
    assert easy["rank"].item() == 1
    assert easy["score"].item() > 1


def test_seasons_back_drops_old_seasons():
    rows = [_row(2023, 1, "AAA", "QB", 50), _row(2025, 1, "AAA", "QB", 10)]
    m = build_game_metrics(pl.DataFrame(rows))
    r = matchup_ratings(m, as_of=(2025, 5), seasons_back=2)
    assert _metric(r, "AAA", "pts_QB")["games"].item() == 1  # 2023 is outside [2024, 2025]


def test_weekly_guide_has_both_sides_of_each_game():
    rows = [_row(2025, 1, d, "QB", 10) for d in ("AAA", "BBB")]
    ratings = matchup_ratings(build_game_metrics(pl.DataFrame(rows)), as_of=(2025, 2))
    sched = pl.DataFrame({"season": [2025], "week": [2], "game_type": ["REG"],
                          "home_team": ["AAA"], "away_team": ["BBB"]})
    g = weekly_guide(ratings, sched, 2025, 2)
    assert sorted(g["team"].to_list()) == ["AAA", "BBB"]
    assert g.filter(pl.col("team") == "AAA")["opponent"].item() == "BBB"
    assert "pts_QB_score" in g.columns and "pts_QB_rank" in g.columns
