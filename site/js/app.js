/* UI layer. All numbers come from FF (core.js); this file only reads data, wires events and renders. */
(function () {
  "use strict";

  const DATA = window.FF_DATA;
  const $ = (sel) => document.querySelector(sel);
  const fmt = (n, d = 1) => (n === null || n === undefined || Number.isNaN(n) ? "–" : Number(n).toFixed(d));
  const signed = (n, d = 1) => (n > 0 ? "+" : "") + fmt(n, d);
  const pct = (x) => (x === null || x === undefined ? "–" : Math.round(x * 100) + "%");

  function el(tag, props, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (v === true) n.setAttribute(k, "");
      else n.setAttribute(k, v);
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return n;
  }
  const clear = (n) => { while (n.firstChild) n.removeChild(n.firstChild); return n; };

  if (!DATA || !DATA.players) {
    $("#asof").textContent = "Data file missing: run `python -m ff.models.value` to generate site/data/player_values.js.";
    return;
  }

  const MODEL = DATA.model;
  const SLEEPER_BASE = (window.FF_CONFIG && window.FF_CONFIG.sleeperBase) || "https://api.sleeper.app/v1";
  const api = FF.createSleeper(SLEEPER_BASE, (...a) => window.fetch(...a));

  const state = {
    league: { ...DATA.default_league }, leagueName: null, repl: {}, values: {}, ctx: null, ranks: {}, tiers: {},
    user: null, leagues: [], teams: [],
    adj: { players: {}, teams: {} },
    trade: { meId: null, partnerId: null, give: [], get: [] },
    sim: null, // trade-chain simulation: { rosters, log, baseline } — see startSim()/applySimTrade()
    finder: { focus: [], results: null },
    weeks: null, weeksPromise: null, // lazy-loaded site/data/player_weeks.json (see ensureWeeklyData)
    usage: { data: null, promise: null, team: null, pos: null }, // lazy-loaded site/data/team_usage.json
    ros: { data: null, promise: null, kind: "overall" }, // lazy-loaded site/data/ros.json
    charts: { data: null, promise: null, kind: "qb", min: {} }, // lazy-loaded site/data/stats_charts.json
    sort: { key: "value", dir: -1 }, pos: "ALL", q: "",
  };
  const teamById = (id) => state.teams.find((t) => String(t.rosterId) === String(id));
  // team's roster during a trade simulation (see startSim); real Sleeper roster otherwise
  function activeRoster(id) {
    const t = teamById(id);
    if (!t) return null;
    const r = state.sim && state.sim.rosters[String(id)];
    return r ? { ...t, players: r.players, reserve: r.reserve, taxi: r.taxi } : t;
  }

  // ------------------------------------------------------------------ adjustments + recompute
  function loadAdj() {
    try { const a = JSON.parse(localStorage.getItem("ff_adj_v1") || "null"); if (a && a.players && a.teams) state.adj = a; } catch (e) { /* ignore */ }
  }
  function saveAdj() { try { localStorage.setItem("ff_adj_v1", JSON.stringify(state.adj)); } catch (e) { /* ignore */ } }

  function recompute() {
    state.repl = FF.replacementLevels(DATA.pools, state.league, MODEL);
    state.values = FF.valuesForLeague(DATA.players, state.repl, state.adj.players, state.adj.teams);
    state.ctx = { elig: MODEL.slot_eligible, repl: state.repl, league: state.league };
    state.ranks = FF.computeRanks(state.values);
    state.tiers = FF.computeTiers(state.values);
  }

  function setAdj(id, delta) {
    if (!delta || Number.isNaN(delta)) delete state.adj.players[id]; else state.adj.players[id] = delta;
    saveAdj(); recompute(); renderAll();
  }

  // ------------------------------------------------------------------ small view helpers
  function matchupBadge(p) {
    if (!p || ["K", "DEF"].includes(p.pos)) return null;
    if (!p.opp) return el("span", { class: "mu", text: "BYE" });
    const m = FF.matchupLabel(p.mu, MODEL);
    return el("span", { class: "mu " + m.cls, title: `Opponent ${p.opp}: matchup score ${p.mu}/100 (100 = easiest defense for ${p.pos})`, text: `vs ${p.opp} · ${m.label} ${p.mu}` });
  }
  function statusTag(p) {
    if (!p.status) return null;
    return el("span", { class: "tag" + (p.status === "IR" ? " ir" : ""), text: p.status });
  }
  function roleText(p) {
    if (p.share === null || p.share === undefined) return null;
    let t = `Role: ${pct(p.share)} of team touches (last season ${pct(p.share_prev)})`;
    if (p.mate) t += ` · shares with ${p.mate} (${pct(p.mate_share)})`;
    return t;
  }
  /* Whether he practiced in the MOST RECENT practice report — for every player, not just hurt ones.
   * Deliberately just this fact and nothing guessed from it: we used to show a specific "games until he's
   * back" estimate, but on a thin per-injury sample (especially a player who went straight to IR with no
   * weekly report history at all) that number could be badly wrong, so it's gone. This is the latest
   * verified signal the public report gives, full stop. */
  const PRACTICE_LABEL = { Full: "Practiced fully", Limited: "Limited practice", "Did Not": "Did not practice" };
  function practiceText(p) {
    if (!p.practice) return p.status ? "No practice report found for the most recent week." : "Not on this week's injury report (expected to be full go).";
    return `Last practice: ${PRACTICE_LABEL[p.practice] || p.practice}`;
  }
  /* Sleeper's own injury detail ("Knee - ACL · Surgery · updated Oct 21"), present only when Sleeper currently lists him as hurt. */
  function injuryText(p) {
    if (!p.sl || !p.sl.part) return null;
    const when = p.sl.ts ? new Date(p.sl.ts).toLocaleDateString("en-US", { month: "short", day: "numeric" }) : null;
    return `Injury (Sleeper): ${p.sl.part}${p.sl.note ? " · " + p.sl.note : ""}${when ? " · updated " + when : ""}`;
  }
  /* Tiny samples drive the projection a lot: say so. */
  const sampleText = (p) => (p.g !== undefined && p.g !== null && p.g < 3 ? `Small sample: only ${p.g} game${p.g === 1 ? "" : "s"} behind this projection.` : null);
  const label = (p) => `${p.name} · ${p.pos}${p.team ? " " + p.team : ""}`;
  const TIER_TITLE = "Tier: natural groupings in the value gaps for this position (S = best). Two players a tier apart differ more than two in the same tier.";
  function rankBadges(id) {
    const r = state.ranks[id];
    if (!r) return null;
    const p = state.values[id];
    const tier = state.tiers[id];
    return el("span", { class: "ranks" },
      el("span", { class: "tag rk", title: "Overall rank among rostered players" }, `#${r.overall} ovr`),
      el("span", { class: "tag rk", title: `Rank among ${p.pos}s` }, FF.posLabel(p.pos, r.pos)),
      tier ? el("span", { class: "tag tier tier-" + tier, title: TIER_TITLE }, "Tier " + tier) : null);
  }

  // ------------------------------------------------------------------ player card modal
  async function ensureWeeklyData() {
    if (state.weeks) return state.weeks;
    if (!state.weeksPromise) {
      state.weeksPromise = fetch("data/player_weeks.json")
        .then((r) => (r.ok ? r.json() : {}))
        .then((d) => { state.weeks = d; return d; })
        .catch(() => { state.weeks = {}; return {}; });
    }
    return state.weeksPromise;
  }
  const per = (num, den) => (den ? (num / den).toFixed(1) : "–");
  function weeklyTable(rows, pos) {
    const dash = (v) => (v === null || v === undefined ? "–" : v);
    const played = (g) => g.fpts !== null && g.fpts !== undefined;
    const isQb = pos === "QB";
    const head = isQb
      ? ["WK", "OPP", "PROJ", "FPTS", "SNP%", "ATT", "CMP", "YD", "TD", "INT", "ATT", "YD", "YPC", "TD", "SK", "YDS", "FUM", "LOST"]
      : ["WK", "OPP", "PROJ", "FPTS", "SNP%", "ATT", "YD", "YPC", "TD", "TAR", "REC", "YD", "Y/T", "Y/R", "TD", "FUM", "LOST", "KR", "KYD", "PR", "SP TD"];
    const groups = isQb
      ? [["", 2], ["FANTASY", 2], ["", 1], ["PASS", 5], ["RUSHING", 4], ["SACKED", 2], ["FUMBLE", 2]]
      : [["", 2], ["FANTASY", 2], ["", 1], ["RUSHING", 4], ["RECEIVING", 5], ["FUMBLE", 2], ["RETURNING", 5]];
    const cells = (g) => {
      const p = played(g);
      const common = [
        el("td", { text: g.wk }),
        el("td", { class: g.inj ? "inj" : "", title: g.inj || null }, g.opp || "BYE"),
        el("td", { class: "num", text: dash(g.proj) }), el("td", { class: "num strong", text: dash(g.fpts) }),
        el("td", { class: "num", text: g.snap === null || g.snap === undefined ? "–" : g.snap + "%" }),
      ];
      if (isQb) {
        return [...common,
          el("td", { class: "num", text: dash(g.pa) }), el("td", { class: "num", text: dash(g.pc) }), el("td", { class: "num", text: dash(g.py) }),
          el("td", { class: "num", text: dash(g.pt) }), el("td", { class: "num", text: dash(g.pi) }),
          el("td", { class: "num", text: dash(g.ra) }), el("td", { class: "num", text: dash(g.ry) }),
          el("td", { class: "num", text: p ? per(g.ry, g.ra) : "–" }), el("td", { class: "num", text: dash(g.rt) }),
          el("td", { class: "num", text: dash(g.sk) }), el("td", { class: "num", text: dash(g.sky) }),
          el("td", { class: "num", text: dash(g.fm) }), el("td", { class: "num", text: dash(g.fl) })];
      }
      return [...common,
        el("td", { class: "num", text: dash(g.ra) }), el("td", { class: "num", text: dash(g.ry) }),
        el("td", { class: "num", text: p ? per(g.ry, g.ra) : "–" }), el("td", { class: "num", text: dash(g.rt) }),
        el("td", { class: "num", text: dash(g.tg) }), el("td", { class: "num", text: dash(g.rc) }), el("td", { class: "num", text: dash(g.cy) }),
        el("td", { class: "num", text: p ? per(g.cy, g.tg) : "–" }), el("td", { class: "num", text: p ? per(g.cy, g.rc) : "–" }),
        el("td", { class: "num", text: dash(g.ct) }),
        el("td", { class: "num", text: dash(g.fm) }), el("td", { class: "num", text: dash(g.fl) }),
        el("td", { class: "num", text: dash(g.kr) }), el("td", { class: "num", text: dash(g.kyd) }), el("td", { class: "num", text: dash(g.pr) }), el("td", { class: "num", text: dash(g.spt) })];
    };
    return el("div", { class: "table-wrap" }, el("table", { class: "weekly-table" },
      el("thead", {},
        el("tr", {}, groups.map(([g, span]) => el("th", { colspan: String(span), class: g ? "grp" : "" }, g || ""))),
        el("tr", {}, head.map((h) => el("th", { text: h })))),
      el("tbody", {}, rows.map((g) => el("tr", { class: played(g) ? "" : "future" }, cells(g))))));
  }
  function summaryTab(id, p) {
    return el("div", {},
      el("div", { class: "pc-badges" }, rankBadges(id), statusTag(p), matchupBadge(p)),
      el("div", { class: "stats" },
        el("div", { class: "stat" }, el("span", { class: "muted small", text: "Proj PPG" }), el("b", { text: fmt(p.ppg) })),
        el("div", { class: "stat" }, el("span", { class: "muted small", text: "Exp. games" }), el("b", { text: fmt(p.exp_games) }), el("span", { class: "muted small", text: `${Math.round(p.avail * 100)}% availability` })),
        el("div", { class: "stat" }, el("span", { class: "muted small", text: "Value" }), el("b", { text: fmt(p.value, 0) + " pts" }), el("span", { class: "muted small", text: "over replacement" }))),
      roleText(p) ? el("p", { class: "ctx", text: roleText(p) }) : null,
      el("p", { class: "ctx", text: practiceText(p) }),
      injuryText(p) ? el("p", { class: "ctx", text: injuryText(p) }) : null,
      sampleText(p) ? el("p", { class: "ctx", text: sampleText(p) }) : null,
      p.opp ? el("p", { class: "ctx", text: `This week: vs ${p.opp}, matchup score ${p.mu}/100 for ${p.pos}s (100 = easiest defense)` }) : null,
      el("p", { class: "muted small", text: "PPG is a recency-weighted average of his own games (current season weighted heaviest); it is not a week-by-week box score." }));
  }
  async function openPlayerCard(id, tab = "summary") {
    const p = state.values[id];
    if (!p) return;
    const modal = $("#player-modal");
    const body = clear($("#player-modal-body"));
    const tabs = el("div", { class: "pc-tabs" },
      el("button", { class: "pc-tab" + (tab === "summary" ? " active" : ""), onclick: () => openPlayerCard(id, "summary"), text: "Summary" }),
      el("button", { class: "pc-tab" + (tab === "weeks" ? " active" : ""), onclick: () => openPlayerCard(id, "weeks"), text: "Week by week" }));
    body.append(
      el("div", { class: "pc-head" }, el("h3", { text: p.name }), el("span", { class: "muted", text: `${p.pos}${p.team ? " · " + p.team : ""}` })),
      tabs, el("div", { id: "pc-body" }));
    modal.classList.remove("hidden");
    const slot = $("#pc-body");
    if (tab === "summary") {
      clear(slot).append(summaryTab(id, p));
      return;
    }
    clear(slot).append(el("p", { class: "muted small", text: "Loading…" }));
    const weeks = await ensureWeeklyData();
    const rows = weeks[id];
    clear(slot).append(
      rows
        ? weeklyTable(rows, p.pos)
        : el("p", { class: "muted small", text: "No weekly history for this player yet." }),
      el("p", { class: "muted small", text: "PROJ is his season-long projection (the same every week), not a projection specific to that week's opponent." }));
  }
  function wirePlayerCard() {
    $("#player-modal").addEventListener("click", (e) => { if (e.target.id === "player-modal") $("#player-modal").classList.add("hidden"); });
    $("#player-modal-close").addEventListener("click", () => $("#player-modal").classList.add("hidden"));
  }
  const nameLink = (id, text) => el("button", { class: "link", onclick: () => openPlayerCard(id), text: text || (state.values[id] && state.values[id].name) || id });

  // small green/amber/red chip for a power delta, so "who gains, who loses" reads at a glance
  function deltaChip(value, labelText) {
    const cls = value > 0.3 ? "good" : value < -0.3 ? "bad" : "mid";
    const arrow = value > 0.3 ? "▲" : value < -0.3 ? "▼" : "•";
    return el("span", { class: "delta-chip " + cls },
      el("span", { class: "dc-label", text: labelText }), el("span", { class: "dc-val", text: `${arrow} ${signed(value)}` }));
  }
  function deltaBar(myDelta, theirDelta) {
    return el("div", { class: "delta-row" }, deltaChip(myDelta, "You"), deltaChip(theirDelta, "Them"));
  }

  // ------------------------------------------------------------------ status / header
  function setStatus(msg, kind) {
    const s = $("#status");
    s.textContent = msg || "";
    s.className = "status" + (kind ? " " + kind : "");
  }
  function renderHeader() {
    const l = state.league;
    $("#asof").textContent =
      `Projections as of ${DATA.as_of.season} week ${DATA.as_of.week} · PPR · ${l.teams} teams · ` +
      `${l.slots.filter((s) => s !== "K" && s !== "DEF").join("/")}${l.slots.includes("K") ? " + K" : ""}${l.slots.includes("DEF") ? " + DEF" : ""} · ` +
      `${l.bench} bench, ${l.ir} IR` + (state.leagueName ? ` · ${state.leagueName}` : "");
  }
  function renderWarnings(list) {
    const ul = clear($("#warnings"));
    list.forEach((w) => ul.append(el("li", { text: w })));
  }

  // ------------------------------------------------------------------ Sleeper connection
  async function findLeagues() {
    const username = $("#username").value.trim();
    const season = Number($("#season").value) || DATA.as_of.season;
    if (!username) return setStatus("Type your Sleeper username first.", "error");
    setStatus("Looking up your leagues…");
    try {
      const user = await api.getUser(username);
      const leagues = await api.getLeagues(user.user_id, season);
      state.user = user;
      state.leagues = leagues || [];
      try { localStorage.setItem("ff_username", username); } catch (e) { /* storage may be blocked */ }
      if (!state.leagues.length) return setStatus(`No NFL leagues found for ${user.display_name || username} in ${season}.`, "error");
      const sel = clear($("#league-select"));
      state.leagues.forEach((l) => sel.append(el("option", { value: l.league_id, text: `${l.name} (${l.total_rosters} teams)` })));
      $("#league-row").classList.remove("hidden");
      setStatus(`Found ${state.leagues.length} league(s). Pick one and load it.`, "ok");
      if (state.leagues.length === 1) await loadLeague(state.leagues[0].league_id);
    } catch (e) {
      setStatus(e.message, "error");
    }
  }

  async function loadLeague(leagueId) {
    if (!leagueId) return setStatus("Pick a league or enter a league ID.", "error");
    setStatus("Loading league…");
    try {
      const { league, rosters, users } = await api.loadLeague(leagueId);
      const parsed = FF.parseLeague(league, MODEL, DATA.default_league);
      state.league = parsed.league;
      state.leagueName = league.name;
      state.teams = FF.buildTeams(rosters, users, state.user && state.user.user_id);
      state.finder = { focus: [], results: null };
      recompute();
      renderWarnings(parsed.warnings);
      state.sim = null;
      resetTrade();
      renderAll();
      setStatus(`Loaded "${league.name}" (${state.teams.length} teams).`, "ok");
    } catch (e) {
      setStatus(e.message, "error");
    }
  }

  // ------------------------------------------------------------------ power rankings
  function lineupView(r) {
    const labels = FF.slotLabels(state.league.slots);
    const rows = r.lineup.map((x, i) => el("tr", {},
      el("td", {}, el("span", { class: "slot", text: labels[i] })),
      x.player
        ? [el("td", {}, nameLink(x.player.id), el("span", { class: "muted small", text: ` ${x.player.pos}${x.player.team ? " " + x.player.team : ""}` })),
           el("td", { class: "num", text: fmt(x.player.ppg) + " ppg" }),
           el("td", {}, statusTag(x.player), " ", matchupBadge(x.player))]
        : [el("td", { class: "muted", text: "empty (waiver replacement assumed)" }), el("td"), el("td")]));
    const simple = (title, list) => list.length ? [
      el("div", { class: "section-label", text: title }),
      el("table", { class: "lineup-table" }, el("tbody", {}, list.map((p) => el("tr", {},
        el("td", {}, nameLink(p.id), el("span", { class: "muted small", text: ` ${p.pos}${p.team ? " " + p.team : ""}` })), el("td", { class: "num", text: fmt(p.ppg) + " ppg" }),
        el("td", {}, statusTag(p), " ", matchupBadge(p)))))),
    ] : [];
    return el("div", {},
      el("div", { class: "section-label", text: "Starting lineup (expected)" }),
      el("table", { class: "lineup-table" }, el("tbody", {}, rows)),
      simple(`Bench (${r.bench.length}/${state.league.bench})`, r.bench),
      simple(`IR (${r.ir.length}/${state.league.ir})`, r.ir),
      simple("Taxi", r.taxi));
  }

  function renderPower() {
    const root = clear($("#tab-power"));
    const card = el("div", { class: "card" }, el("h2", { text: "Power rankings" }));
    root.append(card);
    if (!state.teams.length) {
      card.append(el("p", { class: "muted", text: "Connect a Sleeper league above to rank every team by lineup strength and depth." }));
      return;
    }
    const ranked = FF.powerRankings(state.teams, state.values, state.ctx);
    card.append(el("p", { class: "muted small", text: "Power = expected weekly points of the best lineup (flex, K and DEF included; injured players count for the games they are expected to play) + depth. Depth counts only as much as your starters are likely to miss games, so a thin team with elite starters is not punished. IR and taxi players do not use bench spots. Click a row for the lineup." }));

    const table = el("table");
    table.append(el("thead", {}, el("tr", {}, ...[["#", "num"], ["Team", ""], ["Power", "num"], ["Lineup", "num"], ["Depth", "num"], ["Top player", ""]].map(([h, c]) => el("th", { class: c, text: h })))));
    const body = el("tbody");
    ranked.forEach((r) => {
      const detail = el("tr", { class: "detail hidden" }, el("td", { colspan: "6" }, lineupView(r)));
      const row = el("tr", { class: "clickable" + (r.team.isMe ? " me" : ""), onclick: () => detail.classList.toggle("hidden") },
        el("td", { class: "num", text: r.rank }),
        el("td", {}, r.team.name, r.team.isMe ? el("span", { class: "badge", text: "you" }) : null, r.team.owner && r.team.owner !== r.team.name ? el("span", { class: "muted small", text: "  " + r.team.owner }) : null),
        el("td", { class: "num", text: fmt(r.power) }),
        el("td", { class: "num", text: fmt(r.startersPpg) }),
        el("td", { class: "num", text: fmt(r.depth) }),
        el("td", { text: r.topPlayer || "–" }));
      body.append(row, detail);
    });
    table.append(body);
    card.append(el("div", { class: "table-wrap" }, table));
  }

  // ------------------------------------------------------------------ trade-chain simulation
  function startSim() {
    state.sim = {
      rosters: Object.fromEntries(state.teams.map((t) => [String(t.rosterId), { players: t.players.slice(), reserve: t.reserve.slice(), taxi: t.taxi.slice() }])),
      log: [], meId: state.trade.meId,
    };
    state.sim.baseline = FF.teamPower(activeRoster(state.sim.meId).players, state.values, state.ctx, activeRoster(state.sim.meId).reserve, activeRoster(state.sim.meId).taxi).power;
    renderTradeTeams(); renderTradeSides(); renderTradeResult();
  }
  function endSim() {
    state.sim = null;
    renderTradeTeams(); renderTradeSides(); renderTradeResult();
  }
  function applySimTrade(full) {
    const { meId, partnerId, give, get } = state.trade;
    state.sim.rosters = FF.applyTrade(state.sim.rosters, meId, partnerId, give, get, state.ctx);
    state.sim.log.push({ partner: teamById(partnerId).name, give: give.slice(), get: get.slice(), delta: full.you.deltaPower });
    state.trade.give = []; state.trade.get = [];
    renderTradeSides(); renderTradeResult();
  }
  function simPanel() {
    if (!state.sim) {
      return el("div", { class: "sim-banner" },
        "Simulate several trades in a row: each one you apply is saved, and the next one starts from there, with any team. ",
        el("button", { class: "primary", text: "Start simulation", onclick: startSim }));
    }
    const me = activeRoster(state.sim.meId);
    const now = FF.teamPower(me.players, state.values, state.ctx, me.reserve, me.taxi).power;
    const cum = now - state.sim.baseline;
    return el("div", { class: "sim-banner" },
      el("div", {}, el("b", { text: "Simulation active" }), ` para ${teamById(state.sim.meId).name} — `,
        deltaChip(cum, "Total"), ` (${state.sim.log.length} trade(s) applied)`,
        el("button", { text: "End simulation", onclick: endSim })),
      state.sim.log.length ? el("div", { class: "sim-log" }, state.sim.log.map((l, i) => el("div", { class: "row" },
        el("span", {}, `${i + 1}. con ${l.partner}: das ${l.give.map((id) => state.values[id] ? state.values[id].name : id).join("+")}, recibes ${l.get.map((id) => state.values[id] ? state.values[id].name : id).join("+")}`),
        deltaChip(l.delta, "")))) : null);
  }

  // ------------------------------------------------------------------ trade calculator
  function resetTrade() {
    const me = state.teams.find((t) => t.isMe) || state.teams[0];
    const partner = state.teams.find((t) => me && t.rosterId !== me.rosterId);
    state.trade = { meId: me ? String(me.rosterId) : null, partnerId: partner ? String(partner.rosterId) : null, give: [], get: [] };
  }

  function renderTradeTeams() {
    const has = state.teams.length > 1;
    $("#trade-teams").classList.toggle("hidden", !has);
    if (!has) return;
    const me = clear($("#me-select"));
    state.teams.forEach((t) => me.append(el("option", { value: String(t.rosterId), text: t.name })));
    me.value = state.trade.meId;
    me.disabled = !!state.sim; // locked mid-simulation so the running total stays about one team
    fillPartners();
  }
  function fillPartners() {
    const sel = clear($("#partner-select"));
    state.teams.filter((t) => String(t.rosterId) !== state.trade.meId)
      .forEach((t) => sel.append(el("option", { value: String(t.rosterId), text: t.name })));
    sel.value = state.trade.partnerId;
  }

  const labelToId = new Map();
  function renderPlayerOptions() {
    const list = clear($("#player-options"));
    labelToId.clear();
    Object.entries(state.values).sort((a, b) => b[1].value - a[1].value).forEach(([id, p]) => {
      let l = label(p);
      if (labelToId.has(l)) l += ` #${id}`;
      labelToId.set(l, id);
      list.append(el("option", { value: l }));
    });
  }

  function toggle(side, id) {
    const arr = state.trade[side];
    const i = arr.indexOf(id);
    if (i >= 0) arr.splice(i, 1); else arr.push(id);
    renderTradeSides();
    renderTradeResult();
  }

  function renderSide(side, teamId) {
    const arr = state.trade[side];
    const chips = clear($(`#${side}-chips`));
    arr.forEach((id) => {
      const p = state.values[id];
      chips.append(el("span", { class: "chip" }, p ? `${p.name} (${p.pos}) ${fmt(p.ppg)} ppg` : id, el("button", { title: "Remove", onclick: () => toggle(side, id), text: "×" })));
    });
    const list = clear($(`#${side}-roster`));
    const team = teamId ? activeRoster(teamId) : null;
    if (!team) return;
    const inSlot = (id) => (team.reserve.includes(id) ? " (IR)" : team.taxi.includes(id) ? " (taxi)" : "");
    team.players.filter((id) => state.values[id])
      .sort((a, b) => state.values[b].value - state.values[a].value)
      .forEach((id) => {
        const p = state.values[id];
        list.append(el("button", { class: arr.includes(id) ? "on" : "", onclick: () => toggle(side, id) },
          el("span", { text: `${p.pos} ${p.name}${inSlot(id)}` }), el("span", { class: "muted", text: `${fmt(p.value, 0)} pts` })));
      });
  }
  function renderTradeSides() {
    renderSide("give", state.trade.meId);
    renderSide("get", state.trade.partnerId);
  }

  function contextCard(title, ids) {
    if (!ids.length) return null;
    return el("div", {}, el("div", { class: "section-label", text: title }), ids.map((id) => {
      const p = state.values[id];
      if (!p) return null;
      const adj = state.adj.players[id];
      return el("div", { class: "proposal" },
        el("div", { class: "head" }, nameLink(id), " ", el("span", { class: "muted", text: `${p.pos} ${p.team || ""}` }), " ", statusTag(p), " ", matchupBadge(p)),
        el("div", {}, rankBadges(id)),
        el("div", { class: "ctx", text: `Projection ${fmt(p.ppg)} ppg × ${fmt(p.exp_games)} expected games (${Math.round(p.avail * 100)}% availability) = ${fmt(p.value, 0)} pts above replacement` }),
        roleText(p) ? el("div", { class: "ctx", text: roleText(p) }) : null,
        el("div", { class: "ctx", text: practiceText(p) }),
        injuryText(p) ? el("div", { class: "ctx", text: injuryText(p) }) : null,
        sampleText(p) ? el("div", { class: "ctx", text: sampleText(p) }) : null,
        el("label", { class: "small" }, "Your adjustment (ppg) ",
          el("input", { class: "adj", type: "number", step: "0.5", value: adj === undefined ? "" : String(adj), placeholder: "0",
            onchange: (e) => setAdj(id, parseFloat(e.target.value)) })));
    }));
  }

  const GROUP_LABEL = { League: "League Rank", QB: "QB Rank", RB: "RB Rank", WR: "WR Rank", TE: "TE Rank", K: "K Rank", DEF: "DEF Rank" };
  function rankTable(give, get) {
    const teamsForRanks = state.teams.map((t) => { const r = activeRoster(t.rosterId); return { rosterId: t.rosterId, players: r.players, reserve: r.reserve, taxi: r.taxi }; });
    const afterTeams = teamsForRanks.map((t) => {
      if (String(t.rosterId) === String(state.trade.meId)) return { ...t, players: t.players.filter((id) => !give.includes(id)).concat(get) };
      if (String(t.rosterId) === String(state.trade.partnerId)) return { ...t, players: t.players.filter((id) => !get.includes(id)).concat(give) };
      return t;
    });
    const before = FF.leagueRanks(teamsForRanks, state.values, state.ctx, state.trade.meId);
    const after = FF.leagueRanks(afterTeams, state.values, state.ctx, state.trade.meId);
    if (!before || !after) return null;
    const rows = Object.keys(before).map((k) => {
      const cls = after[k] < before[k] ? "better" : after[k] > before[k] ? "worse" : "";
      return el("tr", {}, el("td", { text: GROUP_LABEL[k] || k }), el("td", { class: "num", text: before[k] }),
        el("td", { class: "num rt-arrow" }, "→"), el("td", { class: "num rt-cell " + cls, text: after[k] }));
    });
    return el("div", {}, el("h3", { text: "Your team's ranking in the league" }),
      el("div", { class: "table-wrap" }, el("table", { class: "rank-table" },
        el("thead", {}, el("tr", {}, el("th", { text: "" }), el("th", { class: "num", text: "Current" }), el("th", {}), el("th", { class: "num", text: "After Trade" }))),
        el("tbody", {}, rows))),
      el("p", { class: "muted small", text: "1 = best in the league in that group. Green = improves, red = worsens." }));
  }

  function renderTradeResult() {
    const root = clear($("#trade-result"));
    root.append(simPanel());
    const { give, get } = state.trade;
    if (!give.length && !get.length) return;
    const ctx = state.ctx;
    let full = null, note = null;
    const me = activeRoster(state.trade.meId), them = activeRoster(state.trade.partnerId);
    if (me && them) {
      try { full = FF.evaluateTrade(me, them, give, get, state.values, ctx); }
      catch (e) { note = `${e.message}. Showing player values only.`; }
    }
    const base = full || FF.evaluateValue(give, get, state.values);
    const cls = base.verdict === "fair" ? "" : base.verdict === "favors you" ? "good" : "bad";
    const card = el("div", { class: "card" }, el("h2", { text: "Result" }));
    card.append(el("div", { class: "stats" },
      el("div", { class: "stat" }, el("span", { class: "muted small", text: "You give" }), el("b", { text: fmt(base.giveValue, 0) + " pts" }), el("span", { class: "muted small", text: `${give.length} player(s)` })),
      el("div", { class: "stat" }, el("span", { class: "muted small", text: "You get" }), el("b", { text: fmt(base.getValue, 0) + " pts" }), el("span", { class: "muted small", text: `${get.length} player(s)` })),
      el("div", { class: "stat" }, el("span", { class: "muted small", text: "Value verdict" }), el("span", { class: "verdict " + cls, text: base.verdict }), el("span", { class: "muted small", text: ` ${signed(base.fairness * 100, 0)}% for you` }))));
    if (note) card.append(el("p", { class: "status error", text: note }));
    if (full) {
      const row = (name, s) => el("tr", {},
        el("td", { text: name }),
        el("td", { class: "num", text: `${fmt(s.before)} → ${fmt(s.after)}` }),
        el("td", { class: "num" }, deltaChip(s.deltaPower, "")),
        el("td", { class: "num" }, deltaChip(s.deltaStarters, "")),
        el("td", { class: "num" }, deltaChip(s.deltaDepth, "")));
      card.append(el("h3", { text: "Effect on each lineup (power score)" }), deltaBar(full.you.deltaPower, full.them.deltaPower),
        el("div", { class: "table-wrap" }, el("table", {},
          el("thead", {}, el("tr", {}, ...["Team", "Power", "Δ Power", "Δ Lineup", "Δ Depth"].map((h, i) => el("th", { class: i ? "num" : "", text: h })))),
          el("tbody", {}, row(me.name + " (you)", full.you), row(them.name, full.them)))));
      card.append(rankTable(give, get));
      card.append(el("p", { class: "muted small", text: "Value counts what each player is worth on any roster; the power delta counts what your lineup actually gains or loses (flex included). A third RB is worth little to a team that already starts two good ones." }));
      if (get.length > give.length) card.append(el("p", { class: "muted small", text: `You receive ${get.length - give.length} more player(s) than you send; you would need to drop that many. Only your best ${state.league.bench} bench players count for depth.` }));
      if (state.sim) card.append(el("button", { class: "primary", text: "Apply this trade to the simulation", onclick: () => applySimTrade(full) }));
    } else if (!note) {
      card.append(el("p", { class: "muted small", text: "Connect a league and pick both teams to see how each lineup changes, not just the raw value." }));
    }
    card.append(el("h3", { text: "Context (this season's situation)" }),
      el("p", { class: "muted small", text: "The model is history-weighted (current-season games count 3×) but cannot know about offseason changes, committees that just formed, or an improved offense. Use the adjustment box to add your own read and everything above recalculates." }),
      contextCard("You give", give), contextCard("You get", get));
    root.append(card);
  }

  function wireSearch(side) {
    const input = $(`#${side}-input`);
    const tryAdd = () => {
      const id = labelToId.get(input.value);
      if (!id) return;
      if (!state.trade[side].includes(id)) state.trade[side].push(id);
      input.value = "";
      renderTradeSides();
      renderTradeResult();
    };
    input.addEventListener("input", tryAdd);
    input.addEventListener("change", tryAdd);
  }

  // ------------------------------------------------------------------ trade finder
  function renderFinderSetup() {
    const has = state.teams.length > 1;
    const sel = $("#finder-team");
    const prev = sel.value;
    clear(sel);
    state.teams.forEach((t) => sel.append(el("option", { value: String(t.rosterId), text: t.name })));
    if (has) sel.value = state.teams.some((t) => String(t.rosterId) === prev) ? prev : state.trade.meId;
    const box = clear($("#finder-focus"));
    ["QB", "RB", "WR", "TE"].forEach((pos) => box.append(el("button", {
      class: state.finder.focus.includes(pos) ? "active" : "", text: pos,
      onclick: () => { const f = state.finder.focus; const i = f.indexOf(pos); if (i >= 0) f.splice(i, 1); else f.push(pos); renderFinderSetup(); },
    })));
    const needs = clear($("#finder-needs"));
    if (!has) { needs.append(el("p", { class: "muted", text: "Connect a Sleeper league to find trades." })); return; }
    const rows = FF.teamNeeds(state.teams, state.values, state.ctx, sel.value);
    needs.append(el("div", { class: "section-label", text: "Your lineup vs. the league (1 = best)" }),
      ...rows.map((n, i) => el("span", { class: "need" + (i < 2 ? " weak" : "") },
        el("b", { text: n.group }), `${n.rank}/${n.of}`, el("span", { class: "muted", text: `${signed(n.gap)} vs avg` }))));
  }

  function runFinder() {
    const root = clear($("#finder-results"));
    if (state.teams.length < 2) return;
    const meId = $("#finder-team").value;
    const focus = state.finder.focus.length ? state.finder.focus : null;
    const results = FF.findTrades(state.teams, meId, state.values, state.ctx, { focus });
    const card = el("div", { class: "card" }, el("h2", { text: results.length ? "Suggested trades" : "No trades found" }));
    if (!results.length) {
      card.append(el("p", { class: "muted", text: "Nothing improves your lineup by at least 0.5 points without hurting the other team. Try removing the position filter, or adjust player values." }));
    } else {
      card.append(el("p", { class: "muted small", text: "Sorted by how much they improve your lineup, giving credit when the other team also improves (those are the ones most likely to be accepted). Values stay within about ±12% of each other for you." }));
    }
    results.forEach((r) => {
      const names = (ids) => ids.map((id) => nameLink(id));
      const join = (nodes) => nodes.flatMap((n, i) => (i ? [" + ", n] : [n]));
      card.append(el("div", { class: "proposal" },
        el("div", { class: "head" }, "Send ", ...join(names(r.give)), " → get ", ...join(names(r.get)), r.mutual ? el("span", { class: "tag good", text: "win-win" }) : null),
        el("div", { class: "muted small" }, `with ${r.partner.name}`),
        (() => { // flag proposals that lean on a player with a tiny sample
          const thin = r.give.concat(r.get).map((id) => state.values[id]).filter((p) => p && p.g !== undefined && p.g !== null && p.g < 3);
          return thin.length ? el("div", { class: "small", style: "color: var(--bad)" }, "Small sample: " + thin.map((p) => `${p.name} (${p.g} game${p.g === 1 ? "" : "s"})`).join(", ")) : null;
        })(),
        deltaBar(r.myDelta, r.theirDelta),
        r.fills.length ? el("div", { class: "muted small" }, "Fills: " + r.fills.map((f) => `${f.slot} ${signed(f.gain)}`).join(", ")) : null,
        el("button", { class: "primary", text: "View comparison →", onclick: () => openInCalculator(meId, r) })));
    });
    root.append(card);
  }

  function openInCalculator(meId, r) {
    state.trade = { meId: String(meId), partnerId: String(r.partner.rosterId), give: r.give.slice(), get: r.get.slice() };
    renderTradeTeams(); renderTradeSides(); renderTradeResult();
    showTab("trade");
  }

  // ------------------------------------------------------------------ player values table
  const COLUMNS = [
    ["rank", "#", true], ["name", "Player", false], ["pos_rank", "Pos", true], ["tier", "Tier", false], ["team", "Team", false], ["age", "Age", true],
    ["ppg", "Proj PPG", true], ["exp_games", "Exp games", true], ["value", "Value", true],
    ["mu", "Matchup", true], ["share", "Role", true], ["adj", "Adj", true], ["status", "Status", false],
  ];
  function renderPosFilter() {
    const box = clear($("#pos-filter"));
    ["ALL", "QB", "RB", "WR", "TE", "K", "DEF"].forEach((p) => box.append(el("button", {
      class: state.pos === p ? "active" : "", text: p, onclick: () => { state.pos = p; renderPosFilter(); renderValues(); },
    })));
  }
  function renderValues() {
    let rows = Object.entries(state.values).map(([id, p]) => ({
      id, ...p, adj: state.adj.players[id], rank: state.ranks[id] ? state.ranks[id].overall : null,
      pos_rank: state.ranks[id] ? state.ranks[id].pos : null, tier: state.tiers[id] || null,
    }));
    if (state.pos !== "ALL") rows = rows.filter((r) => r.pos === state.pos);
    if (state.q) rows = rows.filter((r) => r.name.toLowerCase().includes(state.q.toLowerCase()));
    const { key, dir } = state.sort;
    rows.sort((a, b) => {
      const x = a[key], y = b[key];
      if (x === y) return 0;
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return (x > y ? 1 : -1) * dir;
    });
    const table = clear($("#values-table"));
    table.append(el("thead", {}, el("tr", {}, ...COLUMNS.map(([k, lab, num]) =>
      el("th", { class: "sortable" + (num ? " num" : ""), onclick: () => { if (!k || k === "cmp") return; state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : (num ? -1 : 1) }; renderValues(); },
        text: lab + (state.sort.key === k ? (state.sort.dir > 0 ? " ▲" : " ▼") : "") })))));
    table.append(el("tbody", {}, rows.slice(0, 300).map((r) => el("tr", {},
      el("td", { class: "num", text: r.rank }), el("td", {}, nameLink(r.id)),
      el("td", { class: "num" }, el("span", { class: "tag rk", text: FF.posLabel(r.pos, r.pos_rank) })),
      el("td", {}, r.tier ? el("span", { class: "tag tier tier-" + r.tier, title: TIER_TITLE, text: r.tier }) : null),
      el("td", { text: r.team || "" }),
      el("td", { class: "num", text: fmt(r.age) }), el("td", { class: "num", text: fmt(r.ppg) }), el("td", { class: "num", text: fmt(r.exp_games) }),
      el("td", { class: "num", text: fmt(r.value, 0) }),
      el("td", { class: "num" }, matchupBadge(r)),
      el("td", { class: "num", title: roleText(r) || "", text: r.share === null || r.share === undefined ? "" : pct(r.share) + (r.mate ? " ↔" : "") }),
      el("td", { class: "num" }, el("input", { class: "adj", type: "number", step: "0.5", value: r.adj === undefined ? "" : String(r.adj), placeholder: "0",
        onchange: (e) => setAdj(r.id, parseFloat(e.target.value)) })),
      el("td", {}, statusTag(r))))));
    $("#matchup-note").textContent =
      `Matchup: how many fantasy points the ${DATA.as_of.season} week ${DATA.as_of.week} opponent allows to that position vs. the other defenses (100 = easiest, ` +
      `Favorable ≥ ${MODEL.matchup.favorable}, Tough ≤ ${MODEL.matchup.tough}). In backtests this was a weak predictor: use it as context. ` +
      `Role: share of team rushes + targets over the last 4 games (↔ = shares touches with a teammate; hover for detail).`;
  }

  // ------------------------------------------------------------------ team adjustments
  function renderAdjustments() {
    const teams = [...new Set(Object.values(DATA.players).filter((p) => ["QB", "RB", "WR", "TE"].includes(p.pos)).map((p) => p.team))].filter(Boolean).sort();
    const sel = $("#adj-team");
    if (!sel.options.length) teams.forEach((t) => sel.append(el("option", { value: t, text: t })));
    const list = clear($("#adj-list"));
    Object.entries(state.adj.teams).forEach(([t, v]) => list.append(el("span", { class: "chip" }, `${t} offense ${signed(v, 0)}%`, el("button", { title: "Remove", text: "×", onclick: () => { delete state.adj.teams[t]; saveAdj(); recompute(); renderAll(); } }))));
    const n = Object.keys(state.adj.players).length;
    if (n) list.append(el("span", { class: "muted small", text: `${n} player adjustment(s) set in the table below.` }));
  }

  // ------------------------------------------------------------------ wiring
  function moveGlider() {
    const bar = document.querySelector(".tabs"), on = bar && bar.querySelector("button.active");
    let g = bar && bar.querySelector(".glider");
    if (!bar || !on) return;
    if (!g) { g = document.createElement("span"); g.className = "glider"; bar.prepend(g); }
    g.style.width = on.offsetWidth + "px"; g.style.transform = `translateX(${on.offsetLeft}px)`;
    if (on.scrollIntoView) { try { on.scrollIntoView({ inline: "center", block: "nearest", behavior: "smooth" }); } catch (e) { /* ignore */ } }
  }
  function showTab(name) {
    document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x.dataset.tab === name));
    document.querySelectorAll(".tab").forEach((t) => {
      const show = t.id === "tab-" + name, wasHidden = t.classList.contains("hidden");
      t.classList.toggle("hidden", !show);
      if (show && wasHidden) { // replay the entrance animation, then drop the class so re-renders don't re-animate
        t.classList.remove("enter"); void t.offsetWidth; t.classList.add("enter");
        setTimeout(() => t.classList.remove("enter"), 900);
      }
    });
    moveGlider();
    if (name === "charts") renderCharts();
    if (name === "ros") renderRos();
  }

  // ------------------------------------------------------------------ season stats (usage) tab
  const USAGE_COLUMNS = {
    RB: [["gms", "GMS", (p) => p.gms], ["snap", "Snaps", (p) => pct(p.snap)], ["rush_att", "Rush ATT", (p) => pct(p.rush_att)], ["targets", "Targets", (p) => pct(p.targets)]],
    FB: [["gms", "GMS", (p) => p.gms], ["snap", "Snaps", (p) => pct(p.snap)], ["rush_att", "Rush ATT", (p) => pct(p.rush_att)], ["targets", "Targets", (p) => pct(p.targets)]],
    WR: [["gms", "GMS", (p) => p.gms], ["snap", "Snaps", (p) => pct(p.snap)], ["targets", "Targets", (p) => pct(p.targets)],
      ["catchable", "Catchable TGTS", (p) => pct(p.catchable)], ["adot", "ADOT", (p) => fmt(p.adot)], ["air_yards", "Air Yards", (p) => pct(p.air_yards)],
      ["ez", "EZ TGTS", (p) => pct(p.ez)], ["d34", "3/4 Down TGTS", (p) => pct(p.d34)]],
    TE: [["gms", "GMS", (p) => p.gms], ["snap", "Snaps", (p) => pct(p.snap)], ["targets", "Targets", (p) => pct(p.targets)],
      ["catchable", "Catchable TGTS", (p) => pct(p.catchable)], ["adot", "ADOT", (p) => fmt(p.adot)], ["air_yards", "Air Yards", (p) => pct(p.air_yards)],
      ["ez", "EZ TGTS", (p) => pct(p.ez)], ["d34", "3/4 Down TGTS", (p) => pct(p.d34)]],
  };
  async function ensureUsageData() {
    if (state.usage.data) return state.usage.data;
    if (!state.usage.promise) {
      state.usage.promise = fetch("data/team_usage.json")
        .then((r) => (r.ok ? r.json() : {}))
        .then((d) => { state.usage.data = d; return d; })
        .catch(() => { state.usage.data = {}; return {}; });
    }
    return state.usage.promise;
  }
  async function renderUsageSetup() {
    const data = await ensureUsageData();
    const teams = Object.keys(data).sort();
    const sel = $("#usage-team");
    if (!sel.options.length) {
      teams.forEach((t) => sel.append(el("option", { value: t, text: t })));
      if (teams.length) { sel.value = teams[0]; state.usage.team = teams[0]; }
    }
    renderUsageTable();
  }
  function renderUsagePills() {
    const team = state.usage.data && state.usage.data[state.usage.team];
    const positions = team ? Object.keys(USAGE_COLUMNS).filter((p) => team[p] && team[p].length) : [];
    if (!positions.includes(state.usage.pos)) state.usage.pos = positions[0] || null;
    const box = clear($("#usage-pos"));
    positions.forEach((p) => box.append(el("button", { class: state.usage.pos === p ? "active" : "", text: p,
      onclick: () => { state.usage.pos = p; renderUsagePills(); renderUsageTable(); } })));
  }
  function renderUsageTable() {
    renderUsagePills();
    const table = clear($("#usage-table"));
    const data = state.usage.data;
    const rows = (data && state.usage.team && state.usage.pos && data[state.usage.team][state.usage.pos]) || [];
    const cols = USAGE_COLUMNS[state.usage.pos] || USAGE_COLUMNS.RB;
    table.append(el("thead", {}, el("tr", {}, el("th", { text: "Player" }), ...cols.map(([, label]) => el("th", { class: "num", text: label })))));
    table.append(el("tbody", {}, rows.map((p) => el("tr", {},
      el("td", {}, nameLink(p.id, p.name)), ...cols.map(([key, , fn]) => el("td", { class: "num", text: p[key] === null || p[key] === undefined ? "–" : fn(p) }))))));
    $("#usage-note").textContent = rows.length
      ? "Percentages are a share of THAT team's season total (offensive snaps, carries, targets), except Catchable/EZ/3-4 Down TGTS which are a % of the player's own targets. Routes and TPRR are not included: nflverse's public participation data is not updated in-season, only through last season — I won't fake it from snaps."
      : "No data for this team/position yet.";
  }

  // ------------------------------------------------------------------ stats charts tab (RBSDM-style scatters)
  const TEAM_COLORS = {
    ARI: "#97233f", ATL: "#a71930", BAL: "#241773", BUF: "#00338d", CAR: "#0085ca", CHI: "#c83803", CIN: "#fb4f14", CLE: "#ff3c00",
    DAL: "#003594", DEN: "#fb4f14", DET: "#0076b6", GB: "#203731", HOU: "#a71930", IND: "#002c5f", JAX: "#006778", KC: "#e31837",
    LA: "#003594", LAC: "#0080c6", LV: "#a5acaf", MIA: "#008e97", MIN: "#4f2683", NE: "#002244", NO: "#d3bc8d", NYG: "#0b2265",
    NYJ: "#125740", PHI: "#004c54", PIT: "#ffb612", SEA: "#69be28", SF: "#aa0000", TB: "#d50a0a", TEN: "#4b92db", WAS: "#773141",
  };
  const lastName = (full) => {
    const parts = String(full).split(" ");
    while (parts.length > 1 && /^(Jr\.?|Sr\.?|II|III|IV|V)$/i.test(parts[parts.length - 1])) parts.pop();
    return parts[parts.length - 1];
  };
  const teamColor = (t) => TEAM_COLORS[t] || "#888";
  // ESPN's public logo CDN (same abbreviations as ours except WAS/LA); if a logo fails to load we fall back to a colored dot.
  const LOGO_ABBR = { WAS: "wsh", LA: "lar" };
  const logoUrl = (t) => `img/logos/${t}.png`; // saved next to the site by the pipeline
  const logoFallback = (t) => `https://a.espncdn.com/i/teamlogos/nfl/500/${LOGO_ABBR[t] || String(t).toLowerCase()}.png`;
  // Each chart is one entry; "x"/"y" = [data key, axis title, decimals, optional scale (e.g. 100 for %)]; "quad" labels the four corners
  // (top-right / top-left / bottom-right / bottom-left) like the community charts do. After flipping (defense) up/right is still "better".
  const Q = (tr, tl, br, bl) => ({ tr, tl, br, bl });
  const CHARTS = {
    qb: { group: "Quarterbacks", pill: "Efficiency vs accuracy", title: "QB efficiency: EPA per dropback vs completion % over expected", src: "qb", unit: "dropbacks",
      x: ["cpoe", "Accuracy vs expected (CPOE, %)", 1], y: ["epa", "Points added per dropback (EPA)", 2],
      quad: Q("Elite", "Efficient, less accurate", "Accurate, less efficient", "Struggling"),
      note: "Up and right is better: efficient AND accurate. A dropback is any pass attempt, sack or scramble." },
    rb: { group: "Running backs", pill: "Efficiency vs consistency", title: "RB efficiency: EPA per carry vs success rate", src: "rb", unit: "carries",
      x: ["success", "Success rate (%)", 0, 100], y: ["epa", "Points added per carry (EPA)", 2],
      quad: Q("Elite", "Boom / bust", "Chain movers", "Struggling"),
      note: "Designed runs only (QB scrambles excluded). Success rate = share of runs that kept the offense on schedule." },
    off: { group: "Teams", pill: "Offense", title: "Team offense: passing vs rushing EPA per play", src: "team",
      x: ["off_rush", "Rushing: points added per play (EPA)", 2], y: ["off_pass", "Passing: points added per play (EPA)", 2],
      quad: Q("Complete offense", "Pass-led", "Run-led", "Struggling"), note: "Up and right is better." },
    def: { group: "Teams", pill: "Defense", title: "Team defense: passing vs rushing EPA allowed", src: "team", flip: true,
      x: ["def_rush", "Run defense: points prevented per play (EPA, flipped)", 2], y: ["def_pass", "Pass defense: points prevented per play (EPA, flipped)", 2],
      quad: Q("Lockdown", "Strong vs the pass", "Strong vs the run", "Vulnerable"),
      note: "Axes are flipped (minus the EPA the defense allows) so up and right is better, like the other charts." },
  };
  [["WR", "Wide receivers"], ["TE", "Tight ends"]].forEach(([pos, group]) => {
    const k = pos.toLowerCase();
    CHARTS[k + "_epa"] = { group, pill: "Efficiency vs depth", title: `${pos} efficiency: EPA per target vs average depth of target`, src: "rec", pos, unit: "targets",
      x: ["adot", "Average depth of target (air yards)", 1], y: ["epa", "Points added per target (EPA)", 2],
      quad: Q("Deep threats", "Efficient underneath", "Deep, low payoff", "Low impact"),
      note: "Right = deeper targets, up = more efficient. Incompletions count against the player." };
    CHARTS[k + "_eff"] = { group, pill: "Usage vs efficiency", title: `${pos} usage & efficiency: target share vs yards per target`, src: "rec", pos, unit: "targets",
      x: ["ypt", "Yards per target", 1], y: ["tgt_share", "Share of team pass attempts (%)", 0, 100],
      quad: Q("Elite", "High target share, low efficiency", "High efficiency, low target share", "Low usage"),
      note: "Routes run are not published in-season, so this uses target share (targets / team pass attempts) and yards per target instead of TPRR and YPRR." };
    CHARTS[k + "_xfp"] = { group, pill: "Opportunity vs production", title: `${pos} opportunity & production: expected vs actual fantasy points (PPR)`, src: "xfp", pos, unit: "targets",
      x: ["xfp", "Expected fantasy points (xFP, from usage)", 1], y: ["fp", "Actual fantasy points", 1],
      quad: Q("High opportunity, high production", "Making it count", "Missed opportunity", "Low opportunity, low production"),
      note: "xFP = the fantasy points an average player would have scored on the same targets, from nflverse. Above the trend line = scoring more than his usage predicts." };
    CHARTS[k + "_tgt"] = { group, pill: "Target share vs red zone", title: `${pos} overall target share vs red-zone target share`, src: "rec", pos, unit: "targets",
      x: ["tgt_share", "Overall target share (% of team pass attempts)", 0, 100], y: ["rz_share", "Red-zone target share (% of team red-zone attempts)", 0, 100],
      quad: Q("High overall, high red-zone share", "Red-zone specialist", "High overall, low red-zone share", "Low share, low red-zone share"),
      note: "Red zone = inside the opponent's 20. Shares are of the team's pass attempts while he played for his current team." };
    CHARTS[k + "_sep"] = { group, pill: "Separation vs YAC", title: `${pos} separation & after-the-catch value`, src: "sep", pos, unit: "targets",
      x: ["sep", "Average separation at the catch (yards)", 2], y: ["yacoe", "Yards after catch above expected", 2],
      quad: Q("Separates and creates", "Creates after the catch", "Open, no extra yards", "Struggles"),
      note: "From NFL Next Gen Stats (via nflverse). These are raw averages, not percentile grades." };
  });
  const QUAD_TONE = { tr: "good", tl: "blue", br: "gold", bl: "bad" };
  async function ensureChartData() {
    if (state.charts.data) return state.charts.data;
    if (!state.charts.promise) {
      state.charts.promise = fetch("data/stats_charts.json").then((r) => (r.ok ? r.json() : null))
        .then((d) => { state.charts.data = d || {}; return state.charts.data; }).catch(() => { state.charts.data = {}; return {}; });
    }
    return state.charts.promise;
  }
  function chartPoints(cfg, data) {
    const [xk, , , xs = 1] = cfg.x, [yk, , , ys = 1] = cfg.y, sign = cfg.flip ? -1 : 1;
    const num = (v, k) => (v === null || v === undefined ? null : v * k * sign);
    return (data[cfg.src] || []).filter((r) => !cfg.pos || r.pos === cfg.pos).map((r) => ({
      ...r, label: cfg.src === "team" ? r.team : lastName(r.name), px: num(r[xk], xs), py: num(r[yk], ys),
    }));
  }
  const svgEl = (tag, attrs, text) => {
    const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attrs || {}).forEach(([k, v]) => n.setAttribute(k, v));
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const wrapWords = (text, max) => {
    const lines = []; let cur = "";
    String(text).split(" ").forEach((w) => { if (cur && (cur + " " + w).length > max) { lines.push(cur); cur = w; } else cur = cur ? cur + " " + w : w; });
    if (cur) lines.push(cur);
    return lines;
  };
  /* A corner label: a colored pill with the text wrapped to a couple of lines, anchored to a corner of the plot area. */
  function cornerLabel(svg, text, tone, x, y, right, bottom, maxChars) {
    const lines = wrapWords(text, maxChars), w = Math.max(...lines.map((l) => l.length)) * 6 + 18, h = lines.length * 13 + 9;
    const g = svgEl("g", { class: "qpill " + tone }), rx = right ? x - w : x, ry = bottom ? y - h : y;
    g.append(svgEl("rect", { x: rx, y: ry, width: w, height: h, rx: 9 }));
    lines.forEach((l, i) => g.append(svgEl("text", { x: rx + w / 2, y: ry + 15 + i * 13, "text-anchor": "middle" }, l)));
    svg.append(g);
  }
  async function renderCharts() {
    const data = await ensureChartData();
    const groups = {};
    Object.entries(CHARTS).forEach(([k, c]) => (groups[c.group] = groups[c.group] || []).push([k, c]));
    const kinds = clear($("#chart-kind"));
    ["Quarterbacks", "Running backs", "Wide receivers", "Tight ends", "Teams"].forEach((name) => {
      const items = groups[name] || [];
      const pills = el("div", { class: "pills" });
      items.forEach(([k, c]) => pills.append(el("button", { class: state.charts.kind === k ? "active" : "", text: c.pill, onclick: () => { state.charts.kind = k; renderCharts(); } })));
      kinds.append(el("div", { class: "chart-group" }, [el("span", { class: "grp", text: name }), pills]));
    });
    const cfg = CHARTS[state.charts.kind], box = clear($("#chart-box"));
    $("#chart-title").textContent = cfg.title;
    const all = chartPoints(cfg, data);
    if (!all.length) { $("#chart-note").textContent = "No chart data yet: run `python -m ff.models.value` to generate site/data/stats_charts.json."; $("#chart-min-wrap").classList.add("hidden"); return; }
    const hasMin = cfg.src !== "team";
    $("#chart-min-wrap").classList.toggle("hidden", !hasMin);
    let pts = all;
    if (hasMin) {
      const maxN = Math.max(...all.map((p) => p.n)), slider = $("#chart-min");
      if (state.charts.min[state.charts.kind] === undefined) state.charts.min[state.charts.kind] = Math.round(maxN * 0.35);
      slider.max = String(maxN); slider.value = String(state.charts.min[state.charts.kind]);
      $("#chart-min-unit").textContent = cfg.unit; $("#chart-min-val").textContent = slider.value;
      pts = all.filter((p) => p.n >= Number(slider.value));
    }
    const W = Math.max(320, Math.min(900, box.clientWidth || 900)), narrow = W < 620;
    const H = Math.round(narrow ? W * 1.05 : W * 0.62), pad = { l: narrow ? 46 : 56, r: 14, t: 14, b: 46 };
    const lay = FF.scatterLayout(pts, "px", "py", { width: W, height: H, pad });
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": cfg.title });
    const clip = svgEl("clipPath", { id: "plot-clip" });
    clip.append(svgEl("rect", { x: pad.l, y: pad.t, width: W - pad.l - pad.r, height: H - pad.t - pad.b }));
    svg.append(clip);
    const L = pad.l, R = W - pad.r, T = pad.t, B = H - pad.b;
    if (lay.meanX !== null && cfg.quad) { // tinted quadrants around the average, like the reference charts
      const mx = lay.meanX, my = lay.meanY;
      [["tl", L, T, mx, my], ["tr", mx, T, R, my], ["bl", L, my, mx, B], ["br", mx, my, R, B]].forEach(([q, x0, y0, x1, y1]) =>
        svg.append(svgEl("rect", { class: "quad " + QUAD_TONE[q], x: x0, y: y0, width: Math.max(0, x1 - x0), height: Math.max(0, y1 - y0) })));
    }
    lay.xTicks.forEach((t) => { svg.append(svgEl("line", { class: "grid", x1: t.at, x2: t.at, y1: T, y2: B }), svgEl("text", { x: t.at, y: B + 16, "text-anchor": "middle" }, String(+t.v.toFixed(2)))); });
    lay.yTicks.forEach((t) => { svg.append(svgEl("line", { class: "grid", x1: L, x2: R, y1: t.at, y2: t.at }), svgEl("text", { x: L - 8, y: t.at + 4, "text-anchor": "end" }, String(+t.v.toFixed(2)))); });
    if (lay.zeroX !== null) svg.append(svgEl("line", { class: "zero", x1: lay.zeroX, x2: lay.zeroX, y1: T, y2: B }));
    if (lay.zeroY !== null) svg.append(svgEl("line", { class: "zero", x1: L, x2: R, y1: lay.zeroY, y2: lay.zeroY }));
    if (lay.meanX !== null) svg.append(svgEl("line", { class: "mean", x1: lay.meanX, x2: lay.meanX, y1: T, y2: B }), svgEl("line", { class: "mean", x1: L, x2: R, y1: lay.meanY, y2: lay.meanY }));
    if (lay.fit) svg.append(svgEl("line", { class: "fit", x1: lay.fit.x1, y1: lay.fit.y1, x2: lay.fit.x2, y2: lay.fit.y2, "clip-path": "url(#plot-clip)" }));
    svg.append(svgEl("text", { class: "axis-title", x: (L + R) / 2, y: H - 8, "text-anchor": "middle" }, cfg.x[1]),
      svgEl("text", { class: "axis-title", x: 14, y: (T + B) / 2, "text-anchor": "middle", transform: `rotate(-90 14 ${(T + B) / 2})` }, cfg.y[1]));
    const tip = el("div", { class: "chart-tip hidden" });
    const fmtVal = (v, d) => (v === null || v === undefined ? "–" : Number(v).toFixed(d));
    const sign = cfg.flip ? -1 : 1, mc = narrow ? 14 : 22, placed = [];
    lay.points.slice().sort((a, b) => (b.p.n || 0) - (a.p.n || 0)).forEach(({ p, x, y }) => {
      const c = svgEl("circle", { cx: x, cy: y, r: cfg.src === "team" ? 7 : 5.5, fill: teamColor(p.team) });
      const show = () => {
        const lines = [`<b>${p.name || p.team}</b>${p.team && p.name ? " · " + p.team : ""}`];
        if (p.n !== undefined) lines.push(`${p.n} ${cfg.unit}`);
        lines.push(`${cfg.x[1].split(" (")[0]}: ${fmtVal(p.px * sign, cfg.x[2])}`, `${cfg.y[1].split(" (")[0]}: ${fmtVal(p.py * sign, cfg.y[2] + 1)}`);
        tip.innerHTML = lines.join("<br>"); tip.classList.remove("hidden");
        tip.style.left = Math.min(x / W * box.clientWidth + 12, box.clientWidth - 170) + "px"; tip.style.top = (y / H * (svg.getBoundingClientRect().height || H)) + "px";
      };
      // every point is the team's logo (our own copy first, ESPN as a second try, a colored dot as the last resort)
      const isTeam = cfg.src === "team", size = isTeam ? 28 : 26, lblY = y + size / 2 + 10;
      c.setAttribute("display", "none");
      const img = svgEl("image", { x: x - size / 2, y: y - size / 2, width: size, height: size, href: logoUrl(p.team), class: "logo" });
      let retried = false;
      const needsLabel = !placed.some((q) => Math.abs(q.x - x) < (isTeam ? 30 : 40) && Math.abs(q.y - y) < (isTeam ? 30 : 26));
      img.addEventListener("error", () => {
        if (!retried) { retried = true; img.setAttribute("href", logoFallback(p.team)); return; }
        img.remove(); c.removeAttribute("display");
        if (isTeam) svg.append(svgEl("text", { class: "lbl", x: x + 9, y: y + 3 }, p.team));
      });
      [c, img].forEach((n) => { ["mouseenter", "click"].forEach((ev) => n.addEventListener(ev, show)); n.addEventListener("mouseleave", () => tip.classList.add("hidden")); });
      svg.append(c, img);
      if (needsLabel) { placed.push({ x, y }); if (!isTeam) svg.append(svgEl("text", { class: "lbl", x, y: lblY, "text-anchor": "middle" }, p.label)); }
    });
    if (cfg.quad) { // drawn last so the corner labels sit above the dots
      cornerLabel(svg, cfg.quad.tl, "blue", L + 8, T + 8, false, false, mc); cornerLabel(svg, cfg.quad.tr, "good", R - 8, T + 8, true, false, mc);
      cornerLabel(svg, cfg.quad.bl, "bad", L + 8, B - 8, false, true, mc); cornerLabel(svg, cfg.quad.br, "gold", R - 8, B - 8, true, true, mc);
    }
    box.append(svg, tip);
    $("#chart-note").textContent = `${cfg.note} Through week ${data.as_of ? data.as_of.through_week : "?"} of ${data.as_of ? data.as_of.season : ""}. Dotted lines = average of the dots shown; solid gray line = overall trend.`;
  }

  // ------------------------------------------------------------------ ROS (rest-of-season schedule) tab
  const ROS_VIEWS = {
    overall: { pill: "All teams", title: "Remaining Strength of Schedule", center: 50, pct: false,
      sub: "Average remaining opponent win% vs. a league-average team on a neutral field, site-adjusted. Dashed line = league average (50%). Purple = harder, green = easier.",
      note: "Opponent strength comes from betting-market point spreads (including next week's posted lines) blended with real score margins, solved jointly so each rating accounts for who the team played." },
    QB: { pill: "QB", title: "Remaining QB Schedule", center: 0, pct: true, pos: "QB" },
    RB: { pill: "RB", title: "Remaining RB Schedule", center: 0, pct: true, pos: "RB" },
    RB: { pill: "RB", title: "Remaining RB Schedule", center: 0, pct: true, pos: "RB" },
    WR: { pill: "WR", title: "Remaining WR Schedule", center: 0, pct: true, pos: "WR" },
    TE: { pill: "TE", title: "Remaining TE Schedule", center: 0, pct: true, pos: "TE" },
  };
  ["QB", "RB", "WR", "TE"].forEach((p) => {
    ROS_VIEWS[p].sub = `How many fewer (or more) fantasy points than average the remaining opponents allow to ${p}s. Dashed line = league average. Purple = tougher defenses ahead, green = softer.`;
    ROS_VIEWS[p].note = "Based on fantasy points allowed to the position (PPR, last two seasons, recent games weigh more). Home/away is not adjusted. Historically this signal is weak, so treat it as context, not a forecast.";
  });
  async function ensureRosData() {
    if (state.ros.data) return state.ros.data;
    if (!state.ros.promise) {
      state.ros.promise = fetch("data/ros.json").then((r) => (r.ok ? r.json() : null))
        .then((d) => { state.ros.data = d || {}; return state.ros.data; }).catch(() => { state.ros.data = {}; return {}; });
    }
    return state.ros.promise;
  }
  async function renderRos() {
    const data = await ensureRosData(), view = ROS_VIEWS[state.ros.kind];
    const kinds = clear($("#ros-kind")), pills = el("div", { class: "pills" });
    Object.entries(ROS_VIEWS).forEach(([k, v]) => pills.append(el("button", { class: state.ros.kind === k ? "active" : "", text: v.pill, onclick: () => { state.ros.kind = k; renderRos(); } })));
    kinds.append(el("div", { class: "chart-group" }, [pills]));
    $("#ros-title").textContent = view.title; $("#ros-sub").textContent = view.sub;
    const box = clear($("#ros-box")), rows = Object.entries(data[state.ros.kind] || {}).map(([team, r]) => ({ team, v: r.v, n: r.n }));
    if (!rows.length) { $("#ros-note").textContent = "No ROS data yet: run `python -m ff.models.value` to generate site/data/ros.json."; return; }
    const W = 960, rowH = 18, pad = { l: 40, r: 56, t: 34, b: 28 }, H = pad.t + pad.b + rowH * rows.length;
    const lay = FF.barLayout(rows, { width: W, height: H, pad, center: view.center });
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": view.title });
    const fmtV = (v) => (view.pct ? `${v > 0 ? "+" : ""}${v.toFixed(1)}%` : `${v.toFixed(1)}%`);
    lay.ticks.forEach((t) => {
      svg.append(svgEl("line", { class: "grid", x1: t.at, x2: t.at, y1: pad.t, y2: H - pad.b }));
      svg.append(svgEl("text", { x: t.at, y: H - pad.b + 16, "text-anchor": "middle" }, view.pct ? `${t.v > 0 ? "+" : ""}${t.v}%` : `${t.v}%`));
    });
    svg.append(svgEl("line", { class: "ros-mid", x1: lay.cx, x2: lay.cx, y1: pad.t, y2: H - pad.b }));
    lay.rows.forEach(({ item, x, y }) => {
      const hard = item.v > view.center, size = 18;
      svg.append(svgEl("line", { class: "ros-bar " + (hard ? "hard" : "easy"), x1: lay.cx, x2: x, y1: y, y2: y }));
      const g = svgEl("g", { class: "ros-row" });
      g.append(svgEl("title", {}, `${item.team}: ${fmtV(item.v)} (${item.n} games left)`));
      const img = svgEl("image", { x: x - size / 2, y: y - size / 2, width: size, height: size, href: logoUrl(item.team), class: "logo" });
      let retried = false;
      img.addEventListener("error", () => {
        if (!retried) { retried = true; img.setAttribute("href", logoFallback(item.team)); return; }
        img.remove(); g.append(svgEl("text", { class: "lbl", x, y: y + 3, "text-anchor": "middle" }, item.team));
      });
      g.append(img);
      g.append(svgEl("text", { x: x + (hard ? size / 2 + 4 : -size / 2 - 4), y: y + 4, "text-anchor": hard ? "start" : "end" }, fmtV(item.v)));
      svg.append(g);
    });
    svg.append(svgEl("text", { class: "ros-tag hard", x: W - pad.r, y: 20, "text-anchor": "end" }, "HARD \u2192"));
    svg.append(svgEl("text", { class: "ros-tag easy", x: pad.l, y: 20 }, "\u2190 EASY"));
    box.append(svg);
    const asOf = data.as_of ? ` As of week ${data.as_of[1]} of ${data.as_of[0]}.` : "";
    $("#ros-note").textContent = view.note + asOf;
  }

  function renderAll() {
    renderHeader();
    renderPlayerOptions();
    renderPower();
    renderTradeTeams();
    renderTradeSides();
    renderTradeResult();
    renderFinderSetup();
    renderAdjustments();
    renderPosFilter();
    renderValues();
  }

  function init() {
    loadAdj();
    recompute();
    wirePlayerCard();
    $("#season").value = DATA.as_of.season;
    try { $("#username").value = localStorage.getItem("ff_username") || ""; } catch (e) { /* ignore */ }

    $("#connect-btn").addEventListener("click", findLeagues);
    $("#username").addEventListener("keydown", (e) => { if (e.key === "Enter") findLeagues(); });
    $("#load-league-btn").addEventListener("click", () => loadLeague($("#league-select").value));
    $("#load-id-btn").addEventListener("click", () => loadLeague($("#league-id").value.trim()));

    document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
    moveGlider();
    let rz = null;
    window.addEventListener("resize", () => { moveGlider(); clearTimeout(rz); rz = setTimeout(() => { if (!$("#tab-charts").classList.contains("hidden")) renderCharts(); }, 200); });
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(moveGlider);

    $("#me-select").addEventListener("change", (e) => {
      state.trade.meId = e.target.value;
      if (state.trade.partnerId === state.trade.meId) state.trade.partnerId = String(state.teams.find((t) => String(t.rosterId) !== state.trade.meId).rosterId);
      state.trade.give = [];
      fillPartners(); renderTradeSides(); renderTradeResult();
    });
    $("#partner-select").addEventListener("change", (e) => {
      state.trade.partnerId = e.target.value;
      state.trade.get = [];
      renderTradeSides(); renderTradeResult();
    });
    wireSearch("give");
    wireSearch("get");
    $("#values-search").addEventListener("input", (e) => { state.q = e.target.value; renderValues(); });
    $("#finder-team").addEventListener("change", () => { $("#finder-results").textContent = ""; renderFinderSetup(); });
    $("#finder-run").addEventListener("click", runFinder);
    $("#usage-team").addEventListener("change", (e) => { state.usage.team = e.target.value; renderUsageTable(); });
    $("#adj-add").addEventListener("click", () => {
      const t = $("#adj-team").value, v = parseFloat($("#adj-pct").value);
      if (!t || Number.isNaN(v)) return;
      if (v === 0) delete state.adj.teams[t]; else state.adj.teams[t] = v;
      $("#adj-pct").value = ""; saveAdj(); recompute(); renderAll();
    });
    $("#adj-reset").addEventListener("click", () => { state.adj = { players: {}, teams: {} }; saveAdj(); recompute(); renderAll(); });

    $("#chart-min").addEventListener("input", (e) => { state.charts.min[state.charts.kind] = Number(e.target.value); renderCharts(); });
    renderAll();
    renderUsageSetup();
    renderCharts();
  }

  init();
})();
