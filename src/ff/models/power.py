"""Team power rankings from player values (same logic the website will run in the browser).

    power = expected weekly points of the starting lineup + bench_weight x bench depth above replacement

  * Lineup: best players by ``exp_ppg`` for each slot (QB 1, RB 2, WR 2, TE 1; no flex, so picking the best
    per position is optimal). An empty slot is filled with a replacement-level waiver player.
  * ``exp_ppg`` already includes injury risk / current injuries (PPG x expected availability).
  * Depth: bench players (best ``bench`` by value) count for how much they beat replacement, at
    ``bench_weight`` (0.3 by default) because they only matter when a starter is out or on a bye.
  * K and DST are excluded (streamable, ~zero value over replacement).

Input rosters are ``{team name: [sleeper player ids]}`` and ``values`` is the ``players`` dict of
``site/data/player_values.json`` (keyed by Sleeper id).
"""
from __future__ import annotations

import argparse
import json

import polars as pl

from ff.config import ROOT
from ff.league import League


def team_power(
    player_ids: list[str],
    values: dict[str, dict],
    replacement_ppg: dict[str, float],
    league: League,
    bench_weight: float = 0.3,
) -> dict:
    roster = [values[i] for i in player_ids if i in values]  # unknown ids (K, DST, ...) are ignored
    starters: list[dict] = []
    taken: set[int] = set()
    weekly = 0.0
    for pos, n in league.starters.items():
        pool = sorted((p for p in roster if p["pos"] == pos), key=lambda p: p["exp_ppg"], reverse=True)
        chosen = pool[:n]
        starters += chosen
        taken |= {id(p) for p in chosen}
        weekly += sum(p["exp_ppg"] for p in chosen) + (n - len(chosen)) * replacement_ppg[pos]

    bench = sorted((p for p in roster if id(p) not in taken), key=lambda p: p["value"], reverse=True)[: league.bench]
    depth = sum(max(p["exp_ppg"] - replacement_ppg[p["pos"]], 0.0) for p in bench)
    best = max(roster, key=lambda p: p["value"], default=None)
    return {
        "starters_ppg": round(weekly, 1),
        "depth": round(depth, 1),
        "power": round(weekly + bench_weight * depth, 1),
        "top_player": best["name"] if best else None,
    }


def power_rankings(
    rosters: dict[str, list[str]],
    values: dict[str, dict],
    replacement_ppg: dict[str, float],
    league: League | None = None,
    bench_weight: float = 0.3,
) -> pl.DataFrame:
    league = league or League()
    rows = [{"team": t, **team_power(ids, values, replacement_ppg, league, bench_weight)} for t, ids in rosters.items()]
    return (
        pl.DataFrame(rows)
        .sort("power", descending=True)
        .with_columns(rank=pl.int_range(1, len(rows) + 1))
        .select("rank", "team", "power", "starters_ppg", "depth", "top_player")
    )


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Rank teams from a {team: [sleeper ids]} JSON file.")
    p.add_argument("rosters", help="JSON file: {\"Team A\": [\"4034\", ...], ...}")
    p.add_argument("--values", default=str(ROOT / "site" / "data" / "player_values.json"))
    a = p.parse_args(argv)
    data = json.load(open(a.values, encoding="utf-8"))
    league = League(teams=data["league"]["teams"])
    print(power_rankings(json.load(open(a.rosters, encoding="utf-8")), data["players"], data["replacement_ppg"], league))


if __name__ == "__main__":
    main()
