import polars as pl
import pytest

from ff.models.season_usage import build_usage_table


def _frames():
    active = pl.DataFrame(
        {"gsis_id": ["p1", "p2"], "sleeper_id": ["1", "2"], "full_name": ["A", "B"], "position": ["WR", "WR"], "team": ["AAA", "AAA"]}
    )
    player_stats = pl.DataFrame(
        {
            "player_id": ["p1", "p1", "p2", "p2"], "team": ["AAA"] * 4, "season": [2026] * 4, "week": [1, 2, 1, 2],
            "season_type": ["REG"] * 4, "carries": [0, 0, 0, 0], "targets": [8, 6, 2, 2],
        }
    )
    snap_counts = pl.DataFrame(
        {"pfr_player_id": ["pfr1", "pfr1", "pfr2"], "season": [2026, 2026, 2026], "week": [1, 2, 1], "offense_pct": [0.9, 0.8, 0.3]}
    )
    players = pl.DataFrame({"gsis_id": ["p1", "p2"], "pfr_id": ["pfr1", "pfr2"]})
    ngs_receiving = pl.DataFrame(
        {
            "player_gsis_id": ["p1", "p1"], "week": [1, 2], "targets": [8, 6],
            "avg_intended_air_yards": [10.0, 12.0], "percent_share_of_intended_air_yards": [40.0, 35.0],
        }
    )
    pbp = pl.DataFrame(
        {
            "game_id": ["g1"] * 10, "play_id": list(range(10)), "season": [2026] * 10, "week": [1] * 10,
            "season_type": ["REG"] * 10, "pass_attempt": [1] * 10, "down": [1, 2, 3, 3, 1, 2, 3, 4, 1, 2],
            "air_yards": [5, 5, 20, 5, 5, 5, 5, 5, 5, 5], "yardline_100": [15, 15, 15, 15, 15, 15, 15, 15, 15, 15],
            "receiver_player_id": ["p1"] * 5 + ["p2"] * 5,
        }
    )
    ftn_charting = pl.DataFrame(
        {"nflverse_game_id": ["g1"] * 10, "nflverse_play_id": list(range(10)), "is_catchable_ball": [True, True, False, True, True] + [True] * 5}
    )
    return active, player_stats, snap_counts, players, ngs_receiving, pbp, ftn_charting


def test_build_usage_table_computes_shares_without_touching_disk():
    """No load_raw() calls in this module (everything is passed in), so this runs fine even before
    `ff-download` has ever put anything in data/raw — e.g. in CI, where pytest runs before the download step."""
    active, player_stats, snap_counts, players, ngs_receiving, pbp, ftn_charting = _frames()
    out = build_usage_table(active, (2026, 3), player_stats, snap_counts, players, ngs_receiving, pbp, ftn_charting)
    rows = {r["gsis_id"]: r for r in out.iter_rows(named=True)}

    assert rows["p1"]["gms"] == 2
    assert rows["p1"]["targets_pct"] == pytest.approx(14 / 18)  # 14 of the team's 18 total targets
    assert rows["p1"]["snap_pct"] == pytest.approx(0.85)  # avg of 0.9, 0.8
    assert rows["p1"]["adot"] == pytest.approx(152 / 14)  # targets-weighted: (10*8 + 12*6)/14
    assert rows["p1"]["catchable_pct"] == pytest.approx(0.8)  # 4 of 5 catchable
    assert rows["p1"]["ez_tgt_pct"] == pytest.approx(0.2)  # 1 of 5 (air_yards 20 >= yardline_100 15)
    assert rows["p1"]["down34_tgt_pct"] == pytest.approx(0.4)  # 2 of 5 (two rows with down==3)
    assert rows["p2"]["down34_tgt_pct"] == pytest.approx(0.4)  # 2 of 5 (down 3 and 4)
