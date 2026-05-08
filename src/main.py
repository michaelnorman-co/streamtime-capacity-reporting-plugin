"""Entry point: pull StreamTime data, compute capacity, write to Google Sheet."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .capacity import availability_windows, compute_availability
from .config import Config, DEPARTMENT_ROWS
from .sheets_writer import write_capacity
from .streamtime_client import StreamTimeClient


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="StreamTime → Google Sheets capacity report")
    p.add_argument(
        "--months",
        type=int,
        default=3,
        help="How many months ahead (including current) to compute. Default 3.",
    )
    p.add_argument(
        "--start",
        type=str,
        help="YYYY-MM start month. Defaults to current month.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print results without writing to Google Sheets.",
    )
    p.add_argument(
        "--cache-dir",
        type=str,
        help="If set, write/read raw API responses to JSON files in this dir for debugging.",
    )
    p.add_argument(
        "--lookahead-months",
        type=int,
        default=3,
        help=(
            "Extra months to fetch *after* the last target month, used only for finding "
            "future START DATE / END DATE markers. Default 3."
        ),
    )
    return p.parse_args()


def month_targets(start: date, count: int) -> list[tuple[int, int]]:
    out = []
    y, m = start.year, start.month
    for _ in range(count):
        out.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def main() -> int:
    args = parse_args()
    cfg = Config()
    cfg.assert_streamtime()

    if args.start:
        y, m = map(int, args.start.split("-"))
        start = date(y, m, 1)
    else:
        today = date.today()
        start = date(today.year, today.month, 1)

    targets = month_targets(start, args.months)

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)

    client = StreamTimeClient(cfg.streamtime_token, cfg.streamtime_base_url)
    print("Fetching users...", file=sys.stderr)
    users = client.list_users()
    if cache_dir:
        (cache_dir / "users.json").write_text(json.dumps(users, indent=2))

    # Fetch target months. Then also fetch an extra `lookahead-months` past the last target,
    # used only for marker detection (so a START DATE in a future month correctly suppresses
    # earlier availability).
    extended_targets = month_targets(start, args.months + args.lookahead_months)

    records_by_month: dict[tuple[int, int], list] = {}
    for y, m in extended_targets:
        print(f"Fetching scheduled time for {y}-{m:02d}...", file=sys.stderr)
        records = client.search_logged_time_for_month(y, m)
        if cache_dir:
            (cache_dir / f"logged_{y}-{m:02d}.json").write_text(
                json.dumps(records, indent=2)
            )
        records_by_month[(y, m)] = records

    all_records = [r for recs in records_by_month.values() for r in recs]
    user_windows = availability_windows(all_records)

    availability_by_month: dict[tuple[int, int], list] = {}
    for (y, m) in targets:
        records = records_by_month[(y, m)]
        people = compute_availability(
            users,
            records,
            y,
            m,
            cfg.full_day_max_booked_hours,
            cfg.half_day_max_booked_hours,
            user_windows=user_windows,
        )
        availability_by_month[(y, m)] = people

    # Always print a summary table.
    print()
    for (y, m), people in availability_by_month.items():
        print(f"=== {y}-{m:02d} ===")
        by_dept: dict[str, list] = {}
        for p in people:
            by_dept.setdefault(p.department or "?", []).append(p)
        for dept in DEPARTMENT_ROWS:
            ps = by_dept.get(dept, [])
            total = sum(p.available_days for p in ps)
            def _fmt(d: float) -> str:
                return f"{d:.1f}".rstrip("0").rstrip(".") or "0"
            breakdown = ", ".join(
                f"{p.name}:{_fmt(p.available_days)}"
                + (f"(+{_fmt(p.pencil_days)}✏)" if p.pencil_days else "")
                for p in sorted(ps, key=lambda x: x.name.lower())
                if p.available_days
            )
            print(f"  {dept:18} total={_fmt(total):>5}  {breakdown}")
        print()

    if args.dry_run:
        print("(dry run — sheet not written)")
        return 0

    cfg.assert_sheets()
    print("Writing to Google Sheet...", file=sys.stderr)
    stats = write_capacity(
        cfg.sheet_id,
        cfg.sheet_tab,
        cfg.service_account_path,
        availability_by_month,
    )
    print(f"Done: {stats}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
