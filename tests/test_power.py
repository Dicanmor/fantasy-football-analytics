import pytest

from ff.league import League
from ff.models.power import p_at_least, power_rankings, replacement_levels, team_power, values_for_league

REPL = {"QB": 17.0, "RB": 9.0, "WR": 10.0, "TE": 9.0, "K": 8.0, "DEF": 6.0}
NO_FLEX = League(slots=("QB", "RB", "RB", "WR", "WR", "TE", "K", "DEF"))


def _p(pos, ppg, avail=1.0, value=None, name="x"):
    return {"pos": pos, "ppg": ppg, "avail": avail, "value": (ppg - 8) * 10 if value is None else value,
            "name": name, "team": "AAA", "exp_games": 10}


def test_p_at_least():
    assert p_at_least(1, [0.5, 0.5]) == pytest.approx(0.75)
    assert p_at_least(2, [0.5, 0.5]) == pytest.approx(0.25)
    assert p_at_least(3, [0.5, 0.5]) == 0.0 and p_at_least(0, []) == 1.0


def test_best_players_fill_slots_and_empty_slot_gets_replacement():
    values = {"q": _p("QB", 20), "r1": _p("RB", 18), "r2": _p("RB", 15), "w1": _p("WR", 14), "w2": _p("WR", 13)}
    out = team_power(list(values), values, REPL, NO_FLEX)
    # QB 20 + RB 18 + 15 + WR 14 + 13 + empty TE 9 + empty K 8 + empty DEF 6
    assert out["starters_ppg"] == pytest.approx(103.0)


def test_flex_takes_the_best_leftover_and_bench_is_not_cut():
    values = {f"r{i}": _p("RB", 20 - i) for i in range(5)}  # 5 RBs: 2 start, 1 in FLEX, 2 on the bench
    out = team_power(list(values), values, REPL, League())
    assert [p for s, p in out["lineup"] if s == "FLEX"] == ["r2"]
    assert out["bench"] == ["r3", "r4"]  # the flex player did NOT push a bench player out of the roster


def test_ir_and_taxi_do_not_take_bench_spots():
    values = {"bench1": _p("WR", 12), "ir_star": _p("RB", 19, avail=0.5), "taxi": _p("RB", 15)}
    out = team_power(list(values), values, REPL, League(bench=1), ir_ids=["ir_star"], taxi_ids=["taxi"])
    assert "taxi" not in out["bench"] and "taxi" not in [p for _, p in out["lineup"]]
    assert "ir_star" not in out["bench"] and "ir_star" in out["ir"]


def test_depth_is_worth_more_when_starters_are_fragile():
    def depth(starter_avail):
        values = {"r1": _p("RB", 18, starter_avail), "r2": _p("RB", 17, starter_avail), "b": _p("RB", 13)}
        return team_power(list(values), values, REPL, NO_FLEX)["depth"]
    assert depth(0.6) > depth(0.95) > 0


def test_thin_team_with_two_elite_rbs_is_not_ranked_last():
    stars = {"r1": _p("RB", 22), "r2": _p("RB", 21), "q": _p("QB", 22), "w1": _p("WR", 15), "w2": _p("WR", 14), "t": _p("TE", 13)}
    deep_avg = {f"x{i}": _p("RB", 12) for i in range(3)} | {f"y{i}": _p("WR", 12) for i in range(3)} | {"q2": _p("QB", 19), "t2": _p("TE", 10)}
    df = power_rankings({"thin_stars": list(stars), "deep_average": list(deep_avg)}, {**stars, **deep_avg}, REPL, League())
    assert df["team"][0] == "thin_stars"


def test_replacement_and_adjustments():
    pools = {"RB": [20, 18, 16, 14, 12, 10, 8, 6, 4], "QB": [25, 20]}
    repl = replacement_levels(pools, League(teams=1, slots=("QB", "RB", "RB", "FLEX")))
    assert repl["RB"] == 14  # rank round(1 x (2 starters + 0.45 flex share + 1.8 bench share)) = 4 -> 4th best PPG
    players = {"a": {"ppg": 12.0, "exp_games": 10, "pos": "RB", "team": "AAA"}}
    v = values_for_league(players, {"RB": 9.0}, adjust={"a": 2.0}, team_adjust={"AAA": 10})
    assert v["a"]["ppg"] == pytest.approx(15.4) and v["a"]["value"] == pytest.approx((15.4 - 9) * 10)
    assert players["a"]["ppg"] == 12.0  # input untouched
