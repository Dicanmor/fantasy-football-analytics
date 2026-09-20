"""Trade evaluation: raw value exchanged plus the effect on BOTH teams' power score.

Two lenses, because they answer different questions:

  * value (points over replacement, rest of season): how much each side gives up / receives, ignoring who needs
    what. A high-value RB is worth the same to every team.
  * power delta: how each team's best lineup (flex included) and depth change. A third RB adds almost nothing to a
    team that already starts two good ones; a QB upgrade matters more than its raw value suggests.

The website runs the same logic in JavaScript (``site/js/core.js``); ``tests/test_js_parity.py`` checks that both
agree on real data.
"""
from __future__ import annotations

from ff.league import League
from ff.models.power import team_power

FAIR_TOLERANCE = 0.10  # values within 10% of each other count as an even swap


def _side(ids, out, incoming, incoming_ir, ir, taxi, values, repl, league) -> dict:
    before = team_power(ids, values, repl, league, ir, taxi)
    after_ids = [i for i in ids if i not in out] + list(incoming)
    # an incoming player keeps his IR tag if this team still has an IR slot for him
    after_ir = ([i for i in ir if i not in out] + [i for i in incoming if i in incoming_ir])[: league.ir]
    after = team_power(after_ids, values, repl, league, after_ir, [i for i in taxi if i not in out])
    return {
        "before": before["power"],
        "after": after["power"],
        "delta_power": round(after["power"] - before["power"], 1),
        "delta_starters": round(after["starters_ppg"] - before["starters_ppg"], 1),
        "delta_depth": round(after["depth"] - before["depth"], 1),
    }


def evaluate_trade(
    my_ids: list[str],
    their_ids: list[str],
    give: list[str],
    get: list[str],
    values: dict[str, dict],
    replacement_ppg: dict[str, float],
    league: League | None = None,
    my_ir: list[str] = (),
    their_ir: list[str] = (),
    my_taxi: list[str] = (),
    their_taxi: list[str] = (),
) -> dict:
    """``give`` must be on my roster and ``get`` on theirs (Sleeper player ids)."""
    league = league or League()
    if not set(give) <= set(my_ids):
        raise ValueError("Every player you give must be on your roster")
    if not set(get) <= set(their_ids):
        raise ValueError("Every player you get must be on the partner's roster")

    give_value = sum(values[i]["value"] for i in give if i in values)
    get_value = sum(values[i]["value"] for i in get if i in values)
    top = max(give_value, get_value)
    fairness = (get_value - give_value) / top if top > 0 else 0.0
    if abs(fairness) <= FAIR_TOLERANCE:
        verdict = "fair"
    else:
        verdict = "favors you" if fairness > 0 else "favors them"

    return {
        "give_value": round(give_value, 1),
        "get_value": round(get_value, 1),
        "fairness": round(fairness, 3),
        "verdict": verdict,
        "you": _side(my_ids, give, get, set(their_ir), my_ir, my_taxi, values, replacement_ppg, league),
        "them": _side(their_ids, get, give, set(my_ir), their_ir, their_taxi, values, replacement_ppg, league),
    }
