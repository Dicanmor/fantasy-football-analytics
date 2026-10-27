import polars as pl

from ff.models.ros import build_ros, fit_ratings, overall_sos, position_sos, win_prob

TEAMS = ["AAA", "BBB", "CCC", "DDD"]


def sched():
    rows = []
    # week 1-2 played: AAA is a strong team (favoured by 7 at home, 4 away), DDD weak
    def g(week, home, away, spread, result):
        rows.append(dict(season=2026, game_type="REG", week=week, home_team=home, away_team=away,
                         spread_line=spread, result=result, home_score=None if result is None else 20 + max(result, 0)))
    g(1, "AAA", "DDD", 9.0, 10); g(1, "BBB", "CCC", 1.0, 0)
    g(2, "CCC", "AAA", -6.0, -5); g(2, "DDD", "BBB", -4.0, -3)
    g(3, "AAA", "BBB", 5.0, None); g(3, "CCC", "DDD", 4.0, None)   # remaining
    g(4, "DDD", "AAA", -9.0, None); g(4, "BBB", "CCC", 1.0, None)  # remaining
    return pl.DataFrame(rows, schema_overrides={"result": pl.Int64, "home_score": pl.Int64})


def test_win_prob_is_a_probability_centered_on_half():
    assert win_prob(0) == 0.5 and 0.5 < win_prob(3) < win_prob(10) < 1


def test_ratings_rank_the_team_the_market_likes():
    r, hfa = fit_ratings(sched(), 2026)
    assert r["AAA"] > r["BBB"] > r["DDD"] and r["AAA"] > r["CCC"]
    assert 0.5 <= hfa <= 3.5


def test_overall_sos_only_counts_unplayed_games_and_orders_hard_first():
    o = overall_sos(sched(), 2026)
    assert set(o["n"].to_list()) == {2}
    # DDD faces AAA twice (one on the road): the hardest schedule; AAA faces weak teams: the easiest
    assert o["team"][0] == "DDD" and o["team"][-1] == "AAA"
    assert 40 < o["v"].min() < o["v"].max() < 60


def test_position_sos_positive_means_harder_defenses():
    ratings = pl.DataFrame({"defense": TEAMS * 1, "metric": ["pts_QB"] * 4, "score": [0.8, 1.0, 1.1, 1.3]})
    p = position_sos(sched(), ratings, 2026, "QB")
    got = dict(zip(p["team"], p["v"]))
    # AAA plays BBB (1.0) and DDD (1.3): avg 1.15 -> -15% (easy); DDD plays CCC (1.1) and AAA (0.8): avg .95 -> +5% (hard)
    assert abs(got["AAA"] + 15) < 1e-6 and abs(got["DDD"] - 5) < 1e-6
    assert p["team"][0] == max(got, key=got.get)


def test_build_ros_shape():
    ratings = pl.DataFrame({"defense": TEAMS * 4, "metric": ["pts_QB"] * 4 + ["pts_RB"] * 4 + ["pts_WR"] * 4 + ["pts_TE"] * 4, "score": [1.0] * 16})
    d = build_ros(sched(), ratings, (2026, 3))
    assert set(d) == {"as_of", "overall", "QB", "RB", "WR", "TE"} and d["QB"]["AAA"] == {"v": 0.0, "n": 2}
