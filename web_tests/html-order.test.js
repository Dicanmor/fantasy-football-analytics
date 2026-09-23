// Static check: every element app.js's init() looks up synchronously (before any event fires) must appear
// BEFORE the first blocking <script src> tag in the raw HTML. A blocking <script> (no defer/async) pauses HTML
// parsing until it downloads and runs, so any element placed after it does not exist yet when that script runs
// — document.querySelector returns null and the whole init() throws, breaking every button on the page. jsdom's
// full-file parse does not reproduce this real-browser behavior, so this needs a plain text-order check instead
// of a DOM test. If this fails, move the missing element above the <script> tags in site/index.html.
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(path.join(__dirname, "..", "site", "index.html"), "utf8");
const firstScript = html.search(/<script\s+src=/);
assert.ok(firstScript > 0, "expected at least one blocking <script src> tag");

// IDs referenced by $("#...") anywhere in app.js's module-scope code or inside init() before any listener fires.
const app = fs.readFileSync(path.join(__dirname, "..", "site", "js", "app.js"), "utf8");
const idsUsedAtInit = new Set();
for (const m of app.matchAll(/\$\("#([\w-]+)"\)/g)) idsUsedAtInit.add(m[1]);

const missing = [];
for (const id of idsUsedAtInit) {
  const pos = html.indexOf(`id="${id}"`);
  if (pos === -1) { missing.push(`${id} (not found in index.html at all)`); continue; }
  if (pos > firstScript) missing.push(`${id} (appears at byte ${pos}, after the first <script src> at byte ${firstScript})`);
}
assert.deepStrictEqual(missing, [], "elements must be defined before the blocking <script> tags:\n" + missing.join("\n"));
console.log("html-order.test.js: all checks passed");
