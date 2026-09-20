import polars as pl
import pytest

from ff.features.injuries import (
    age_bucket, build_episodes, control_ratios, merge_gapped_episodes, recovery_metrics, team_game_index,
)


def _schedule(bye_week=6, weeks=10):
    rows = [dict(season=2025, week=w, game_type="REG", home_team="AAA", away_team="ZZZ")
            for w in range(1, weeks + 1) if w != bye_week]
    return pl.DataFrame(rows)


def _directory():
    return pl.DataFrame({"gsis_id": ["p1"], "full_name": ["Player One"], "position": ["RB"],
                         "birth_date": [None]}, schema_overrides={"birth_date": pl.Date})


def _signals(weeks):
    return pl.DataFrame({"gsis_id": ["p1"] * len(weeks), "season": [2025] * len(weeks), "week": weeks,
                         "team": ["AAA"] * len(weeks), "injury": ["Knee"] * len(weeks),
                         "ir": [False] * len(weeks)}).with_columns(
        pl.col("season").cast(pl.Int32), pl.col("week").cast(pl.Int32))


def test_bye_week_does_not_split_an_episode():
    eps = build_episodes(_signals([4, 5, 7]), team_game_index(_schedule()), _directory())  # week 6 = bye
    assert eps.height == 1
    row = eps.row(0, named=True)
    assert (row["start_week"], row["end_week"], row["games_missed"]) == (4, 7, 3)


def test_a_played_game_splits_episodes():
    eps = build_episodes(_signals([2, 4]), team_game_index(_schedule()), _directory())
    assert eps.height == 2


def test_carried_over_flags_season_ending():
    eps = build_episodes(_signals([9, 10]), team_game_index(_schedule()), _directory())
    assert eps["carried_over"].item() is True


def _eps(rows):
    return pl.DataFrame(rows).with_columns(pl.col("start_key", "end_key").cast(pl.Int64))


def _two_episodes():
    base = dict(gsis_id="p1", full_name="P", position="RB", team="AAA", season=2025, carried_over=False,
                injury="Knee", ir_games=0, starts_on_ir=False)
    return _eps([
        dict(base, episode_id=0, start_week=4, end_week=5, games_missed=2, start_key=202504, end_key=202505),
        dict(base, episode_id=1, start_week=7, end_week=8, games_missed=2, start_key=202507, end_key=202508),
    ])


def test_episodes_merge_when_no_game_played_between():
    log = pl.DataFrame({"gsis_id": ["p1"], "key": [202503]})  # nothing played in week 6
    merged = merge_gapped_episodes(_two_episodes(), log)
    assert merged.height == 1 and merged["games_missed"].item() == 4 and merged["parts"].item() == 2


def test_episodes_stay_separate_when_he_played_between():
    log = pl.DataFrame({"gsis_id": ["p1"], "key": [202506]})
    assert merge_gapped_episodes(_two_episodes(), log).height == 2


def test_recovery_skips_injury_game_in_baseline_and_finds_normal():
    weeks = list(range(1, 9)) + [9] + [12, 13, 14, 15, 16, 17, 18, 19, 20, 21]
    share = [0.8] * 8 + [0.3] + [0.4, 0.4, 0.8, 0.8] + [0.8] * 6      # week 9 = the game he got hurt in
    log = pl.DataFrame({"gsis_id": ["p1"] * len(weeks), "key": [202500 + w for w in weeks],
                        "offense_pct": share, "ppr": [10.0] * len(weeks)})
    ep = _eps([dict(episode_id=0, gsis_id="p1", start_key=202510, end_key=202511, games_missed=2)])
    rec = recovery_metrics(ep, log)
    r = rec.row(0, named=True)
    assert r["base_share"] == pytest.approx(0.8)          # would be ~0.74 if the injury game were included
    assert r["games_to_normal"] == 2                       # ratios .5, .5, 1, 1 -> first pair at game 3 -> 2 games
    assert r["r1"] == pytest.approx(0.5) and r["returned"] is True


def test_control_ratio_is_one_for_flat_players():
    log = pl.DataFrame({"gsis_id": ["p1"] * 14, "season": [2025] * 14, "week": list(range(1, 15)),
                        "offense_pct": [0.5] * 14, "ppr": [10.0] * 14})
    c = control_ratios(log)
    assert c["n_windows"].item() > 0
    assert c["median_ppg_ratio_first4"].item() == pytest.approx(1.0)


def test_age_bucket_handles_null():
    df = pl.DataFrame({"age": [22.0, 25.0, 31.0, None]}).with_columns(b=age_bucket(pl.col("age")))
    assert df["b"].to_list() == ["<24", "24-25", "30+", None]
