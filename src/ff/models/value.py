"""Player value model: rest-of-season points over replacement (VORP) for QB / RB / WR / TE.

    value = max(0, (projected PPG - replacement PPG) x expected games played)

Ingredients
  1. Projected PPG      recency-weighted PPR points per game over the last 3 seasons, shrunk toward the
                        replacement level (a player with no evidence is worth a replacement player), plus an
                        optional manual "ball knowledge" adjustment from ``data/manual/adjustments.csv``.
  2. Replacement level  PPG of the player ranked ``teams x (starters + bench share)`` at his position among
                        players currently on a roster. This depends on the league (see ``ff.league``).
  3. Expected games     remaining team games (byes handled) x (1 - injury-proneness), minus the expected
                        remaining absence if he is injured right now (from historical injury episodes)
                        or the historical miss rate for his Questionable/Doubtful status.

Everything is computed "as of" a (season, week) using only games played before that week, so the
projection part can be backtested (``--evaluate``).

Run::

    python -m ff.models.value --teams 12
    python -m ff.models.value --evaluate
"""
from __future__ import annotations

import argparse
import json

import polars as pl

from ff.config import PROCESSED_DIR, ROOT
from ff.data import load_raw
from ff.features.injuries import IR_STATUSES, ON_ROSTER, build_all
from ff.league import POSITIONS, League

WEEKS = 18  # week-index granularity used for recency weights
MANUAL = ROOT / "data" / "manual" / "adjustments.csv"
SITE_JSON = ROOT / "site" / "data" / "player_values.json"


# --------------------------------------------------------------------------- inputs
def current_roster(rosters_weekly: pl.DataFrame, as_of: tuple[int, int]) -> pl.DataFrame:
    """Skill players currently on a roster (latest weekly row up to ``as_of``), with sleeper_id and age."""
    season, week = as_of
    return (
        rosters_weekly.filter(
            (pl.col("season") == season) & (pl.col("game_type") == "REG") & (pl.col("week") <= week)
            & pl.col("position").is_in(POSITIONS)
        )
        .sort("week")
        .group_by("gsis_id")
        .agg(
            pl.col("full_name").last(), pl.col("position").last(), pl.col("team").last(),
            pl.col("status").last(), pl.col("sleeper_id").last(), pl.col("birth_date").last(),
            pl.col("week").max().alias("latest_week"),
        )
        .filter(pl.col("status").is_in(ON_ROSTER))
        .with_columns(age=((pl.date(season, 9, 1) - pl.col("birth_date")).dt.total_days() / 365.25).round(1))
    )


def remaining_games(schedules: pl.DataFrame, as_of: tuple[int, int]) -> pl.DataFrame:
    """Team games left in the regular season from ``as_of`` week on (byes are handled automatically)."""
    season, week = as_of
    reg = schedules.filter(
        (pl.col("season") == season) & (pl.col("game_type") == "REG") & (pl.col("week") >= week)
    )
    sides = pl.concat([reg.select(team="home_team"), reg.select(team="away_team")])
    return sides.group_by("team").agg(pl.len().alias("remaining"))


def load_adjustments(path=MANUAL) -> pl.DataFrame:
    """Manual 'ball knowledge': columns gsis_id (or name), ppg_delta, note. Missing file = no adjustments."""
    schema = {"gsis_id": pl.String, "name": pl.String, "ppg_delta": pl.Float64, "note": pl.String}
    if not path.exists():
        return pl.DataFrame(schema=schema)
    return pl.read_csv(path, schema_overrides=schema).filter(pl.col("ppg_delta").is_not_null())


# --------------------------------------------------------------------------- projection
def project_ppg(
    log: pl.DataFrame,
    directory: pl.DataFrame,
    active: pl.DataFrame,
    league: League,
    as_of: tuple[int, int],
    half_life: float = 17.0,
    min_share: float = 0.2,
    prior_k: float = 4.0,
    window_seasons: int = 3,
) -> tuple[pl.DataFrame, dict[str, float]]:
    """Projected PPG for every active player + replacement PPG per position.

    Games where the player had < ``min_share`` of the offensive snaps are ignored (the game he got hurt in,
    garbage time): they say little about his role. Weights halve every ``half_life`` weeks.
    """
    season, week = as_of
    now_t = season * WEEKS + week
    hist = (
        log.filter(
            (pl.col("key") < season * 100 + week) & (pl.col("season") > season - window_seasons)
            & (pl.col("offense_pct") >= min_share) & pl.col("ppr").is_not_null()
        )
        .join(active.select("gsis_id", "position"), on="gsis_id", how="inner")
        .with_columns(w=0.5 ** ((now_t - (pl.col("season") * WEEKS + pl.col("week"))) / half_life))
    )
    agg = (
        hist.group_by("gsis_id")
        .agg(
            pl.len().alias("games"),
            pl.col("w").sum().alias("eff_games"),
            (pl.col("w") * pl.col("ppr")).sum().alias("wp"),
        )
        .with_columns(raw_ppg=pl.col("wp") / pl.col("eff_games"))
    )
    table = active.select("gsis_id", "position").join(agg, on="gsis_id", how="left").with_columns(
        pl.col("games").fill_null(0), pl.col("eff_games").fill_null(0.0), pl.col("wp").fill_null(0.0)
    )

    replacement: dict[str, float] = {}
    for pos in POSITIONS:
        vals = (
            table.filter((pl.col("position") == pos) & (pl.col("games") >= 6))
            .sort("raw_ppg", descending=True)["raw_ppg"]
        )
        replacement[pos] = float(vals[min(league.replacement_rank(pos), len(vals)) - 1]) if len(vals) else 0.0

    repl = pl.DataFrame({"position": list(replacement), "replacement_ppg": list(replacement.values())})
    out = (
        table.join(repl, on="position")
        .with_columns(
            proj_ppg=(pl.col("wp") + prior_k * pl.col("replacement_ppg")) / (pl.col("eff_games") + prior_k)
        )
    )
    return out, replacement


# --------------------------------------------------------------------------- availability
def miss_rates(injuries: pl.DataFrame, log: pl.DataFrame, directory: pl.DataFrame, before_key: int) -> dict[str, float]:
    """P(player does not play | his game status on the injury report), from history (2013+)."""
    inj = (
        injuries.filter(pl.col("game_type") == "REG")
        .with_columns(pl.col("season").cast(pl.Int32), pl.col("week").cast(pl.Int32))
        .filter((pl.col("season") >= 2013) & (pl.col("season") * 100 + pl.col("week") < before_key))
        .join(directory.select("gsis_id"), on="gsis_id", how="semi")
        .filter(pl.col("report_status").is_in(["Questionable", "Doubtful", "Out"]))
    )
    played = log.select("gsis_id", "season", "week").with_columns(played=pl.lit(True))
    j = inj.join(played, on=["gsis_id", "season", "week"], how="left").with_columns(pl.col("played").fill_null(False))
    rows = j.group_by("report_status").agg((1 - pl.col("played").mean()).alias("p"))
    return dict(zip(rows["report_status"], rows["p"]))


def expected_remaining_absence(episodes: pl.DataFrame, k: int, before_season: int, on_ir: bool = False) -> float:
    """Games still to miss (this one included) for a player who has been out k straight games.

    Mean of (games_missed - k + 1) over past episodes that lasted at least k games. Players on injured
    reserve are compared with past episodes that involved IR; players merely listed Out are compared with
    past episodes that started on the report (many IR stints would otherwise inflate a routine 'Out').
    """
    kind = (pl.col("ir_games") >= 1) if on_ir else ~pl.col("starts_on_ir")
    past = episodes.filter((pl.col("season") < before_season) & (pl.col("games_missed") >= k) & kind)
    if past.height == 0:
        return 1.0
    return float((past["games_missed"] - k + 1).mean())


def availability(
    players: pl.DataFrame,
    tables: dict[str, pl.DataFrame],
    injuries: pl.DataFrame,
    as_of: tuple[int, int],
) -> pl.DataFrame:
    """Adds ``absence_now`` (games he is expected to miss from now) and ``base_miss_rate``."""
    season, week = as_of
    episodes, risk, log, directory = tables["episodes"], tables["risk"], tables["log"], tables["directory"]
    p_miss = miss_rates(injuries, log, directory, season * 100 + week)

    # Injury-report status for the as_of week (fall back to the previous week if not published yet).
    inj = injuries.filter((pl.col("game_type") == "REG") & (pl.col("season") == season))
    weeks = inj["week"].cast(pl.Int32).unique().sort().to_list()
    usable = [w for w in weeks if w <= week]
    report_week = usable[-1] if usable else week
    status = (
        inj.filter(pl.col("week") == report_week)
        .select("gsis_id", "report_status")
        .unique("gsis_id")
    )

    # Ongoing episode = one that reaches the latest week we have signals for.
    ongoing = (
        episodes.filter((pl.col("season") == season) & (pl.col("end_week") >= players["latest_week"].max()))
        .select("gsis_id", k=pl.col("games_missed"))
        .unique("gsis_id")
    )
    on_ir = pl.col("status").is_in(list(IR_STATUSES))
    pairs = (
        players.select("gsis_id", on_ir=on_ir).join(ongoing, on="gsis_id").select("k", "on_ir").unique()
    )
    abs_tbl = pl.DataFrame(
        [
            {"k": r["k"], "on_ir": r["on_ir"], "absence": expected_remaining_absence(episodes, int(r["k"]), season, r["on_ir"])}
            for r in pairs.iter_rows(named=True)
        ],
        schema={"k": pl.Int64, "on_ir": pl.Boolean, "absence": pl.Float64},
    )

    fallback_rate = risk.group_by("position").agg(pl.col("shrunk_rate").mean().alias("pos_rate"))
    return (
        players.with_columns(on_ir=on_ir)
        .join(status, on="gsis_id", how="left")
        .join(ongoing, on="gsis_id", how="left")
        .join(abs_tbl, on=["k", "on_ir"], how="left")
        .join(risk.select("gsis_id", "shrunk_rate", "expected_missed_per_17", "most_common_injury"), on="gsis_id", how="left")
        .join(fallback_rate, on="position", how="left")
        .with_columns(
            base_miss_rate=pl.coalesce("shrunk_rate", "pos_rate"),
            absence_now=pl.when(pl.col("absence").is_not_null())
            .then(pl.col("absence"))
            .when(pl.col("on_ir"))
            .then(pl.lit(4.0))  # on IR but no episode found: the in-season IR minimum
            .otherwise(pl.col("report_status").replace_strict(p_miss, default=0.0, return_dtype=pl.Float64))
            .fill_null(0.0),
        )
        .drop("shrunk_rate", "pos_rate", "k", "absence", "on_ir")
    )


# --------------------------------------------------------------------------- assembly
def build_player_values(league: League, as_of: tuple[int, int]) -> tuple[pl.DataFrame, dict[str, float]]:
    season, week = as_of
    rw = load_raw("rosters_weekly")
    injuries = load_raw("injuries")
    tables = build_all(season)

    active = current_roster(rw, as_of)
    proj, replacement = project_ppg(tables["log"], tables["directory"], active, league, as_of)
    players = (
        active.join(proj.select("gsis_id", "games", "eff_games", "raw_ppg", "proj_ppg", "replacement_ppg"), on="gsis_id")
        .join(remaining_games(load_raw("schedules"), as_of), on="team", how="left")
        .with_columns(pl.col("remaining").fill_null(0))
    )
    players = availability(players, tables, injuries, as_of)

    # Manual adjustments: by gsis_id, else by (case-insensitive) name.
    adj = load_adjustments()
    by_id = adj.filter(pl.col("gsis_id").is_not_null() & (pl.col("gsis_id") != "")).select("gsis_id", d_id="ppg_delta", n_id="note")
    by_name = (
        adj.filter(pl.col("gsis_id").is_null() | (pl.col("gsis_id") == ""))
        .select(name_lc=pl.col("name").str.to_lowercase(), d_nm="ppg_delta", n_nm="note")
    )
    players = (
        players.with_columns(name_lc=pl.col("full_name").str.to_lowercase())
        .join(by_id, on="gsis_id", how="left")
        .join(by_name, on="name_lc", how="left")
        .with_columns(
            adj_ppg=pl.coalesce("d_id", "d_nm").fill_null(0.0),
            adj_note=pl.coalesce("n_id", "n_nm"),
        )
        .drop("name_lc", "d_id", "d_nm", "n_id", "n_nm")
    )

    players = (
        players.with_columns(
            final_ppg=pl.col("proj_ppg") + pl.col("adj_ppg"),
            exp_games=(
                (pl.col("remaining") - pl.col("absence_now").clip(upper_bound=pl.col("remaining"))).clip(lower_bound=0)
                * (1 - pl.col("base_miss_rate"))
            ),
        )
        .with_columns(
            availability=pl.when(pl.col("remaining") > 0).then(pl.col("exp_games") / pl.col("remaining")).otherwise(0.0),
            vorp=(pl.col("final_ppg") - pl.col("replacement_ppg")) * pl.col("exp_games"),
        )
        .with_columns(
            exp_ppg=pl.col("final_ppg") * pl.col("availability"),
            value=pl.col("vorp").clip(lower_bound=0),
        )
        .with_columns(
            overall_rank=pl.col("value").rank("ordinal", descending=True).cast(pl.Int32),
            pos_rank=pl.col("value").rank("ordinal", descending=True).over("position").cast(pl.Int32),
        )
        .select(
            "gsis_id", "sleeper_id", "full_name", "position", "team", "age", "status", "report_status",
            "games", pl.col("raw_ppg").round(2), pl.col("proj_ppg").round(2), pl.col("adj_ppg"), "adj_note",
            pl.col("replacement_ppg").round(2), "remaining", pl.col("absence_now").round(1),
            pl.col("base_miss_rate").round(3), "most_common_injury", pl.col("exp_games").round(1),
            pl.col("availability").round(2), pl.col("exp_ppg").round(2), pl.col("vorp").round(1),
            pl.col("value").round(1), "overall_rank", "pos_rank",
        )
        .sort("overall_rank")
    )
    return players, replacement


def export_json(players: pl.DataFrame, league: League, replacement: dict[str, float], as_of: tuple[int, int], path=SITE_JSON) -> None:
    """Static JSON for the website, keyed by Sleeper player id (so a Sleeper roster can be scored in the browser)."""
    rows = players.filter(pl.col("sleeper_id").is_not_null())
    payload = {
        "as_of": {"season": as_of[0], "week": as_of[1]},
        "league": {"teams": league.teams, "starters": league.starters, "bench": league.bench, "scoring": "ppr"},
        "replacement_ppg": {k: round(v, 2) for k, v in replacement.items()},
        "players": {
            str(r["sleeper_id"]): {
                "name": r["full_name"], "pos": r["position"], "team": r["team"], "age": r["age"],
                "ppg": r["proj_ppg"], "exp_ppg": r["exp_ppg"], "exp_games": r["exp_games"],
                "value": r["value"], "status": r["report_status"],
            }
            for r in rows.iter_rows(named=True)
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


# --------------------------------------------------------------------------- backtest
def evaluate_projection(league: League, seasons=(2022, 2023, 2024, 2025), weeks=(4, 8, 12)) -> pl.DataFrame:
    """Does the PPG projection beat simple baselines at predicting rest-of-season PPG?

    For each (season, week): project as of that week, then compare with the actual mean PPG over the
    remaining regular-season games (players with >= 4 such games). Baselines: last season's PPG and
    season-to-date PPG. Only the projection is evaluated here (availability uses full-season risk tables).
    """
    rw = load_raw("rosters_weekly")
    tables = build_all(2025)
    log, directory = tables["log"], tables["directory"]
    played = log.filter((pl.col("offense_pct") >= 0.2) & pl.col("ppr").is_not_null())
    rows = []
    for season in seasons:
        for week in weeks:
            as_of = (season, week)
            active = current_roster(rw, as_of)
            proj, _ = project_ppg(log, directory, active, league, as_of)
            actual = (
                played.filter((pl.col("season") == season) & (pl.col("week") >= week) & (pl.col("week") <= 17))
                .group_by("gsis_id").agg(pl.len().alias("n_ros"), pl.col("ppr").mean().alias("actual"))
                .filter(pl.col("n_ros") >= 4)
            )
            last = (
                played.filter(pl.col("season") == season - 1).group_by("gsis_id")
                .agg(pl.len().alias("n_last"), pl.col("ppr").mean().alias("last_season"))
                .filter(pl.col("n_last") >= 6)
            )
            std = (
                played.filter((pl.col("season") == season) & (pl.col("week") < week)).group_by("gsis_id")
                .agg(pl.len().alias("n_std"), pl.col("ppr").mean().alias("to_date")).filter(pl.col("n_std") >= 3)
            )
            j = (
                proj.select("gsis_id", "position", "proj_ppg").join(actual, on="gsis_id").join(last, on="gsis_id")
                .join(std, on="gsis_id").with_columns(season=season, week=week)
            )
            rows.append(j)
    d = pl.concat(rows)
    out = []
    for name, col in [("projection (this model)", "proj_ppg"), ("last season PPG", "last_season"), ("season-to-date PPG", "to_date")]:
        out.append(
            d.select(
                pl.lit(name).alias("method"),
                pl.corr(col, "actual").alias("corr"),
                (pl.col(col) - pl.col("actual")).abs().mean().alias("mae"),
                pl.len().alias("n"),
            )
        )
    return pl.concat(out)


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> None:
    import nflreadpy as nfl

    p = argparse.ArgumentParser(description="Build player values (rest-of-season points over replacement).")
    p.add_argument("--teams", type=int, default=12, help="teams in the league (sets replacement level)")
    p.add_argument("--season", type=int, default=None)
    p.add_argument("--week", type=int, default=None)
    p.add_argument("--evaluate", action="store_true", help="backtest the PPG projection and exit")
    a = p.parse_args(argv)
    league = League(teams=a.teams)

    if a.evaluate:
        print(evaluate_projection(league))
        return

    as_of = (a.season or nfl.get_current_season(), a.week or nfl.get_current_week())
    players, replacement = build_player_values(league, as_of)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    players.write_csv(PROCESSED_DIR / "player_values.csv")
    export_json(players, league, replacement, as_of)
    print(f"As of {as_of[0]} week {as_of[1]} | {league.teams} teams | replacement PPG: "
          + ", ".join(f"{k} {v:.1f}" for k, v in replacement.items()))
    print(players.select("overall_rank", "full_name", "position", "team", "proj_ppg", "exp_games", "value").head(20))


if __name__ == "__main__":
    main()
