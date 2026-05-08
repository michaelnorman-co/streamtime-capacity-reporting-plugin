"""Configuration loaded from environment + department mapping rules."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


@dataclass
class Config:
    streamtime_token: str = os.environ.get("STREAMTIME_API_TOKEN", "")
    streamtime_base_url: str = os.environ.get(
        "STREAMTIME_BASE_URL", "https://api.streamtime.net/v2"
    )
    sheet_id: str = os.environ.get("GOOGLE_SHEET_ID", "")
    sheet_tab: str = os.environ.get("GOOGLE_SHEET_TAB", "FY26 Capacity")
    service_account_path: str = os.environ.get(
        "GOOGLE_SERVICE_ACCOUNT_JSON", str(PROJECT_ROOT / "service-account.json")
    )
    hours_per_day: float = float(os.environ.get("HOURS_PER_DAY", "7.5"))
    # Tiered availability thresholds (per Jess's rules):
    #   booked <= full_day_max:      1.0 day available
    #   booked <= half_day_max:      0.5 day available
    #   booked >  half_day_max:      0
    full_day_max_booked_hours: float = float(
        os.environ.get("FULL_DAY_MAX_BOOKED_HOURS", "2.0")
    )
    half_day_max_booked_hours: float = float(
        os.environ.get("HALF_DAY_MAX_BOOKED_HOURS", "4.0")
    )

    def assert_streamtime(self) -> None:
        if not self.streamtime_token:
            raise SystemExit("STREAMTIME_API_TOKEN missing — set it in .env")

    def assert_sheets(self) -> None:
        if not self.sheet_id:
            raise SystemExit("GOOGLE_SHEET_ID missing — set it in .env")
        if not Path(self.service_account_path).exists():
            raise SystemExit(
                f"Service account JSON not found at {self.service_account_path}. "
                "See README for setup."
            )


# Departments rendered in the spreadsheet (in order, excluding LEADS which Jess maintains by hand).
DEPARTMENT_ROWS = [
    "DESIGN",
    "COPY",
    "STRATEGY",
    "DIGITAL",
    "3D / CT / MOTION",
    "ARTWORK",
]

# Users with any of these StreamTime labels are excluded from the report entirely.
# Default: empty. (Freelancers are NOT auto-excluded — Jess includes some, e.g. Ester.)
EXCLUDE_LABELS: set[str] = set()

# Users whose role name (case-insensitive substring) matches any of these are routed to
# the LEADS row in the spreadsheet. Since Jess maintains LEADS by hand, the script
# silently skips them rather than touching that row.
LEAD_ROLE_SUBSTRINGS: list[str] = [
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
]

# Map StreamTime user labels → spreadsheet department, in priority order.
# First matching label wins, so put more-specific labels (e.g. "3D Team") before
# general ones (e.g. "Design Team"). Matching is case-insensitive exact match on label.name.
LABEL_TO_DEPARTMENT: list[tuple[str, str]] = [
    ("3D Team", "3D / CT / MOTION"),
    ("3D", "3D / CT / MOTION"),
    ("Artwork Team", "ARTWORK"),
    ("Digital Team", "DIGITAL"),
    ("Strategy Team", "STRATEGY"),
    ("Copy Team", "COPY"),
    ("Design Team", "DESIGN"),
]

# Fallback role-name substring rules (case-insensitive, first match wins).
# Used when a user has no team label.
ROLE_SUBSTRING_RULES: list[tuple[str, str]] = [
    ("creative technologist", "3D / CT / MOTION"),
    ("3d", "3D / CT / MOTION"),
    ("motion", "3D / CT / MOTION"),
    ("artwork", "ARTWORK"),
    ("digital", "DIGITAL"),
    ("strategist", "STRATEGY"),
    ("strategy", "STRATEGY"),
    ("copywriter", "COPY"),
    ("copy", "COPY"),
    ("designer", "DESIGN"),
]

# Per-person overrides: email or displayName (case-insensitive) → department.
# Use to handle ambiguous cases (e.g. someone tagged with multiple team labels).
PERSON_DEPARTMENT_OVERRIDES: dict[str, str] = {
    # "michael@raggededge.com": "3D / CT / MOTION",
    # "erwin@raggededge.com": "3D / CT / MOTION",
}

# Detect a "pencil" booking. The StreamTime public API does NOT expose a native pencil flag,
# so we approximate: any time entry whose linked job name contains one of these substrings
# (case-insensitive) is treated as pencil rather than confirmed work.
# This list will need real-world tuning once we compare output to Jess's manual count.
PENCIL_JOB_NAME_HINTS: list[str] = [
    "pencil",
    "personal",
    # Note: "creative admin" intentionally excluded — Ragged Edge logs real internal work
    # (e.g. AI projects without a dedicated job) under that. Jess will create a dedicated
    # "Pencil" job code in StreamTime; add it here once it exists.
]

# Time entries against jobs matching these names are excluded from the day's "booked" total
# entirely (not even counted as pencils) — e.g. public holidays don't make a day "booked".
NON_BOOKING_JOB_NAME_HINTS: list[str] = [
    "bank holiday",
    "public holiday",
    "annual leave",
    "holiday",
    "sick",
    "tomr",  # if you use a "TOMR" / time-off job
]
