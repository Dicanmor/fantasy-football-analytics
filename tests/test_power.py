import pytest

from ff.league import League
from ff.models.power import power_rankings, team_power

REPL = {"QB": 17.0, "RB": 9.0, "WR": 10.0, "TE": 9.0}


def _p(pos, exp_ppg, value=None, name="x"):
    return {"pos": pos, "exp_ppg": exp_ppg, "value": exp_ppg if value is None else value, "name": name}


def test_best_players_start_and_empty_slot_gets_replacement():
    values = {
        "q": _p("QB", 20), "r1": _p("RB", 18), "r2": _p("RB", 15), "r3": _p("RB", 12),
        "w1": _p("WR", 14), "w2": _p("WR", 13),
    }  # no TE on the roster
    out = team_power(list(values), values, REPL, League(), bench_weight=0.0)
    # QB 20 + RB 18 + RB 15 + WR 14 + WR 13 + TE replacement 9
    assert out["starters_ppg"] == pytest.approx(89.0)


def test_no_flex_third_rb_only_counts_as_depth():
    values = {"r1": _p("RB", 18), "r2": _p("RB", 15), "r3": _p("RB", 12)}
    out = team_power(list(values), values, REPL, League(), bench_weight=0.3)
    assert out["depth"] == pytest.approx(12 - 9)  # only the 3 points above replacement
    assert out["power"] == pytest.approx(out["starters_ppg"] + 0.3 * 3)


def test_unknown_ids_are_ignored():
    values = {"q": _p("QB", 20)}
    assert team_power(["q", "kicker-123"], values, REPL, League())["top_player"] == "x"


def test_rankings_sorted_by_power():
    values = {"a": _p("RB", 20), "b": _p("RB", 10)}
    df = power_rankings({"weak": ["b"], "strong": ["a"]}, values, REPL, League())
    assert df["team"].to_list() == ["strong", "weak"] and df["rank"].to_list() == [1, 2]
