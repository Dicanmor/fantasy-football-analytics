"""Download nflverse datasets into ``data/raw`` as Parquet files.

Usage (from the repo root, after ``pip install -e .``)::

    ff-download                                   # all datasets, default seasons
    ff-download --datasets injuries snap_counts   # only some datasets
    ff-download --start 2018                      # override the first season
    ff-download --refresh                         # re-download finished seasons too
    ff-download --report                          # summarize what is on disk

Design notes:
  * One file per dataset per season (``data/raw/<dataset>/<dataset>_<season>.parquet``)
    so re-runs are cheap: finished seasons are skipped, the in-progress season is
    always refreshed.
  * Datasets without seasons (players, ID mappings, teams) are stored as a single
    file and refreshed on every run because they are small.
"""
from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import nflreadpy as nfl
import polars as pl

from ff.config import RAW_DIR

log = logging.getLogger("ff.ingest")


@dataclass(frozen=True)
class Dataset:
    name: str
    loader: Callable[..., pl.DataFrame]
    seasonal: bool = True
    default_start: int = 2012


# Which datasets feed which feature:
#   pbp, player_stats, schedules -> matchup guide (yards / fantasy points allowed by defenses)
#   injuries, snap_counts, rosters_weekly, players -> injury history, age, return-to-workload
#   ff_opportunity, ff_playerids -> player value / power rankings / ID mapping
DATASETS: dict[str, Dataset] = {
    d.name: d
    for d in [
        # Play-by-play is huge, and matchup data only needs 1-2 seasons of history.
        Dataset("pbp", nfl.load_pbp, default_start=2024),
        Dataset("player_stats", nfl.load_player_stats),
        Dataset("schedules", nfl.load_schedules),
        Dataset("injuries", nfl.load_injuries),
        Dataset("snap_counts", nfl.load_snap_counts, default_start=2013),  # no 2012 data
        Dataset("rosters", nfl.load_rosters),
        Dataset("team_stats", nfl.load_team_stats, default_start=2022),  # defense stats for the DEF projection
        Dataset("rosters_weekly", nfl.load_rosters_weekly),  # IR/PUP status by week: catches injuries the report misses
        Dataset("ff_opportunity", nfl.load_ff_opportunity),
        Dataset("players", nfl.load_players, seasonal=False),
        Dataset("ff_playerids", nfl.load_ff_playerids, seasonal=False),
        Dataset("teams", nfl.load_teams, seasonal=False),
    ]
}


def season_path(name: str, season: int) -> Path:
    return RAW_DIR / name / f"{name}_{season}.parquet"


def flat_path(name: str) -> Path:
    return RAW_DIR / f"{name}.parquet"


def download_dataset(ds: Dataset, start: int | None, end: int, current: int, refresh: bool) -> None:
    if not ds.seasonal:
        df = ds.loader()
        flat_path(ds.name).parent.mkdir(parents=True, exist_ok=True)
        df.write_parquet(flat_path(ds.name))
        log.info("%-15s %s rows", ds.name, f"{df.height:,}")
        return

    first = start if start is not None else ds.default_start
    for season in range(first, end + 1):
        path = season_path(ds.name, season)
        # Finished seasons never change (mostly), so skip them unless --refresh.
        if path.exists() and season < current and not refresh:
            log.debug("%-15s %s already on disk, skipping", ds.name, season)
            continue
        try:
            df = ds.loader([season])
        except Exception as exc:  # e.g. season not published yet
            log.warning("%-15s %s failed: %s", ds.name, season, exc)
            continue
        if df.height == 0:
            log.warning("%-15s %s returned no rows, skipping", ds.name, season)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        df.write_parquet(path)
        log.info("%-15s %s  %s rows", ds.name, season, f"{df.height:,}")


def count_rows(files: list[Path]) -> int:
    """Row count per file (schemas can differ between seasons, so don't scan them together)."""
    return sum(pl.scan_parquet(f).select(pl.len()).collect().item() for f in files)


def report() -> None:
    """Print rows and season coverage of everything currently in data/raw."""
    print(f"{'dataset':<16}{'seasons':<14}{'files':>6}{'rows':>12}{'size (MB)':>12}")
    for name, ds in DATASETS.items():
        if ds.seasonal:
            files = sorted((RAW_DIR / name).glob(f"{name}_*.parquet"))
            if not files:
                print(f"{name:<16}{'-':<14}{0:>6}{0:>12}{0:>12}")
                continue
            years = [int(m.group(1)) for f in files if (m := re.search(r"_(\d{4})\.parquet$", f.name))]
            rows = count_rows(files)
            span = f"{min(years)}-{max(years)}"
        else:
            files = [flat_path(name)] if flat_path(name).exists() else []
            if not files:
                print(f"{name:<16}{'-':<14}{0:>6}{0:>12}{0:>12}")
                continue
            rows = count_rows(files)
            span = "n/a"
        mb = sum(f.stat().st_size for f in files) / 1e6
        print(f"{name:<16}{span:<14}{len(files):>6}{rows:>12,}{mb:>12.1f}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Download nflverse data to data/raw as Parquet.")
    parser.add_argument("--datasets", nargs="+", choices=sorted(DATASETS), default=None,
                        help="datasets to download (default: all)")
    parser.add_argument("--start", type=int, default=None,
                        help="first season (default: per-dataset, see DATASETS)")
    parser.add_argument("--end", type=int, default=None,
                        help="last season (default: current season)")
    parser.add_argument("--refresh", action="store_true",
                        help="re-download seasons that are already on disk")
    parser.add_argument("--report", action="store_true",
                        help="only print a summary of data/raw and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    if args.report:
        report()
        return

    current = nfl.get_current_season()
    end = args.end if args.end is not None else current
    for name in args.datasets or DATASETS:
        download_dataset(DATASETS[name], args.start, end, current, args.refresh)

    print()
    report()


if __name__ == "__main__":
    main()
