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
    // unused healthy backups (t = 1 few snaps, 2 none; see ff.models.roles) rank after everybody else; ties by ppg
    const order = (a, b) => ((a[1].t || 0) - (b[1].t || 0)) || (b[1].value - a[1].value) || ((b[1].ppg || 0) - (a[1].ppg || 0));
    const overall = byId.slice().sort(order);
    const ranks = {};
    overall.forEach(([id], i) => { ranks[id] = { overall: i + 1, pos: 0 }; });
    const byPos = {};
    byId.forEach(([id, p]) => { (byPos[p.pos] = byPos[p.pos] || []).push([id, p]); });
    Object.values(byPos).forEach((list) => {
      list.sort(order).forEach(([id], i) => { ranks[id].pos = i + 1; });
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

  // ---------------------------------------------------------------- stats-tab charts
  /* "Nice" axis ticks covering [lo, hi] (1/2/5 x 10^k steps), used by the Stats tab's scatter charts. */
  function niceTicks(lo, hi, target = 6) {
    if (!(hi > lo)) return [lo];
    const raw = (hi - lo) / target, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 5, 10].map((m) => m * mag).find((x) => x >= raw);
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Math.round(v / step) * step);
    return out;
  }
  /* Axis range with 8% padding; keeps zero in view when the data straddles it (so the quadrant lines show). */
  function axisRange(values) {
    const v = values.filter((x) => x !== null && x !== undefined && !Number.isNaN(x));
    if (!v.length) return [0, 1];
    let lo = Math.min(...v), hi = Math.max(...v);
    if (lo === hi) { lo -= 1; hi += 1; }
    const pad = (hi - lo) * 0.08;
    return [lo - pad, hi + pad];
  }
  /* Least-squares line y = a + b*x through the points (null when fewer than 3 points or no spread in x). */
  function linearFit(points, xKey, yKey) {
    const pts = points.filter((p) => p[xKey] !== null && p[xKey] !== undefined && p[yKey] !== null && p[yKey] !== undefined);
    const n = pts.length;
    if (n < 3) return null;
    const mx = pts.reduce((s, p) => s + p[xKey], 0) / n, my = pts.reduce((s, p) => s + p[yKey], 0) / n;
    let sxx = 0, sxy = 0;
    pts.forEach((p) => { sxx += (p[xKey] - mx) ** 2; sxy += (p[xKey] - mx) * (p[yKey] - my); });
    if (sxx === 0) return null;
    const slope = sxy / sxx;
    return { slope, intercept: my - slope * mx };
  }
  /* Pure layout for a scatter: pixel position of each point plus ticks. Points missing x or y are dropped.
   * ``flipY`` puts bigger y at the TOP (SVG y grows downward). */
  function scatterLayout(points, xKey, yKey, { width, height, pad }) {
    const pts = points.filter((p) => p[xKey] !== null && p[xKey] !== undefined && p[yKey] !== null && p[yKey] !== undefined);
    const [x0, x1] = axisRange(pts.map((p) => p[xKey])), [y0, y1] = axisRange(pts.map((p) => p[yKey]));
    const px = (x) => pad.l + ((x - x0) / (x1 - x0)) * (width - pad.l - pad.r);
    const py = (y) => pad.t + (1 - (y - y0) / (y1 - y0)) * (height - pad.t - pad.b);
    return {
      points: pts.map((p) => ({ p, x: px(p[xKey]), y: py(p[yKey]) })),
      xTicks: niceTicks(x0, x1).map((v) => ({ v, at: px(v) })), yTicks: niceTicks(y0, y1).map((v) => ({ v, at: py(v) })),
      zeroX: x0 < 0 && x1 > 0 ? px(0) : null, zeroY: y0 < 0 && y1 > 0 ? py(0) : null,
      fit: (() => {
        const f = linearFit(pts, xKey, yKey);
        return f ? { x1: px(x0), y1: py(f.intercept + f.slope * x0), x2: px(x1), y2: py(f.intercept + f.slope * x1) } : null;
      })(),
      meanX: pts.length ? px(pts.reduce((s, p) => s + p[xKey], 0) / pts.length) : null,
      meanY: pts.length ? py(pts.reduce((s, p) => s + p[yKey], 0) / pts.length) : null,
    };
  }

  /* Horizontal "lollipop" bars from a center line (ROS schedule chart): rows sorted high value first (top), the
     domain always contains the center so the bars have a baseline. Returns pixel positions only; app.js draws them. */
  function barLayout(items, { width, height, pad, center }) {
    const rows = items.filter((i) => i.v !== null && i.v !== undefined && !Number.isNaN(i.v)).sort((a, b) => b.v - a.v);
    const [lo, hi] = axisRange(rows.map((r) => r.v).concat([center]));
    const px = (v) => pad.l + ((v - lo) / (hi - lo)) * (width - pad.l - pad.r);
    const rowH = rows.length ? (height - pad.t - pad.b) / rows.length : 0;
    return {
      cx: px(center), rowH,
      rows: rows.map((item, i) => ({ item, x: px(item.v), y: pad.t + rowH * (i + 0.5) })),
      ticks: niceTicks(lo, hi).map((v) => ({ v, at: px(v) })),
    };
  }

  // ---------------------------------------------------------------- trade breakdown (explains a trade, never changes its numbers)
  const ordinal = (n) => { const m = n % 100, d = n % 10; return n + (m >= 11 && m <= 13 ? "th" : d === 1 ? "st" : d === 2 ? "nd" : d === 3 ? "rd" : "th"); };
  const median = (xs) => { const a = xs.slice().sort((x, y) => x - y), m = a.length >> 1; return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2; };
  const POS_ROS = ["QB", "RB", "WR", "TE"];
  const fmt1 = (n) => (Math.round(n * 10) / 10).toFixed(1);
  const sgn1 = (n) => (n > 0 ? "+" : "") + fmt1(n);
  const names = (ps) => ps.map((p) => p.name).join(" + ");

  /* Played weeks of one player from player_weeks.json rows: {n, median, floor, boom, booms(>=20)}; null under 3 games. */
  function weeklyRange(rows) {
    const pts = (rows || []).filter((g) => g.fpts !== null && g.fpts !== undefined).map((g) => g.fpts);
    if (pts.length < 3) return null;
    return { n: pts.length, median: median(pts), floor: Math.min(...pts), boom: Math.max(...pts), booms: pts.filter((x) => x >= 20).length };
  }
  const byeWeek = (rows) => { const r = (rows || []).find((g) => g.opp === null || g.opp === undefined); return r ? r.wk : null; };

  /* Value gap the trade has (|incoming - outgoing|) and the projection the best player on the AHEAD side would need to
   * fall to for the value verdict to flip. Pure arithmetic on the same formula the site uses for value. */
  function flipPoint(give, get, values, repl) {
    const g = sumValue(give, values), t = sumValue(get, values), gap = Math.abs(t - g);
    const side = t >= g ? get : give;
    const top = side.map((i) => ({ id: i, ...values[i] })).filter((p) => p.value !== undefined).sort((a, b) => b.value - a.value)[0];
    if (!top || !(top.exp_games > 0) || gap <= 0) return null;
    const left = top.value - gap, floorPpg = repl[top.pos] || 0;
    return { player: top, incoming: t >= g, holds: left <= 0, ppg: left <= 0 ? floorPpg : floorPpg + left / top.exp_games, gap };
  }

  /* Plain-language breakdown of a trade from MY side: "get" is incoming (YOU), "give" is outgoing (THEM). Every sentence is built
   * from numbers the site already has (values, ranks, ROS schedule, weekly log, lineup change); nothing is invented.
   * input: {give, get, values, ranks, ros, weeks, full, rankBefore, rankAfter, repl}. ros/weeks/full/rank* may be null. */
  function tradeBreakdown({ give, get, values, ranks, ros, weeks, full, rankBefore, rankAfter, repl }) {
    const P = (ids) => ids.filter((i) => values[i]).map((i) => ({ id: i, ...values[i], rank: ranks && ranks[i] ? ranks[i].pos : null }));
    const inc = P(get), out = P(give);
    const sections = [];
    const edge = (diff, tol) => (diff > tol ? "you" : diff < -tol ? "them" : "even");
    const lab = (p) => `${p.name} (${p.pos}${p.rank || ""})`;

    // ---- best asset: the hardest single player to replace in the deal
    const all = inc.map((p) => ({ ...p, mine: true })).concat(out.map((p) => ({ ...p, mine: false }))).sort((a, b) => b.value - a.value || b.ppg - a.ppg);
    if (all.length) {
      const b = all[0], second = all.find((p) => p.mine !== b.mine);
      const rel = second && b.value > 0 ? (b.value - second.value) / b.value : 1;
      const share = b.share !== null && b.share !== undefined ? `, ${Math.round(b.share * 100)}% of ${b.team}'s rush + target touches` : "";
      let text = `${lab(b)} is the hardest piece to replace in this deal: ${fmt1(b.value)} pts over replacement at ${fmt1(b.ppg)} ppg${share}.`;
      if (second) text += ` The best piece on the other side is ${lab(second)} with ${fmt1(second.value)} pts.`;
      sections.push({ key: "best", title: "Best asset", edge: rel < 0.1 ? "even" : b.mine ? "you" : "them", text });
    }

    // ---- ROS schedule: how hard are the remaining opponents for each player's position (positive = tougher)
    if (ros) {
      const of = (p) => (ros[p.pos] && ros[p.pos][p.team] ? ros[p.pos][p.team].v : null);
      const score = (ps) => {
        const rows = ps.filter((p) => POS_ROS.includes(p.pos) && of(p) !== null);
        const w = rows.reduce((s, p) => s + Math.max(p.value, 1), 0);
        return rows.length ? { v: rows.reduce((s, p) => s + of(p) * Math.max(p.value, 1), 0) / w, rows } : null;
      };
      const a = score(inc), o = score(out);
      if (a && o) {
        const diff = o.v - a.v; // outgoing tougher than incoming -> good for me
        const fmtSide = (s) => s.rows.map((p) => `${p.name} ${sgn1(of(p))}%`).join(", ");
        let text = `Remaining opponents vs. their position (positive = tougher defenses): incoming ${fmtSide(a)}; outgoing ${fmtSide(o)}.`;
        if (weeks) {
          const po = (p) => {
            const opps = (weeks[p.id] || []).filter((g) => g.wk >= 15 && g.wk <= 17 && g.opp && ros.def && ros.def[p.pos] && ros.def[p.pos][g.opp] !== undefined).map((g) => ros.def[p.pos][g.opp]);
            return opps.length ? opps.reduce((x, y) => x + y, 0) / opps.length : null;
          };
          const pAvg = (ps) => { const r = ps.map(po).filter((x) => x !== null); return r.length ? r.reduce((x, y) => x + y, 0) / r.length : null; };
          const pi = pAvg(a.rows), pout = pAvg(o.rows);
          if (pi !== null && pout !== null) text += ` Fantasy-playoff stretch (weeks 15-17): incoming ${sgn1(pi)}% vs outgoing ${sgn1(pout)}%.`;
        }
        sections.push({ key: "ros", title: "ROS schedule", edge: edge(diff, 0.5), text });
      }
    }

    // ---- weekly range: this season's real weekly scores (median / floor / best week)
    if (weeks) {
      const R = (ps) => ps.map((p) => ({ p, r: weeklyRange(weeks[p.id]) })).filter((x) => x.r);
      const a = R(inc), o = R(out);
      if (a.length && o.length) {
        const sum = (xs, k) => xs.reduce((s, x) => s + x.r[k], 0);
        const diff = sum(a, "median") - sum(o, "median");
        const line = (xs) => `${xs.map((x) => `${x.p.name} ${fmt1(x.r.median)} median / ${fmt1(x.r.floor)} floor (${x.r.n} gm)`).join(", ")}`;
        const booms = [...a, ...o].filter((x) => x.r.booms >= 2).map((x) => `${x.p.name} has ${x.r.booms} weeks of 20+`);
        sections.push({
          key: "range", title: "Weekly range", edge: edge(diff, 1),
          text: `PPR points this season. Incoming: ${line(a)} = ${fmt1(sum(a, "median"))} combined median. Outgoing: ${line(o)} = ${fmt1(sum(o, "median"))}.${booms.length ? " " + booms.join("; ") + "." : ""}`,
        });
      }
    }

    // ---- position impact: where my team ranks in the league at each traded position, before -> after
    if (rankBefore && rankAfter) {
      const keys = Object.keys(rankBefore).filter((k) => k !== "League" && rankAfter[k] !== undefined);
      const touched = keys.filter((k) => inc.concat(out).some((p) => p.pos === k || (k === "RB" && p.pos === "FB")));
      const use = touched.length ? touched : keys;
      const net = use.reduce((s, k) => s + (rankBefore[k] - rankAfter[k]), 0);
      const parts = use.map((k) => `${k} ${ordinal(rankBefore[k])} → ${ordinal(rankAfter[k])}`);
      if (rankBefore.League !== undefined) parts.push(`overall ${ordinal(rankBefore.League)} → ${ordinal(rankAfter.League)}`);
      sections.push({ key: "position", title: "Position impact", edge: edge(net, 0), text: `Your league rank: ${parts.join(", ")}.` });
    }

    // ---- lineup impact: who starts and who sits, and the projected points per week
    let lineupText = null;
    if (full && full.you) {
      const ids = (lineup) => lineup.filter((x) => x.player).map((x) => x.player.id);
      const b4 = ids(full.you.beforeLineup), af = ids(full.you.afterLineup);
      const nm = (id) => (values[id] ? values[id].name : id);
      const benched = b4.filter((i) => !af.includes(i)).filter((i) => !give.includes(i)).map(nm);
      const added = af.filter((i) => !b4.includes(i)).map(nm);
      const d = full.you.deltaStarters;
      lineupText = `Your starting lineup projects ${sgn1(d)} pts per week${added.length ? `; ${added.join(" and ")} ${added.length > 1 ? "enter" : "enters"} the lineup` : ""}${benched.length ? `; ${benched.join(" and ")} ${benched.length > 1 ? "move" : "moves"} to the bench` : ""}.`;
      sections.push({ key: "lineup", title: "Your lineup", edge: edge(d, 0.5), text: lineupText });

      // bye weeks: do the starters after the trade pile up on one bye?
      if (weeks) {
        const groups = (list) => { const m = {}; list.forEach((id) => { const w = byeWeek(weeks[id]); if (w) (m[w] = m[w] || []).push(id); }); return m; };
        const worst = (m) => Object.entries(m).reduce((best, [w, l]) => (l.length > best.n ? { w, n: l.length, ids: l } : best), { w: null, n: 0, ids: [] });
        const wb = worst(groups(b4)), wa = worst(groups(af));
        if (wa.n >= 3 || wb.n >= 3) {
          sections.push({
            key: "byes", title: "Bye weeks", edge: wa.n > wb.n ? "them" : wa.n < wb.n ? "you" : "even",
            text: wa.n >= 3 ? `After the trade ${wa.n} of your starters share the week-${wa.w} bye (${wa.ids.map(nm).join(", ")}); before it was ${wb.n}.` : `The trade removes a ${wb.n}-starter pile-up on the week-${wb.w} bye.`,
          });
        }
      }
    }

    // ---- verdict sentence + what would flip it
    const base = evaluateValue(give, get, values);
    const f = base.fairness;
    const label = f > 0.25 ? "Strong accept" : f > 0.1 ? "Lean accept" : f >= -0.1 ? "Toss up" : f >= -0.25 ? "Lean decline" : "Decline";
    const flip = repl ? flipPoint(give, get, values, repl) : null;
    let flips = null;
    if (flip) {
      const pl = flip.player, drop = pl.ppg > 0 ? Math.round((1 - flip.ppg / pl.ppg) * 100) : 0;
      flips = flip.holds
        ? `The value call holds even if ${pl.name} fell to replacement level (${fmt1(flip.ppg)} ppg).`
        : `The value call flips if ${pl.name}'s projection falls below ${fmt1(flip.ppg)} ppg (now ${fmt1(pl.ppg)}, about ${drop}% lower)${pl.share !== null && pl.share !== undefined ? `; his current touch share is ${Math.round(pl.share * 100)}%` : ""}.`;
    }
    const lead = sections.find((x) => x.key === "best");
    const against = sections.filter((x) => x.edge !== "even" && x.key !== "best" && (f >= 0 ? x.edge === "them" : x.edge === "you"));
    let take = `${label}. ${lead ? lead.text.split(". The best piece")[0] + "." : ""}`;
    if (lineupText) take += ` ${lineupText}`;
    if (against.length) take += ` Counterweight: ${against.map((x) => x.title.toLowerCase()).join(" and ")} lean${against.length > 1 ? "" : "s"} the other way.`;
    return { label, sections, take, flips };
  }


  return {
    replacementRank, replacementLevels, valuesForLeague, pAtLeast, teamPower, powerRankings, slotLabels,
    evaluateValue, evaluateTrade, verdictFor, teamNeeds, findTrades, matchupLabel, parseLeague, buildTeams,
    searchPlayers, createSleeper, SleeperError, FAIR_TOLERANCE, computeRanks, computeTiers, posLabel, applyTrade, leagueRanks,
    niceTicks, axisRange, linearFit, scatterLayout, barLayout, tradeBreakdown, flipPoint, weeklyRange, ordinal,
  };
});
