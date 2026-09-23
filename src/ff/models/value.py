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
from ff.features.defense import build_game_metrics, matchup_ratings
from ff.features.injuries import IR_STATUSES, ON_ROSTER, build_all
from ff.features.special import SLEEPER_TEAM, dst_games, kicker_games, project_special
from ff.league import BENCH_SHARE, FLEX_SHARE, KD_DEPTH, POSITIONS, SLOT_ELIGIBLE, League
from ff.models.value_common import WEEKS

MATCHUP_FAVORABLE = 67  # matchup score (0-100) at or above this = "Favorable"
MATCHUP_TOUGH = 33      # at or below this = "Tough"; in between = "Medium"
MANUAL = ROOT / "data" / "manual" / "adjustments.csv"
SITE_JSON = ROOT / "site" / "data" / "player_values.json"  # its folder also gets player_values.js


# --------------------------------------------------------------------------- inputs
def current_roster(
    rosters_weekly: pl.DataFrame, as_of: tuple[int, int], positions: tuple[str, ...] = POSITIONS
) -> pl.DataFrame:
    """Players currently on a roster (latest weekly row up to ``as_of``), with sleeper_id and age."""
    season, week = as_of
    return (
        rosters_weekly.filter(
            (pl.col("season") == season) & (pl.col("game_type") == "REG") & (pl.col("week") <= week)
            & pl.col("position").is_in(list(positions))
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
    season_boost: float = 3.0,
    boost_ramp_games: int = 3,
) -> tuple[pl.DataFrame, dict[str, float]]:
    """Projected PPG for every active player + replacement PPG per position.

    Games where the player had < ``min_share`` of the offensive snaps are ignored (the game he got hurt in,
    garbage time): they say little about his role. Weights halve every ``half_life`` weeks, and games of the
    current season count more (roles change over the offseason; in backtests a boost of 3 cut the error ~4%
    versus no boost). Early in the season that boost is RAMPED UP over a player's first ``boost_ramp_games``
    current-season games instead of applied at full strength immediately: at 3x from game one, a single boom
    or bust week (e.g. one bad game in Week 1) got 3x the weight of any single game in the player's whole
    history, which swings his projection far more than one game should. Backtesting the ramp against the
    non-ramped version on weeks 1-3 showed a negligible cost in aggregate error (MAE +0.02) for a large drop
    in that kind of single-game swing, so it is on by default.
    """
    season, week = as_of
    now_t = season * WEEKS + week
    hist = (
        log.filter(
            (pl.col("key") < season * 100 + week) & (pl.col("season") > season - window_seasons)
            & (pl.col("offense_pct") >= min_share) & pl.col("ppr").is_not_null()
        )
        .join(active.select("gsis_id", "position"), on="gsis_id", how="inner")
        .with_columns(
            cur_rank=pl.when(pl.col("season") == season)
            .then(pl.col("week").rank("ordinal").over("gsis_id", "season"))
            .otherwise(None)
        )
        .with_columns(
            eff_boost=pl.when(pl.col("season") != season)
            .then(1.0)
            .when(boost_ramp_games <= 0)
            .then(pl.lit(season_boost))
            .otherwise(1.0 + (season_boost - 1.0) * (pl.col("cur_rank").clip(upper_bound=boost_ramp_games) / max(boost_ramp_games, 1)))
        )
        .with_columns(w=0.5 ** ((now_t - (pl.col("season") * WEEKS + pl.col("week"))) / half_life) * pl.col("eff_boost"))
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


# --------------------------------------------------------------------------- context (matchup, role)
def matchup_scores(as_of: tuple[int, int]) -> pl.DataFrame:
    """Opponent and matchup score (0-100, 100 = easiest defense for that position) for the as_of week.

    Score = 100 x (32 - rank) / 31, where rank 1 = the defense that allows the most fantasy points to the
    position (recency-weighted, last 2 seasons; see ``ff.features.defense``). In backtests this signal was weak
    (correlation ~0.06-0.12 with actual points allowed), so the site shows it as context, not as a forecast.
    """
    season, week = as_of
    stats = load_raw("player_stats", seasons=[season - 2, season - 1, season])
    ratings = matchup_ratings(build_game_metrics(stats, "ppr"), as_of).filter(pl.col("metric").str.starts_with("pts_"))
    n = ratings["defense"].n_unique()
    sched = load_raw("schedules").filter(
        (pl.col("season") == season) & (pl.col("week") == week) & (pl.col("game_type") == "REG")
    )
    sides = pl.concat(
        [sched.select(team="home_team", opp="away_team"), sched.select(team="away_team", opp="home_team")]
    )
    return (
        sides.join(ratings.select("defense", "metric", "rank"), left_on="opp", right_on="defense")
        .with_columns(
            position=pl.col("metric").str.replace("pts_", ""),
            mu=(100 * (n - pl.col("rank")) / (n - 1)).round(0).cast(pl.Int32),
        )
        .select("team", "position", "opp", "mu")
    )


def role_context(players: pl.DataFrame, as_of: tuple[int, int]) -> pl.DataFrame:
    """Share of the team's rush+target opportunities (last 4 games vs. last season) and the main teammate at
    the same position, so a committee (e.g. two RBs splitting carries) is visible."""
    season, week = as_of
    fo = (
        load_raw("ff_opportunity", seasons=[season - 1, season])
        .with_columns(pl.col("season").cast(pl.Int32), pl.col("week").cast(pl.Int32))
        .filter(pl.col("season") * 100 + pl.col("week") < season * 100 + week)
        .select(
            gsis_id="player_id", season="season", week="week",
            opp=pl.col("rush_attempt").fill_null(0) + pl.col("rec_attempt").fill_null(0),
            opp_team=pl.col("rush_attempt_team").fill_null(0) + pl.col("rec_attempt_team").fill_null(0),
        )
        .filter(pl.col("opp_team") > 0)
        .with_columns(share=pl.col("opp") / pl.col("opp_team"))
        .unique(["gsis_id", "season", "week"])
        .sort("gsis_id", "season", "week")
    )
    now = fo.group_by("gsis_id").agg(pl.col("share").tail(4).mean().alias("share_now"))
    prev = fo.filter(pl.col("season") == season - 1).group_by("gsis_id").agg(pl.col("share").mean().alias("share_prev"))
    df = (
        players.filter(pl.col("position") != "QB").select("gsis_id", "team", "position", "full_name")
        .join(now, on="gsis_id", how="left").join(prev, on="gsis_id", how="left")
    )
    mates = (
        df.select("gsis_id", "team", "position")
        .join(df.select(mate_id="gsis_id", team="team", position="position", mate="full_name", mate_share="share_now"),
              on=["team", "position"])
        .filter((pl.col("gsis_id") != pl.col("mate_id")) & pl.col("mate_share").is_not_null())
        .sort("mate_share", descending=True)
        .group_by("gsis_id", maintain_order=True)
        .first()
        .select("gsis_id", "mate", "mate_share")
    )
    return df.select("gsis_id", "share_now", "share_prev").join(mates, on="gsis_id", how="left")


# --------------------------------------------------------------------------- assembly
def _adjustments_frame() -> tuple[pl.DataFrame, pl.DataFrame]:
    adj = load_adjustments()
    by_id = adj.filter(pl.col("gsis_id").is_not_null() & (pl.col("gsis_id") != "")).select(
        "gsis_id", d_id="ppg_delta", n_id="note"
    )
    by_name = adj.filter(pl.col("gsis_id").is_null() | (pl.col("gsis_id") == "")).select(
        name_lc=pl.col("name").str.to_lowercase(), d_nm="ppg_delta", n_nm="note"
    )
    return by_id, by_name


def _score(players: pl.DataFrame) -> pl.DataFrame:
    """Manual adjustments, expected games, availability, VORP and value (needs replacement_ppg column)."""
    by_id, by_name = _adjustments_frame()
    return (
        players.with_columns(name_lc=pl.col("full_name").str.to_lowercase())
        .join(by_id, on="gsis_id", how="left")
        .join(by_name, on="name_lc", how="left")
        .with_columns(
            adj_ppg=pl.coalesce("d_id", "d_nm").fill_null(0.0),
            adj_note=pl.coalesce("n_id", "n_nm"),
        )
        .drop("name_lc", "d_id", "d_nm", "n_id", "n_nm")
        .with_columns(
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
        .with_columns(exp_ppg=pl.col("final_ppg") * pl.col("availability"), value=pl.col("vorp").clip(lower_bound=0))
    )


def _replacement(table: pl.DataFrame, pos: str, league: League) -> tuple[float, list[float]]:
    vals = table.filter((pl.col("position") == pos) & (pl.col("games") >= 6)).sort("raw_ppg", descending=True)["raw_ppg"]
    if not len(vals):
        return 0.0, []
    return float(vals[min(league.replacement_rank(pos), len(vals)) - 1]), [round(float(v), 2) for v in vals]


def build_player_values(
    league: League, as_of: tuple[int, int], tables: dict[str, pl.DataFrame] | None = None
) -> tuple[pl.DataFrame, dict[str, float], dict[str, list[float]]]:
    """Values for QB/RB/WR/TE plus K and DEF. Returns (players, replacement PPG per position, ppg pools).

    ``tables`` (from ``build_all``) can be passed in to avoid recomputing the injury tables.
    """
    season, week = as_of
    rw = load_raw("rosters_weekly")
    injuries = load_raw("injuries")
    tables = tables or build_all(season)

    active = current_roster(rw, as_of)
    proj, replacement = project_ppg(tables["log"], tables["directory"], active, league, as_of)
    players = (
        active.join(proj.select("gsis_id", "games", "eff_games", "raw_ppg", "proj_ppg", "replacement_ppg"), on="gsis_id")
        .join(remaining_games(load_raw("schedules"), as_of), on="team", how="left")
        .with_columns(pl.col("remaining").fill_null(0))
    )
    players = availability(players, tables, injuries, as_of)

    # pools of raw PPG (players with >= 6 games) so the website can recompute replacement for ITS league
    pools: dict[str, list[float]] = {}
    for pos in POSITIONS:
        pools[pos] = _replacement(players.with_columns(games=pl.col("games")), pos, league)[1]

    # ---- kickers and team defenses
    ps = load_raw("player_stats")
    schedules = load_raw("schedules")
    kact = current_roster(rw, as_of, positions=("K",)).with_columns(position=pl.lit("K"))
    kproj = project_special(kicker_games(ps), "gsis_id", kact, as_of)
    teams = pl.DataFrame({"team": sorted(set(schedules.filter(pl.col("season") == season)["home_team"].to_list()))})
    dproj = project_special(dst_games(load_raw("team_stats"), schedules), "team", teams, as_of).with_columns(
        gsis_id=pl.col("team"), full_name=pl.col("team") + " D/ST", position=pl.lit("DEF"),
        sleeper_id=pl.col("team").replace(SLEEPER_TEAM), age=pl.lit(None, dtype=pl.Float64),
        status=pl.lit("ACT"), birth_date=pl.lit(None, dtype=pl.Date),
    )
    special = pl.concat([kproj.select(*_SPECIAL_COLS), dproj.select(*_SPECIAL_COLS)], how="vertical_relaxed")
    for pos in ("K", "DEF"):
        replacement[pos], pools[pos] = _replacement(special, pos, league)
    special = (
        special.join(pl.DataFrame({"position": ["K", "DEF"], "replacement_ppg": [replacement["K"], replacement["DEF"]]}), on="position")
        .join(remaining_games(schedules, as_of), on="team", how="left")
        .with_columns(
            pl.col("remaining").fill_null(0), absence_now=pl.lit(0.0), base_miss_rate=pl.lit(0.0),
            report_status=pl.lit(None, dtype=pl.String), most_common_injury=pl.lit(None, dtype=pl.String),
        )
    )

    players = _score(players)
    special = _score(special)

    # ---- context: matchup + role
    mu = matchup_scores(as_of)
    role = role_context(players, as_of)
    players = players.join(mu, on=["team", "position"], how="left").join(role, on="gsis_id", how="left")
    special = special.with_columns(
        opp=pl.lit(None, dtype=pl.String), mu=pl.lit(None, dtype=pl.Int32), share_now=pl.lit(None, dtype=pl.Float64),
        share_prev=pl.lit(None, dtype=pl.Float64), mate=pl.lit(None, dtype=pl.String), mate_share=pl.lit(None, dtype=pl.Float64),
    )

    cols = _OUT_COLS
    out = (
        pl.concat([players.select(cols), special.select(cols)], how="vertical_relaxed")
        .with_columns(
            overall_rank=pl.col("value").rank("ordinal", descending=True).cast(pl.Int32),
            pos_rank=pl.col("value").rank("ordinal", descending=True).over("position").cast(pl.Int32),
        )
        .sort("overall_rank")
    )
    return out, replacement, pools


_SPECIAL_COLS = ["gsis_id", "sleeper_id", "full_name", "position", "team", "age", "status", "games", "raw_ppg", "proj_ppg"]
_OUT_COLS = [
    "gsis_id", "sleeper_id", "full_name", "position", "team", "age", "status", "report_status", "games", "raw_ppg",
    "proj_ppg", "adj_ppg", "final_ppg", "adj_note", "replacement_ppg", "remaining", "absence_now", "base_miss_rate",
    "most_common_injury", "exp_games", "availability", "exp_ppg", "vorp", "value",
    "opp", "mu", "share_now", "share_prev", "mate", "mate_share",
]


def _model_constants() -> dict:
    return {
        "slot_eligible": {k: list(v) for k, v in SLOT_ELIGIBLE.items()},
        "flex_share": FLEX_SHARE, "bench_share": BENCH_SHARE, "kd_depth": KD_DEPTH,
        "matchup": {"favorable": MATCHUP_FAVORABLE, "tough": MATCHUP_TOUGH},
    }


def export_site_data(
    players: pl.DataFrame, pools: dict[str, list[float]], league: League, as_of: tuple[int, int],
    directory=SITE_JSON.parent,
) -> None:
    """Static data for the website, keyed by Sleeper player id.

    Writes ``player_values.json`` (tooling) and ``player_values.js`` (sets ``window.FF_DATA``, so the page also
    works when opened from disk). Replacement levels are NOT stored: the browser recomputes them from ``pools``
    for the connected league (its size and lineup), and re-derives value after the user's own adjustments.
    """
    r = lambda v, d=2: None if v is None else round(v, d)  # noqa: E731
    rows = players.filter(pl.col("sleeper_id").is_not_null())
    payload = {
        "as_of": {"season": as_of[0], "week": as_of[1]},
        "default_league": {"teams": league.teams, "slots": list(league.slots), "bench": league.bench, "ir": league.ir},
        "model": _model_constants(),
        "pools": pools,
        "players": {
            str(x["sleeper_id"]): {
                "name": x["full_name"], "pos": x["position"], "team": x["team"], "age": x["age"],
                "ppg": r(x["final_ppg"] if "final_ppg" in x else x["proj_ppg"]),
                "avail": r(x["availability"]), "exp_games": r(x["exp_games"], 1), "remaining": x["remaining"],
                "value": r(x["value"], 1), "status": x["report_status"] or ("IR" if x["status"] in IR_STATUSES else None),
                "opp": x["opp"], "mu": x["mu"], "share": r(x["share_now"], 3), "share_prev": r(x["share_prev"], 3),
                "mate": x["mate"], "mate_share": r(x["mate_share"], 3),
            }
            for x in rows.iter_rows(named=True)
        },
    }
    text = json.dumps(payload, separators=(",", ":"))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "player_values.json").write_text(text, encoding="utf-8")
    (directory / "player_values.js").write_text(f"window.FF_DATA = {text};\n", encoding="utf-8")


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

    from ff.features.injuries import build_all
    from ff.models.weeklog import export_weekly_log

    p = argparse.ArgumentParser(description="Build player values (rest-of-season points over replacement).")
    p.add_argument("--teams", type=int, default=12, help="teams in the default league (the site recomputes for yours)")
    p.add_argument("--season", type=int, default=None)
    p.add_argument("--week", type=int, default=None)
    p.add_argument("--evaluate", action="store_true", help="backtest the PPG projection and exit")
    a = p.parse_args(argv)
    league = League(teams=a.teams)

    if a.evaluate:
        print(evaluate_projection(league))
        return

    as_of = (a.season or nfl.get_current_season(), a.week or nfl.get_current_week())
    players, replacement, pools = build_player_values(league, as_of)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    players.write_csv(PROCESSED_DIR / "player_values.csv")
    export_site_data(players, pools, league, as_of)

    active = current_roster(load_raw("rosters_weekly"), as_of)
    tables = build_all(as_of[0])
    proj_by_sid = {
        r["sleeper_id"]: r["final_ppg"] for r in players.iter_rows(named=True) if r["sleeper_id"]
    }
    export_weekly_log(active, tables["log"], as_of, proj_by_sid, SITE_JSON.parent)

    print(f"As of {as_of[0]} week {as_of[1]} | default league: {league.teams} teams, slots {list(league.slots)}")
    print("replacement PPG: " + ", ".join(f"{k} {v:.1f}" for k, v in replacement.items()))
    print(players.select("overall_rank", "full_name", "position", "team", "proj_ppg", "exp_games", "value", "opp", "mu").head(12))


if __name__ == "__main__":
    main()
