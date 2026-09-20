"""Injury analysis for fantasy-relevant players (QB / RB / WR / TE).

Questions this answers
  1. How injury-prone is a player, given his age?         -> ``player_risk``
  2. How much does an injury hurt afterwards, and how long until his workload is back to normal?
                                                            -> ``build_episodes`` + ``recovery_metrics``

Important data caveat (checked against real data)
  The nflverse *injury report* only lists players on the active roster's weekly report. Players on
  injured reserve simply disappear from it (e.g. Saquon Barkley's 2020 ACL season has zero rows). So an
  "injury absence" here is a player-week where EITHER the report says ``Out`` OR the weekly roster
  says the player is on reserve (``RES``) / PUP. Without the roster signal, long injuries are missed.

Definitions
  * episode   consecutive team games missed with an injury signal (byes are skipped, one team per episode);
              episodes separated only by games the player did not play are merged (a relapse is one absence)
  * workload  ``offense_pct`` (share of the team's offensive snaps) from snap counts, 2013+
  * baseline  mean workload over the ``n_base`` games played before the injury, SKIPPING the last one
              (the game where he got hurt has truncated snaps and would understate the baseline)
  * "back to normal"  first post-return game where this game AND the next are >= ``normal_ratio`` x baseline
  * control   the same "next 4 games vs. previous 8" ratio for windows with NO injury, to separate the
              injury effect from ordinary week-to-week drift (see ``control_ratios``)

Run it::

    python -m ff.features.injuries
"""
from __future__ import annotations

import polars as pl

from ff.config import PROCESSED_DIR
from ff.data import load_raw

SKILL = ("QB", "RB", "WR", "TE")
IR_STATUSES = ("RES", "PUP")            # reserve/injured and physically-unable-to-perform
ON_ROSTER = ("ACT", "RES", "PUP", "INA")  # counts toward "games available"
KEY = pl.col("season") * 100 + pl.col("week")  # sortable (season, week) key


# --------------------------------------------------------------------------- episodes
def team_game_index(schedules: pl.DataFrame) -> pl.DataFrame:
    """season, team, week, gidx (0-based index of the team's games, so byes are skipped), n_games."""
    reg = schedules.filter(pl.col("game_type") == "REG")
    sides = pl.concat(
        [
            reg.select("season", "week", team="home_team"),
            reg.select("season", "week", team="away_team"),
        ]
    )
    return (
        sides.sort("season", "team", "week")
        .with_columns(
            gidx=pl.int_range(pl.len()).over("season", "team"),
            n_games=pl.len().over("season", "team"),
        )
        .select("season", "team", "week", "gidx", "n_games")
    )


def injury_signals(injuries: pl.DataFrame, rosters_weekly: pl.DataFrame) -> pl.DataFrame:
    """One row per (player, season, week, team) where the player missed time due to injury."""
    cast = [pl.col("season").cast(pl.Int32), pl.col("week").cast(pl.Int32)]
    report = (
        injuries.filter((pl.col("game_type") == "REG") & (pl.col("report_status") == "Out"))
        .with_columns(*cast)
        .select("gsis_id", "season", "week", "team", injury="report_primary_injury", ir=pl.lit(False))
    )
    reserve = (
        rosters_weekly.filter((pl.col("game_type") == "REG") & pl.col("status").is_in(IR_STATUSES))
        .with_columns(*cast)
        .select("gsis_id", "season", "week", "team", injury=pl.lit(None, dtype=pl.String), ir=pl.lit(True))
    )
    return (
        pl.concat([report, reserve])
        .drop_nulls(["gsis_id", "team"])
        .group_by("gsis_id", "season", "week", "team")
        .agg(pl.col("injury").drop_nulls().first(), pl.col("ir").any())
    )


def player_directory(rosters_weekly: pl.DataFrame) -> pl.DataFrame:
    """Latest known name / position / birth date per player (skill positions only)."""
    return (
        rosters_weekly.filter(pl.col("position").is_in(SKILL))
        .sort("season", "week")
        .group_by("gsis_id")
        .agg(
            pl.col("full_name").last(),
            pl.col("position").last(),
            pl.col("birth_date").last(),
        )
    )


def build_episodes(signals: pl.DataFrame, game_index: pl.DataFrame, directory: pl.DataFrame) -> pl.DataFrame:
    """Group injury-signal weeks into episodes of consecutive team games."""
    df = (
        signals.join(game_index, on=["season", "team", "week"], how="inner")  # drops non-REG weeks
        .join(directory.select("gsis_id", "full_name", "position"), on="gsis_id", how="inner")
        .sort("gsis_id", "season", "team", "gidx")
        .with_columns(
            new_ep=(pl.col("gidx") - pl.col("gidx").shift(1).over("gsis_id", "season", "team")).fill_null(0) != 1
        )
        .with_columns(ep_no=pl.col("new_ep").cast(pl.Int32).cum_sum().over("gsis_id", "season", "team"))
    )
    episodes = (
        df.group_by("gsis_id", "season", "team", "ep_no")
        .agg(
            pl.col("full_name").first(),
            pl.col("position").first(),
            pl.col("week").min().alias("start_week"),
            pl.col("week").max().alias("end_week"),
            pl.len().alias("games_missed"),
            pl.col("gidx").max().alias("end_gidx"),
            pl.col("n_games").first(),
            pl.col("injury").drop_nulls().mode().first().alias("injury"),
            pl.col("ir").sum().alias("ir_games"),          # games on reserve/PUP
            pl.col("ir").first().alias("starts_on_ir"),    # went straight to IR vs. first listed as Out
        )
        .with_columns(
            # season-ending: the episode runs through the team's last game (only meaningful for finished seasons)
            carried_over=pl.col("end_gidx") == pl.col("n_games") - 1,
            start_key=pl.col("season") * 100 + pl.col("start_week"),
            end_key=pl.col("season") * 100 + pl.col("end_week"),
        )
        .sort("gsis_id", "start_key")
        .with_row_index("episode_id")
        .drop("ep_no", "end_gidx", "n_games")
    )
    return episodes


UNLABELED = "Unlabeled (IR)"


def injury_labels(injuries: pl.DataFrame) -> pl.DataFrame:
    """Any injury text the report gives for a player-week (game status OR practice status)."""
    return (
        injuries.filter(pl.col("game_type") == "REG")
        .select(
            "gsis_id",
            pl.col("season").cast(pl.Int32),
            pl.col("week").cast(pl.Int32),
            injury=pl.coalesce("report_primary_injury", "practice_primary_injury").str.strip_chars(),
        )
        .filter(pl.col("injury").is_not_null() & (pl.col("injury").str.len_chars() > 0))
    )


def label_episodes(episodes: pl.DataFrame, labels: pl.DataFrame) -> pl.DataFrame:
    """Fill missing injury names from report rows in [start_week - 1, end_week] (IR players vanish from
    the report, but they usually appeared on it the week they got hurt)."""
    lab = (
        episodes.select("episode_id", "gsis_id", "season", "start_week", "end_week")
        .join(labels, on=["gsis_id", "season"])
        .filter((pl.col("week") >= pl.col("start_week") - 1) & (pl.col("week") <= pl.col("end_week")))
        .group_by("episode_id")
        .agg(pl.col("injury").mode().first().alias("label"))
    )
    return (
        episodes.join(lab, on="episode_id", how="left")
        .with_columns(injury=pl.coalesce("injury", "label").fill_null(UNLABELED))
        .drop("label")
    )


def merge_gapped_episodes(episodes: pl.DataFrame, log: pl.DataFrame) -> pl.DataFrame:
    """Merge a player's consecutive episodes when he played NO game between them.

    Example: out weeks 6-8, and also out week 10 with no game played in week 9 -> one absence. Without
    this, the "return" of the first episode would really be the return of the second one.
    """
    ep = episodes.sort("gsis_id", "start_key").with_columns(
        next_start=pl.col("start_key").shift(-1).over("gsis_id")
    )
    played_between = (
        ep.filter(pl.col("next_start").is_not_null())
        .select("episode_id", "gsis_id", "end_key", "next_start")
        .join(log.select("gsis_id", "key"), on="gsis_id")
        .filter((pl.col("key") > pl.col("end_key")) & (pl.col("key") < pl.col("next_start")))
        .select("episode_id")
        .unique()
        .with_columns(played_between=pl.lit(True))
    )
    ep = (
        ep.join(played_between, on="episode_id", how="left")
        .with_columns(merge_next=pl.col("next_start").is_not_null() & pl.col("played_between").is_null())
        .with_columns(
            new_group=~pl.col("merge_next").shift(1).over("gsis_id").fill_null(False),
        )
        .with_columns(grp=pl.col("new_group").cast(pl.Int32).cum_sum().over("gsis_id"))
    )
    return (
        ep.group_by("gsis_id", "grp", maintain_order=True)
        .agg(
            pl.col("full_name").first(),
            pl.col("position").first(),
            pl.col("team").first(),
            pl.col("season").first(),
            pl.col("start_week").first(),
            pl.col("end_week").last(),
            pl.col("games_missed").sum(),
            pl.col("ir_games").sum(),
            pl.col("starts_on_ir").first(),
            pl.col("injury").filter(pl.col("injury") != UNLABELED).first().alias("injury"),
            pl.col("carried_over").last(),
            pl.col("start_key").first(),
            pl.col("end_key").last(),
            pl.len().alias("parts"),
        )
        .with_columns(pl.col("injury").fill_null(UNLABELED))
        .sort("gsis_id", "start_key")
        .with_row_index("episode_id")
        .drop("grp")
    )


# --------------------------------------------------------------------------- recovery
def build_game_log(snap_counts: pl.DataFrame, players: pl.DataFrame, player_stats: pl.DataFrame) -> pl.DataFrame:
    """Games actually played on offense: gsis_id, key, offense_pct, ppr (fantasy points)."""
    ids = players.select("gsis_id", "pfr_id").drop_nulls()
    snaps = (
        snap_counts.filter((pl.col("game_type") == "REG") & (pl.col("offense_snaps") > 0))
        .join(ids, left_on="pfr_player_id", right_on="pfr_id", how="inner")
        .select("gsis_id", pl.col("season").cast(pl.Int32), pl.col("week").cast(pl.Int32), "offense_pct")
        .unique(["gsis_id", "season", "week"])
    )
    pts = player_stats.filter(pl.col("season_type") == "REG").select(
        gsis_id="player_id", season=pl.col("season").cast(pl.Int32), week=pl.col("week").cast(pl.Int32),
        ppr="fantasy_points_ppr",
    )
    return snaps.join(pts, on=["gsis_id", "season", "week"], how="left").with_columns(key=KEY)


def recovery_metrics(
    episodes: pl.DataFrame,
    log: pl.DataFrame,
    n_base: int = 8,
    n_post: int = 8,
    min_base: int = 3,
    normal_ratio: float = 0.85,
    skip_last: int = 1,
) -> pl.DataFrame:
    """Per episode: return game, workload after return vs. baseline, and games until back to normal.

    Episodes without a usable baseline (fewer than ``min_base`` prior games, e.g. rookies or pre-2013)
    are dropped. ``games_to_normal`` is null when the player never got back within ``n_post`` games
    (right-censored: still recovering, or retired), and ``returned`` says whether he played again at all.
    """
    j = episodes.select("episode_id", "gsis_id", "start_key", "end_key").join(
        log.select("gsis_id", "key", "offense_pct", "ppr"), on="gsis_id"
    )
    base = (
        j.filter(pl.col("key") < pl.col("start_key"))
        .sort(["episode_id", "key"], descending=[False, True])
        .with_columns(pre_rank=pl.int_range(pl.len()).over("episode_id"))
        .filter((pl.col("pre_rank") >= skip_last) & (pl.col("pre_rank") < skip_last + n_base))
        .group_by("episode_id")
        .agg(
            pl.len().alias("n_base"),
            pl.col("offense_pct").mean().alias("base_share"),
            pl.col("ppr").mean().alias("base_ppg"),
        )
        .filter((pl.col("n_base") >= min_base) & (pl.col("base_share") > 0))
    )
    post = (
        j.filter(pl.col("key") > pl.col("end_key"))
        .sort("episode_id", "key")
        .with_columns(post_idx=pl.int_range(pl.len()).over("episode_id") + 1)
        .filter(pl.col("post_idx") <= n_post + 1)  # +1 so the last game can look ahead one game
        .join(base, on="episode_id")
        .with_columns(ratio=pl.col("offense_pct") / pl.col("base_share"))
        .with_columns(
            pair_ok=(pl.col("ratio") >= normal_ratio)
            & (pl.col("ratio").shift(-1).over("episode_id") >= normal_ratio)
        )
    )
    after = post.group_by("episode_id").agg(
        pl.col("key").min().alias("return_key"),
        (pl.col("post_idx").filter(pl.col("pair_ok") & (pl.col("post_idx") <= n_post)).min() - 1).alias("games_to_normal"),
        pl.col("ratio").filter(pl.col("post_idx") <= 4).mean().alias("share_ratio_first4"),
        pl.col("ppr").filter(pl.col("post_idx") <= 4).mean().alias("ppg_after_first4"),
        *[pl.col("ratio").filter(pl.col("post_idx") == k).first().alias(f"r{k}") for k in range(1, n_post + 1)],
    )
    return (
        episodes.join(base, on="episode_id", how="inner")
        .join(after, on="episode_id", how="left")
        .with_columns(
            returned=pl.col("return_key").is_not_null(),
            ppg_ratio_first4=pl.when(pl.col("base_ppg") > 0).then(pl.col("ppg_after_first4") / pl.col("base_ppg")),
        )
        .drop("start_key", "end_key")
    )


def summarize_recovery(rec: pl.DataFrame, by: tuple[str, ...] = ("position", "severity"), min_n: int = 15) -> pl.DataFrame:
    """Typical recovery for groups with >= min_n returned episodes.

    ``r1``/``r3``/``r5``/``r8`` = median workload (vs. baseline) in the 1st/3rd/5th/8th game after return,
    among players who reached that game. ``pct_not_back`` = share never back to normal within the window.
    """
    sev = (
        pl.when(pl.col("games_missed") <= 2).then(pl.lit("1-2 games"))
        .when(pl.col("games_missed") <= 5).then(pl.lit("3-5 games"))
        .otherwise(pl.lit("6+ games"))
    )
    return (
        rec.filter(pl.col("returned"))
        .with_columns(severity=sev)
        .group_by(*by)
        .agg(
            pl.len().alias("n"),
            pl.col("games_to_normal").median().alias("median_games_to_normal"),
            pl.col("games_to_normal").is_null().mean().alias("pct_not_back"),
            pl.col("r1").median(), pl.col("r3").median(), pl.col("r5").median(), pl.col("r8").median(),
            pl.col("ppg_ratio_first4").median().alias("median_ppg_ratio_first4"),
        )
        .filter(pl.col("n") >= min_n)
        .sort(*by)
    )


def control_ratios(log: pl.DataFrame, n_base: int = 8, n_post: int = 4, skip: int = 1) -> pl.DataFrame:
    """Same 'next games vs. previous games' ratios for windows with NO absence (the control group).

    Only windows of contiguous games inside one season are used (at most one bye). Comparing the
    injured players' ratios with these shows how much of the post-injury drop is just ordinary drift.
    """
    g = (
        log.filter(pl.col("ppr").is_not_null())
        .sort("gsis_id", "season", "week")
        .with_columns(
            b_share=pl.col("offense_pct").rolling_mean(n_base).shift(skip + 1).over("gsis_id", "season"),
            b_ppg=pl.col("ppr").rolling_mean(n_base).shift(skip + 1).over("gsis_id", "season"),
            p_share=pl.col("offense_pct").rolling_mean(n_post).shift(-(n_post - 1)).over("gsis_id", "season"),
            p_ppg=pl.col("ppr").rolling_mean(n_post).shift(-(n_post - 1)).over("gsis_id", "season"),
            span=pl.col("week").shift(-(n_post - 1)).over("gsis_id", "season")
            - pl.col("week").shift(skip + n_base).over("gsis_id", "season"),
        )
        .filter((pl.col("span") <= n_base + skip + n_post) & (pl.col("b_share") > 0) & (pl.col("b_ppg") > 0))
        .with_columns(
            share_ratio=pl.col("p_share") / pl.col("b_share"), ppg_ratio=pl.col("p_ppg") / pl.col("b_ppg")
        )
    )
    return g.select(
        pl.len().alias("n_windows"),
        pl.col("share_ratio").median().alias("median_share_ratio_first4"),
        pl.col("ppg_ratio").median().alias("median_ppg_ratio_first4"),
    )


# --------------------------------------------------------------------------- durability / risk
def age_bucket(age: pl.Expr) -> pl.Expr:
    return (
        pl.when(age.is_null()).then(pl.lit(None, dtype=pl.String))
        .when(age < 24).then(pl.lit("<24"))
        .when(age < 26).then(pl.lit("24-25"))
        .when(age < 28).then(pl.lit("26-27"))
        .when(age < 30).then(pl.lit("28-29"))
        .otherwise(pl.lit("30+"))
    )


def player_risk(
    episodes: pl.DataFrame,
    rosters_weekly: pl.DataFrame,
    directory: pl.DataFrame,
    as_of_season: int,
    seasons_back: int = 3,
    prior_games: float = 17.0,
) -> pl.DataFrame:
    """Injury-proneness with age, for players on a roster in ``as_of_season``.

    Rate = injury games missed / games on the roster. It is shrunk toward the average rate of players
    with the same position and age bucket (worth ``prior_games`` games), so a player with two healthy
    seasons is not rated "immune" and a rookie is not rated on one game.
    """
    on_roster = (
        rosters_weekly.filter((pl.col("game_type") == "REG") & pl.col("status").is_in(ON_ROSTER)
                              & pl.col("position").is_in(SKILL))
        .with_columns(pl.col("season").cast(pl.Int32))
        .group_by("gsis_id", "season")
        .agg(pl.col("week").n_unique().alias("games_on_roster"))
    )
    missed = episodes.group_by("gsis_id", "season").agg(
        pl.col("games_missed").sum().alias("missed"), pl.len().alias("episodes")
    )
    ps = (
        on_roster.join(missed, on=["gsis_id", "season"], how="left")
        .with_columns(pl.col("missed", "episodes").fill_null(0))
        .join(directory, on="gsis_id", how="inner")
        .with_columns(
            age=(pl.date(pl.col("season"), 9, 1) - pl.col("birth_date")).dt.total_days() / 365.25
        )
        .with_columns(bucket=age_bucket(pl.col("age")))
    )
    prior = (
        ps.filter(pl.col("season") < as_of_season)
        .group_by("position", "bucket")
        .agg((pl.col("missed").sum() / pl.col("games_on_roster").sum()).alias("prior_rate"))
    )
    prior_pos = (
        ps.filter(pl.col("season") < as_of_season)
        .group_by("position")
        .agg((pl.col("missed").sum() / pl.col("games_on_roster").sum()).alias("prior_pos"))
    )
    recent = ps.filter(
        (pl.col("season") > as_of_season - seasons_back) & (pl.col("season") <= as_of_season)
    )
    top_injury = (
        episodes.filter(pl.col("season") > as_of_season - seasons_back)
        .group_by("gsis_id")
        .agg(pl.col("injury").filter(pl.col("injury") != "Unlabeled (IR)").mode().first().alias("most_common_injury"))
    )
    now = ps.filter(pl.col("season") == as_of_season).select("gsis_id", "age", "bucket")
    return (
        recent.group_by("gsis_id")
        .agg(
            pl.col("full_name").first(),
            pl.col("position").first(),
            pl.col("games_on_roster").sum().alias("games_observed"),
            pl.col("missed").sum().alias("games_missed"),
            pl.col("episodes").sum().alias("injuries"),
        )
        .join(now, on="gsis_id", how="inner")  # only players on a roster this season
        .join(prior, left_on=["position", "bucket"], right_on=["position", "bucket"], how="left")
        .join(prior_pos, on="position", how="left")
        .join(top_injury, on="gsis_id", how="left")
        .with_columns(prior_rate=pl.coalesce("prior_rate", "prior_pos"))
        .with_columns(
            raw_rate=pl.col("games_missed") / pl.col("games_observed"),
            shrunk_rate=(pl.col("games_missed") + prior_games * pl.col("prior_rate"))
            / (pl.col("games_observed") + prior_games),
        )
        .with_columns(expected_missed_per_17=(pl.col("shrunk_rate") * 17).round(1))
        .select(
            "gsis_id", "full_name", "position", pl.col("age").round(1), "games_observed", "injuries",
            "games_missed", pl.col("raw_rate").round(3), pl.col("prior_rate").round(3),
            pl.col("shrunk_rate").round(3), "expected_missed_per_17", "most_common_injury",
        )
        .sort("expected_missed_per_17", descending=True)
    )


# --------------------------------------------------------------------------- CLI
def build_all(season: int) -> dict[str, pl.DataFrame]:
    """Run the whole injury pipeline. Returns episodes, log, rec, control, risk, directory."""
    rw = load_raw("rosters_weekly")
    injuries = load_raw("injuries")
    directory = player_directory(rw)
    episodes = build_episodes(
        injury_signals(injuries, rw), team_game_index(load_raw("schedules")), directory
    )
    episodes = label_episodes(episodes, injury_labels(injuries))

    log = build_game_log(load_raw("snap_counts"), load_raw("players"), load_raw("player_stats"))
    log = log.join(directory.select("gsis_id"), on="gsis_id", how="semi")  # skill positions only
    episodes = merge_gapped_episodes(episodes, log)

    return {
        "episodes": episodes,
        "log": log,
        "directory": directory,
        "rec": recovery_metrics(episodes, log),
        "control": control_ratios(log),
        "risk": player_risk(episodes, rw, directory, season),
    }


def main(argv: list[str] | None = None) -> None:
    import argparse

    import nflreadpy as nfl

    p = argparse.ArgumentParser(description="Build injury episodes, recovery metrics and player risk.")
    p.add_argument("--season", type=int, default=None, help="as-of season for player risk (default: current)")
    a = p.parse_args(argv)
    season = a.season or nfl.get_current_season()

    t = build_all(season)
    episodes, rec, risk = t["episodes"], t["rec"], t["risk"]

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    episodes.write_csv(PROCESSED_DIR / "injury_episodes.csv")
    rec.write_csv(PROCESSED_DIR / "injury_recovery.csv", float_precision=3)
    summarize_recovery(rec).write_csv(PROCESSED_DIR / "injury_recovery_by_position.csv", float_precision=3)
    summarize_recovery(rec, by=("injury", "severity")).write_csv(
        PROCESSED_DIR / "injury_recovery_by_injury.csv", float_precision=3
    )
    t["control"].write_csv(PROCESSED_DIR / "injury_control.csv", float_precision=3)
    risk.write_csv(PROCESSED_DIR / "player_injury_risk.csv")
    print(f"{episodes.height:,} episodes | {rec.height:,} with a workload baseline | {risk.height:,} players rated for {season}")


if __name__ == "__main__":
    main()
