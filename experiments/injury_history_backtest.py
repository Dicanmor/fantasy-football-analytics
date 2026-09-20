"""Does a player's own injury history predict next season's games missed? (shrinkage-strength sweep, 2016-2025).
Run: python experiments/injury_history_backtest.py  (result: barely; see README, Injury analysis)."""
import polars as pl, numpy as np
from ff.data import load_raw
from ff.features.injuries import build_all, player_risk, ON_ROSTER, SKILL
T = build_all(2025)
ep, log, d = T["episodes"], T["log"], T["directory"]
rw = load_raw("rosters_weekly")
onr = (rw.filter((pl.col("game_type")=="REG")&pl.col("status").is_in(ON_ROSTER)&pl.col("position").is_in(SKILL))
       .with_columns(pl.col("season").cast(pl.Int32)).group_by("gsis_id","season").agg(pl.col("week").n_unique().alias("g")))
miss = ep.group_by("gsis_id","season").agg(pl.col("games_missed").sum().alias("m"))
act = onr.join(miss, on=["gsis_id","season"], how="left").with_columns(pl.col("m").fill_null(0), rate=pl.col("m").fill_null(0)/pl.col("g"))
played = log.group_by("gsis_id","season").agg(pl.len().alias("gp"))
res = {}
for k in [1e-6, 8, 17, 34, 68, 170, 1e6]:
    preds=[]
    for S in range(2016, 2026):
        r = player_risk(ep.filter(pl.col("season")<S), rw, d, S-1, prior_games=k)
        # relevant players: >= 8 games played in S-1
        rel = played.filter((pl.col("season")==S-1)&(pl.col("gp")>=8)).select("gsis_id")
        a = act.filter(pl.col("season")==S).select("gsis_id", act_rate="rate", g="g").filter(pl.col("g")>=8)
        j = r.join(rel, on="gsis_id", how="semi").join(a, on="gsis_id").select(pred="shrunk_rate", act="act_rate")
        preds.append(j)
    dd = pl.concat(preds)
    res[k] = (len(dd), float(np.corrcoef(dd["pred"].to_numpy(), dd["act"].to_numpy())[0,1]), float((dd["pred"]-dd["act"]).abs().mean()*17), float(((dd["pred"]-dd["act"])**2).mean()**0.5*17))
print(f"{'prior_games':>12}{'n':>7}{'corr':>7}{'MAE/17g':>9}{'RMSE/17g':>10}")
for k,(n,c,m,r) in res.items(): print(f"{k:>12g}{n:>7}{c:>7.3f}{m:>9.2f}{r:>10.2f}")
