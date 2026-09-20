# FF Analytics

Fantasy football analytics built on open NFL data ([nflverse](https://github.com/nflverse)).
The goal is a fast, easy-to-use website that answers the questions I actually ask every week during the season.

> **Status:** working end to end (data pipeline, models, website with Sleeper import). Backtests below say what the models can and cannot do.

## Features

| Feature | What it answers | Status |
|---|---|---|
| **Data pipeline** | Reproducible download of play-by-play, player stats, injuries, snap counts, rosters, team stats and opportunity data | ✅ Done |
| **Matchup guide** | How many fantasy points does each defense allow to each position? Shown as a 0-100 score with Favorable / Medium / Tough labels | 🟡 Works, but a weak predictor (see below): used as context |
| **Injury analysis** | How long do injuries last, how do stats change after a return, does injury history predict anything? | ✅ Done (findings below) |
| **Player value model** | Rest-of-season points over replacement, with role context and your own adjustments | ✅ v2 (backtested) |
| **Team power rankings** | Rank every team by lineup (flex, K, DEF, IR aware) and depth | ✅ v2 |
| **Trade calculator** | Value exchanged **and** the effect on both lineups, with this season's context | ✅ v2 |
| **Trade finder** | Trades that fix the weakest spots of your lineup without gutting the other team | ✅ New |
| **Website** | Static site on GitHub Pages, refreshed weekly by GitHub Actions; reads your real Sleeper lineup | ✅ v2 |

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
  survivorship (injured veterans leave the league).
- **A player's own injury history barely predicts next season** (backtest 2016-2025: correlation 0.01-0.04 between his shrunk rate and games
  missed next year; error is lowest when position/age base rates dominate). So "injury-prone" labels are mostly noise; the value model
  uses base rates plus any *current* injury, which does matter.

**Known limitations:** IR stints count every game missed even for backups who would not have played; the first week of an
IR stint can be missed (roster status updates late); "not back to normal" mixes lingering injury with losing the job; no
matched control for player quality. v2 will restrict to fantasy-relevant roles.

## Player value model (v2)

Default league: **PPR, 1 QB / 2 RB / 2 WR / 1 TE / 1 FLEX / 1 K / 1 DEF, 5 bench, 3 IR, 12 teams** (`ff/league.py`). The website does not
use this default: it reads the connected Sleeper league's real lineup (`roster_positions`: any mix of FLEX / SUPER_FLEX / REC_FLEX /
WRRB_FLEX, K, DEF), bench size, IR and taxi slots, and recomputes replacement levels for that league in the browser.

```
value = max(0, (projected PPG - replacement PPG) x expected games played)      # rest-of-season points over replacement
```

- **Projected PPG:** PPR points per game over the last 3 seasons; weights halve every 17 weeks and **games of the current season count 3x**
  (roles change in the offseason); games with < 20% of the offensive snaps are ignored; shrunk toward replacement level.
- **Replacement level:** PPG of the player ranked `teams x (starters + flex share + bench share)` at his position (assumption constants in `ff/league.py`).
  K and DEF are streamed, so their replacement is ~1.5 x teams deep.
- **Expected games:** remaining team games (byes handled) x (1 - base miss rate by position/age) minus the expected remaining absence
  if he is injured now (IR vs routine "Out" are different), or the historical miss rate for Questionable / Doubtful.
- **K and DEF:** kicker points and team-defense points computed from play data with a common default scoring (documented in
  `ff/features/special.py`; not read from your league), same recency weighting, shrunk toward the average.
- **Context shown next to every player** (context, not part of the projection): this week's opponent and matchup score, the share of
  team rushes + targets over the last 4 games vs. last season, and the teammate he shares touches with (committee backfields).
- **Ball knowledge:** `data/manual/adjustments.csv` (`gsis_id` or `name`, `ppg_delta`, `note`) for permanent adjustments, and the website
  lets you set your own per-player PPG or per-team offense % on the fly; everything recalculates.

### What the backtests say (and what they do not)

As-of weeks 2 / 4 / 8 / 12, seasons 2022-2025, 4,373 player-windows, target = actual PPG over the rest of the season
(`experiments/projection_variants.py`; `python -m ff.models.value --evaluate` runs the smaller 4 / 8 / 12 version: corr 0.80, MAE 2.74
vs 3.15 for season-to-date PPG and 3.21 for last season's PPG):

| Variant | Corr. | MAE (PPG) | Adopted? |
|---|---|---|---|
| Half-life 17 weeks, no boost (baseline) | 0.799 | 2.886 | |
| Shorter half-life 10 / 6 weeks | 0.796 / 0.781 | 2.958 / 3.093 | No, worse: more history is better |
| **Current-season games x 3** | **0.803** | **2.779** | **Yes** (x2: 2.805, x5: 2.779) |
| Blend with opportunity-based expected points (30-70%) | 0.799-0.792 | 2.90-2.93 | No, no gain |
| Team-offense trend factor | 0.799 | 2.803 | No, worse |
| Recent-usage-share trend factor (gamma 0.3 / 1.0) | 0.803 / 0.790 | 2.768 / 2.823 | No, +/-0.4% is noise |

Honest reading: generic statistical adjustments do not add much on top of "weighted recent PPG". What the stats cannot see (an offense that
improved in the offseason, a committee that just formed, a rookie's real role) is exactly what the adjustment boxes are for. The context lines
show the *facts the data does have* (touch shares, teammates, matchup) so your adjustment is informed.

**Assumptions to challenge:** redraft horizon (rest of this season; no aging curve or future value), flex/bench share constants, default K/DEF scoring,
DEF ids assume Sleeper uses team abbreviations (LAR for the Rams), no schedule-strength adjustment (the matchup signal is weak).

## Power rankings

`ff/models/power.py` (mirrored in `site/js/core.js`, checked by `tests/test_js_parity.py` on three different league formats):

- A player's expected contribution is `replacement + availability x (PPG - replacement)`: when he is out, a waiver replacement plays.
- The **lineup** is filled with the real slots: positional slots first, then flex slots (FLEX / SUPER_FLEX / ...) with the best remaining
  eligible players; K and DEF are slots too. An empty slot counts as replacement level.
- **Depth is not linear.** A bench player only plays when a starter he can replace is out, so he is worth
  `P(at least r of those starters miss the week) x (PPG - replacement)`, where r is his rank among bench players of that position.
  A thin team with two elite RBs and healthy starters loses almost nothing for lacking a third RB, and a team with fragile starters gets more credit for depth.
- **IR** players do not use bench spots (they are listed separately), **taxi** players are ignored, only the best `bench` players count.

Try it: `python -m ff.models.power rosters.json`.

## Trade calculator and finder

- **Calculator:** value each side gives and gets (points over replacement), the effect on each team's power (lineup and depth), and a
  *Context* section per player (projection breakdown, matchup, touch share, teammate competition) with an adjustment box.
  Value says what a player is worth on any roster; the power delta says what *your* lineup gains or loses.
- **Finder:** ranks your lineup groups (QB, RB, WR, TE, FLEX, K, DEF) against the league, then searches 1-for-1 and 2-for-1 trades
  (you give two, get one) with every other team. A proposal must improve your lineup by >= 0.5 PPG, not hurt the partner's by more than 0.3
  and keep values within about +/-12% for you; proposals that also improve the partner are marked win-win. You can restrict it to positions.
  It runs in ~0.1 s in the browser for a 12-team league.

## Website

Static site in `site/` (plain HTML/CSS/JS, no build step). Tabs: **Power rankings** (click a team for its lineup with slots, bench, IR, taxi and
matchup badges), **Trade calculator**, **Trade finder**, **Player values** (sortable, with matchup, role and an adjustment column).

Matchup labels: score 0-100 = where the opponent ranks among the 32 defenses in fantasy points allowed to that position (100 = easiest).
Favorable >= 67, Tough <= 33, Medium in between (`MATCHUP_FAVORABLE` / `MATCHUP_TOUGH` in `ff/models/value.py`). Backtests showed only a weak link
between this score and actual points allowed (correlation 0.06-0.12), so treat it as context, not a forecast.

How it works: the Python pipeline exports `site/data/player_values.js` (values keyed by Sleeper id, plus PPG pools so the browser can compute
replacement levels for your league). The browser calls Sleeper's public read-only API for leagues and rosters and computes everything locally.
No server, no API keys; your username and adjustments are stored in `localStorage` only.

```bash
python -m ff.models.value                      # refresh site/data
python -m http.server -d site 8000             # then open http://localhost:8000
```

**Deploy (GitHub Pages):** repo Settings -> Pages -> Source: **GitHub Actions**. `.github/workflows/deploy.yml` runs the tests, downloads
fresh data, rebuilds the values and publishes `site/` every Tuesday (or on demand from the Actions tab).

**Tests:** `pytest` (Python + JS parity) and `cd web_tests && npm install && npm test` (JS logic and a jsdom smoke test of the page with a
mocked Sleeper API).

**Known limits:** the league import was confirmed against the live Sleeper API by hand (username and league ID); the automated tests use mocks.
The GitHub Actions workflow has been validated but not yet run on GitHub. Sleeper's API is free for non-commercial use only.

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
python -m ff.models.value                                    # player values -> data/processed + site/data JSON
python -m ff.models.value --evaluate                        # backtest the projection
python -m ff.models.power rosters.json                      # rank teams: {"Team": ["sleeper_id", ...]}
```

Data is stored as Parquet under `data/raw/` (git-ignored, fully reproducible). Finished seasons are skipped on re-runs;
the current season is always refreshed.

## Project structure

```
src/ff/
  config.py            # paths
  league.py            # league format (scoring, lineup, bench)
  data.py              # load_raw(): read downloaded data
  ingest/              # data download (ff-download CLI)
  features/defense.py  # matchup guide: allowed-per-game ratings by defense
  features/injuries.py # injury episodes, recovery vs. control, player durability
  models/value.py      # player value: rest-of-season points over replacement
  features/special.py  # kicker and team-defense projections
  models/power.py      # lineup-aware team power rankings
  models/trade.py      # trade evaluation: value + power delta for both teams
experiments/           # backtests behind the model defaults (projection variants, injury history)
site/                  # the website (index.html, js/core.js, js/app.js, data/)
web_tests/             # Node tests for the site (logic + jsdom smoke test)
tests/                 # pytest (includes JS parity check)
data/raw/              # downloaded data (git-ignored)
data/processed/        # derived tables (CSV)
data/manual/           # adjustments.csv: manual "ball knowledge" (tracked in git)
notebooks/             # exploration
.github/workflows/     # weekly data refresh + Pages deploy
```

## License

Code: MIT. Data: see nflverse licensing above.
