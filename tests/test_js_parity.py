"""The website's JavaScript must give the same numbers as the Python models (checked on the real values file)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ff.league import League
from ff.models.power import replacement_levels, team_power, values_for_league
from ff.models.trade import evaluate_trade

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "site" / "data" / "player_values.json"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not DATA.exists(), reason="needs node and site/data/player_values.json"
)


def _by_pos(values, pos):
    return [i for i, p in sorted(values.items(), key=lambda kv: -kv[1]["value"]) if p["pos"] == pos]


def test_power_and_trades_match_python(tmp_path):
    data = json.loads(DATA.read_text(encoding="utf-8"))
    players, pools = data["players"], data["pools"]
    ids = {pos: _by_pos(players, pos) for pos in ("QB", "RB", "WR", "TE", "K", "DEF")}
    qb, rb, wr, te, k, d = (ids[p] for p in ("QB", "RB", "WR", "TE", "K", "DEF"))

    rosters = [
        {"players": qb[:1] + rb[:4] + wr[:3] + te[:1] + k[:1] + d[:1]},
        {"players": qb[5:6] + rb[10:14] + wr[10:14] + te[6:7] + k[3:4], "reserve": [rb[12]]},
        {"players": qb[2:3] + rb[0:2] + wr[20:26] + d[5:7]},                  # no TE, deep WR bench
        {"players": rb[20:24] + wr[30:33] + qb[8:9], "reserve": [wr[31]], "taxi": [rb[23]]},  # IR + taxi
    ]
    trades = [
        {"my": 0, "their": 1, "give": [rb[3]], "get": [wr[10]]},
        {"my": 1, "their": 0, "give": [rb[10], wr[11]], "get": [qb[0]]},
        {"my": 2, "their": 3, "give": [wr[20]], "get": [rb[20], rb[21]]},
        {"my": 3, "their": 1, "give": [rb[22]], "get": [rb[12]]},              # IR player changes teams
    ]
    leagues = [
        League(),
        League(teams=10, slots=("QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "SUPER_FLEX", "K", "DEF"), bench=6, ir=2),
        League(teams=14, slots=("QB", "RB", "RB", "WR", "WR", "TE", "WRRB_FLEX", "REC_FLEX", "DEF"), bench=4, ir=1),
    ]
    adjust = {wr[10]: 2.0, rb[0]: -1.5}
    team_adjust = {players[rb[3]]["team"]: 5.0}

    case = {
        "players": players, "pools": pools, "model": data["model"], "rosters": rosters, "trades": trades,
        "adjust": adjust, "team_adjust": team_adjust,
        "leagues": [{"teams": lg.teams, "slots": list(lg.slots), "bench": lg.bench, "ir": lg.ir} for lg in leagues],
    }
    f = tmp_path / "case.json"
    f.write_text(json.dumps(case))
    out = subprocess.run(["node", str(ROOT / "web_tests" / "parity.js"), str(f)], capture_output=True, text=True, check=True)
    js = json.loads(out.stdout)

    for lg, res in zip(leagues, js):
        repl = replacement_levels(pools, lg)
        assert res["repl"] == pytest.approx(repl)
        values = values_for_league(players, repl, adjust, team_adjust)

        for r, got in zip(rosters, res["power"]):
            py = team_power(r["players"], values, repl, lg, r.get("reserve", ()), r.get("taxi", ()))
            assert got["power"] == pytest.approx(py["power"], abs=0.06)
            assert got["starters"] == pytest.approx(py["starters_ppg"], abs=0.06)
            assert got["depth"] == pytest.approx(py["depth"], abs=0.06)
            assert got["lineup"] == [pid for _, pid in py["lineup"]]
            assert got["bench"] == py["bench"]

        for t, got in zip(trades, res["trades"]):
            my, their = rosters[t["my"]], rosters[t["their"]]
            py = evaluate_trade(my["players"], their["players"], t["give"], t["get"], values, repl, lg,
                                my.get("reserve", ()), their.get("reserve", ()), my.get("taxi", ()), their.get("taxi", ()))
            assert got["give"] == pytest.approx(py["give_value"], abs=0.06)
            assert got["get"] == pytest.approx(py["get_value"], abs=0.06)
            assert got["verdict"] == py["verdict"]
            assert got["you"] == pytest.approx(py["you"]["delta_power"], abs=0.11)
            assert got["them"] == pytest.approx(py["them"]["delta_power"], abs=0.11)
