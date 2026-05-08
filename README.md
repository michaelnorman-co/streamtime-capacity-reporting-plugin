# StreamTime Capacity Reporting

Replaces the manual weekly count of available days per person in the studio capacity tracker. Pulls scheduled time from the StreamTime public API, computes available days per person and per department for each month in the spreadsheet, and writes the results into the existing Google Sheet — leaving Jess's notes/comments untouched.

## How "available" is defined

For each working day in the month (per the user's contracted weekday hours in StreamTime):

- Sum the user's scheduled time entries for that day.
- A day counts as **available** if non-pencil scheduled time is ≤ 3.75 hours (half of a 7.5-hour day).
- If a pencil booking exists on an otherwise-available day, the day still counts as available, but is also tagged in the `WHO` breakdown as `(1✏)`.
- Distribution hint (`block` / `mixed` / `scattered`) is added when a person has multiple available days, so Jess can see at a glance whether the time comes in a chunk or is broken up.

A "pencil" is detected by matching the linked job name against `PENCIL_JOB_NAME_HINTS` in `src/config.py` (default: `pencil`, `creative admin`, `personal`). StreamTime doesn't expose a native pencil flag through the API, so this list will need tuning once we see real data.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env — paste your StreamTime token + sheet ID
```

### Google Sheets credentials

1. Create a Google Cloud project (or reuse one).
2. Enable **Google Sheets API**.
3. Create a service account, generate a JSON key, save it as `service-account.json` in this directory (already in `.gitignore`).
4. **Share the target sheet with the service account's email** (Editor access).

## Run

```bash
# Dry run — prints summary, doesn't touch the sheet:
python -m src.main --dry-run --months 3

# Write to the sheet (current month + next 2):
python -m src.main --months 3

# Specific start:
python -m src.main --start 2026-05 --months 3

# Cache raw API responses for debugging:
python -m src.main --dry-run --cache-dir ./debug
```

## Departments

The sheet has these dept rows (the script writes to all of them; `LEADS` is left alone):

```
DESIGN, COPY, STRATEGY, DIGITAL, 3D / CT / MOTION, ARTWORK
```

Department mapping is rule-based — see `DEPARTMENT_RULES` in `src/config.py`. Each StreamTime user is matched against their role/jobTitle/labels; first matching substring wins. Use `PERSON_DEPARTMENT_OVERRIDES` to pin a specific person (by email or displayName) to a department.

## Automated weekly run (GitHub Actions)

The repo runs itself every **Monday at 08:00 UK time** via `.github/workflows/weekly-capacity.yml`. Jess does nothing — opens the sheet at the finance meeting and the data's fresh.

### Required GitHub config

Set these in the repo's **Settings → Secrets and variables → Actions**:

| Type     | Name                          | Value                                                              |
| -------- | ----------------------------- | ------------------------------------------------------------------ |
| Secret   | `STREAMTIME_API_TOKEN`        | The token from Settings → Integrations → API                       |
| Secret   | `GOOGLE_SERVICE_ACCOUNT_JSON` | The full JSON contents of the service-account file (single line OK)|
| Variable | `GOOGLE_SHEET_ID`             | `1r43tKhSVuQQI3ZKps0gEx_ro_Ibdc2Px7Kq8Totlssc` (or new sheet)      |
| Variable | `GOOGLE_SHEET_TAB`            | `FY26 Capacity`                                                    |

### Manual trigger

Anyone with repo access can run it on demand: **Actions → Weekly capacity refresh → Run workflow** — optionally override the start month or `months` parameter.

## Files

- `src/streamtime_client.py` — minimal v2 API client (users + logged-time search)
- `src/capacity.py` — per-day availability calculator, pencil detection, distribution hint
- `src/sheets_writer.py` — finds month blocks in the sheet, writes `DAYS` + `WHO`
- `src/config.py` — env config + department rules
- `src/main.py` — CLI entry point
