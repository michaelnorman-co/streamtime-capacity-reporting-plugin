// Parity check: the Apps Script port must match the Python model exactly.
//
//   node --test apps_script/test/parity.test.js
//   CAPACITY_DATA=debug node --test apps_script/test/parity.test.js   # real cached data (local only)
//
// Runs the Python model (make_expected.py) and Code.gs over the same users.json +
// logged_*.json and compares every person and every WHO cell. Defaults to the
// anonymised fixtures in ./fixtures.
const { test } = require("node:test");
const assert = require("node:assert");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const DATA = path.resolve(ROOT, process.env.CAPACITY_DATA || "apps_script/test/fixtures");
const PYTHON = process.env.PYTHON || path.join(ROOT, ".venv/bin/python");

// Load Code.gs the way Apps Script does: as a plain script with a shared global scope.
const sandbox = { module: { exports: {} } };
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../Code.gs"), "utf8"), sandbox);
const gs = sandbox.module.exports;

const expected = JSON.parse(
  execFileSync(PYTHON, [path.join(__dirname, "make_expected.py"), DATA], {
    maxBuffer: 1 << 28,
  }).toString()
);
const load = (f) => JSON.parse(fs.readFileSync(path.join(DATA, f), "utf8"));
const users = load("users.json");
const months = Object.keys(expected).sort();
const recordsByMonth = Object.fromEntries(months.map((m) => [m, load(`logged_${m}.json`)]));
const built = gs.buildMonths(users, recordsByMonth, months);

for (const [i, month] of months.entries()) {
  test(`${month}: per-person availability matches Python`, () => {
    const got = Object.values(built[i].byDept).flat().map((p) => ({
      userId: p.userId,
      name: p.name,
      department: p.department,
      availableDays: p.availableDays,
      pencilDays: p.pencilDays,
      availableDates: p.availableDates,
      distribution: p.distribution,
      pencilProjects: p.pencilProjects,
    }));
    const byId = (a, b) => a.userId - b.userId;
    assert.deepStrictEqual(
      JSON.parse(JSON.stringify(got.sort(byId))),
      expected[month].people.slice().sort(byId)
    );
  });

  test(`${month}: WHO cells match Python`, () => {
    for (const [dept, who] of Object.entries(expected[month].who)) {
      assert.strictEqual(gs.formatWho(built[i].byDept[dept] || []), who, dept);
    }
  });
}

test("monthRange rolls over the year", () => {
  assert.deepStrictEqual(Array.from(gs.monthRange("2026-11", 3)), ["2026-11", "2026-12", "2027-01"]);
});

test("titleCase mirrors Python str.title()", () => {
  const cases = ["nike pitch", "it's 2nd round", "CAFÉ brand", "o'neill-smith"];
  const py = JSON.parse(
    execFileSync(PYTHON, ["-c", `import json,sys; print(json.dumps([s.title() for s in ${JSON.stringify(cases)}]))`]).toString()
  );
  assert.deepStrictEqual(cases.map(gs.titleCase), py);
});
