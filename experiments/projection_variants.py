"""Backtest of projection variants (recency half-life, current-season boost, opportunity blend, team-offense
factor, usage-trend factor). Run: python experiments/projection_variants.py  (results are summarized in the README).
Not part of the site: it is the evidence behind the model defaults."""
import polars as pl, numpy as np
from ff.data import load_raw
from ff.features.injuries import build_all
from ff.league import League, POSITIONS
from ff.models.value import current_roster, WEEKS

rw = load_raw("rosters_weekly")
T = build_all(2025)
log, directory = T["log"], T["directory"]
fo = (load_raw("ff_opportunity").select(gsis_id="player_id", season=pl.col("season").cast(pl.Int32), week=pl.col("week").cast(pl.Int32), exp="total_fantasy_points_exp")
      .unique(["gsis_id","season","week"]))
log = log.join(fo, on=["gsis_id","season","week"], how="left").with_columns(exp2=pl.coalesce("exp","ppr"))
sched = load_raw("schedules").filter(pl.col("game_type")=="REG")
tp = pl.concat([sched.select("season","week",team="home_team",pts="home_score"), sched.select("season","week",team="away_team",pts="away_score")]).drop_nulls("pts")

def project(as_of, hl, boost, lam, k=4.0, min_share=0.2, window=3, gamma=0.0, tw=6):
    season, week = as_of
    active = current_roster(rw, as_of)
    now_t = season*WEEKS+week
    h = (log.filter((pl.col("key")<season*100+week)&(pl.col("season")>season-window)&(pl.col("offense_pct")>=min_share)&pl.col("ppr").is_not_null())
         .join(active.select("gsis_id","position","team"), on="gsis_id")
         .with_columns(w=0.5**((now_t-(pl.col("season")*WEEKS+pl.col("week")))/hl) * pl.when(pl.col("season")==season).then(boost).otherwise(1.0),
                       pts=(1-lam)*pl.col("ppr")+lam*pl.col("exp2")))
    agg = h.group_by("gsis_id").agg(pl.len().alias("games"), pl.col("w").sum().alias("sw"), (pl.col("w")*pl.col("pts")).sum().alias("wp")).with_columns(raw=pl.col("wp")/pl.col("sw"))
    tab = active.select("gsis_id","position","team").join(agg, on="gsis_id", how="left").with_columns(pl.col("games").fill_null(0), pl.col("sw").fill_null(0.0), pl.col("wp").fill_null(0.0))
    League_ = League(teams=12)
    repl = {}
    for pos in POSITIONS:
        v = tab.filter((pl.col("position")==pos)&(pl.col("games")>=6)).sort("raw", descending=True)["raw"]
        repl[pos] = float(v[min(League_.replacement_rank(pos), len(v))-1])
    r = pl.DataFrame({"position":list(repl),"repl":list(repl.values())})
    out = tab.join(r, on="position").with_columns(proj=(pl.col("wp")+k*pl.col("repl"))/(pl.col("sw")+k))
    if gamma:
        # team offense recent vs history factor
        hist = tp.filter((pl.col("season")*100+pl.col("week")<season*100+week)&(pl.col("season")>season-window))
        allp = hist.with_columns(t=pl.col("season")*WEEKS+pl.col("week")).sort("team","t")
        recent = allp.group_by("team").agg(pl.col("pts").tail(tw).mean().alias("recent"), pl.col("pts").mean().alias("hist"))
        recent = recent.with_columns(f=(1+gamma*(pl.col("recent")/pl.col("hist")-1)).clip(0.9,1.1))
        out = out.join(active.select("gsis_id","team"), on="gsis_id").join(recent.select("team","f"), on="team", how="left").with_columns(proj=pl.col("proj")*pl.col("f").fill_null(1.0))
    return out.select("gsis_id","position","proj","games")

played = log.filter((pl.col("offense_pct")>=0.2)&pl.col("ppr").is_not_null())
def run(hl, boost, lam, gamma=0.0, weeks=(2,4,8,12), seasons=(2022,2023,2024,2025)):
    rows=[]
    for s in seasons:
        for w in weeks:
            p = project((s,w), hl, boost, lam, gamma=gamma)
            act = played.filter((pl.col("season")==s)&(pl.col("week")>=w)&(pl.col("week")<=17)).group_by("gsis_id").agg(pl.len().alias("n"), pl.col("ppr").mean().alias("actual")).filter(pl.col("n")>=4)
            rows.append(p.join(act, on="gsis_id").filter(pl.col("games")>=4).with_columns(week=w))
    d = pl.concat(rows)
    mae = (d["proj"]-d["actual"]).abs().mean()
    cor = np.corrcoef(d["proj"].to_numpy(), d["actual"].to_numpy())[0,1]
    early = d.filter(pl.col("week")<=4); maee = (early["proj"]-early["actual"]).abs().mean()
    return len(d), cor, mae, maee

print(f"{'config':<34}{'n':>6}{'corr':>7}{'MAE':>7}{'MAE wk2-4':>10}")
for name, args in [("baseline hl17 b1 λ0",(17,1,0)),("hl10",(10,1,0)),("hl6",(6,1,0)),("boost2",(17,2,0)),("boost3",(17,3,0)),("boost5",(17,5,0)),
                   ("λ0.3",(17,1,0.3)),("λ0.5",(17,1,0.5)),("λ0.7",(17,1,0.7)),("hl10 boost3 λ0.5",(10,3,0.5)),("hl17 boost3 λ0.5",(17,3,0.5)),("hl10 boost2 λ0.5",(10,2,0.5)),("hl17 boost3 λ0.5 team.5",(17,3,0.5,0.5))]:
    n,c,m,me = run(*args)
    print(f"{name:<34}{n:>6}{c:>7.3f}{m:>7.3f}{me:>10.3f}")

print("\n--- usage-trend factor (opportunity share now vs baseline) ---")
fo2 = (load_raw("ff_opportunity").with_columns(pl.col("season").cast(pl.Int32), pl.col("week").cast(pl.Int32))
       .select(gsis_id="player_id", season="season", week="week",
               opp=(pl.col("rush_attempt").fill_null(0)+pl.col("rec_attempt").fill_null(0)),
               opp_team=(pl.col("rush_attempt_team").fill_null(0)+pl.col("rec_attempt_team").fill_null(0)))
       .with_columns(share=pl.when(pl.col("opp_team")>0).then(pl.col("opp")/pl.col("opp_team"))).unique(["gsis_id","season","week"]))
log_u = log.join(fo2.select("gsis_id","season","week","share"), on=["gsis_id","season","week"], how="left")

def usage_factor(as_of, active, hl_now, hl_base, boost, window=3):
    season, week = as_of
    now_t = season*WEEKS+week
    h = (log_u.filter((pl.col("key")<season*100+week)&(pl.col("season")>season-window)&(pl.col("offense_pct")>=0.2)&pl.col("share").is_not_null())
         .join(active.select("gsis_id","position"), on="gsis_id").filter(pl.col("position")!="QB")
         .with_columns(dt=now_t-(pl.col("season")*WEEKS+pl.col("week")), b=pl.when(pl.col("season")==season).then(boost).otherwise(1.0))
         .with_columns(wn=0.5**(pl.col("dt")/hl_now)*pl.col("b"), wb=0.5**(pl.col("dt")/hl_base)*pl.col("b")))
    g = h.group_by("gsis_id").agg((pl.col("wn")*pl.col("share")).sum()/pl.col("wn").sum(), (pl.col("wb")*pl.col("share")).sum()/pl.col("wb").sum(), pl.len().alias("n_u"))
    g = g.rename({"share":"u_now","share_right":"u_base"}) if "share_right" in g.columns else g
    return g

def run_u(gamma, hl_now=4, boost=3, lo=0.75, hi=1.33, weeks=(2,4,8,12), seasons=(2022,2023,2024,2025)):
    rows=[]
    for s in seasons:
        for w in weeks:
            p = project((s,w), 17, boost, 0.0)
            active = current_roster(rw, (s,w))
            u = usage_factor((s,w), active, hl_now, 17, boost)
            cols = u.columns
            u = u.with_columns(f=(pl.col(cols[1])/pl.col(cols[2])).clip(lo,hi))
            p = p.join(u.select("gsis_id","f","n_u"), on="gsis_id", how="left").with_columns(proj=pl.col("proj")*(pl.col("f").fill_null(1.0)**gamma))
            act = played.filter((pl.col("season")==s)&(pl.col("week")>=w)&(pl.col("week")<=17)).group_by("gsis_id").agg(pl.len().alias("n"), pl.col("ppr").mean().alias("actual")).filter(pl.col("n")>=4)
            rows.append(p.join(act, on="gsis_id").filter(pl.col("games")>=4).with_columns(week=w))
    d = pl.concat(rows)
    early = d.filter(pl.col("week")<=4)
    return len(d), np.corrcoef(d["proj"].to_numpy(), d["actual"].to_numpy())[0,1], (d["proj"]-d["actual"]).abs().mean(), (early["proj"]-early["actual"]).abs().mean()
print(f"{'config':<34}{'n':>6}{'corr':>7}{'MAE':>7}{'MAE wk2-4':>10}")
for name, kw in [("boost3 (no usage)",dict(gamma=0.0)),("usage γ0.3",dict(gamma=0.3)),("usage γ0.6",dict(gamma=0.6)),("usage γ1.0",dict(gamma=1.0)),("usage γ0.6 hl_now2",dict(gamma=0.6,hl_now=2)),("usage γ0.6 hl_now8",dict(gamma=0.6,hl_now=8))]:
    n,c,m,me = run_u(**kw)
    print(f"{name:<34}{n:>6}{c:>7.3f}{m:>7.3f}{me:>10.3f}")
