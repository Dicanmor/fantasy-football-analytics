import pytest

from ff.league import League
from ff.models.trade import evaluate_trade

NO_FLEX = League(slots=("QB", "RB", "RB", "WR", "WR", "TE", "K", "DEF"))
REPL = {"QB": 17.0, "RB": 9.0, "WR": 10.0, "TE": 9.0, "K": 8.0, "DEF": 6.0}


def _p(pos, ppg, value, name="x"):
    return {"pos": pos, "ppg": ppg, "avail": 1.0, "value": value, "name": name, "team": "AAA", "exp_games": 10}


VALUES = {
    "rb_star": _p("RB", 20, 150), "rb_mid": _p("RB", 14, 60), "rb_bench": _p("RB", 11, 20),
    "wr1": _p("WR", 15, 70), "wr2": _p("WR", 13, 40),
}


def test_even_swap_is_fair_and_validates_rosters():
    r = evaluate_trade(["rb_mid"], ["wr1"], ["rb_mid"], ["wr1"], {**VALUES, "wr1": _p("WR", 15, 62)}, REPL, NO_FLEX)
    assert r["verdict"] == "fair"
    with pytest.raises(ValueError):
        evaluate_trade(["rb_mid"], ["wr1"], ["rb_star"], ["wr1"], VALUES, REPL, NO_FLEX)  # not on my roster
    with pytest.raises(ValueError):
        evaluate_trade(["rb_mid"], ["wr1"], ["rb_mid"], ["wr2"], VALUES, REPL, NO_FLEX)   # not on theirs


def test_lopsided_trade_favors_the_side_getting_more_value():
    r = evaluate_trade(["rb_bench"], ["rb_star"], ["rb_bench"], ["rb_star"], VALUES, REPL, NO_FLEX)
    assert r["verdict"] == "favors you" and r["give_value"] == 20 and r["get_value"] == 150


def test_power_delta_is_roster_aware():
    # I already have two good RBs, so a 3rd RB is worth much less to me than his raw value suggests.
    mine = ["rb_star", "rb_mid", "wr1", "wr2"]
    theirs = ["rb_bench"]  # they have only a weak RB
    r = evaluate_trade(mine, theirs, ["rb_mid"], ["rb_bench"], VALUES, REPL, NO_FLEX)
    assert r["you"]["delta_power"] < 0 and r["them"]["delta_power"] > 0
    # two-for-one: I give two players, so the partner's bench absorbs depth
    r2 = evaluate_trade(mine, ["wr1x"], ["wr2"], [], {**VALUES, "wr1x": _p("WR", 12, 30)}, REPL, NO_FLEX)
    assert r2["give_value"] == 40 and r2["get_value"] == 0
