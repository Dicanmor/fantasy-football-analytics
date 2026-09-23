// End-to-end smoke test of the page in jsdom with a mocked Sleeper API (needs: npm install in web_tests/).
// Verifies: load, connect to a (fake) league with FLEX / K / DEF / IR / taxi, lineups, matchup badges, trades,
// trade finder, user adjustments, error handling.
const assert = require("assert");
const path = require("path");
const fs = require("fs");
const { JSDOM, VirtualConsole } = require("jsdom");

const site = path.resolve(__dirname, "..", "site");
const data = JSON.parse(fs.readFileSync(path.join(site, "data", "player_values.json"), "utf8"));
const byPos = (pos) => Object.entries(data.players).filter(([, p]) => p.pos === pos).sort((a, b) => b[1].value - a[1].value).map(([id]) => id);
const [qb, rb, wr, te, kk, dd] = ["QB", "RB", "WR", "TE", "K", "DEF"].map(byPos);

// 4 teams. Team 1 (Diego = u2 is team 2): "thin stars" has only 3 RBs, two of them elite, and nothing else deep.
const rosters = [
  { roster_id: 1, owner_id: "u1", players: [qb[8], rb[10], rb[11], rb[12], wr[10], wr[11], wr[12], wr[13], te[10], kk[0], dd[0], "999999"], reserve: [], taxi: [] },
  { roster_id: 2, owner_id: "u2", players: [qb[1], rb[0], rb[1], rb[2], wr[3], wr[30], te[8], kk[1], dd[1], rb[25]], reserve: [rb[25]], taxi: [] },
  { roster_id: 3, owner_id: "u3", players: [qb[4], rb[3], rb[4], rb[5], rb[6], wr[0], wr[1], wr[2], te[0], kk[2], dd[2], wr[20], wr[21]], reserve: [], taxi: [wr[21]] },
  { roster_id: 4, owner_id: "u4", players: [qb[12], rb[20], rb[21], wr[20], wr[22], wr[23], te[20], kk[3], dd[3]], reserve: [], taxi: [] },
];
const users = [1, 2, 3, 4].map((i) => ({ user_id: "u" + i, display_name: "Owner" + i, metadata: i === 3 ? { team_name: "Team <b>Three</b>" } : {} }));

function makeFetch(mode) {
  const routes = {
    "/user/diego": { user_id: "u2", display_name: "Diego" },
    "/user/u2/leagues/nfl/2026": [{ league_id: "L1", name: "My League", total_rosters: 4 }],
    "/league/L1": { league_id: "L1", name: "My League", total_rosters: 4, settings: { reserve_slots: 3, taxi_slots: 1 },
      roster_positions: ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF", "BN", "BN", "BN", "BN", "BN", "IR", "IR", "IR"], scoring_settings: { rec: 1 } },
    "/league/L1/rosters": rosters, "/league/L1/users": users,
  };
  return async (url) => {
    if (mode === "offline") throw new TypeError("Failed to fetch");
    const hit = Object.keys(routes).find((k) => url.endsWith(k));
    return hit ? { ok: true, status: 200, json: async () => routes[hit] } : { ok: false, status: 404, json: async () => null };
  };
}

async function open(mode) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push(e.message));
  vc.on("error", (m) => errors.push(String(m)));
  const dom = await JSDOM.fromFile(path.join(site, "index.html"), {
    runScripts: "dangerously", resources: "usable", virtualConsole: vc, pretendToBeVisual: true,
    beforeParse(w) {
      w.fetch = makeFetch(mode);
      const mem = {}; // jsdom has no localStorage for file:// pages; provide a minimal one
      Object.defineProperty(w, "localStorage", { value: { getItem: (k) => (k in mem ? mem[k] : null), setItem: (k, v) => { mem[k] = String(v); } } });
    },
  });
  await new Promise((r) => dom.window.addEventListener("load", r));
  return { dom, w: dom.window, d: dom.window.document, errors };
}
const tick = (ms = 40) => new Promise((r) => setTimeout(r, ms));
const click = (w, n) => n.dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
const change = (w, n, v) => { n.value = v; n.dispatchEvent(new w.Event("change", { bubbles: true })); };

(async () => {
  // ---- happy path
  let { w, d, errors } = await open("ok");
  assert.match(d.querySelector("#asof").textContent, /week \d+ · PPR · 12 teams/);
  assert.ok(d.querySelectorAll("#values-table tbody tr").length > 100, "values table renders");
  assert.match(d.querySelector("#tab-power").textContent, /Connect a Sleeper league/);
  assert.ok([...d.querySelectorAll("#pos-filter button")].some((b) => b.textContent === "DEF") && [...d.querySelectorAll("#pos-filter button")].some((b) => b.textContent === "K"));

  d.querySelector("#username").value = "diego";
  click(w, d.querySelector("#connect-btn"));
  await tick(120);
  assert.match(d.querySelector("#status").textContent, /Loaded "My League" \(4 teams\)/);
  assert.match(d.querySelector("#asof").textContent, /4 teams.*3 IR/);
  const rows = [...d.querySelectorAll("#tab-power tbody tr.clickable")];
  assert.strictEqual(rows.length, 4);
  assert.ok(d.querySelector("#tab-power tr.me"), "my team highlighted");
  assert.ok(!d.querySelector("#tab-power").innerHTML.includes("<b>Three</b>"), "team names are not injected as HTML");
  const powers = rows.map((r) => Number(r.children[2].textContent));
  assert.deepStrictEqual(powers, [...powers].sort((a, b) => b - a), "ranked by power, descending");
  assert.strictEqual(d.querySelectorAll("#warnings li").length, 0, "PPR + FLEX league: no warnings");

  // lineup view: real slots incl. FLEX, K and DEF; IR separate from the bench; taxi listed
  click(w, [...rows].find((r) => r.textContent.includes("Owner2") || r.classList.contains("me")));
  const detail = d.querySelector("#tab-power tr.me").nextElementSibling;
  assert.ok(!detail.classList.contains("hidden"), "row expands");
  const slots = [...detail.querySelectorAll(".slot")].map((x) => x.textContent);
  assert.deepStrictEqual(slots, ["QB", "RB1", "RB2", "WR1", "WR2", "TE", "FLEX", "K", "DEF"]);
  assert.match(detail.textContent, /IR \(1\/3\)/);
  assert.ok(detail.querySelector(".mu"), "matchup badges shown");
  rows.forEach((r) => { if (r.nextElementSibling.classList.contains("hidden")) click(w, r); });
  assert.match(d.querySelector("#tab-power").textContent, /Bench \(\d\/5\)/);   // teams with extra players show a bench
  assert.match(d.querySelector("#tab-power").textContent, /Taxi/);                  // team 3 has a taxi player

  // trade tab: pick players from both rosters
  click(w, d.querySelector('.tabs button[data-tab="trade"]'));
  assert.strictEqual(d.querySelector("#me-select").value, "2");
  const giveBtns = d.querySelectorAll("#give-roster button"), getBtns = d.querySelectorAll("#get-roster button");
  assert.ok(giveBtns.length >= 5 && getBtns.length >= 5);
  click(w, giveBtns[0]);
  click(w, getBtns[0]);
  const result = d.querySelector("#trade-result").textContent;
  assert.match(result, /Effect on each lineup/);
  assert.match(result, /Context/);
  assert.match(result, /availability/);
  assert.match(result, /Role:/);

  // user adjustment changes the value (ball knowledge) and persists in localStorage
  const giveId = Object.keys(data.players).find((id) => data.players[id].name === d.querySelector("#give-chips .chip").textContent.split(" (")[0]);
  const before = d.querySelector("#trade-result .stat b").textContent;
  const adjInput = d.querySelector("#trade-result input.adj");
  change(w, adjInput, "-6");
  assert.notStrictEqual(d.querySelector("#trade-result .stat b").textContent, before, "adjusting the player recalculates the trade");
  assert.strictEqual(JSON.parse(w.localStorage.getItem("ff_adj_v1")).players[giveId], -6);

  // free-mode search adds a player not on the roster -> graceful message
  const input = d.querySelector("#give-input");
  input.value = d.querySelector("#player-options option:nth-child(60)").value;
  input.dispatchEvent(new w.Event("input", { bubbles: true }));
  assert.match(d.querySelector("#trade-result").textContent, /must be on your roster\. Showing player values only/);

  // trade finder
  click(w, d.querySelector('.tabs button[data-tab="finder"]'));
  assert.ok(d.querySelectorAll("#finder-needs .need").length >= 5, "needs by lineup group");
  click(w, d.querySelector("#finder-run"));
  const found = d.querySelector("#finder-results").textContent;
  assert.match(found, /Suggested trades|No trades found/);
  const open1 = d.querySelector("#finder-results button.primary");
  if (open1) {
    click(w, open1);
    assert.ok(!d.querySelector("#tab-trade").classList.contains("hidden"), "finder proposal opens in the calculator");
    assert.match(d.querySelector("#trade-result").textContent, /Effect on each lineup/);
  }

  // team-level adjustment
  click(w, d.querySelector('.tabs button[data-tab="values"]'));
  const valuesBefore = d.querySelector("#values-table tbody tr td:nth-child(8)").textContent;
  d.querySelector("#adj-team").value = data.players[rb[0]].team;
  d.querySelector("#adj-pct").value = "20";
  click(w, d.querySelector("#adj-add"));
  assert.ok(d.querySelector("#adj-list").textContent.includes("+20%"));
  assert.ok(d.querySelector("#matchup-note").textContent.includes("Favorable ≥ 67"));
  click(w, d.querySelector("#adj-reset"));
  assert.strictEqual(d.querySelector("#adj-list").textContent, "");

  // values tab: filter + sort
  click(w, [...d.querySelectorAll("#pos-filter button")].find((b) => b.textContent === "DEF"));
  assert.strictEqual(d.querySelectorAll("#values-table tbody tr").length, 32);
  click(w, [...d.querySelectorAll("#pos-filter button")].find((b) => b.textContent === "TE"));
  const tePos = [...d.querySelectorAll("#values-table tbody tr")].map((r) => r.children[2].textContent); // Pos column now shows a rank badge, e.g. "TE5"
  assert.ok(tePos.length > 10 && tePos.every((p) => p.startsWith("TE")));

  // rank/tier badges and the player-card modal
  const posBadge = d.querySelector("#values-table tbody tr td:nth-child(3) .tag");
  assert.match(posBadge.textContent, /^TE\d+$/);
  const nameBtn = d.querySelector("#values-table tbody tr button.link");
  click(w, nameBtn);
  const modal = d.querySelector("#player-modal");
  assert.ok(!modal.classList.contains("hidden"), "player card opens");
  assert.match(modal.textContent, /Proj PPG/);
  assert.match(modal.textContent, /Tier/);
  click(w, d.querySelector("#player-modal-close"));
  assert.ok(modal.classList.contains("hidden"), "player card closes");

  assert.deepStrictEqual(errors, [], "no script errors: " + errors.join("; "));

  // ---- Sleeper unreachable (e.g. CORS): clear message, page still usable
  ({ w, d, errors } = await open("offline"));
  d.querySelector("#username").value = "diego";
  click(w, d.querySelector("#connect-btn"));
  await tick(60);
  const st = d.querySelector("#status");
  assert.match(st.textContent, /could not reach Sleeper/);
  assert.ok(st.classList.contains("error"));
  assert.ok(d.querySelectorAll("#values-table tbody tr").length > 100);
  assert.deepStrictEqual(errors, []);

  console.log("smoke.test.js: all checks passed");
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
