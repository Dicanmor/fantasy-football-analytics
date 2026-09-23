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
    compare: new Set(), // sleeper ids picked in the Player values tab for side-by-side comparison
    weeks: null, weeksPromise: null, // lazy-loaded site/data/player_weeks.json (see ensureWeeklyData)
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
  function weeklyTable(rows) {
    const head = ["WK", "OPP", "PROJ", "FPTS", "SNP%", "ATT", "YD", "YPC", "TD", "TAR", "REC", "YD", "Y/T", "Y/R", "TD", "FUM", "LOST", "KR", "KYD", "PR", "SP TD"];
    const groups = [["", 2], ["FANTASY", 2], ["", 1], ["RUSHING", 4], ["RECEIVING", 5], ["FUMBLE", 2], ["RETURNING", 5]];
    const dash = (v) => (v === null || v === undefined ? "–" : v);
    return el("div", { class: "table-wrap" }, el("table", { class: "weekly-table" },
      el("thead", {},
        el("tr", {}, groups.map(([g, span]) => el("th", { colspan: String(span), class: g ? "grp" : "" }, g || ""))),
        el("tr", {}, head.map((h) => el("th", { text: h })))),
      el("tbody", {}, rows.map((g) => {
        const played = g.fpts !== null && g.fpts !== undefined;
        return el("tr", { class: played ? "" : "future" },
          el("td", { text: g.wk }), el("td", { text: g.opp || "BYE" }),
          el("td", { class: "num", text: dash(g.proj) }), el("td", { class: "num strong", text: dash(g.fpts) }),
          el("td", { class: "num", text: g.snap === null || g.snap === undefined ? "–" : g.snap + "%" }),
          el("td", { class: "num", text: dash(g.ra) }), el("td", { class: "num", text: dash(g.ry) }),
          el("td", { class: "num", text: played ? per(g.ry, g.ra) : "–" }), el("td", { class: "num", text: dash(g.rt) }),
          el("td", { class: "num", text: dash(g.tg) }), el("td", { class: "num", text: dash(g.rc) }), el("td", { class: "num", text: dash(g.cy) }),
          el("td", { class: "num", text: played ? per(g.cy, g.tg) : "–" }), el("td", { class: "num", text: played ? per(g.cy, g.rc) : "–" }),
          el("td", { class: "num", text: dash(g.ct) }),
          el("td", { class: "num", text: dash(g.fm) }), el("td", { class: "num", text: dash(g.fl) }),
          el("td", { class: "num", text: dash(g.kr) }), el("td", { class: "num", text: dash(g.kyd) }), el("td", { class: "num", text: dash(g.pr) }), el("td", { class: "num", text: dash(g.spt) }));
      }))));
  }
  function summaryTab(id, p) {
    return el("div", {},
      el("div", { class: "pc-badges" }, rankBadges(id), statusTag(p), matchupBadge(p)),
      el("div", { class: "stats" },
        el("div", { class: "stat" }, el("span", { class: "muted small", text: "Proj PPG" }), el("b", { text: fmt(p.ppg) })),
        el("div", { class: "stat" }, el("span", { class: "muted small", text: "Exp. games" }), el("b", { text: fmt(p.exp_games) }), el("span", { class: "muted small", text: `${Math.round(p.avail * 100)}% availability` })),
        el("div", { class: "stat" }, el("span", { class: "muted small", text: "Value" }), el("b", { text: fmt(p.value, 0) + " pts" }), el("span", { class: "muted small", text: "over replacement" }))),
      roleText(p) ? el("p", { class: "ctx", text: roleText(p) }) : null,
      p.opp ? el("p", { class: "ctx", text: `This week: vs ${p.opp}, matchup score ${p.mu}/100 for ${p.pos}s (100 = easiest defense)` }) : null,
      el("p", { class: "muted small", text: "PPG is a recency-weighted average of his own games (current season weighted heaviest); it is not a week-by-week box score." }));
  }
  async function openPlayerCard(id, tab = "summary") {
    const p = state.values[id];
    if (!p) return;
    const modal = $("#player-modal");
    const body = clear($("#player-modal-body"));
    const tabs = el("div", { class: "pc-tabs" },
      el("button", { class: "pc-tab" + (tab === "summary" ? " active" : ""), onclick: () => openPlayerCard(id, "summary"), text: "Resumen" }),
      el("button", { class: "pc-tab" + (tab === "weeks" ? " active" : ""), onclick: () => openPlayerCard(id, "weeks"), text: "Semana a semana" }));
    body.append(
      el("div", { class: "pc-head" }, el("h3", { text: p.name }), el("span", { class: "muted", text: `${p.pos}${p.team ? " · " + p.team : ""}` })),
      tabs, el("div", { id: "pc-body" }));
    modal.classList.remove("hidden");
    const slot = $("#pc-body");
    if (tab === "summary") {
      clear(slot).append(summaryTab(id, p));
      return;
    }
    clear(slot).append(el("p", { class: "muted small", text: "Cargando…" }));
    const weeks = await ensureWeeklyData();
    const rows = weeks[id];
    clear(slot).append(
      rows
        ? weeklyTable(rows)
        : el("p", { class: "muted small", text: "No hay historial semanal para este jugador todavía." }),
      el("p", { class: "muted small", text: "PROJ es tu proyección de temporada (la misma cada semana), no una proyección específica del rival de esa semana." }));
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
    return el("div", { class: "delta-row" }, deltaChip(myDelta, "Tú"), deltaChip(theirDelta, "Ellos"));
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
        "Simula varias trades seguidas: cada una que apliques se guarda y la siguiente parte de ahí, con cualquier equipo. ",
        el("button", { class: "primary", text: "Iniciar simulación", onclick: startSim }));
    }
    const me = activeRoster(state.sim.meId);
    const now = FF.teamPower(me.players, state.values, state.ctx, me.reserve, me.taxi).power;
    const cum = now - state.sim.baseline;
    return el("div", { class: "sim-banner" },
      el("div", {}, el("b", { text: "Simulación activa" }), ` para ${teamById(state.sim.meId).name} — `,
        deltaChip(cum, "Acumulado"), ` (${state.sim.log.length} trade(s) aplicado(s))`,
        el("button", { text: "Terminar simulación", onclick: endSim })),
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
        el("label", { class: "small" }, "Your adjustment (ppg) ",
          el("input", { class: "adj", type: "number", step: "0.5", value: adj === undefined ? "" : String(adj), placeholder: "0",
            onchange: (e) => setAdj(id, parseFloat(e.target.value)) })));
    }));
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
      card.append(el("p", { class: "muted small", text: "Value counts what each player is worth on any roster; the power delta counts what your lineup actually gains or loses (flex included). A third RB is worth little to a team that already starts two good ones." }));
      if (get.length > give.length) card.append(el("p", { class: "muted small", text: `You receive ${get.length - give.length} more player(s) than you send; you would need to drop that many. Only your best ${state.league.bench} bench players count for depth.` }));
      if (state.sim) card.append(el("button", { class: "primary", text: "Aplicar este trade a la simulación", onclick: () => applySimTrade(full) }));
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
        deltaBar(r.myDelta, r.theirDelta),
        r.fills.length ? el("div", { class: "muted small" }, "Fills: " + r.fills.map((f) => `${f.slot} ${signed(f.gain)}`).join(", ")) : null,
        el("button", { class: "primary", text: "Ver comparativo →", onclick: () => openInCalculator(meId, r) })));
    });
    root.append(card);
  }

  function openInCalculator(meId, r) {
    state.trade = { meId: String(meId), partnerId: String(r.partner.rosterId), give: r.give.slice(), get: r.get.slice() };
    renderTradeTeams(); renderTradeSides(); renderTradeResult();
    showTab("trade");
  }

  function toggleCompare(id) {
    if (state.compare.has(id)) state.compare.delete(id);
    else if (state.compare.size < 5) state.compare.add(id);
    renderValues();
  }
  function renderCompareBar() {
    const old = $("#compare-bar");
    if (old) old.remove();
    if (state.compare.size < 2) return;
    const bar = el("div", { id: "compare-bar", class: "compare-bar" },
      el("span", { class: "muted small", text: `${state.compare.size} seleccionados` }),
      el("button", { class: "primary", text: "Comparar →", onclick: openCompareView }),
      el("button", { text: "Limpiar", onclick: () => { state.compare.clear(); renderValues(); } }));
    $("#tab-values .card").appendChild(bar);
  }
  function openCompareView() {
    const ids = [...state.compare];
    const players = ids.map((id) => ({ id, ...state.values[id] }));
    const modal = $("#player-modal"), body = clear($("#player-modal-body"));
    const rowDef = [
      ["Equipo", (p) => p.team || ""],
      ["Rank", (p) => `#${state.ranks[p.id].overall} ovr / ${FF.posLabel(p.pos, state.ranks[p.id].pos)}`],
      ["Tier", (p) => state.tiers[p.id] || "–"],
      ["Proj PPG", (p) => fmt(p.ppg)],
      ["Exp. games", (p) => fmt(p.exp_games)],
      ["Value", (p) => fmt(p.value, 0)],
      ["Matchup", (p) => (p.opp ? `vs ${p.opp} (${p.mu})` : "–")],
      ["Role", (p) => (p.share === null || p.share === undefined ? "–" : pct(p.share))],
      ["Status", (p) => p.status || "–"],
    ];
    body.append(
      el("div", { class: "pc-head" }, el("h3", { text: "Comparativo" })),
      el("div", { class: "table-wrap" }, el("table", { class: "compare-table" },
        el("thead", {}, el("tr", {}, el("th", {}), ...players.map((p) => el("th", {}, nameLink(p.id))))),
        el("tbody", {}, rowDef.map(([label, fn]) => el("tr", {},
          el("td", { text: label }), ...players.map((p) => el("td", { text: fn(p) }))))))));
    modal.classList.remove("hidden");
  }

  // ------------------------------------------------------------------ player values table
  const COLUMNS = [
    ["cmp", "", false], ["rank", "#", true], ["name", "Player", false], ["pos_rank", "Pos", true], ["tier", "Tier", false], ["team", "Team", false], ["age", "Age", true],
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
      el("td", {}, el("input", { class: "cmp-check", type: "checkbox", checked: state.compare.has(r.id) || null,
        disabled: !state.compare.has(r.id) && state.compare.size >= 5 || null, onchange: () => toggleCompare(r.id) })),
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
    renderCompareBar();
    $("#matchup-note").textContent =
      `Matchup: how many fantasy points the ${DATA.as_of.season} week ${DATA.as_of.week} opponent allows to that position vs. the other defenses (100 = easiest, ` +
      `Favorable ≥ ${MODEL.matchup.favorable}, Tough ≤ ${MODEL.matchup.tough}). In backtests this was a weak predictor: use it as context. ` +
      `Role: share of team rushes + targets over the last 4 games (↔ = shares touches with a teammate; hover for detail). Marca hasta 5 jugadores para comparar.`;
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
  function showTab(name) {
    document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x.dataset.tab === name));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== "tab-" + name));
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
    $("#adj-add").addEventListener("click", () => {
      const t = $("#adj-team").value, v = parseFloat($("#adj-pct").value);
      if (!t || Number.isNaN(v)) return;
      if (v === 0) delete state.adj.teams[t]; else state.adj.teams[t] = v;
      $("#adj-pct").value = ""; saveAdj(); recompute(); renderAll();
    });
    $("#adj-reset").addEventListener("click", () => { state.adj = { players: {}, teams: {} }; saveAdj(); recompute(); renderAll(); });

    renderAll();
  }

  init();
})();
