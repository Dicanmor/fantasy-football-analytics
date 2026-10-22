"""Sleeper's public player dump -> a small table of CURRENT injury details, keyed by Sleeper player id.

``GET https://api.sleeper.app/v1/players/nfl`` is one big JSON (several MB, Sleeper asks for at most one call a day)
whose player objects carry, besides the documented ``injury_status``, the fields ``injury_body_part`` (e.g.
"Hamstring", "Knee - ACL"), ``injury_notes`` (e.g. "Strain", "Surgery") and ``news_updated`` (epoch ms of the latest
news item). Unlike the weekly NFL injury report this exists for players who went straight to IR (rookies included),
and it can name the specific injury (ACL vs MCL).

It is fetched once per pipeline run and cached as ``data/raw/sleeper_injuries.parquet``; if Sleeper is unreachable
the previous file is used, and if there is none the pipeline simply runs without it.
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

import polars as pl

SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
SCHEMA = {"sleeper_id": pl.String, "sl_status": pl.String, "sl_part": pl.String, "sl_notes": pl.String, "sl_news": pl.Int64}
# A torn ACL or Achilles ends the season for practical purposes (typical return is 8-12 months). Assumption, not data.
SEASON_ENDING = re.compile(r"\b(ACL|Achilles)\b", re.IGNORECASE)


def default_fetch(url: str = SLEEPER_PLAYERS_URL) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "ff-analytics (github.com/Dicanmor/fantasy-football-analytics)"})
    with urllib.request.urlopen(req, timeout=90) as r:  # noqa: S310 - fixed https URL
        return json.loads(r.read())


def injury_frame(players: dict) -> pl.DataFrame:
    """Only players Sleeper currently lists with an injury status (healthy players keep stale body-part text)."""
    rows = []
    for sid, p in players.items():
        status = p.get("injury_status")
        if not status:
            continue
        news = p.get("news_updated")
        rows.append({
            "sleeper_id": str(sid), "sl_status": status, "sl_part": p.get("injury_body_part"), "sl_notes": p.get("injury_notes"),
            "sl_news": int(news) if isinstance(news, (int, float)) else None,
        })
    return pl.DataFrame(rows, schema=SCHEMA)


def base_body_part(part: str | None) -> str | None:
    """"Knee - ACL" -> "Knee" (the label the weekly NFL report uses, so injury history can be matched)."""
    return part.split(" - ")[0].strip() if part else None


def is_season_ending(part: str | None) -> bool:
    return bool(part and SEASON_ENDING.search(part))


def refresh_injuries(path: Path, fetch=default_fetch) -> pl.DataFrame | None:
    """Download and cache today's injury table; fall back to the cached file; ``None`` if neither exists."""
    try:
        frame = injury_frame(fetch())
        if frame.height:
            path.parent.mkdir(parents=True, exist_ok=True)
            frame.write_parquet(path)
            return frame
    except Exception as e:  # noqa: BLE001 - this source is a bonus, never fail the build over it
        print(f"sleeper injuries not refreshed: {e}")
    return pl.read_parquet(path) if path.exists() else None
