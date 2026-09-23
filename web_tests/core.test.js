// Run: node web_tests/core.test.js   (no dependencies)
const assert = require("assert");
const FF = require("../site/js/core.js");

const model = {
  slot_eligible: { QB: ["QB"], RB: ["RB"], WR: ["WR"], TE: ["TE"], K: ["K"], DEF: ["DEF"], FLEX: ["RB", "WR", "TE"], SUPER_FLEX: ["QB", "RB", "WR", "TE"] },
  flex_share: { FLEX: { RB: 0.45, WR: 0.45, TE: 0.1 }, SUPER_FLEX: { QB: 0.7, RB: 0.1, WR: 0.15, TE: 0.05 } },
  bench_share: { QB: 0.4, RB: 1.8, WR: 1.8, TE: 0.5 }, kd_depth: 0.5, matchup: { favorable: 67, tough: 33 },
};
const repl = { QB: 17, RB: 9, WR: 10, TE: 9, K: 8, DEF: 6 };
const p = (pos, ppg, avail = 1, name = "x", team = "AAA") => ({ pos, ppg, avail, value: Math.max(0, (ppg - repl[pos]) * 10), exp_games: 10, name, team });
const mk = (league) => ({ elig: model.slot_eligible, repl, league });
const NOFLEX = mk({ teams: 12, slots: ["QB", "RB", "RB", "WR", "WR", "TE", "K", "DEF"], bench: 5, ir: 3 });
const FLEX = mk({ teams: 12, slots: ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF"], bench: 5, ir: 3 });
const close = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

// replacement level: flex share and K/DEF depth, rounding half up
close(FF.replacementRank("RB", FLEX.league, model), 51);
close(FF.replacementRank("RB", NOFLEX.league, model), 46);          // 12 x (2 + 1.8) = 45.6
close(FF.replacementRank("K", FLEX.league, model), 18);
close(FF.replacementRank("RB", { ...FLEX.league, teams: 10 }, model), 43); // 10 x 4.25 = 42.5 -> 43 (half up, like Python)
assert.deepStrictEqual(FF.replacementLevels({ RB: [20, 18, 16, 14, 12], QB: [] }, { teams: 1, slots: ["RB", "RB", "FLEX"], bench: 5, ir: 3 }, model), { RB: 14 });

// adjustments: player delta, then team %
const adj = FF.valuesForLeague({ a: { pos: "RB", ppg: 12, exp_games: 10, team: "AAA" } }, repl, { a: 2 }, { AAA: 10 });
close(adj.a.ppg, 15.4); close(adj.a.value, (15.4 - 9) * 10);

// lineup: flex takes best leftover, bench is not cut, empty slots get replacement
const rbs = Object.fromEntries([0, 1, 2, 3, 4].map((i) => ["r" + i, p("RB", 20 - i)]));
let t = FF.teamPower(Object.keys(rbs), rbs, FLEX);
assert.strictEqual(t.lineup.find((x) => x.slot === "FLEX").player.id, "r2");
assert.deepStrictEqual(t.bench.map((x) => x.id), ["r3", "r4"]);
t = FF.teamPower(["q", "r1", "SEA", "kicker-9"], { q: p("QB", 20), r1: p("RB", 18) }, NOFLEX);
close(t.startersPpg, 20 + 18 + 9 + 10 + 10 + 9 + 8 + 6); // unknown ids ignored; empty slots at replacement

// IR and taxi never use bench spots; IR player can still be counted in the lineup by expected value
const v2 = { b1: p("WR", 12), irs: p("RB", 19, 0.5), tx: p("RB", 15) };
t = FF.teamPower(Object.keys(v2), v2, mk({ ...NOFLEX.league, bench: 1 }), ["irs"], ["tx"]);
assert.ok(!t.bench.some((x) => x.id === "tx" || x.id === "irs") && t.ir.map((x) => x.id).includes("irs") && t.taxi.length === 1);

// depth is worth more when starters are fragile, and a thin team with two elite RBs is not ranked last
const depthOf = (a) => FF.teamPower(["r1", "r2", "b"], { r1: p("RB", 18, a), r2: p("RB", 17, a), b: p("RB", 13) }, NOFLEX).depth;
assert.ok(depthOf(0.6) > depthOf(0.95) && depthOf(0.95) > 0);
assert.ok(FF.pAtLeast(1, [0.5, 0.5]) - 0.75 < 1e-12 && FF.pAtLeast(3, [0.5]) === 0);
const stars = { r1: p("RB", 22), r2: p("RB", 21), q: p("QB", 22), w1: p("WR", 15), w2: p("WR", 14), t: p("TE", 13) };
const avg = { x0: p("RB", 12), x1: p("RB", 12), x2: p("RB", 12), y0: p("WR", 12), y1: p("WR", 12), y2: p("WR", 12), q2: p("QB", 19), t2: p("TE", 10) };
const ranked = FF.powerRankings(
  [{ rosterId: 1, name: "thin", players: Object.keys(stars) }, { rosterId: 2, name: "deep", players: Object.keys(avg) }], { ...stars, ...avg }, FLEX);
assert.strictEqual(ranked[0].team.name, "thin");

// trades: validation, verdicts, roster-aware deltas, flex-aware
const V = { rb_star: p("RB", 20), rb_mid: p("RB", 14), rb_bench: p("RB", 11), wr1: p("WR", 15), wr2: p("WR", 13) };
const A = { players: ["rb_star", "rb_mid", "wr1", "wr2"], reserve: [], taxi: [] }, B = { players: ["rb_bench"], reserve: [], taxi: [] };
assert.throws(() => FF.evaluateTrade(A, B, ["rb_bench"], ["rb_bench"], V, NOFLEX));
assert.throws(() => FF.evaluateTrade(A, B, ["rb_mid"], ["wr1"], V, NOFLEX));
let r = FF.evaluateTrade(A, B, ["rb_mid"], ["rb_bench"], V, NOFLEX);
assert.ok(r.you.deltaPower < 0 && r.them.deltaPower > 0);
assert.strictEqual(FF.evaluateValue(["rb_bench"], ["rb_star"], V).verdict, "favors you");
assert.strictEqual(FF.evaluateValue([], [], V).verdict, "fair");

// needs + finder: I am thin at WR, partner has surplus WR and needs RB
const me = { rosterId: 1, isMe: true, name: "me", reserve: [], taxi: [], players: ["q", "r1", "r2", "r3", "r4", "w1", "t"] };
const them = { rosterId: 2, name: "them", reserve: [], taxi: [], players: ["q2", "r5", "w2", "w3", "w4", "w5", "t2"] };
const others = [3, 4].map((n) => ({ rosterId: n, name: "o" + n, reserve: [], taxi: [], players: [`q${n}x`, `a${n}`, `b${n}`, `c${n}`, `d${n}`, `e${n}`, `t${n}x`] }));
const F = { q: p("QB", 20), r1: p("RB", 19), r2: p("RB", 17), r3: p("RB", 16), r4: p("RB", 15), w1: p("WR", 11), t: p("TE", 12),
  q2: p("QB", 19), r5: p("RB", 10), w2: p("WR", 18), w3: p("WR", 16), w4: p("WR", 15), w5: p("WR", 14), t2: p("TE", 11) };
others.forEach((o, i) => o.players.forEach((id) => { F[id] = p(id.startsWith("q") ? "QB" : id.startsWith("t") ? "TE" : i ? "WR" : "RB", 12 + i); }));
const teams = [me, them, ...others];
const needs = FF.teamNeeds(teams, F, FLEX, 1);
assert.strictEqual(needs[0].group, "WR", "weakest group is WR: " + JSON.stringify(needs.map((n) => [n.group, n.rank])));
const found = FF.findTrades(teams, 1, F, FLEX, { focus: ["WR"] });
assert.ok(found.length > 0 && found[0].myDelta > 0.5 && found[0].theirDelta >= -0.3 && found[0].fairness >= -0.12);
assert.ok(found[0].fills.length > 0 && found.every((x) => x.get.every((id) => F[id].pos === "WR")));

// ranks + tiers
const rv = (pos, value, name) => ({ pos, value, ppg: value, avail: 1, exp_games: 10, name, team: "AAA" });
const RV = {
  a: rv("WR", 200, "a"), b: rv("WR", 180, "b"), c: rv("WR", 40, "c"), d: rv("WR", 35, "d"),
  e: rv("WR", 30, "e"), f: rv("WR", 25, "f"), g: rv("WR", 20, "g"), h: rv("WR", 15, "h"), q: rv("QB", 90, "q"),
};
const ranks = FF.computeRanks(RV);
assert.deepStrictEqual([ranks.a.pos, ranks.b.pos, ranks.c.pos, ranks.d.pos], [1, 2, 3, 4]);
assert.strictEqual(ranks.a.overall, 1); // highest value overall (200) among these
assert.strictEqual(FF.posLabel("WR", 2), "WR2");
const tiers = FF.computeTiers(RV);
assert.strictEqual(tiers.a, tiers.b, "close values (200 vs 180) land in the same tier");
assert.notStrictEqual(tiers.b, tiers.c, "a big gap (180 vs 40) starts a new tier");
assert.ok(["S", "A", "B", "C", "D", "F"].includes(tiers.a));

// labels + matchup
assert.deepStrictEqual(FF.slotLabels(["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF"]), ["QB", "RB1", "RB2", "WR1", "WR2", "TE", "FLEX", "K", "DEF"]);
assert.deepStrictEqual(["Favorable", "Medium", "Tough", null].map((l, i) => FF.matchupLabel([87, 60, 30, null][i], model).label), ["Favorable", "Medium", "Tough", null]);

// league parsing: real lineup, IR/taxi slots, IDP warning, non-PPR warning
const parsed = FF.parseLeague({ total_rosters: 10, roster_positions: ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF", "DL", "BN", "BN", "BN", "BN", "BN"],
  settings: { reserve_slots: 3, taxi_slots: 1 }, scoring_settings: { rec: 0.5 } }, model, { teams: 12, slots: ["QB"], bench: 5, ir: 3 });
assert.deepStrictEqual([parsed.league.teams, parsed.league.slots.length, parsed.league.bench, parsed.league.ir], [10, 9, 5, 3]);
assert.strictEqual(parsed.warnings.length, 2);

// teams incl. reserve/taxi, search
const tm = FF.buildTeams([{ roster_id: 1, owner_id: "u1", players: [1, "2"], reserve: ["2"], taxi: null }, { roster_id: 2, owner_id: "u2", players: null }],
  [{ user_id: "u1", display_name: "Diego", metadata: { team_name: "Los Pollos" } }], "u1");
assert.deepStrictEqual(tm.map((x) => [x.name, x.isMe, x.players, x.reserve, x.taxi]), [["Los Pollos", true, ["1", "2"], ["2"], []], ["Team 2", false, [], [], []]]);
assert.deepStrictEqual(FF.searchPlayers({ a: p("RB", 10, 1, "Jahmyr Gibbs"), b: p("RB", 14, 1, "Gibbs Jr") }, "gibbs").map((x) => x.id), ["b", "a"]);

// Sleeper client with a mocked fetch
(async () => {
  const fake = (routes) => async (url) => {
    const hit = Object.keys(routes).find((k) => url.endsWith(k));
    return hit ? { ok: true, status: 200, json: async () => routes[hit] } : { ok: false, status: 404, json: async () => null };
  };
  const s = FF.createSleeper("https://x/v1", fake({
    "/user/diego": { user_id: "u1" }, "/user/u1/leagues/nfl/2026": [{ league_id: "L1" }],
    "/league/L1": { total_rosters: 12 }, "/league/L1/rosters": [{ roster_id: 1 }], "/league/L1/users": [], "/user/ghost": null,
  }));
  assert.strictEqual((await s.getUser(" diego ")).user_id, "u1");
  assert.strictEqual((await s.loadLeague("L1")).league.total_rosters, 12);
  await assert.rejects(s.getUser("ghost"), (e) => e.kind === "not_found");
  await assert.rejects(FF.createSleeper("https://x/v1", async () => { throw new TypeError("Failed to fetch"); }).getUser("a"), (e) => e.kind === "network");
  await assert.rejects(FF.createSleeper("https://x/v1", async () => ({ ok: false, status: 500 })).getUser("a"), (e) => e.kind === "http");
  console.log("core.test.js: all checks passed");
})().catch((e) => { console.error(e); process.exit(1); });
