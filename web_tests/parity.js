// Reads a case JSON (data, leagues, rosters, trades), prints the JS results as JSON.
// Used by tests/test_js_parity.py to check that JS and Python compute the same numbers.
const fs = require("fs");
const FF = require("../site/js/core.js");
const c = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const out = [];
for (const lg of c.leagues) {
  const repl = FF.replacementLevels(c.pools, lg, c.model);
  const values = FF.valuesForLeague(c.players, repl, c.adjust, c.team_adjust);
  const ctx = { elig: c.model.slot_eligible, repl, league: lg };
  const power = c.rosters.map((r) => {
    const t = FF.teamPower(r.players, values, ctx, r.reserve, r.taxi);
    return { power: t.power, starters: t.startersPpg, depth: t.depth, lineup: t.lineup.map((x) => (x.player ? x.player.id : null)), bench: t.bench.map((x) => x.id) };
  });
  const trades = c.trades.map((x) => {
    const r = FF.evaluateTrade(c.rosters[x.my], c.rosters[x.their], x.give, x.get, values, ctx);
    return { give: r.giveValue, get: r.getValue, verdict: r.verdict, you: r.you.deltaPower, them: r.them.deltaPower, youStarters: r.you.deltaStarters };
  });
  out.push({ repl, power, trades });
}
console.log(JSON.stringify(out));
