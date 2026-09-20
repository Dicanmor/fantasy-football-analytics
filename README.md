# FF Analytics

Fantasy football analytics built on open NFL data ([nflverse](https://github.com/nflverse)).
The goal is a fast, easy-to-use website that answers the questions I actually ask every week during the season.

> **Status:** early development. Data ingestion is done; modeling and the website are next.

## Planned features

| Feature | What it answers | Status |
|---|---|---|
| **Data pipeline** | Reproducible download of play-by-play, player stats, injuries, snap counts and rosters | ✅ Done |
| **Matchup guide** | How many rushing / passing yards and fantasy points does each defense allow, by position? | 🟡 v1 (backtested, being improved) |
| **Injury analysis** | How often does a player get hurt, how does age factor in, how do stats change after an injury, and how long until his snap share is back to normal? | 🟡 v1 (see findings below) |
| **Player value model** | Points-of-value per player, blending stats with a subjective ("ball knowledge") adjustment | 🟡 v1 (backtested) |
| **Trade calculator** | Is this trade a win or a loss? | ⬜ Planned |
| **Team power rankings** | Rank every team in a league by roster value | 🟡 v1 (Python; browser version + Sleeper import next) |
| **Website** | Static site on GitHub Pages, refreshed weekly by GitHub Actions | ⬜ Planned |

## Data

All raw data comes from **nflverse** through the [`nflreadpy`](https://github.com/nflverse/nflreadpy) package
(no API keys, no scraping). Most nflverse data is licensed CC-BY 4.0; please credit nflverse if you reuse it.

| Dataset | Used for |
|---|---|
| `pbp`, `player_stats`, `schedules` | Matchup guide (what defenses allow) |
| `injuries`, `rosters_weekly`, `snap_counts`, `players` | Injury history, IR stints, age, return to normal workload |
| `ff_opportunity`, `ff_playerids` | Expected fantasy points, player value, ID mapping across sources |

Design choices worth knowing:

- **Different history per feature.** Matchups only need ~2 seasons (rosters and schemes change every year); injuries need a
  long history so each injury type has a usable sample. That is why `pbp` defaults to 2024+ and the rest to 2012+.
- **The injury report misses injured-reserve players** (e.g. Saquon Barkley's 2020 ACL season has zero rows). Injury
  episodes therefore combine the report (`Out`) with weekly roster status (`RES`/`PUP`), and workload is measured with snap share.
- **Schemas drift between seasons** (e.g. `injuries` gained `season_type` and lost `date_modified` in 2025), so use
  `ff.data.load_raw()`, which concatenates seasons safely.
- **IDs differ across tables** (`gsis_id` in injuries, `pfr_player_id` in snap counts), so `players` / `ff_playerids`
  are used to join them.

## Matchup guide (v1)

For each defense: fantasy points allowed to QB / RB / WR / TE plus passing and rushing yards allowed, averaged with
exponential recency weights over the last 2 seasons and expressed relative to the league (`score` 1.10 = allows 10% more
than average; `rank` 1 = easiest). Ratings "as of" week W use only games before W, so there is no look-ahead leakage.

**Backtest (2023-2025, weeks 4+):** correlation between the pre-game score and what the defense actually allowed that week
is small: about 0.06-0.07 for QB/WR/TE points, 0.12 for RB points and 0.13-0.17 for yards. Defense-vs-position signal is real but weak
on its own; v2 will adjust for the strength of the offenses each defense faced and combine it with the opposing offense.

## Injury analysis (v1)

`python -m ff.features.injuries` builds injury *episodes* (consecutive team games missed, byes skipped, relapses merged),
measures recovery, and rates player durability. Outputs go to `data/processed/`.

**How recovery is measured:** workload = share of the team's offensive snaps; baseline = the 8 games before the injury
(excluding the game he got hurt in); "back to normal" = two straight games at >= 85% of baseline. Production is compared
as fantasy points per game (PPR) over the first 4 games back vs. baseline, and against a **control group** of injury-free
windows so ordinary drift is not blamed on the injury.

**Findings (QB/RB/WR/TE, 2013-2026):**

| | PPG in first 4 games back vs. before |
|---|---|
| Control (no injury) | 0.96 |
| Injured, 1-2 games missed | 0.83-0.91 |
| Injured, 6+ games missed | RB 0.60, WR 0.68, TE 0.69, QB 0.91 |

- Snap share usually comes back almost immediately (median 0-1 games); the damage shows up in **production**, not opportunity.
- Age is *not* a clean risk factor here: players 30+ miss fewer injury games per 17 (2.2) than players under 24 (3.0), most likely
  survivorship (injured veterans leave the league). A player's own history and position matter more than an age penalty.

**Known limitations:** IR stints count every game missed even for backups who would not have played; the first week of an
IR stint can be missed (roster status updates late); "not back to normal" mixes lingering injury with losing the job; no
matched control for player quality. v2 will restrict to fantasy-relevant roles.

## Player value model (v1)

League format: **PPR, 1 QB / 2 RB / 2 WR / 1 TE / 1 K / 1 DST, 5 bench, no flex** (see `ff/league.py`; team count via `--teams`).

```
value = max(0, (projected PPG - replacement PPG) x expected games played)      # rest-of-season points over replacement
```

- **Projected PPG:** PPR points per game over the last 3 seasons, weights halving every 17 weeks, ignoring games with < 20% of the
  team's offensive snaps (the game he got hurt in, garbage time), shrunk toward replacement level (no evidence = replacement player).
- **Replacement level:** PPG of the player ranked `teams x (starters + bench share)` at his position among players on a roster now.
- **Expected games:** remaining team games (byes handled) x (1 - injury-proneness from the injury module), minus expected remaining
  absence if he is injured now (learned from past injury episodes, separately for injured reserve vs. routine "Out") or the historical
  miss rate for Questionable / Doubtful.
- **Ball knowledge:** add rows to `data/manual/adjustments.csv` (`gsis_id` or `name`, `ppg_delta`, `note`) to adjust a player's PPG
  for things the stats cannot see (rookie usage, coaching change, new QB). Adjustments are explicit and versioned in git.

**Backtest of the PPG projection** (2022-2025, as of weeks 4 / 8 / 12; 2,127 player-windows; target = actual PPG over the rest of the season):

| Method | Correlation | Mean abs. error (PPG) |
|---|---|---|
| **This model** | **0.79** | **2.86** |
| Season-to-date PPG | 0.75 | 3.15 |
| Last season PPG | 0.72 | 3.21 |

Reproduce with `python -m ff.models.value --evaluate`. Only the PPG projection is backtested; windows overlap (same player at several
weeks) and only players with enough history are compared.

**Assumptions to challenge:** redraft horizon (rest of this season; no aging curve or future value), bench-slot shares per position,
K/DST excluded (streamable, ~zero value over replacement), no schedule-strength adjustment yet (the matchup signal was weak in backtests).

**Power rankings** (`ff/models/power.py`): expected weekly points of the best lineup (each player's PPG x expected availability) plus 30% of
bench depth above replacement. Try it: `python -m ff.models.power rosters.json`. The website will run the same logic in the browser
on rosters pulled from the Sleeper API, using `site/data/player_values.json` (keyed by Sleeper id).

## Quickstart

```bash
git clone https://github.com/<your-user>/fantasy-football-analytics.git
cd fantasy-football-analytics

python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

ff-download --report            # what is on disk right now
ff-download                     # download everything (pbp from 2024, the rest from 2012)
ff-download --datasets injuries snap_counts --start 2018
```

```python
from ff.data import load_raw

injuries = load_raw("injuries")                       # all downloaded seasons, Polars DataFrame
stats = load_raw("player_stats", seasons=[2025])
```

Build the matchup guide (writes CSVs to `data/processed/`):

```bash
python -m ff.features.defense                              # current season/week, PPR
python -m ff.features.defense --season 2025 --week 10 --scoring half
python -m ff.features.injuries                              # episodes, recovery, player risk
python -m ff.models.value --teams 12                        # player values -> data/processed + site/data JSON
python -m ff.models.value --evaluate                        # backtest the projection
python -m ff.models.power rosters.json                      # rank teams: {"Team": ["sleeper_id", ...]}
```

Data is stored as Parquet under `data/raw/` (git-ignored, fully reproducible). Finished seasons are skipped on re-runs;
the current season is always refreshed.

## Project structure

```
src/ff/
  config.py        # paths
  ingest/          # data download (ff-download CLI)
  features/defense.py  # matchup guide: allowed-per-game ratings by defense
  features/injuries.py # injury episodes, recovery vs. control, player durability
  league.py        # league format (scoring, lineup, bench)
  models/value.py  # player value: rest-of-season points over replacement
  models/power.py  # team power rankings from player values
data/manual/       # adjustments.csv: manual "ball knowledge" (tracked in git)
  data.py          # load_raw(): read downloaded data
  features/        # (planned) defense splits, injury episodes, player value
  models/          # (planned) power rankings, trade value
data/raw/          # downloaded data (git-ignored)
data/processed/    # small derived tables used by the site
notebooks/         # exploration
site/              # static website (GitHub Pages)
tests/             # pytest
```

## License

Code: MIT. Data: see nflverse licensing above.
