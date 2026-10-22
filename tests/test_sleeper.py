import polars as pl
import pytest

from ff.ingest.sleeper import base_body_part, injury_frame, is_season_ending, refresh_injuries
from ff.models.value import sleeper_absence

RAW = {
    "8437": {"injury_status": "IR", "injury_body_part": "Knee - ACL", "injury_notes": "Surgery", "news_updated": 1790626507704},
    "9001": {"injury_status": "IR", "injury_body_part": "Hamstring", "injury_notes": "Strain", "news_updated": 1792590054731},
    "1111": {"injury_status": None, "injury_body_part": "Ankle", "injury_notes": "old", "news_updated": 1},  # healthy, stale text
}


def test_injury_frame_keeps_only_currently_injured_players():
    f = injury_frame(RAW)
    assert sorted(f["sleeper_id"].to_list()) == ["8437", "9001"]
    assert f.filter(pl.col("sleeper_id") == "8437").row(0, named=True)["sl_part"] == "Knee - ACL"


def test_body_part_and_season_ending_parsing():
    assert base_body_part("Knee - ACL") == "Knee" and base_body_part("Hamstring") == "Hamstring" and base_body_part(None) is None
    assert is_season_ending("Knee - ACL") and is_season_ending("Achilles") and not is_season_ending("Knee - MCL") and not is_season_ending(None)


def test_refresh_uses_cache_when_sleeper_is_unreachable(tmp_path):
    path = tmp_path / "s.parquet"
    assert refresh_injuries(path, fetch=lambda: RAW).height == 2
    def boom():
        raise OSError("offline")
    assert refresh_injuries(path, fetch=boom).height == 2  # cached copy
    assert refresh_injuries(tmp_path / "none.parquet", fetch=boom) is None


def test_sleeper_absence_acl_is_season_ending_and_body_part_uses_history():
    episodes = pl.DataFrame({
        "gsis_id": ["a", "b", "c"], "season": [2024, 2024, 2025], "end_week": [10, 12, 9], "games_missed": [6, 8, 4],
        "injury": ["Hamstring", "Hamstring", "Ankle"], "starts_on_ir": [True, True, True], "ir_games": [4, 6, 3],
    })
    players = pl.DataFrame({
        "on_ir": [True, True, True, False, True], "k": [None, None, 5, None, 3],
        "cur_injury": [None, None, "Hamstring", None, "Unlabeled (IR)"], "remaining": [10, 10, 10, 10, 10],
        "sl_status": ["IR", "IR", "IR", None, "IR"], "sl_part": ["Knee - ACL", "Hamstring", "Hamstring", None, "Hamstring"],
    })
    out = sleeper_absence(players, episodes, 2026).to_list()
    assert out[0] == 10.0  # ACL on IR: the rest of the season
    assert out[1] is not None and out[1] > 0  # IR with no NFL-report episode: body-part history
    assert out[2] is None  # has an NFL-report episode already: leave the episode model in charge
    assert out[3] is None  # healthy
    assert out[4] is not None  # NFL report only said "Unlabeled (IR)": Sleeper's body part sharpens it
