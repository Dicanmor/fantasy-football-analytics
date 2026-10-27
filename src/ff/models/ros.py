"""Rest-of-season (ROS) strength of schedule: how hard are the games each team still has to play?

Two views, both from data already in the pipeline (nothing scraped):

* **Overall**: average win% of the remaining opponents against a league-average team on a neutral field.
  Team ratings come from the BETTING MARKET: the closing point spreads in nflverse ``schedules`` (including next
  week's already-posted lines), blended 70/30 with the real score margins of the games played, solved jointly
  (every team's rating accounts for who it played) with a small ridge shrinkage and a fitted home-field edge.
  A rating gap becomes a win probability with a normal curve (NFL margins have sd ~ 13.5 points). The opponent's
  rating is adjusted for the site of each remaining game (a game in his stadium is harder).
* **By position (QB / WR / TE)**: average of the remaining opponents' fantasy points allowed to that position,
  relative to the league (``ff.features.defense.matchup_ratings``, recency-weighted). Shown as "% fewer points
  than average": positive = harder defenses ahead. Home/away is not adjusted here.

Pure functions over DataFrames (no ``load_raw``), so the tests run without downloaded data.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import polars as pl

MARKET_WEIGHT = 0.7      # weight of the closing spread vs the real margin for games already played
RIDGE = 1.0              # shrinks ratings toward 0 (matters early in the season)
MARGIN_SD = 13.5         # sd of an NFL game margin around the spread, in points
POSITIONS = ("QB", "WR", "TE")


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Solve a x = b (Gauss-Jordan with partial pivoting); a is small (33 x 33), so no numpy needed."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        piv = m[c][c]
        m[c] = [v / piv for v in m[c]]
        for r in range(n):
            if r != c and m[r][c]:
                f = m[r][c]
                m[r] = [v - f * w for v, w in zip(m[r], m[c])]
    return [m[i][n] for i in range(n)]


def fit_ratings(schedules: pl.DataFrame, season: int) -> tuple[dict[str, float], float]:
    """(rating in points per team, home-field edge in points) from this season's spreads and results."""
    g = schedules.filter((pl.col("season") == season) & (pl.col("game_type") == "REG") & pl.col("spread_line").is_not_null())
    teams = sorted(set(schedules.filter((pl.col("season") == season) & (pl.col("game_type") == "REG"))["home_team"].to_list()))
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams) + 1  # + home-field edge
    ata = [[0.0] * n for _ in range(n)]
    atb = [0.0] * n
    for r in g.iter_rows(named=True):
        y = float(r["spread_line"])
        if r["result"] is not None:
            y = MARKET_WEIGHT * y + (1 - MARKET_WEIGHT) * float(r["result"])
        cols = {idx[r["home_team"]]: 1.0, idx[r["away_team"]]: -1.0, n - 1: 1.0}
        for i, vi in cols.items():
            atb[i] += vi * y
            for j, vj in cols.items():
                ata[i][j] += vi * vj
    for i in range(n - 1):
        ata[i][i] += RIDGE
    sol = _solve(ata, atb) if g.height else [0.0] * n
    return {t: sol[i] for t, i in idx.items()}, min(max(sol[-1], 0.5), 3.5)


def win_prob(points_better: float) -> float:
    """P(a team that is ``points_better`` points better wins), normal margin with sd ``MARGIN_SD``."""
    return 0.5 * (1 + math.erf(points_better / (MARGIN_SD * math.sqrt(2))))


def _remaining_sides(schedules: pl.DataFrame, season: int) -> pl.DataFrame:
    """One row per (team, remaining game): opponent and whether the OPPONENT is at home. Remaining = not played yet."""
    s = schedules.filter((pl.col("season") == season) & (pl.col("game_type") == "REG") & pl.col("home_score").is_null())
    return pl.concat([
        s.select("week", team="home_team", opp="away_team", opp_home=pl.lit(False)),
        s.select("week", team="away_team", opp="home_team", opp_home=pl.lit(True)),
    ])


def overall_sos(schedules: pl.DataFrame, season: int) -> pl.DataFrame:
    """team, v (average remaining-opponent win% vs an average team on a neutral field, in %), n (games left)."""
    ratings, hfa = fit_ratings(schedules, season)
    rows = []
    for r in _remaining_sides(schedules, season).iter_rows(named=True):
        adj = ratings.get(r["opp"], 0.0) + (hfa if r["opp_home"] else -hfa)
        rows.append({"team": r["team"], "p": win_prob(adj)})
    if not rows:
        return pl.DataFrame(schema={"team": pl.String, "v": pl.Float64, "n": pl.UInt32})
    return pl.DataFrame(rows).group_by("team").agg(v=pl.col("p").mean() * 100, n=pl.len()).sort("v", descending=True)


def position_sos(schedules: pl.DataFrame, ratings: pl.DataFrame, season: int, pos: str) -> pl.DataFrame:
    """team, v (% FEWER fantasy points than the league average that the remaining opponents allow to ``pos``), n."""
    d = ratings.filter(pl.col("metric") == f"pts_{pos}").select(opp="defense", score="score")
    j = _remaining_sides(schedules, season).join(d, on="opp", how="inner")
    if not j.height:
        return pl.DataFrame(schema={"team": pl.String, "v": pl.Float64, "n": pl.UInt32})
    return j.group_by("team").agg(v=(1 - pl.col("score").mean()) * 100, n=pl.len()).sort("v", descending=True)


def build_ros(schedules: pl.DataFrame, ratings: pl.DataFrame, as_of: tuple[int, int]) -> dict:
    season = as_of[0]
    frames = {"overall": overall_sos(schedules, season), **{p: position_sos(schedules, ratings, season, p) for p in POSITIONS}}
    out: dict = {"as_of": list(as_of)}
    for k, f in frames.items():
        out[k] = {r["team"]: {"v": round(r["v"], 2), "n": int(r["n"])} for r in f.iter_rows(named=True)}
    return out


def export_ros(data: dict, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "ros.json"
    path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    return path
