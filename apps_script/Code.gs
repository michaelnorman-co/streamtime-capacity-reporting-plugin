/**
 * Live capacity — pulls scheduled time from Streamtime and writes a rolling
 * current-month-plus-two view into this Google Sheet. Runs entirely inside the
 * sheet (no GitHub Actions, no service account).
 *
 * Setup (one time) — see README "Google Sheet setup":
 *   1. Sheet → Extensions → Apps Script. Replace Code.gs with this file, and
 *      appsscript.json with the one next to it (Project Settings → "Show
 *      appsscript.json manifest file in editor").
 *   2. Project Settings → Script properties → add STREAMTIME_API_TOKEN.
 *   3. Reload the sheet → Live Capacity → Refresh now (authorise when asked).
 *   4. Live Capacity → Turn on daily auto-refresh.
 *
 * The capacity rules are a straight port of src/capacity.py + src/config.py;
 * apps_script/test/parity.test.js checks the two agree on real cached data.
 */

// ---- Settings ---------------------------------------------------------------

const DEFAULT_TAB = "Live Capacity";
const DEFAULT_MONTHS = 3; // current month + 2 ahead
// Extra months fetched past the last shown month, only to find future
// START DATE / END DATE markers (a START DATE in January hides December).
const LOOKAHEAD_MONTHS = 3;
const AUTO_REFRESH_HOUR = 7; // daily, in the script's time zone (Europe/London)

const STREAMTIME_V2 = "https://api.streamtime.net/v2";
const STREAMTIME_V1 = "https://api.streamtime.net/v1";

// Tiered availability thresholds (per Jess's rules):
//   booked <= FULL_DAY_MAX_BOOKED_HOURS: 1.0 day available
//   booked <= HALF_DAY_MAX_BOOKED_HOURS: 0.5 day available
//   booked >  HALF_DAY_MAX_BOOKED_HOURS: 0
const FULL_DAY_MAX_BOOKED_HOURS = 2.0;
const HALF_DAY_MAX_BOOKED_HOURS = 4.0;

// ---- Department mapping (mirrors src/config.py) -----------------------------

// Departments rendered in the sheet, in order (LEADS is maintained by hand).
const DEPARTMENT_ROWS = [
  "DESIGN",
  "COPY",
  "STRATEGY",
  "DIGITAL",
  "3D / CT / MOTION",
  "ARTWORK",
];
const ALL_BLOCK_ROWS = ["LEADS"].concat(DEPARTMENT_ROWS);

// Users with any of these Streamtime labels are excluded entirely.
const EXCLUDE_LABELS = [];

// Lead-level roles (case-insensitive substring) are skipped — LEADS is hand-written.
const LEAD_ROLE_SUBSTRINGS = [
  "agency principle",
  "executive creative director",
  "associate creative director",
  "creative director",
  "design director",
  "strategy director",
  "copy director",
  "digital design director",
  "head of",
  "business director",
  "director of",
];

// Streamtime user label → department, first match wins (case-insensitive exact).
const LABEL_TO_DEPARTMENT = [
  ["3D Team", "3D / CT / MOTION"],
  ["3D", "3D / CT / MOTION"],
  ["Artwork Team", "ARTWORK"],
  ["Digital Team", "DIGITAL"],
  ["Strategy Team", "STRATEGY"],
  ["Copy Team", "COPY"],
  ["Design Team", "DESIGN"],
];

// Fallback when a user has no team label: role/job-title substring → department.
const ROLE_SUBSTRING_RULES = [
  ["creative technologist", "3D / CT / MOTION"],
  ["3d", "3D / CT / MOTION"],
  ["motion", "3D / CT / MOTION"],
  ["artwork", "ARTWORK"],
  ["digital", "DIGITAL"],
  ["strategist", "STRATEGY"],
  ["strategy", "STRATEGY"],
  ["copywriter", "COPY"],
  ["copy", "COPY"],
  ["designer", "DESIGN"],
];

// Per-person overrides: email or displayName (case-insensitive) → department.
const PERSON_DEPARTMENT_OVERRIDES = {
  // "someone@raggededge.com": "3D / CT / MOTION",
};

// Job names that mark a booking as a pencil (scheduleNotes ✏/PENCIL is the main rule).
const PENCIL_JOB_NAME_HINTS = ["pencil", "personal"];

// Time against these jobs doesn't count as booked at all (holidays, leave).
const NON_BOOKING_JOB_NAME_HINTS = [
  "bank holiday",
  "public holiday",
  "annual leave",
  "holiday",
  "sick",
  "tomr",
];

const AVAILABILITY_KEY =
  "Key — block: all free days back-to-back · mixed: some free days back-to-back, some apart · " +
  "scattered: no two free days back-to-back · ✏ pencilled (counted as free) · half-days count as 0.5";

// ---- Menu & triggers --------------------------------------------------------

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("Live Capacity")
    .addItem("Refresh now", "refreshCapacity")
    .addSeparator()
    .addItem("Turn on daily auto-refresh", "enableAutoRefresh")
    .addItem("Turn off auto-refresh", "disableAutoRefresh")
    .addToUi();
}

/** Menu action: refresh and tell the person what happened. */
function refreshCapacity() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  ss.toast("Pulling from Streamtime — this takes about a minute.", "Capacity", 10);
  const result = runRefresh_();
  if (result.ok) {
    ss.toast(result.message, "Capacity refreshed", 8);
  } else {
    SpreadsheetApp.getUi().alert("Capacity refresh failed\n\n" + result.message);
  }
}

/** Trigger entry point. Never throws, so Google doesn't send failure emails —
 *  a failure shows in the status line at the top of the tab instead. */
function refreshFromTrigger() {
  runRefresh_();
}

function enableAutoRefresh() {
  removeRefreshTriggers_();
  ScriptApp.newTrigger("refreshFromTrigger")
    .timeBased()
    .everyDays(1)
    .atHour(AUTO_REFRESH_HOUR)
    .create();
  SpreadsheetApp.getUi().alert(
    "Auto-refresh is on: the capacity tab updates every morning around " +
      AUTO_REFRESH_HOUR + ":00.\n\nIt runs as you (" +
      Session.getEffectiveUser().getEmail() + ")."
  );
}

function disableAutoRefresh() {
  const n = removeRefreshTriggers_();
  SpreadsheetApp.getUi().alert(n ? "Auto-refresh is off." : "Auto-refresh wasn't on.");
}

function removeRefreshTriggers_() {
  let n = 0;
  for (const t of ScriptApp.getProjectTriggers()) {
    if (t.getHandlerFunction() === "refreshFromTrigger") {
      ScriptApp.deleteTrigger(t);
      n++;
    }
  }
  return n;
}

// ---- Refresh ----------------------------------------------------------------

/** Fetch → compute → write. Returns {ok, message}; never throws. */
function runRefresh_() {
  const lock = LockService.getDocumentLock();
  if (!lock.tryLock(5000)) {
    return { ok: false, message: "A refresh is already running — try again in a minute." };
  }
  const props = PropertiesService.getScriptProperties();
  const tabName = props.getProperty("SHEET_TAB") || DEFAULT_TAB;
  const sheet = getOrCreateSheet_(tabName);
  const tz = Session.getScriptTimeZone();
  try {
    const token = props.getProperty("STREAMTIME_API_TOKEN");
    if (!token) {
      throw new Error(
        "STREAMTIME_API_TOKEN isn't set. Extensions → Apps Script → Project Settings → Script properties."
      );
    }
    const monthCount = Number(props.getProperty("MONTHS")) || DEFAULT_MONTHS;
    const startKey = Utilities.formatDate(new Date(), tz, "yyyy-MM");
    const targets = monthRange(startKey, monthCount);
    const fetched = monthRange(startKey, monthCount + LOOKAHEAD_MONTHS);

    const client = new StreamtimeClient_(token);
    const users = client.listUsers();
    const recordsByMonth = {};
    for (const key of fetched) recordsByMonth[key] = client.loggedTimeForMonth(key);
    client.attachJobs([].concat.apply([], fetched.map((k) => recordsByMonth[k])));

    const months = buildMonths(users, recordsByMonth, targets);
    const stamp = Utilities.formatDate(new Date(), tz, "d/M/yy");
    writeCapacity_(sheet, months, stamp);

    const status = "Live from Streamtime · last refreshed " +
      Utilities.formatDate(new Date(), tz, "EEE d MMM yyyy, HH:mm");
    props.setProperty("LAST_SUCCESS", status);
    writeStatus_(sheet, status, false);
    return { ok: true, message: targets.map(monthHeader).join(", ") + " updated." };
  } catch (err) {
    const msg = (err && err.message) || String(err);
    console.error(err && err.stack ? err.stack : msg);
    const last = props.getProperty("LAST_SUCCESS");
    writeStatus_(
      sheet,
      "⚠ Refresh failed " + Utilities.formatDate(new Date(), tz, "EEE d MMM, HH:mm") + ": " + msg +
        (last ? "  —  figures below are from the last good refresh (" + last.replace(/^.*refreshed /, "") + ")." : ""),
      true
    );
    return { ok: false, message: msg };
  } finally {
    lock.releaseLock();
  }
}

/** Compute per-month, per-department availability for the target months. */
function buildMonths(users, recordsByMonth, targets) {
  const allRecords = [].concat.apply([], Object.keys(recordsByMonth).map((k) => recordsByMonth[k]));
  const windows = availabilityWindows(allRecords);
  return targets.map((key) => {
    const ym = key.split("-").map(Number);
    const people = computeAvailability(
      users, recordsByMonth[key] || [], ym[0], ym[1],
      FULL_DAY_MAX_BOOKED_HOURS, HALF_DAY_MAX_BOOKED_HOURS, windows
    );
    const byDept = {};
    for (const p of people) (byDept[p.department] = byDept[p.department] || []).push(p);
    return { key: key, byDept: byDept };
  });
}

// ---- Streamtime client ------------------------------------------------------

const SEARCH_VIEW_LOGGED_TIME = 8;
const FGT_MONTH = 220;
const FGT_TEAM_MEMBER_STATUS = 127; // 1 = Active
const COND_AND = 1;
const VMT_EQUALS = 1;
const PAGE_SIZE = 500;
const JOB_BATCH = 10;

function StreamtimeClient_(token) {
  this.headers = { Authorization: "Bearer " + token };
  this.jobCache = {};
}

/** One request with retries on network errors, 429 and 5xx. */
StreamtimeClient_.prototype.fetch_ = function (url, options) {
  const opts = Object.assign({ headers: this.headers, muteHttpExceptions: true }, options || {});
  let lastError = "";
  for (let attempt = 0; attempt < 4; attempt++) {
    if (attempt) Utilities.sleep(2000 * Math.pow(2, attempt - 1));
    let resp;
    try {
      resp = UrlFetchApp.fetch(url, opts);
    } catch (e) {
      lastError = e.message;
      continue;
    }
    const code = resp.getResponseCode();
    if (code >= 200 && code < 300) return JSON.parse(resp.getContentText());
    lastError = "Streamtime returned " + code + " for " + url.replace(/\?.*$/, "");
    if (code === 401 || code === 403) {
      throw new Error(lastError + " — the API token is missing, wrong or expired.");
    }
    if (code !== 429 && code < 500) throw new Error(lastError);
  }
  throw new Error(lastError + " (gave up after 4 tries)");
};

StreamtimeClient_.prototype.listUsers = function () {
  // v1 users include role + labels, which the department mapping needs.
  return this.fetch_(STREAMTIME_V1 + "/users");
};

/** All scheduled + logged time for a "YYYY-MM" month, active team members only. */
StreamtimeClient_.prototype.loggedTimeForMonth = function (monthKey) {
  const filterGroups = [
    { filterGroupTypeId: FGT_MONTH, conditionMatchTypeId: COND_AND,
      filters: [{ valueMatchTypeId: VMT_EQUALS, value: monthKey }] },
    { filterGroupTypeId: FGT_TEAM_MEMBER_STATUS, conditionMatchTypeId: COND_AND,
      filters: [{ valueMatchTypeId: VMT_EQUALS, value: 1 }] },
  ];
  const out = [];
  for (let offset = 0; ; offset += PAGE_SIZE) {
    const page = this.fetch_(STREAMTIME_V2 + "/search?search_view=" + SEARCH_VIEW_LOGGED_TIME, {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify({
        maxResults: PAGE_SIZE,
        offset: offset,
        filterGroupCollection: { conditionMatchTypeId: COND_AND, filterGroups: filterGroups },
      }),
    });
    if (!Array.isArray(page)) throw new Error("Unexpected Streamtime search response for " + monthKey);
    Array.prototype.push.apply(out, page);
    if (page.length < PAGE_SIZE) return out;
  }
};

/**
 * Fill in each record's `job` (search no longer returns it — `additional_data=job`
 * makes /search 500), fetching each job once, in small parallel batches.
 */
StreamtimeClient_.prototype.attachJobs = function (records) {
  const ids = [];
  const seen = {};
  for (const r of records) {
    const jid = r.loggedTime && r.loggedTime.jobId;
    if (jid && !seen[jid]) {
      seen[jid] = true;
      ids.push(jid);
    }
  }
  for (let i = 0; i < ids.length; i += JOB_BATCH) {
    const batch = ids.slice(i, i + JOB_BATCH);
    const responses = UrlFetchApp.fetchAll(batch.map((id) => ({
      url: STREAMTIME_V2 + "/jobs/" + id,
      headers: this.headers,
      muteHttpExceptions: true,
    })));
    responses.forEach((resp, j) => {
      const id = batch[j];
      const code = resp.getResponseCode();
      if (code >= 200 && code < 300) {
        this.jobCache[id] = JSON.parse(resp.getContentText());
      } else if (code === 429 || code >= 500) {
        // Retry the slow way; a job that still can't be read is left blank, as before.
        try {
          this.jobCache[id] = this.fetch_(STREAMTIME_V2 + "/jobs/" + id);
        } catch (e) {
          this.jobCache[id] = null;
        }
      } else {
        this.jobCache[id] = null;
      }
    });
    if (i + JOB_BATCH < ids.length) Utilities.sleep(500);
  }
  for (const r of records) {
    const jid = r.loggedTime && r.loggedTime.jobId;
    if (jid) r.job = this.jobCache[jid];
  }
};

// ---- Capacity rules (port of src/capacity.py) -------------------------------

// Index 0 = Monday … 6 = Sunday.
const WEEKDAY_HOUR_FIELDS = [
  "hoursWorkedMonday",
  "hoursWorkedTuesday",
  "hoursWorkedWednesday",
  "hoursWorkedThursday",
  "hoursWorkedFriday",
  "hoursWorkedSaturday",
  "hoursWorkedSunday",
];

const PENCIL_EMOJI = "✏"; // ✏ — also matches '✏️' (U+270F + U+FE0F).
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function pad2(n) {
  return (n < 10 ? "0" : "") + n;
}

/** ["2026-10", "2026-11", …] — `count` months starting at `startKey`. */
function monthRange(startKey, count) {
  const ym = startKey.split("-").map(Number);
  const out = [];
  for (let i = 0; i < count; i++) {
    const m0 = ym[1] - 1 + i;
    out.push((ym[0] + Math.floor(m0 / 12)) + "-" + pad2((m0 % 12) + 1));
  }
  return out;
}

function epochDay(iso) {
  return Math.floor(Date.parse(iso + "T00:00:00Z") / 86400000);
}

function scheduleNotes(record) {
  return ((record.loggedTime || {}).scheduleNotes) || "";
}

function jobText(record) {
  const job = record.job || {};
  return (job.name || "").toLowerCase() + " " + (job.fullName || "").toLowerCase() + " " +
    scheduleNotes(record).toLowerCase();
}

/** Pencil: scheduleNotes has ✏ or the word PENCIL (or the job is a pencil/personal job). */
function isPencilEntry(record) {
  const notes = scheduleNotes(record);
  if (notes.indexOf(PENCIL_EMOJI) !== -1 || notes.toUpperCase().indexOf("PENCIL") !== -1) return true;
  const text = jobText(record);
  return PENCIL_JOB_NAME_HINTS.some((h) => text.indexOf(h) !== -1);
}

/** Presentation: scheduleNotes starts with "p. " — the whole day counts as busy. */
function isPresentationEntry(record) {
  return scheduleNotes(record).replace(/^\s+/, "").toLowerCase().indexOf("p. ") === 0;
}

function isNonBookingEntry(record) {
  const text = jobText(record);
  return NON_BOOKING_JOB_NAME_HINTS.some((h) => text.indexOf(h) !== -1);
}

/** Python str.title(): upper-case a cased char that follows an uncased one, lower-case the rest. */
function titleCase(s) {
  let out = "";
  let prevCased = false;
  for (const ch of s) {
    const cased = ch.toLowerCase() !== ch.toUpperCase();
    out += cased ? (prevCased ? ch.toLowerCase() : ch.toUpperCase()) : ch;
    prevCased = cased;
  }
  return out;
}

/** Project name for a pencil entry: scheduleNotes minus ✏/PENCIL, else the job name. */
function pencilProjectName(record) {
  let cleaned = scheduleNotes(record).split(PENCIL_EMOJI).join("").split("️").join("");
  cleaned = cleaned.replace(/(?<![\p{L}\p{N}_])PENCIL(?![\p{L}\p{N}_])/giu, "");
  cleaned = cleaned.replace(/^[ :\-—,]+|[ :\-—,]+$/g, "").trim();
  if (cleaned) return titleCase(cleaned);
  return (record.job || {}).name || null;
}

function shortName(user) {
  return user.firstName || user.displayName || (user.email || "").split("@")[0];
}

/** Department row for a user, or null to leave them out. */
function departmentForUser(user) {
  const email = (user.email || "").toLowerCase();
  const display = (user.displayName || "").toLowerCase();
  const overrides = {};
  for (const k of Object.keys(PERSON_DEPARTMENT_OVERRIDES)) {
    overrides[k.toLowerCase()] = PERSON_DEPARTMENT_OVERRIDES[k];
  }
  if (email && overrides[email]) return overrides[email];
  if (display && overrides[display]) return overrides[display];

  const labelNames = (user.labels || [])
    .filter((l) => l && typeof l === "object")
    .map((l) => l.name || "");
  if (labelNames.some((n) => EXCLUDE_LABELS.indexOf(n) !== -1)) return null;

  const role = user.role && typeof user.role === "object" ? user.role.name : null;
  const roleText = [role, user.jobTitle].filter(Boolean).join(" ").toLowerCase();
  if (LEAD_ROLE_SUBSTRINGS.some((n) => roleText.indexOf(n) !== -1)) return null;

  const labelsLower = labelNames.map((n) => n.toLowerCase());
  for (const pair of LABEL_TO_DEPARTMENT) {
    if (labelsLower.indexOf(pair[0].toLowerCase()) !== -1) return pair[1];
  }
  for (const pair of ROLE_SUBSTRING_RULES) {
    if (roleText.indexOf(pair[0]) !== -1) return pair[1];
  }
  return null;
}

/** Per-user {start, end} from "START DATE" / "(BOOKING) END DATE" scheduleNotes markers. */
function availabilityWindows(records) {
  const windows = {};
  for (const rec of records) {
    const lt = rec.loggedTime || {};
    const d = lt.date;
    const uid = lt.userId;
    if (!uid || !d || !ISO_DATE.test(d)) continue;
    const notes = (lt.scheduleNotes || "").toUpperCase();
    const w = windows[uid] || (windows[uid] = { start: null, end: null });
    if (notes.indexOf("START DATE") !== -1) {
      if (w.start === null || d < w.start) w.start = d;
    } else if (notes.indexOf("END DATE") !== -1) {
      if (w.end === null || d > w.end) w.end = d;
    }
  }
  return windows;
}

/** "block" (all back-to-back), "scattered" (none back-to-back), "mixed", or "" if none. */
function distribution(dates) {
  if (!dates.length) return "";
  const sorted = dates.slice().sort();
  let maxRun = 1;
  let run = 1;
  for (let i = 1; i < sorted.length; i++) {
    run = epochDay(sorted[i]) - epochDay(sorted[i - 1]) === 1 ? run + 1 : 1;
    if (run > maxRun) maxRun = run;
  }
  if (maxRun === sorted.length) return "block";
  if (maxRun === 1) return "scattered";
  return "mixed";
}

function emptyBucket() {
  return { confirmed: 0, pencil: 0, nonBooking: 0, presentation: false, pencilProjects: [] };
}

/** One entry per active, department-mapped user for the given month. */
function computeAvailability(users, records, year, month, fullDayMaxHours, halfDayMaxHours, windows) {
  const fullDayMaxMin = fullDayMaxHours * 60;
  const halfDayMaxMin = halfDayMaxHours * 60;
  windows = windows || {};
  const prefix = year + "-" + pad2(month) + "-";

  const byUser = {};
  for (const rec of records) {
    const lt = rec.loggedTime || {};
    const uid = lt.userId;
    const d = lt.date;
    if (!uid || !d || !ISO_DATE.test(d) || d.indexOf(prefix) !== 0) continue;
    const perDay = byUser[uid] || (byUser[uid] = {});
    const bucket = perDay[d] || (perDay[d] = emptyBucket());
    const minutes = lt.minutes || 0;
    if (isNonBookingEntry(rec)) {
      bucket.nonBooking += minutes;
    } else if (isPresentationEntry(rec)) {
      bucket.presentation = true;
      bucket.confirmed += minutes;
    } else if (isPencilEntry(rec)) {
      bucket.pencil += minutes;
      const proj = pencilProjectName(rec);
      if (proj) bucket.pencilProjects.push(proj);
    } else {
      bucket.confirmed += minutes;
    }
  }

  const lastDay = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const results = [];
  for (const user of users) {
    if ((user.userStatus || {}).name !== "Active") continue;
    const dept = departmentForUser(user);
    if (!dept) continue;
    const perDay = byUser[user.id] || {};
    const w = windows[user.id] || { start: null, end: null };
    const person = {
      userId: user.id,
      name: shortName(user),
      department: dept,
      availableDays: 0,
      pencilDays: 0,
      availableDates: [],
      pencilProjects: [],
    };
    for (let day = 1; day <= lastDay; day++) {
      const weekday = (new Date(Date.UTC(year, month - 1, day)).getUTCDay() + 6) % 7;
      if (!((user[WEEKDAY_HOUR_FIELDS[weekday]] || 0) > 0)) continue; // not a working day
      const iso = prefix + pad2(day);
      if (w.start && iso < w.start) continue;
      if (w.end && iso > w.end) continue;
      const b = perDay[iso] || emptyBucket();
      // Bank holiday / leave with no real work → neither free nor booked.
      if (b.nonBooking >= halfDayMaxMin && b.confirmed === 0) continue;
      if (b.presentation) continue;
      const fraction = b.confirmed <= fullDayMaxMin ? 1 : b.confirmed <= halfDayMaxMin ? 0.5 : 0;
      if (fraction > 0) {
        person.availableDays += fraction;
        person.availableDates.push(iso);
        if (b.pencil > 0) {
          person.pencilDays += fraction;
          Array.prototype.push.apply(person.pencilProjects, b.pencilProjects);
        }
      }
    }
    person.distribution = distribution(person.availableDates);
    results.push(person);
  }
  return results;
}

// ---- Rendering (matches src/sheets_writer.py) -------------------------------

const MONTH_NAMES = ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
  "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"];

function monthHeader(key) {
  const ym = key.split("-").map(Number);
  return MONTH_NAMES[ym[1] - 1] + " " + ym[0];
}

function fmtDays(d) {
  return String(Math.round(d * 10) / 10);
}

/** WHO cell: one line per person with free time, e.g. "Dom - 6 (2✏ Nike) [mixed]". */
function formatWho(people) {
  return people
    .filter((p) => p.availableDays !== 0)
    .sort((a, b) => {
      const x = a.name.toLowerCase();
      const y = b.name.toLowerCase();
      return x < y ? -1 : x > y ? 1 : 0;
    })
    .map((p) => {
      let suffix = "";
      if (p.pencilDays) {
        const projects = p.pencilProjects.filter((v, i, arr) => arr.indexOf(v) === i);
        suffix = " (" + fmtDays(p.pencilDays) + "✏" + (projects.length ? " " + projects.join(", ") : "") + ")";
      }
      if (p.distribution && p.availableDays > 1) suffix += " [" + p.distribution + "]";
      return p.name + " - " + fmtDays(p.availableDays) + suffix;
    })
    .join("\n");
}

// ---- Sheet output -----------------------------------------------------------

// Columns (1-indexed) — same as the original tracker: B label, C DAYS, D NOTES, F WHO.
const COL_LABEL = 2;
const COL_DAYS = 3;
const COL_NOTES = 4;
const COL_WHO = 6;
const WIDTH = 6; // A..F

const STATUS_ROW = 2;
const FIRST_BLOCK_ROW = 6;
// Month header + sub-header + spacer + LEADS + 6 departments, then 2 blank rows.
const ROWS_PER_BLOCK = 10;
const BLOCK_GAP = 2;

const GREY = "#efefef";
const CYAN = "#d9f2f5";

function getOrCreateSheet_(name) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  return ss.getSheetByName(name) || ss.insertSheet(name, 0);
}

function writeStatus_(sheet, text, isError) {
  sheet.getRange(STATUS_ROW, COL_LABEL)
    .setValue(text)
    .setFontColor(isError ? "#b00020" : "#666666")
    .setFontWeight(isError ? "bold" : "normal")
    .setBackground(isError ? "#fde7e9" : null);
}

/**
 * Hand-typed cells to carry across a refresh: NOTES on every row, plus the
 * whole LEADS row. Keyed "OCTOBER 2026|DESIGN" so they follow their month.
 */
function readHandEdits_(sheet) {
  const edits = {};
  const last = sheet.getLastRow();
  if (last < 1) return edits;
  const values = sheet.getRange(1, 1, last, WIDTH).getValues();
  let month = null;
  for (const row of values) {
    const label = String(row[COL_LABEL - 1]).trim().toUpperCase();
    if (/^[A-Z]+ \d{4}$/.test(label) && MONTH_NAMES.indexOf(label.split(" ")[0]) !== -1) {
      month = label;
    } else if (month && ALL_BLOCK_ROWS.indexOf(label) !== -1) {
      const e = { notes: row[COL_NOTES - 1] };
      if (label === "LEADS") {
        e.days = row[COL_DAYS - 1];
        e.who = row[COL_WHO - 1];
      }
      edits[month + "|" + label] = e;
    }
  }
  return edits;
}

/** Rewrite the tab: title, status, key, then one block per month. */
function writeCapacity_(sheet, months, stamp) {
  const edits = readHandEdits_(sheet);
  const rows = [];
  const blank = () => new Array(WIDTH).fill("");
  const put = (r, col, v) => { rows[r][col - 1] = v; };

  for (let i = 0; i < FIRST_BLOCK_ROW - 1; i++) rows.push(blank());
  put(0, COL_LABEL, "LIVE CAPACITY — current month + " + (months.length - 1) + " ahead");
  put(STATUS_ROW - 1, COL_LABEL, sheet.getRange(STATUS_ROW, COL_LABEL).getValue());
  put(2, COL_LABEL, AVAILABILITY_KEY);

  const blockStarts = [];
  for (const m of months) {
    const header = monthHeader(m.key);
    const start = rows.length;
    blockStarts.push(start + 1);
    for (let i = 0; i < ROWS_PER_BLOCK; i++) rows.push(blank());
    put(start, COL_LABEL, header);
    put(start + 1, COL_LABEL, stamp);
    put(start + 1, COL_DAYS, "DAYS");
    put(start + 1, COL_NOTES, "NOTES");
    put(start + 1, COL_WHO, "WHO");
    ALL_BLOCK_ROWS.forEach((label, i) => {
      const r = start + 3 + i;
      const kept = edits[header + "|" + label] || {};
      put(r, COL_LABEL, label);
      put(r, COL_NOTES, kept.notes || "");
      if (label === "LEADS") {
        // Hand-maintained: carry over whatever was typed.
        put(r, COL_DAYS, kept.days === undefined ? "" : kept.days);
        put(r, COL_WHO, kept.who || "");
        return;
      }
      const people = m.byDept[label] || [];
      put(r, COL_DAYS, Math.round(people.reduce((s, p) => s + p.availableDays, 0) * 10) / 10);
      put(r, COL_WHO, formatWho(people));
    });
    for (let i = 0; i < BLOCK_GAP; i++) rows.push(blank());
  }

  sheet.clear();
  sheet.getRange(1, COL_LABEL, rows.length, 1).setNumberFormat("@"); // keep "29/10/26" as text
  sheet.getRange(1, 1, rows.length, WIDTH).setValues(rows).setVerticalAlignment("middle");
  formatSheet_(sheet, blockStarts);
}

function formatSheet_(sheet, blockStarts) {
  [24, 170, 70, 320, 16, 480].forEach((w, i) => sheet.setColumnWidth(i + 1, w));
  sheet.getRange(1, COL_LABEL).setFontWeight("bold").setFontSize(14);
  sheet.getRange(3, COL_LABEL).setFontColor("#666666").setFontStyle("italic");

  for (const start of blockStarts) {
    sheet.getRange(start, COL_LABEL).setFontWeight("bold").setFontSize(12);
    const sub = sheet.getRange(start + 1, COL_LABEL, 1, WIDTH - 1);
    sub.setBackground(GREY).setFontWeight("bold").setHorizontalAlignment("center");
    sheet.getRange(start + 1, COL_LABEL).setFontStyle("italic");
    const table = sheet.getRange(start + 1, COL_LABEL, ROWS_PER_BLOCK - 1, WIDTH - 1);
    table.setBorder(true, true, true, true, true, true);
    const body = start + 3;
    const n = ALL_BLOCK_ROWS.length;
    sheet.getRange(body, COL_LABEL, n, 1).setFontWeight("bold");
    sheet.getRange(body, COL_DAYS, n, 1)
      .setBackground(CYAN).setFontWeight("bold").setHorizontalAlignment("center");
    sheet.getRange(body, COL_NOTES, n, 1).setWrap(true);
    sheet.getRange(body, COL_WHO, n, 1).setWrap(true);
  }
}

// Node test hook (apps_script/test/parity.test.js); `module` doesn't exist in Apps Script.
if (typeof module !== "undefined") {
  module.exports = {
    availabilityWindows, buildMonths, computeAvailability, distribution, formatWho,
    monthHeader, monthRange, pencilProjectName, titleCase,
  };
}
