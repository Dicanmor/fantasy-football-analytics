import polars as pl

from ff.models.roles import add_roles, hurt_ids, season_snaps

AS_OF = (2026, 8)  # 7 team games played


def log(rows):
    return pl.DataFrame(rows, orient="row", schema={"gsis_id": pl.String, "season": pl.Int32, "week": pl.Int32, "offense_pct": pl.Float64})


def players(*ids):
    return pl.DataFrame({
        "gsis_id": list(ids), "position": ["QB"] * len(ids), "status": ["ACT"] * len(ids),
        "report_status": [None] * len(ids), "sl_status": [None] * len(ids), "absence_now": [0.0] * len(ids),
    }, schema_overrides={"report_status": pl.String, "sl_status": pl.String})


def test_tiers_and_play_rate():
    lg = log(
        [("starter", 2026, w, 0.9) for w in range(1, 8)]                       # plays all year
        + [("fewsnaps", 2026, 2, 0.1), ("fewsnaps", 2026, 3, 0.1)]             # a few garbage-time snaps
        + [("rising", 2026, w, 0.0 if w < 5 else 0.7) for w in range(1, 8)]    # took over in the last 3 weeks
        + [("oldgame", 2025, 5, 0.9)]                                          # last year only
    )
    out = add_roles(players("starter", "fewsnaps", "rising", "oldgame", "nobody"), lg, AS_OF).sort("gsis_id")
    got = dict(zip(out["gsis_id"], out["tier"]))
    assert got == {"starter": 0, "rising": 0, "fewsnaps": 1, "oldgame": 2, "nobody": 2}
    rate = dict(zip(out["gsis_id"], out["play_rate"]))
    assert rate["starter"] == 1.0 and rate["rising"] == 1.0 and 0 < rate["oldgame"] < rate["fewsnaps"] < 1


def test_hurt_players_keep_tier_zero():
    lg = log([("a", 2026, 2, 0.05)])
    inj = pl.DataFrame({"gsis_id": ["a", "b"], "season": [2026, 2026], "week": [3, 3], "game_type": ["REG", "REG"], "report_status": ["Out", "Questionable"]})
    rw = pl.DataFrame({"gsis_id": ["c"], "season": [2026], "week": [4], "game_type": ["REG"], "status": ["RES"]})
    hurt = hurt_ids(inj, rw, AS_OF)
    assert set(hurt["gsis_id"]) == {"a", "c"}          # a Questionable tag alone is not "missed time hurt"
    out = add_roles(players("a", "c", "d"), lg, AS_OF, hurt).sort("gsis_id")
    assert dict(zip(out["gsis_id"], out["tier"])) == {"a": 0, "c": 0, "d": 2}
    sick = players("e").with_columns(report_status=pl.lit("Questionable"))
    assert add_roles(sick, lg, AS_OF)["tier"].to_list() == [0]   # hurt now: stays in tier 0, value already prices it


def test_no_tiers_before_week_four():
    out = add_roles(players("x"), log([]), (2026, 3))
    assert out["tier"].to_list() == [0] and out["play_rate"].to_list() == [1.0]


def test_season_snaps_share_is_over_team_games():
    s = season_snaps(log([("a", 2026, 1, 0.8), ("a", 2026, 2, 0.8)]), (2026, 5))   # 4 team games played
    assert abs(s["cur_share"][0] - 0.4) < 1e-9 and s["cur_games"][0] == 2
