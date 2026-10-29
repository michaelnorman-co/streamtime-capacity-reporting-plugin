"""Dump the Python model's output over cached API data, for parity.test.js.

Usage: .venv/bin/python apps_script/test/make_expected.py <data_dir> > expected.json
<data_dir> holds users.json + logged_YYYY-MM.json (e.g. debug/ from `--cache-dir debug`).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.capacity import availability_windows, compute_availability  # noqa: E402
from src.sheets_writer import format_who_breakdown  # noqa: E402
from src.config import DEPARTMENT_ROWS  # noqa: E402

data = Path(sys.argv[1])
users = json.loads((data / "users.json").read_text())
months = sorted(p.stem.removeprefix("logged_") for p in data.glob("logged_*.json"))
records = {m: json.loads((data / f"logged_{m}.json").read_text()) for m in months}
windows = availability_windows([r for m in months for r in records[m]])

out = {}
for m in months:
    y, mo = map(int, m.split("-"))
    people = compute_availability(users, records[m], y, mo, 2.0, 4.0, user_windows=windows)
    out[m] = {
        "people": [
            {
                "userId": p.user_id,
                "name": p.name,
                "department": p.department,
                "availableDays": p.available_days,
                "pencilDays": p.pencil_days,
                "availableDates": [d.isoformat() for d in p.available_dates],
                "distribution": p.distribution,
                "pencilProjects": p.pencil_projects,
            }
            for p in people
        ],
        "who": {
            dept: format_who_breakdown([p for p in people if p.department == dept])
            for dept in DEPARTMENT_ROWS
        },
    }
json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
