/* Pure logic for the site (no DOM): lineups and power rankings, trades, trade finder, Sleeper client.
 * Loaded in the browser as window.FF and in Node via require() for tests.
 * The Python versions live in src/ff/models/{power,trade}.py; tests/test_js_parity.py checks they agree. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.FF = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const FAIR_TOLERANCE = 0.10;
  const roundHalfUp = (x) => Math.floor(x + 0.5 + 1e-9); // same as Python's League._round_half_up

  // ---------------------------------------------------------------- league + replacement level
  /* league = {teams, slots:[...], bench, ir}; model = data.model (slot_eligible, flex_share, bench_share, kd_depth) */
  function replacementRank(pos, league, model) {
    const count = league.slots.filter((s) => s === pos).length;
    if (pos === "K" || pos === "DEF") return roundHalfUp(league.teams * ((count || 1) + model.kd_depth));
    let n = count;
    for (const s of league.slots) n += (model.flex_share[s] && model.flex_share[s][pos]) || 0;
    return roundHalfUp(league.teams * (n + (model.bench_share[pos] || 0)));
  }

  function replacementLevels(pools, league, model) {
    const out = {};
    for (const [pos, vals] of Object.entries(pools)) {
      if (vals.length) out[pos] = vals[Math.min(replacementRank(pos, league, model), vals.length) - 1];
    }
    return out;
  }

  /* Re-derive ppg/value from the user's adjustments (ppg delta per player, % per team) and this league's replacement. */
  function valuesForLeague(players, repl, adjust = {}, teamAdjust = {}) {
    const out = {};
    for (const [id, p] of Object.entries(players)) {
      const ppg = Math.max(0, (p.ppg + (adjust[id] || 0)) * (1 + (teamAdjust[p.team] || 0) / 100));
      out[id] = { ...p, ppg, value: Math.max(0, (ppg - (repl[p.pos] || 0)) * p.exp_games) };
    }
    return out;
  }

  // ---------------------------------------------------------------- lineups
  function pAtLeast(k, probs) {
    if (k <= 0) return 1;
    if (k > probs.length) return 0;
    const dist = [1, ...new Array(probs.length).fill(0)];
    for (const p of probs) {
      for (let j = dist.length - 1; j >= 0; j--) dist[j] = dist[j] * (1 - p) + (j ? dist[j - 1] * p : 0);
    }
    return dist.slice(k).reduce((a, b) => a + b, 0);
  }

  const evOf = (p, repl) => (repl[p.pos] || 0) + p.avail * (p.ppg - (repl[p.pos] || 0));

  /* ctx = {elig, repl, league}. Mirrors ff.models.power.team_power. */
  function teamPower(ids, values, ctx, ir = [], taxi = []) {
    const { elig, repl, league } = ctx;
    const irSet = new Set(ir), taxiSet = new Set(taxi);
    const pool = ids.filter((i) => values[i] && !taxiSet.has(i)).map((i) => ({ id: i, ...values[i] }));
    const remaining = pool.slice().sort((a, b) => evOf(b, repl) - evOf(a, repl)); // stable: ties keep roster order

    const slots = league.slots;
    const order = slots.map((_, i) => i).sort((a, b) => elig[slots[a]].length - elig[slots[b]].length || a - b);
    const chosen = new Array(slots.length).fill(null);
    for (const i of order) {
      const k = remaining.findIndex((p) => elig[slots[i]].includes(p.pos));
      if (k >= 0) chosen[i] = remaining.splice(k, 1)[0];
    }

    let lineup = 0;
    slots.forEach((slot, i) => {
      lineup += chosen[i] ? evOf(chosen[i], repl) : Math.max(...elig[slot].map((x) => repl[x] || 0));
    });

    const bench = remaining
      .filter((p) => !irSet.has(p.id))
      .sort((a, b) => b.ppg - (repl[b.pos] || 0) - (a.ppg - (repl[a.pos] || 0)))
      .slice(0, league.bench);
    let depth = 0;
    const seen = {};
    for (const b of bench) {
      const outs = [];
      slots.forEach((slot, i) => { if (chosen[i] && elig[slot].includes(b.pos)) outs.push(1 - chosen[i].avail); });
      seen[b.pos] = (seen[b.pos] || 0) + 1;
      const gain = Math.max(b.ppg - (repl[b.pos] || 0), 0) * b.avail;
      depth += pAtLeast(seen[b.pos], outs) * gain;
    }

    const best = pool.reduce((m, p) => (!m || p.value > m.value ? p : m), null);
    return {
      startersPpg: lineup, depth, power: lineup + depth, topPlayer: best ? best.name : null,
      lineup: slots.map((slot, i) => ({ slot, player: chosen[i], ev: chosen[i] ? evOf(chosen[i], repl) : null })),
      bench, ir: pool.filter((p) => irSet.has(p.id)),
      taxi: ids.filter((i) => values[i] && taxiSet.has(i)).map((i) => ({ id: i, ...values[i] })),
    };
  }

  function powerRankings(teams, values, ctx) {
    return teams
      .map((t) => ({ team: t, ...teamPower(t.players, values, ctx, t.reserve, t.taxi) }))
      .sort((a, b) => b.power - a.power)
      .map((r, i) => ({ ...r, rank: i + 1 }));
  }

  /* Label each lineup slot: QB, RB1, RB2, WR1, WR2, TE, FLEX, K, DEF. */
  function slotLabels(slots) {
    const total = {};
    slots.forEach((s) => { total[s] = (total[s] || 0) + 1; });
    const n = {};
    return slots.map((s) => { n[s] = (n[s] || 0) + 1; return total[s] > 1 ? `${s}${n[s]}` : s; });
  }

  // ---------------------------------------------------------------- trades
  function tradeSide(ids, out, incoming, incomingIr, ir, taxi, values, ctx) {
    const before = teamPower(ids, values, ctx, ir, taxi);
    const afterIds = ids.filter((i) => !out.includes(i)).concat(incoming);
    const afterIr = ir.filter((i) => !out.includes(i)).concat(incoming.filter((i) => incomingIr.includes(i))).slice(0, ctx.league.ir);
    const after = teamPower(afterIds, values, ctx, afterIr, taxi.filter((i) => !out.includes(i)));
    return {
      before: before.power, after: after.power,
      deltaPower: after.power - before.power,
      deltaStarters: after.startersPpg - before.startersPpg,
      deltaDepth: after.depth - before.depth,
      beforeLineup: before.lineup, afterLineup: after.lineup,
    };
  }

  function verdictFor(giveValue, getValue) {
    const top = Math.max(giveValue, getValue);
    const fairness = top > 0 ? (getValue - giveValue) / top : 0;
    const verdict = Math.abs(fairness) <= FAIR_TOLERANCE ? "fair" : fairness > 0 ? "favors you" : "favors them";
    return { fairness, verdict };
  }

  const sumValue = (ids, values) => ids.reduce((s, i) => s + (values[i] ? values[i].value : 0), 0);

  function evaluateValue(give, get, values) {
    const giveValue = sumValue(give, values);
    const getValue = sumValue(get, values);
    return { giveValue, getValue, ...verdictFor(giveValue, getValue) };
  }

  /* my/their = {players, reserve, taxi}. Throws if give/get are not on the right rosters. */
  function evaluateTrade(my, their, give, get, values, ctx) {
    if (!give.every((i) => my.players.includes(i))) throw new Error("Every player you give must be on your roster");
    if (!get.every((i) => their.players.includes(i))) throw new Error("Every player you get must be on the partner's roster");
    return {
      ...evaluateValue(give, get, values),
      you: tradeSide(my.players, give, get, their.reserve || [], my.reserve || [], my.taxi || [], values, ctx),
      them: tradeSide(their.players, get, give, my.reserve || [], their.reserve || [], their.taxi || [], values, ctx),
    };
  }

  // ---------------------------------------------------------------- ranks + tiers
  /* Overall and position rank, computed live from THIS league's values (they shift a little with the
   * connected league's replacement levels and your own adjustments, so this is done in the browser rather
   * than baked into the data file). */
  function computeRanks(values) {
    const byId = Object.entries(values);
    const overall = byId.slice().sort((a, b) => b[1].value - a[1].value);
    const ranks = {};
    overall.forEach(([id], i) => { ranks[id] = { overall: i + 1, pos: 0 }; });
    const byPos = {};
    byId.forEach(([id, p]) => { (byPos[p.pos] = byPos[p.pos] || []).push([id, p]); });
    Object.values(byPos).forEach((list) => {
      list.sort((a, b) => b[1].value - a[1].value).forEach(([id], i) => { ranks[id].pos = i + 1; });
    });
    return ranks;
  }
  const posLabel = (pos, posRank) => `${pos}${posRank}`;

  /* Tiers via Jenks natural breaks (the standard way to find "natural" clusters in a sorted list of numbers,
   * commonly used for exactly this kind of tier list): looks for the breaks that keep each group's numbers as
   * close together, and different groups as far apart, as possible. Computed per position on live values, so
   * tiers reflect the actual gaps in YOUR league, not a fixed cutoff like "top 12". */
  function jenksBreaks(sorted, classes) {
    const n = sorted.length;
    if (n <= classes) return sorted.map((_, i) => i); // one player per tier if there are that few
    const mat1 = Array.from({ length: n + 1 }, () => new Array(classes + 1).fill(0));
    const mat2 = Array.from({ length: n + 1 }, () => new Array(classes + 1).fill(Infinity));
    for (let i = 1; i <= classes; i++) { mat1[1][i] = 1; mat2[1][i] = 0; for (let j = 2; j <= n; j++) mat2[j][i] = Infinity; }
    let v = 0;
    for (let l = 2; l <= n; l++) {
      let s1 = 0, s2 = 0, w = 0;
      for (let m = 1; m <= l; m++) {
        const i3 = l - m + 1;
        const val = sorted[i3 - 1];
        w++; s1 += val; s2 += val * val;
        v = s2 - (s1 * s1) / w;
        const i4 = i3 - 1;
        if (i4 !== 0) {
          for (let j = 2; j <= classes; j++) {
            if (mat2[l][j] >= v + mat2[i4][j - 1]) { mat1[l][j] = i3; mat2[l][j] = v + mat2[i4][j - 1]; }
          }
        }
      }
      mat1[l][1] = 1; mat2[l][1] = v;
    }
    const idx = [n];
    let k = n, cls = classes;
    while (cls > 1) { const id = mat1[k][cls] - 2; idx.push(id); k = mat1[k][cls] - 1; cls--; }
    return idx.sort((a, b) => a - b); // last index (0-based, inclusive) of each tier, ascending
  }

  const TIER_LABELS = ["S", "A", "B", "C", "D", "F"];
  /* Returns {id: "S"|"A"|...} per position, using up to 6 tiers (fewer if the position has few rostered players). */
  function computeTiers(values, classes = 6) {
    const out = {};
    const byPos = {};
    Object.entries(values).forEach(([id, p]) => { if (p.value > 0) (byPos[p.pos] = byPos[p.pos] || []).push([id, p]); });
    Object.entries(byPos).forEach(([, list]) => {
      list.sort((a, b) => b[1].value - a[1].value);
      const vals = list.map(([, p]) => p.value);
      const k = Math.min(classes, list.length);
      const breaks = jenksBreaks(vals.slice().reverse(), k); // ascending for the algorithm
      const cut = breaks.map((b) => vals.length - 1 - b).sort((a, b) => a - b); // back to descending indices
      let tier = 0;
      list.forEach(([id], i) => {
        if (tier < cut.length - 1 && i > cut[tier]) tier++;
        out[id] = TIER_LABELS[Math.min(tier, TIER_LABELS.length - 1)];
      });
    });
    return out;
  }

  // ---------------------------------------------------------------- needs + trade finder
  const groupOf = (slot) => (["QB", "RB", "WR", "TE", "K", "DEF"].includes(slot) ? slot : "FLEX");

  /* Where is each team weak? Group strength = summed expected contribution of the lineup slots in the group. */
  function teamNeeds(teams, values, ctx, meId) {
    const rows = teams.map((t) => ({ t, power: teamPower(t.players, values, ctx, t.reserve, t.taxi) }));
    const groups = [...new Set(ctx.league.slots.map(groupOf))];
    const score = (r, g) => r.power.lineup.reduce((s, x, i) => s + (groupOf(ctx.league.slots[i]) === g ? (x.ev !== null ? x.ev : 0) : 0), 0);
    const meRow = rows.find((r) => String(r.t.rosterId) === String(meId));
    if (!meRow) return [];
    return groups.map((g) => {
      const all = rows.map((r) => score(r, g));
      const mine = score(meRow, g);
      const avg = all.reduce((a, b) => a + b, 0) / all.length;
      const rank = all.filter((x) => x > mine + 1e-9).length + 1;
      return { group: g, mine, avg, gap: mine - avg, rank, of: all.length };
    }).sort((a, b) => b.rank / b.of - a.rank / a.of || a.gap - b.gap);
  }

  /* League Rank and per-group ranks (QB/RB/WR/TE/...) for one team: where it stands against every other team in
   * ``teams``. Used to show "current vs. after this trade" — pass the same ``teams`` array with just meId's roster
   * swapped for the "after" call. Rank 1 = best in the league. */
  function leagueRanks(teams, values, ctx, meId) {
    const rows = teams.map((t) => ({ t, power: teamPower(t.players, values, ctx, t.reserve, t.taxi) }));
    const meRow = rows.find((r) => String(r.t.rosterId) === String(meId));
    if (!meRow) return null;
    const rankOf = (mine, all) => all.filter((x) => x > mine + 1e-9).length + 1;
    const out = { League: rankOf(meRow.power.power, rows.map((r) => r.power.power)) };
    const groups = [...new Set(ctx.league.slots.map(groupOf).filter((g) => g !== "FLEX"))];
    const score = (r, g) => r.power.lineup.reduce((s, x, i) => s + (groupOf(ctx.league.slots[i]) === g ? (x.ev !== null ? x.ev : 0) : 0), 0);
    groups.forEach((g) => { out[g] = rankOf(score(meRow, g), rows.map((r) => score(r, g))); });
    return out;
  }

  function combos(arr, k) {
    if (k === 1) return arr.map((x) => [x]);
    const out = [];
    for (let i = 0; i < arr.length; i++) for (let j = i + 1; j < arr.length; j++) out.push([arr[i], arr[j]]);
    return out;
  }

  /* Proposals that improve MY lineup without wrecking the partner's: 1-for-1 and 2-for-1 (I give two, get one).
   * Filters: value fairness for me within [minFairness, maxFairness], my power gain >= minGain, and the partner's
   * power change >= -maxTheirLoss. opts.focus = optional array of positions I want to add (e.g. ["RB"]). */
  function findTrades(teams, meId, values, ctx, opts = {}) {
    const { minGain = 0.5, maxTheirLoss = 0.3, minFairness = -0.12, maxFairness = 0.22, limit = 12, focus = null, pool = 10 } = opts;
    const me = teams.find((t) => String(t.rosterId) === String(meId));
    if (!me) return [];
    const skill = (id) => values[id] && ["QB", "RB", "WR", "TE"].includes(values[id].pos) && values[id].value > 0;
    const top = (ids) => ids.filter(skill).sort((a, b) => values[b].value - values[a].value).slice(0, pool);
    const myOffer = top(me.players);
    const labels = slotLabels(ctx.league.slots);
    const found = [];
    for (const other of teams) {
      if (other === me) continue;
      const targets = top(other.players).filter((id) => !focus || focus.includes(values[id].pos));
      for (const get of targets) {
        for (const size of [1, 2]) {
          for (const give of combos(myOffer, size)) {
            const giveValue = sumValue(give, values), getValue = values[get].value;
            const { fairness } = verdictFor(giveValue, getValue);
            if (fairness < minFairness || fairness > maxFairness) continue;
            const r = evaluateTrade(me, other, give, [get], values, ctx);
            if (r.you.deltaPower < minGain || r.them.deltaPower < -maxTheirLoss) continue;
            const fills = r.you.afterLineup
              .map((x, i) => ({ slot: labels[i], gain: (x.ev || 0) - (r.you.beforeLineup[i].ev || 0) }))
              .filter((x) => x.gain > 0.3).sort((a, b) => b.gain - a.gain);
            found.push({
              partner: other, give, get: [get], giveValue, getValue, fairness,
              myDelta: r.you.deltaPower, theirDelta: r.them.deltaPower, fills,
              mutual: r.them.deltaPower > 0.3,
              score: r.you.deltaPower + 0.35 * Math.min(Math.max(r.them.deltaPower, -1), 1.5),
            });
          }
        }
      }
    }
    found.sort((a, b) => b.score - a.score);
    const seen = new Set(), out = [];
    for (const f of found) { // one proposal per target player: avoid ten variants of the same idea
      const key = f.partner.rosterId + ":" + f.get[0];
      if (seen.has(key)) continue;
      seen.add(key); out.push(f);
      if (out.length >= limit) break;
    }
    return out;
  }

  // ---------------------------------------------------------------- data helpers
  function matchupLabel(mu, model) {
    if (mu === null || mu === undefined) return { label: null, cls: "" };
    if (mu >= model.matchup.favorable) return { label: "Favorable", cls: "good" };
    if (mu <= model.matchup.tough) return { label: "Tough", cls: "bad" };
    return { label: "Medium", cls: "mid" };
  }

  /* Read the real lineup from a Sleeper league object. */
  function parseLeague(sl, model, fallback) {
    const rp = sl.roster_positions || [];
    const slots = rp.filter((p) => model.slot_eligible[p]);
    const ignored = rp.filter((p) => !["BN", "IR", "TAXI"].includes(p) && !model.slot_eligible[p]);
    const s = sl.settings || {};
    const count = (x) => rp.filter((p) => p === x).length;
    const league = slots.length
      ? { teams: sl.total_rosters || fallback.teams, slots, bench: count("BN"), ir: s.reserve_slots || count("IR"), taxi: s.taxi_slots || count("TAXI") }
      : { ...fallback };
    const warnings = [];
    if (ignored.length) warnings.push(`Your league starts IDP slots (${[...new Set(ignored)].join(", ")}); they are not modeled and are ignored.`);
    const rec = sl.scoring_settings && sl.scoring_settings.rec;
    if (rec !== undefined && rec !== 1) warnings.push(`Reception scoring is ${rec}, not full PPR; values assume PPR.`);
    if (!slots.length) warnings.push("Could not read your lineup from Sleeper; using the default lineup.");
    return { league, warnings };
  }

  function buildTeams(rosters, users, myUserId) {
    const byUser = new Map((users || []).map((u) => [u.user_id, u]));
    return rosters.map((r) => {
      const u = byUser.get(r.owner_id);
      const name = (u && ((u.metadata && u.metadata.team_name) || u.display_name)) || `Team ${r.roster_id}`;
      const mine = !!myUserId && (r.owner_id === myUserId || (r.co_owners || []).includes(myUserId));
      return {
        rosterId: r.roster_id, ownerId: r.owner_id, name, owner: u ? u.display_name : null, isMe: mine,
        players: (r.players || []).map(String), reserve: (r.reserve || []).map(String), taxi: (r.taxi || []).map(String),
      };
    });
  }

  function searchPlayers(values, query, limit = 8) {
    const q = query.trim().toLowerCase();
    if (!q) return [];
    return Object.entries(values)
      .filter(([, p]) => p.name.toLowerCase().includes(q))
      .sort((a, b) => b[1].value - a[1].value)
      .slice(0, limit)
      .map(([id, p]) => ({ id, ...p }));
  }

  // ---------------------------------------------------------------- Sleeper client
  class SleeperError extends Error {
    constructor(kind, message) { super(message); this.kind = kind; }
  }

  function createSleeper(base, fetchFn) {
    async function get(path) {
      let res;
      try {
        res = await fetchFn(base + path);
      } catch (e) {
        throw new SleeperError("network", "The browser could not reach Sleeper (no internet, or the request was blocked by CORS).");
      }
      if (!res.ok) throw new SleeperError("http", `Sleeper answered with HTTP ${res.status}.`);
      return res.json();
    }
    return {
      async getUser(username) {
        const u = await get(`/user/${encodeURIComponent(username.trim())}`);
        if (!u || !u.user_id) throw new SleeperError("not_found", `No Sleeper user called "${username}".`);
        return u;
      },
      getLeagues: (userId, season) => get(`/user/${userId}/leagues/nfl/${season}`),
      async loadLeague(leagueId) {
        const [league, rosters, users] = await Promise.all([
          get(`/league/${leagueId}`), get(`/league/${leagueId}/rosters`), get(`/league/${leagueId}/users`),
        ]);
        if (!league || !Array.isArray(rosters)) throw new SleeperError("not_found", `League ${leagueId} was not found.`);
        return { league, rosters, users: users || [] };
      },
    };
  }

  // ---------------------------------------------------------------- trade-chain simulation
  /* Applies one trade to a plain {rosterId: {players, reserve, taxi}} map and returns a NEW map (does not
   * mutate the input), so a sequence of trades across different team pairs can be simulated by chaining calls.
   * ctx.league.ir caps how many incoming IR players a team can keep tagged IR. */
  function applyTrade(rosters, myId, theirId, give, get, ctx) {
    const my = rosters[myId], their = rosters[theirId];
    const moveOut = (r, out, incoming, incomingIr) => ({
      players: r.players.filter((i) => !out.includes(i)).concat(incoming),
      reserve: r.reserve.filter((i) => !out.includes(i)).concat(incoming.filter((i) => incomingIr.includes(i))).slice(0, ctx.league.ir),
      taxi: r.taxi.filter((i) => !out.includes(i)),
    });
    return {
      ...rosters,
      [myId]: moveOut(my, give, get, their.reserve || []),
      [theirId]: moveOut(their, get, give, my.reserve || []),
    };
  }

  return {
    replacementRank, replacementLevels, valuesForLeague, pAtLeast, teamPower, powerRankings, slotLabels,
    evaluateValue, evaluateTrade, verdictFor, teamNeeds, findTrades, matchupLabel, parseLeague, buildTeams,
    searchPlayers, createSleeper, SleeperError, FAIR_TOLERANCE, computeRanks, computeTiers, posLabel, applyTrade, leagueRanks,
  };
});
