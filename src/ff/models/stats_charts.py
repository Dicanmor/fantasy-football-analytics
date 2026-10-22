"""Data for the website's "Stats" tab: RBSDM-style efficiency charts (EPA, CPOE, success rate) built straight
from nflverse play-by-play, the same source those charts use — nothing is scraped from any other site.

Every number is computed from ``pbp`` for the CURRENT regular season, up to (not including) the as-of week:
  - EPA (expected points added) is nflfastR's own per-play column; "per play" = mean over the plays counted.
  - QB: dropbacks = every ``qb_dropback`` (passes, sacks, scrambles). EPA/dropback uses all of them; CPOE is
    averaged over pass attempts only (nflfastR does not define it for sacks); success rate = share of those
    dropbacks with ``success`` == 1.
  - RB: designed carries only (``rush_attempt`` == 1, QB scrambles excluded).
  - WR/TE: targets (``pass_attempt`` == 1 with a ``receiver_player_id``); EPA/target and average air yards/target.
  - Team: offense and defense EPA/play, split pass vs rush.
  - Receivers also get yards per target, share of their team's pass attempts, and share of the team's red-zone
    pass attempts (yardline_100 <= 20); targets counted only for the team he played for most recently.
  - xFP vs FP comes from nflverse ``ff_opportunity`` (expected fantasy points from the usage), and the separation
    chart from NGS receiving (season line, ``week`` == 0). Routes run are NOT available in-season, so there is no
    YPRR/TPRR chart; target share and yards per target are the honest stand-ins.

Like ``season_usage``, functions take their data as parameters (no ``load_raw`` inside), so this is unit-testable
with synthetic frames and does not depend on downloaded data in CI.
"""
from __future__ import annotations

import json

import polars as pl

QB_POS = ("QB",)
RB_POS = ("RB", "FB")
REC_POS = ("WR", "TE")


def _season_plays(pbp: pl.DataFrame, as_of: tuple[int, int]) -> pl.DataFrame:
    season, week = as_of
    return pbp.filter((pl.col("season") == season) & (pl.col("week") < week) & (pl.col("season_type") == "REG"))


def _directory(players: pl.DataFrame) -> pl.DataFrame:
    return players.select("gsis_id", name="display_name", pos="position").filter(pl.col("gsis_id").is_not_null())


def _latest_team(plays: pl.DataFrame, id_col: str) -> pl.DataFrame:
    return (
        plays.filter(pl.col(id_col).is_not_null() & pl.col("posteam").is_not_null())
        .sort("week").group_by(id_col, maintain_order=True).agg(pl.col("posteam").last().alias("team"))
        .rename({id_col: "gsis_id"})
    )


def _finish(df: pl.DataFrame, directory: pl.DataFrame, teams: pl.DataFrame, positions: tuple[str, ...]) -> pl.DataFrame:
    return (
        df.join(directory, on="gsis_id", how="inner").filter(pl.col("pos").is_in(list(positions)))
        .join(teams, on="gsis_id", how="left")
    )


def qb_table(plays: pl.DataFrame, directory: pl.DataFrame) -> pl.DataFrame:
    db = plays.filter((pl.col("qb_dropback") == 1) & pl.col("passer_player_id").is_not_null())
    g = db.group_by(gsis_id="passer_player_id").agg(
        pl.len().alias("n"),
        pl.col("epa").mean().alias("epa"),
        pl.col("success").mean().alias("success"),
        pl.col("cpoe").filter(pl.col("pass_attempt") == 1).mean().alias("cpoe"),
    ).filter(pl.col("cpoe").is_not_null() & pl.col("epa").is_not_null())
    return _finish(g, directory, _latest_team(db, "passer_player_id"), QB_POS)


def rb_table(plays: pl.DataFrame, directory: pl.DataFrame) -> pl.DataFrame:
    rush = plays.filter((pl.col("rush_attempt") == 1) & (pl.col("qb_scramble").fill_null(0) != 1) & pl.col("rusher_player_id").is_not_null())
    g = rush.group_by(gsis_id="rusher_player_id").agg(
        pl.len().alias("n"), pl.col("epa").mean().alias("epa"), pl.col("success").mean().alias("success"),
    ).filter(pl.col("epa").is_not_null())
    return _finish(g, directory, _latest_team(rush, "rusher_player_id"), RB_POS)


def rec_table(plays: pl.DataFrame, directory: pl.DataFrame) -> pl.DataFrame:
    tgt = plays.filter((pl.col("pass_attempt") == 1) & pl.col("receiver_player_id").is_not_null())
    latest = _latest_team(tgt, "receiver_player_id").rename({"team": "latest"})
    rz = (pl.col("yardline_100") <= 20).fill_null(False)
    tgt = tgt.join(latest, left_on="receiver_player_id", right_on="gsis_id", how="left").with_columns(
        (pl.col("posteam") == pl.col("latest")).fill_null(False).alias("_mine"))
    g = tgt.group_by(gsis_id="receiver_player_id").agg(
        pl.len().alias("n"), pl.col("epa").mean().alias("epa"), pl.col("air_yards").mean().alias("adot"),
        pl.col("success").mean().alias("success"),
        (pl.when(pl.col("complete_pass") == 1).then(pl.col("yards_gained")).otherwise(0).sum() / pl.len()).alias("ypt"),
        pl.col("_mine").sum().alias("_n_mine"), (pl.col("_mine") & rz).sum().alias("_rz_mine"),
    ).filter(pl.col("epa").is_not_null() & pl.col("adot").is_not_null())
    team_att = plays.filter((pl.col("pass_attempt") == 1) & pl.col("posteam").is_not_null()).group_by(team="posteam").agg(
        pl.len().alias("_team_att"), (pl.col("yardline_100") <= 20).fill_null(False).sum().alias("_team_rz"))
    out = _finish(g, directory, latest.rename({"latest": "team"}), REC_POS).join(team_att, on="team", how="left")
    return out.with_columns(
        pl.when(pl.col("_team_att") > 0).then(pl.col("_n_mine") / pl.col("_team_att")).alias("tgt_share"),
        pl.when(pl.col("_team_rz") > 0).then(pl.col("_rz_mine") / pl.col("_team_rz")).alias("rz_share"),
    ).drop("_n_mine", "_rz_mine", "_team_att", "_team_rz")


def xfp_table(opportunity: pl.DataFrame, as_of: tuple[int, int]) -> pl.DataFrame:
    """Expected vs actual fantasy points (nflverse ff_opportunity) per WR/TE, completed regular-season weeks only."""
    season, week = as_of
    o = opportunity.filter(
        (pl.col("season").cast(pl.Utf8) == str(season)) & (pl.col("week") < week) & pl.col("position").is_in(["WR", "TE"])
        & pl.col("player_id").is_not_null()
    ).sort("week")
    return o.group_by("player_id", maintain_order=True).agg(
        pl.col("full_name").first().alias("name"), pl.col("position").first().alias("pos"), pl.col("posteam").last().alias("team"),
        pl.col("rec_attempt").fill_null(0).sum().cast(pl.Int64).alias("n"),
        pl.col("total_fantasy_points_exp").sum().alias("xfp"), pl.col("total_fantasy_points").sum().alias("fp"),
    ).rename({"player_id": "gsis_id"}).filter(pl.col("n") > 0)


def sep_table(ngs: pl.DataFrame, as_of: tuple[int, int]) -> pl.DataFrame:
    """NGS receiving season line (``week`` == 0): average separation and YAC above expectation, WR/TE."""
    return ngs.filter(
        (pl.col("season") == as_of[0]) & (pl.col("season_type") == "REG") & (pl.col("week") == 0)
        & pl.col("player_position").is_in(["WR", "TE"]) & (pl.col("targets") > 0)
        & pl.col("avg_separation").is_not_null() & pl.col("avg_yac_above_expectation").is_not_null()
    ).select(gsis_id="player_gsis_id", name="player_display_name", team="team_abbr", pos="player_position", n="targets",
             sep="avg_separation", yacoe="avg_yac_above_expectation")


def team_table(plays: pl.DataFrame) -> pl.DataFrame:
    """One row per team: offense and defense EPA/play split pass vs rush."""
    pas = plays.filter(pl.col("qb_dropback") == 1)
    rus = plays.filter((pl.col("rush_attempt") == 1) & (pl.col("qb_scramble").fill_null(0) != 1))
    def side(df: pl.DataFrame, by: str, name: str) -> pl.DataFrame:
        return df.filter(pl.col(by).is_not_null()).group_by(team=by).agg(
            pl.col("epa").mean().alias(name), pl.len().alias(name + "_n"))
    return (
        side(pas, "posteam", "off_pass").join(side(rus, "posteam", "off_rush"), on="team", how="full", coalesce=True)
        .join(side(pas, "defteam", "def_pass"), on="team", how="full", coalesce=True)
        .join(side(rus, "defteam", "def_rush"), on="team", how="full", coalesce=True)
        .drop_nulls("team").sort("team")
    )


def _rows(df: pl.DataFrame, cols: dict[str, int]) -> list[dict]:
    """Rows as plain dicts, metrics rounded (``cols`` maps column -> decimals), missing teams/values kept as None."""
    out = []
    for r in df.sort("n", descending=True).iter_rows(named=True):
        row = {"id": r["gsis_id"], "name": r["name"], "team": r.get("team"), "n": r["n"]}
        if r.get("pos") is not None:
            row["pos"] = r["pos"]
        for c, d in cols.items():
            row[c] = None if r[c] is None else round(r[c], d)
        out.append(row)
    return out


def build_stats(pbp: pl.DataFrame, players: pl.DataFrame, as_of: tuple[int, int],
                opportunity: pl.DataFrame | None = None, ngs: pl.DataFrame | None = None) -> dict:
    plays = _season_plays(pbp, as_of)
    d = _directory(players)
    team = team_table(plays)
    team_rows = [
        {"team": r["team"], **{k: (None if r[k] is None else round(r[k], 3)) for k in ("off_pass", "off_rush", "def_pass", "def_rush")}}
        for r in team.iter_rows(named=True)
    ]
    return {
        "as_of": {"season": as_of[0], "through_week": as_of[1] - 1},
        "qb": _rows(qb_table(plays, d), {"epa": 3, "cpoe": 2, "success": 3}),
        "rb": _rows(rb_table(plays, d), {"epa": 3, "success": 3}),
        "rec": _rows(rec_table(plays, d), {"epa": 3, "adot": 2, "success": 3, "ypt": 2, "tgt_share": 4, "rz_share": 4}),
        "xfp": _rows(xfp_table(opportunity, as_of), {"xfp": 1, "fp": 1}) if opportunity is not None else [],
        "sep": _rows(sep_table(ngs, as_of), {"sep": 2, "yacoe": 2}) if ngs is not None else [],
        "team": team_rows,
    }


def export_stats(stats: dict, directory) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stats_charts.json").write_text(json.dumps(stats, separators=(",", ":")), encoding="utf-8")


def export_logos(logo_urls: dict[str, str], directory, fetch=None) -> int:
    """Best-effort: save each team's logo next to the site so the browser loads it from OUR domain (no hotlinking,
    nothing for an ad blocker or CDN to break). ``logo_urls`` maps team abbreviation -> image URL; files that already
    exist are skipped; a failed download is skipped, never fatal (the page falls back to a colored dot). Returns the
    number of logos written. ``fetch`` (url -> bytes) is injectable for tests."""
    import urllib.request

    def default_fetch(url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=20) as r:  # noqa: S310 - fixed https logo URLs from nflverse
            return r.read()

    fetch = fetch or default_fetch
    directory.mkdir(parents=True, exist_ok=True)
    written = 0
    for abbr, url in logo_urls.items():
        dest = directory / f"{abbr}.png"
        if dest.exists() or not url:
            continue
        try:
            data = fetch(url)
        except Exception:  # noqa: BLE001 - logos are cosmetic
            continue
        if data:
            dest.write_bytes(data)
            written += 1
    return written
