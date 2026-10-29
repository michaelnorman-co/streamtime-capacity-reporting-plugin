# Streamtime Capacity Reporting

A **Live Capacity** tab in the capacity Google Sheet showing free days per person and department for the **current month plus the next two**, pulled from Streamtime. It's laid out like the original tracker (one block per month: DAYS, NOTES, WHO), so it can be pasted straight into the finance report.

It runs entirely inside the Google Sheet as Apps Script (`apps_script/Code.gs`): it refreshes itself every morning, and anyone can use **Live Capacity → Refresh now**. It doesn't use GitHub Actions, a service account or a GitHub token, and nobody gets failure emails. If a refresh fails, the previous figures stay in place and a red status line at the top of the tab says what went wrong.

> The earlier versions — the Monday GitHub Action here, and the Capacity Radar in `re-context-layer` (2-week horizon) — are retired for this use. The Python in `src/` is kept as the reference model and a local dry-run tool.

## How "available" is defined

For each contracted working day (from the person's weekday hours in Streamtime):

- Confirmed booked time **≤ 2h → 1 day free**, **≤ 4h → ½ day free**, more → 0.
- **Pencils** (scheduleNotes containing ✏ or the word `PENCIL`) don't count as booked. The day still counts as free and is flagged in WHO, e.g. `Dom - 6 (2✏ Nike)`.
- **Presentation days** (scheduleNotes starting `p. `) are fully busy, however many hours are booked.
- **Holidays / leave** (job names in `NON_BOOKING_JOB_NAME_HINTS`) are neither free nor booked.
- `START DATE` / `END DATE` markers in scheduleNotes limit someone to their active window.
- Lead roles are skipped: the **LEADS** row is filled in by hand.

### Block / mixed / scattered

Shown after anyone with more than one free day, and explained in the key at the top of the tab:

| Label         | Meaning                                    | Example                        |
| ------------- | ------------------------------------------ | ------------------------------ |
| **block**     | every free day is back-to-back             | a clear 2-week run             |
| **mixed**     | some free days back-to-back, some apart    | Mon–Tue free, then Thu         |
| **scattered** | no two free days are back-to-back          | odd single or half days        |

Half days count as 0.5 in DAYS, but the label only looks at *which* days are free.

## Google Sheet setup (one time)

1. Open the capacity sheet → **Extensions → Apps Script**.
2. Replace the contents of `Code.gs` with [`apps_script/Code.gs`](apps_script/Code.gs).
3. **Project Settings (⚙️)** → tick *Show "appsscript.json" manifest file in editor*. Then replace that file with [`apps_script/appsscript.json`](apps_script/appsscript.json). This sets the time zone to Europe/London.
4. **Project Settings → Script properties → Add script property**:
   - `STREAMTIME_API_TOKEN`: Streamtime → Settings → Integrations → API
   - optional `SHEET_TAB`: tab to write to (default `Live Capacity`, created if missing)
   - optional `MONTHS`: how many months to show (default `3`)
5. Reload the sheet. Run **Live Capacity → Refresh now** and approve the permissions prompt (tick **Select all**). If the prompt errors, run it from the Apps Script editor instead: pick `refreshFromTrigger` in the function dropdown, then click **▷ Run**.
6. Run **Live Capacity → Turn on daily auto-refresh** (about 7am daily). The trigger runs as whoever turns it on, so that should be someone who'll keep their account.

### What a refresh does

1. Fetches users and scheduled/logged time for the shown months, plus 3 months beyond them (only to find future START/END DATE markers). It retries Streamtime on rate limits and server errors.
2. Only once every fetch has succeeded, rewrites the tab. Anything typed by hand carries over to the same month: the **NOTES** column and the whole **LEADS** row.
3. Months drop off as time moves on. There is no history: the tab always shows what's in Streamtime now.

## Changing the rules

Department mapping, pencil/holiday hints and thresholds live at the top of `apps_script/Code.gs`, mirroring `src/config.py`. If you change a rule, change it in both places, then check they still agree:

```bash
node --test apps_script/test/parity.test.js
```

The test runs the Python model and the Apps Script over the same data and compares every person and every WHO cell. By default it uses the anonymised fixtures in `apps_script/test/fixtures/`. To use real cached data instead (local only, as `debug/` is gitignored):

```bash
.venv/bin/python -m src.main --dry-run --cache-dir debug
```

```bash
CAPACITY_DATA=debug node --test apps_script/test/parity.test.js
```

## Python reference tool

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python -m src.main --dry-run --months 3
```

It needs `STREAMTIME_API_TOKEN` in `.env` (see `.env.example`). Writing to the sheet from Python (without `--dry-run`) still works through a service account, but isn't needed any more.

## Files

- `apps_script/Code.gs`: the live tool: Streamtime client, capacity rules, sheet output, menu and trigger
- `apps_script/appsscript.json`: Apps Script manifest (time zone, permissions)
- `apps_script/test/`: parity test against the Python model, plus anonymised fixtures
- `src/`: Python reference model (`capacity.py`, `config.py`) and CLI (`main.py`)
