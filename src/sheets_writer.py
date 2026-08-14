"""Write monthly capacity blocks to the Google Sheet.

Each month block looks like:

    APRIL 2026                                     ← month header (col B), year is required
    27/4/26   DAYS    NOTES        WHO             ← sub-header (cols B,C,D,F)
    LEADS     0       ...          ...
    DESIGN    22.0                 Lucy - 2 / ...
    COPY      2                    Eloise
    STRATEGY  8                    Christy
    DIGITAL   0                    ...
    3D / CT / MOTION  7            Michael
    ARTWORK   0                    ...

Two modes:
  - `find_month_blocks`: scan col B for existing headers and write into them
  - `create_month_block`: stamp out a fresh block at a given start row

`write_capacity` handles both: each target month either updates an existing block
or appends a new block at the bottom of the sheet.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from google.oauth2 import service_account
from googleapiclient.discovery import build

from .capacity import PersonAvailability
from .config import DEPARTMENT_ROWS

MONTH_NAMES = {
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "MAY": 5, "JUNE": 6,
    "JULY": 7, "AUGUST": 8, "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12,
}
NUM_TO_MONTH = {v: k for k, v in MONTH_NAMES.items()}

# Column layout (matches the original Capacity tracker template).
COL_LABEL = "B"
COL_DAYS = "C"
COL_NOTES = "D"
COL_WHO = "F"
COL_DATE_HEADER = "B"

# Block layout: 1 header row (month name) + 1 sub-header (date|DAYS|NOTES|WHO) +
# 1 blank spacer + 1 LEADS + 6 dept rows = 10 rows.
# We add 2 trailing blank rows for visual spacing between blocks.
ROWS_PER_BLOCK = 10
BLOCK_GAP = 2

# Order of dept rows in a block, including LEADS (which we leave blank — Jess writes notes by hand).
ALL_BLOCK_ROWS = ["LEADS"] + DEPARTMENT_ROWS

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


@dataclass
class MonthBlock:
    name: str
    month: int
    year: int | None                 # None when the header carries no year, e.g. bare "SEPTEMBER"
    header_row: int                  # 1-indexed
    department_rows: dict[str, int]  # dept label → row


def parse_month_header(val: str) -> tuple[int, int | None] | None:
    """'AUGUST 2025' → (8, 2025); 'SEPTEMBER' → (9, None); anything else → None."""
    parts = [p.strip(",") for p in val.split()]
    if not parts:
        return None
    month = MONTH_NAMES.get(parts[0])
    if month is None:
        return None
    year = None
    if len(parts) > 1:
        token = parts[1].strip(",")
        if token.isdigit() and len(token) == 4:
            year = int(token)
    return month, year


def _service(service_account_path: str):
    creds = service_account.Credentials.from_service_account_file(
        service_account_path, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _read_column_b(svc, sheet_id: str, tab: str) -> list[str]:
    """Return all values in column B (1-indexed by row), as upper-cased strings."""
    resp = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"'{tab}'!B1:B1000")
        .execute()
    )
    rows = resp.get("values", [])
    return [(r[0] if r else "").strip().upper() for r in rows]


def find_month_blocks(svc, sheet_id: str, tab: str) -> list[MonthBlock]:
    col_b = _read_column_b(svc, sheet_id, tab)
    blocks: list[MonthBlock] = []
    for idx, val in enumerate(col_b):
        parsed = parse_month_header(val)
        if parsed is not None:
            month_num, year = parsed
            dept_rows: dict[str, int] = {}
            for j in range(idx + 1, min(idx + 13, len(col_b))):
                if parse_month_header(col_b[j]) is not None:
                    break
                if col_b[j] in DEPARTMENT_ROWS:
                    dept_rows[col_b[j]] = j + 1
            blocks.append(
                MonthBlock(val.split()[0], month_num, year, idx + 1, dept_rows)
            )
    return blocks


def _last_used_row(svc, sheet_id: str, tab: str) -> int:
    col_b = _read_column_b(svc, sheet_id, tab)
    last = 0
    for i, v in enumerate(col_b, 1):
        if v:
            last = i
    return last


def _fmt_days(d: float) -> str:
    """Render 1.0 -> '1', 0.5 -> '0.5', 4.5 -> '4.5'."""
    return f"{d:.1f}".rstrip("0").rstrip(".") or "0"


def format_who_breakdown(people: list[PersonAvailability]) -> str:
    lines = []
    for p in sorted(people, key=lambda x: x.name.lower()):
        if p.available_days == 0:
            continue
        suffix = ""
        if p.pencil_days:
            # Dedupe project names while preserving order.
            seen: dict[str, None] = {}
            for proj in p.pencil_projects:
                seen.setdefault(proj, None)
            project_str = ", ".join(seen) if seen else ""
            inner = f"{_fmt_days(p.pencil_days)}✏"
            if project_str:
                inner += f" {project_str}"
            suffix = f" ({inner})"
        if p.distribution and p.available_days > 1:
            suffix += f" [{p.distribution}]"
        lines.append(f"{p.name} - {_fmt_days(p.available_days)}{suffix}")
    return "\n".join(lines)


def _build_block_updates(
    tab: str,
    block_start_row: int,
    header_label: str,
    snapshot_label: str,
    people_by_dept: dict[str, list[PersonAvailability]],
    write_header: bool,
) -> list[dict]:
    """Build a list of value updates for one full block."""
    updates: list[dict] = []
    header_row = block_start_row
    sub_header_row = block_start_row + 1

    if write_header:
        updates.append(
            {"range": f"'{tab}'!{COL_LABEL}{header_row}", "values": [[header_label]]}
        )
        # Sub-header: snapshot date, DAYS, NOTES, WHO (E is left blank).
        updates.append(
            {"range": f"'{tab}'!{COL_LABEL}{sub_header_row}", "values": [[snapshot_label]]}
        )
        updates.append(
            {"range": f"'{tab}'!{COL_DAYS}{sub_header_row}", "values": [["DAYS"]]}
        )
        updates.append(
            {"range": f"'{tab}'!{COL_NOTES}{sub_header_row}", "values": [["NOTES"]]}
        )
        updates.append(
            {"range": f"'{tab}'!{COL_WHO}{sub_header_row}", "values": [["WHO"]]}
        )
    else:
        updates.append(
            {
                "range": f"'{tab}'!{COL_DATE_HEADER}{sub_header_row}",
                "values": [[snapshot_label]],
            }
        )

    # offset 0 = month-header row, +1 = sub-header, +2 = blank spacer, +3..+9 = dept rows.
    for offset, label in enumerate(ALL_BLOCK_ROWS, start=3):
        row = block_start_row + offset
        if write_header:
            updates.append(
                {"range": f"'{tab}'!{COL_LABEL}{row}", "values": [[label]]}
            )
        if label == "LEADS":
            continue  # Jess maintains LEADS by hand
        people_in_dept = people_by_dept.get(label, [])
        total_days = sum(p.available_days for p in people_in_dept)
        updates.append(
            {"range": f"'{tab}'!{COL_DAYS}{row}", "values": [[total_days]]}
        )
        updates.append(
            {
                "range": f"'{tab}'!{COL_WHO}{row}",
                "values": [[format_who_breakdown(people_in_dept)]],
            }
        )
    return updates


def write_capacity(
    sheet_id: str,
    tab: str,
    service_account_path: str,
    availability_by_month: dict[tuple[int, int], list[PersonAvailability]],
    snapshot_date: date | None = None,
) -> dict[str, int]:
    """For each target month: update an existing block, or append a fresh block at the bottom."""
    svc = _service(service_account_path)
    # Only blocks with an explicit year in the header are matchable. A bare "SEPTEMBER"
    # could be any year, and guessing wrong silently overwrites a previous year's figures —
    # so we leave those alone and append a fresh, dated block instead.
    existing = {
        (b.year, b.month): b
        for b in find_month_blocks(svc, sheet_id, tab)
        if b.year is not None
    }
    snapshot_label = (snapshot_date or date.today()).strftime("%-d/%-m/%y")

    updates: list[dict] = []
    blocks_updated = 0
    blocks_created = 0
    next_append_row = _last_used_row(svc, sheet_id, tab)
    if next_append_row == 0:
        next_append_row = 1
    else:
        next_append_row += BLOCK_GAP + 1  # leave a gap

    # Sort by (year, month) so appended blocks stay in order.
    for (year, month) in sorted(availability_by_month.keys()):
        people = availability_by_month[(year, month)]
        by_dept: dict[str, list[PersonAvailability]] = {}
        for p in people:
            by_dept.setdefault(p.department or "", []).append(p)
        header_label = f"{NUM_TO_MONTH[month]} {year}"

        if (year, month) in existing:
            block = existing[(year, month)]
            updates.extend(
                _build_block_updates(
                    tab,
                    block_start_row=block.header_row,
                    header_label=header_label,
                    snapshot_label=snapshot_label,
                    people_by_dept=by_dept,
                    write_header=False,
                )
            )
            blocks_updated += 1
        else:
            updates.extend(
                _build_block_updates(
                    tab,
                    block_start_row=next_append_row,
                    header_label=header_label,
                    snapshot_label=snapshot_label,
                    people_by_dept=by_dept,
                    write_header=True,
                )
            )
            blocks_created += 1
            next_append_row += ROWS_PER_BLOCK + BLOCK_GAP

    if updates:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": updates},
        ).execute()

    return {"blocks_updated": blocks_updated, "blocks_created": blocks_created}
