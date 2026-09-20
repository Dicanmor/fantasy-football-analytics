import polars as pl
import pytest

from ff.data import load_raw


def _write(tmp_path, name, season, df):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    df.write_parquet(d / f"{name}_{season}.parquet")


def test_load_raw_handles_schema_drift(tmp_path):
    # Mimics the real injuries drift: extra column + float -> int type change
    _write(tmp_path, "x", 2024, pl.DataFrame({"season": [2024], "height": [72.0], "date_modified": ["a"]}))
    _write(tmp_path, "x", 2025, pl.DataFrame({"season": [2025], "height": [73], "season_type": ["REG"]}))

    df = load_raw("x", raw_dir=tmp_path)

    assert df.height == 2
    assert set(df.columns) == {"season", "height", "date_modified", "season_type"}


def test_load_raw_filters_seasons(tmp_path):
    _write(tmp_path, "x", 2024, pl.DataFrame({"season": [2024]}))
    _write(tmp_path, "x", 2025, pl.DataFrame({"season": [2025]}))

    assert load_raw("x", seasons=[2025], raw_dir=tmp_path)["season"].to_list() == [2025]


def test_load_raw_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_raw("nope", raw_dir=tmp_path)
