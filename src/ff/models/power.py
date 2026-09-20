"""Team power rankings from player values (same logic the website runs in the browser).

A team's power is the expected weekly score of its best lineup plus the value of its depth:

  * **Expected contribution** of a player = replacement + availability x (PPG - replacement): when he is out,
    a waiver replacement plays, so absence costs (PPG - replacement), not his whole score.
  * **Lineup**: positional slots are filled first by the best players, then flex slots (FLEX, SUPER_FLEX, ...)
    by the best remaining eligible players. K and DEF are slots too. An empty slot gets replacement level.
  * **Depth** is not a flat bonus. A bench player only plays when one of the starters he can replace is out,
    so his worth is P(at least r of those starters are out) x (his PPG - replacement), where r is his rank among
    bench players of the same position. A team whose starters are healthy needs less depth; a thin team with
    two elite RBs is not punished for having no third RB.
  * **IR** players do not use bench spots (they are shown separately) and **taxi** players are ignored.

``values`` is the ``players`` dict of ``site/data/player_values.json`` (keyed by Sleeper id), optionally after
``values_for_league`` has re-derived value for the league's replacement levels and the user's adjustments.
"""
from __future__ import annotations

import argparse
import json

import polars as pl

from ff.config import ROOT
from ff.league import SLOT_ELIGIBLE, League


# --------------------------------------------------------------------------- league-specific values
def replacement_levels(pools: dict[str, list[float]], league: League) -> dict[str, float]:
    """PPG of the player at the league's replacement rank (see ``League.replacement_rank``) for each position."""
    out = {}
    for pos, vals in pools.items():
        if vals:
            out[pos] = vals[min(league.replacement_rank(pos), len(vals)) - 1]
    return out


def values_for_league(
    players: dict[str, dict],
    replacement: dict[str, float],
    adjust: dict[str, float] | None = None,
    team_adjust: dict[str, float] | None = None,
) -> dict[str, dict]:
    """Re-derive ppg / value with the user's adjustments (ppg delta per player, % per team) and the league's
    replacement levels. Returns new dicts; the input is not modified."""
    adjust, team_adjust = adjust or {}, team_adjust or {}
    out = {}
    for pid, p in players.items():
        ppg = max(0.0, (p["ppg"] + adjust.get(pid, 0.0)) * (1 + team_adjust.get(p["team"], 0.0) / 100))
        repl = replacement.get(p["pos"], 0.0)
        out[pid] = {**p, "ppg": ppg, "value": max(0.0, (ppg - repl) * p["exp_games"])}
    return out


# --------------------------------------------------------------------------- lineup
def eligible(slot: str) -> tuple[str, ...]:
    return SLOT_ELIGIBLE[slot]


def p_at_least(k: int, probs: list[float]) -> float:
    """P(at least k of the independent events happen), events with the given probabilities."""
    if k <= 0:
        return 1.0
    if k > len(probs):
        return 0.0
    dist = [1.0] + [0.0] * len(probs)  # dist[j] = P(exactly j events so far)
    for p in probs:
        for j in range(len(dist) - 1, -1, -1):
            dist[j] = dist[j] * (1 - p) + (dist[j - 1] * p if j else 0.0)
    return sum(dist[k:])


def _ev(p: dict, repl: dict[str, float]) -> float:
    r = repl.get(p["pos"], 0.0)
    return r + p["avail"] * (p["ppg"] - r)


def team_power(
    ids: list[str],
    values: dict[str, dict],
    replacement: dict[str, float],
    league: League,
    ir_ids: list[str] | tuple[str, ...] = (),
    taxi_ids: list[str] | tuple[str, ...] = (),
) -> dict:
    ir, taxi = set(ir_ids), set(taxi_ids)
    pool = [{"id": i, **values[i]} for i in ids if i in values and i not in taxi]  # unknown ids (IDP, ...) ignored
    remaining = sorted(pool, key=lambda p: -_ev(p, replacement))  # stable: ties keep roster order

    slots = list(league.slots)
    order = sorted(range(len(slots)), key=lambda i: (len(eligible(slots[i])), i))  # restrictive slots first
    chosen: list[dict | None] = [None] * len(slots)
    for i in order:
        pick = next((p for p in remaining if p["pos"] in eligible(slots[i])), None)
        if pick is not None:
            remaining.remove(pick)
            chosen[i] = pick

    lineup = 0.0
    for slot, p in zip(slots, chosen):
        lineup += _ev(p, replacement) if p else max((replacement.get(x, 0.0) for x in eligible(slot)), default=0.0)

    bench = sorted((p for p in remaining if p["id"] not in ir), key=lambda p: -(p["ppg"] - replacement.get(p["pos"], 0.0)))
    bench = bench[: league.bench]
    depth, seen = 0.0, {}
    for b in bench:
        outs = [1 - s["avail"] for slot, s in zip(slots, chosen) if s is not None and b["pos"] in eligible(slot)]
        seen[b["pos"]] = seen.get(b["pos"], 0) + 1
        gain = max(b["ppg"] - replacement.get(b["pos"], 0.0), 0.0) * b["avail"]
        depth += p_at_least(seen[b["pos"]], outs) * gain

    best = max(pool, key=lambda p: p["value"], default=None)
    return {
        "starters_ppg": round(lineup, 1),
        "depth": round(depth, 1),
        "power": round(lineup + depth, 1),
        "top_player": best["name"] if best else None,
        "lineup": [(s, p["id"] if p else None) for s, p in zip(slots, chosen)],
        "bench": [b["id"] for b in bench],
        "ir": [p["id"] for p in pool if p["id"] in ir],
    }


def power_rankings(
    rosters: dict[str, dict],
    values: dict[str, dict],
    replacement: dict[str, float],
    league: League | None = None,
) -> pl.DataFrame:
    """``rosters``: {team: {"players": [...], "reserve": [...], "taxi": [...]}} (reserve/taxi optional)."""
    league = league or League()
    rows = []
    for team, r in rosters.items():
        r = r if isinstance(r, dict) else {"players": r}
        t = team_power(r["players"], values, replacement, league, r.get("reserve", ()), r.get("taxi", ()))
        rows.append({"team": team, **{k: t[k] for k in ("power", "starters_ppg", "depth", "top_player")}})
    return (
        pl.DataFrame(rows)
        .sort("power", descending=True)
        .with_columns(rank=pl.int_range(1, len(rows) + 1))
        .select("rank", "team", "power", "starters_ppg", "depth", "top_player")
    )


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Rank teams from a {team: {players, reserve, taxi}} JSON file.")
    p.add_argument("rosters", help='JSON: {"Team A": {"players": ["4034", ...], "reserve": [...]}, ...}')
    p.add_argument("--values", default=str(ROOT / "site" / "data" / "player_values.json"))
    p.add_argument("--teams", type=int, default=None, help="league size (default: the file's default league)")
    a = p.parse_args(argv)
    data = json.load(open(a.values, encoding="utf-8"))
    d = data["default_league"]
    league = League(teams=a.teams or d["teams"], slots=tuple(d["slots"]), bench=d["bench"], ir=d["ir"])
    repl = replacement_levels(data["pools"], league)
    values = values_for_league(data["players"], repl)
    print(power_rankings(json.load(open(a.rosters, encoding="utf-8")), values, repl, league))


if __name__ == "__main__":
    main()
