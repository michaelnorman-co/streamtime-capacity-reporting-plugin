"""Capacity calculator: turn loggedTime + user records into per-person availability."""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

from .config import (
    EXCLUDE_LABELS,
    LABEL_TO_DEPARTMENT,
    LEAD_ROLE_SUBSTRINGS,
    NON_BOOKING_JOB_NAME_HINTS,
    PENCIL_JOB_NAME_HINTS,
    PERSON_DEPARTMENT_OVERRIDES,
    ROLE_SUBSTRING_RULES,
)


# Map: weekday index (0=Mon, 6=Sun) → user field returned by /v1/users.
WEEKDAY_HOUR_FIELDS = {
    0: "hoursWorkedMonday",
    1: "hoursWorkedTuesday",
    2: "hoursWorkedWednesday",
    3: "hoursWorkedThursday",
    4: "hoursWorkedFriday",
    5: "hoursWorkedSaturday",
    6: "hoursWorkedSunday",
}


@dataclass
class PersonAvailability:
    user_id: int
    name: str  # short display name used in the WHO column
    department: str | None
    available_days: float = 0.0  # supports half-days (0.5)
    pencil_days: float = 0.0     # subset of available_days that have a pencil booking
    available_dates: list[date] = field(default_factory=list)  # full-or-half — sorted by date
    pencil_projects: list[str] = field(default_factory=list)   # project names from `p. X` notes

    @property
    def distribution(self) -> str:
        """'5 in a row' vs '5 scattered' — short hint for Jess."""
        if not self.available_dates:
            return ""
        sorted_d = sorted(self.available_dates)
        runs: list[int] = [1]
        for prev, cur in zip(sorted_d, sorted_d[1:]):
            if (cur - prev).days == 1:
                runs[-1] += 1
            else:
                runs.append(1)
        max_run = max(runs)
        if max_run == len(sorted_d):
            return "block"
        if max_run == 1:
            return "scattered"
        return "mixed"


def working_days_for_user(user: dict, year: int, month: int) -> list[date]:
    """Calendar days in `month` where the user is contracted to work (>0 hours)."""
    days_in_month = calendar.monthrange(year, month)[1]
    out: list[date] = []
    for d in range(1, days_in_month + 1):
        cur = date(year, month, d)
        field_name = WEEKDAY_HOUR_FIELDS[cur.weekday()]
        if (user.get(field_name) or 0) > 0:
            out.append(cur)
    return out


def department_for_user(user: dict) -> str | None:
    """Resolve a user to one of the spreadsheet department rows, or None to skip.

    Resolution order:
      1. Per-person override (by email or displayName).
      2. Exclude if any label is in EXCLUDE_LABELS (e.g. Freelancers).
      3. Match labels against LABEL_TO_DEPARTMENT in priority order.
      4. Fallback: substring-match role/jobTitle against ROLE_SUBSTRING_RULES.
    """
    overrides = {k.lower(): v for k, v in PERSON_DEPARTMENT_OVERRIDES.items()}
    email = (user.get("email") or "").lower()
    display = (user.get("displayName") or "").lower()
    if email and email in overrides:
        return overrides[email]
    if display and display in overrides:
        return overrides[display]

    label_names = {
        (label.get("name") or "")
        for label in (user.get("labels") or [])
        if isinstance(label, dict)
    }
    if label_names & EXCLUDE_LABELS:
        return None

    role_text = " ".join(
        filter(
            None,
            [
                (user.get("role") or {}).get("name") if isinstance(user.get("role"), dict) else None,
                user.get("jobTitle"),
            ],
        )
    ).lower()

    # Lead-level roles are managed by Jess in the LEADS row by hand — skip them.
    for needle in LEAD_ROLE_SUBSTRINGS:
        if needle in role_text:
            return None

    label_names_lower = {n.lower() for n in label_names}
    for label_name, dept in LABEL_TO_DEPARTMENT:
        if label_name.lower() in label_names_lower:
            return dept

    for needle, dept in ROLE_SUBSTRING_RULES:
        if needle in role_text:
            return dept
    return None


def _schedule_notes(record: dict) -> str:
    return (record.get("loggedTime", {}).get("scheduleNotes") or "")


def _job_text(record: dict) -> str:
    job = record.get("job") or {}
    return f"{(job.get('name') or '').lower()} {(job.get('fullName') or '').lower()} {_schedule_notes(record).lower()}"


PENCIL_EMOJI = "✏"  # ✏ — also matches '✏️' (the emoji is U+270F + variation selector U+FE0F).


def is_pencil_entry(record: dict) -> bool:
    """Pencil detection (Ragged Edge convention agreed 2026-05-08):

    A scheduled time entry is a pencil if its scheduleNotes contains the ✏️ emoji,
    OR contains the word 'PENCIL' (case-insensitive). Job-name fallback covers a
    couple of edge cases (a job literally named 'Pencil' / 'Personal').
    """
    notes = _schedule_notes(record)
    if PENCIL_EMOJI in notes or "PENCIL" in notes.upper():
        return True
    return any(hint in _job_text(record) for hint in PENCIL_JOB_NAME_HINTS)


def is_presentation_entry(record: dict) -> bool:
    """Presentation detection: scheduleNotes starts with 'p. ' or 'P. ' (case-insensitive).

    Per Jess's rule, a presentation day is whole-day busy regardless of hours booked —
    she won't pull someone onto a complex new ask the day they're presenting.
    """
    notes = _schedule_notes(record).lstrip()
    return notes.lower().startswith("p. ")


def pencil_project_name(record: dict) -> str | None:
    """Best-effort project name for a pencil entry.

    Strips the ✏️ emoji and the literal word 'PENCIL' from scheduleNotes; returns the
    remainder as Title Case. Falls back to the linked job's name.
    """
    notes = _schedule_notes(record)
    cleaned = notes.replace(PENCIL_EMOJI, "").replace("️", "")
    # Remove the word PENCIL anywhere in the string (case-insensitive).
    import re
    cleaned = re.sub(r"\bPENCIL\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" :-—,").strip()
    if cleaned:
        return cleaned.title()
    return (record.get("job") or {}).get("name")


def availability_windows(
    logged_times: list[dict],
) -> dict[int, tuple[date | None, date | None]]:
    """Per-user (start, end) bounds derived from 'START DATE' / 'BOOKING END DATE' markers in scheduleNotes.

    Either bound may be None (meaning unbounded that side).
    """
    windows: dict[int, tuple[date | None, date | None]] = {}
    for rec in logged_times:
        lt = rec.get("loggedTime") or {}
        notes = (lt.get("scheduleNotes") or "").upper()
        d_str = lt.get("date")
        uid = lt.get("userId")
        if not (uid and d_str):
            continue
        try:
            d = date.fromisoformat(d_str)
        except ValueError:
            continue
        start, end = windows.get(uid, (None, None))
        if "START DATE" in notes:
            start = d if start is None else min(start, d)
        elif "END DATE" in notes:
            # Catches both 'BOOKING END DATE' and 'END DATE'.
            end = d if end is None else max(end, d)
        windows[uid] = (start, end)
    return windows


def is_non_booking_entry(record: dict) -> bool:
    """True if the entry shouldn't count as 'booked' time at all (e.g. bank holiday)."""
    text = _job_text(record)
    return any(hint in text for hint in NON_BOOKING_JOB_NAME_HINTS)


def short_name(user: dict) -> str:
    return (
        user.get("firstName")
        or user.get("displayName")
        or (user.get("email") or "").split("@")[0]
    )


def compute_availability(
    users: list[dict],
    logged_times: list[dict],
    year: int,
    month: int,
    full_day_max_hours: float,
    half_day_max_hours: float,
    user_windows: dict[int, tuple[date | None, date | None]] | None = None,
) -> list[PersonAvailability]:
    """Return one PersonAvailability per user (only those with a department mapping).

    Tiered availability per day (matches Jess's manual count):
      booked <= full_day_max_hours: 1.0 day available
      booked <= half_day_max_hours: 0.5 day available
      booked >  half_day_max_hours: 0
    """
    full_day_max_min = full_day_max_hours * 60.0
    half_day_max_min = half_day_max_hours * 60.0
    user_windows = user_windows or {}

    # Per-user, per-date: confirmed minutes, pencil minutes, non-booking minutes,
    # plus a flag if any entry on that day is a presentation (forces whole day to busy).
    by_user: dict[int, dict[date, dict]] = {}
    for rec in logged_times:
        lt = rec.get("loggedTime") or {}
        uid = lt.get("userId")
        d_str = lt.get("date")
        minutes = lt.get("minutes") or 0
        if not uid or not d_str:
            continue
        try:
            d = date.fromisoformat(d_str)
        except ValueError:
            continue
        if d.year != year or d.month != month:
            continue
        bucket = by_user.setdefault(uid, {}).setdefault(
            d,
            {"confirmed": 0.0, "pencil": 0.0, "non_booking": 0.0,
             "presentation": False, "pencil_projects": []},
        )
        if is_non_booking_entry(rec):
            bucket["non_booking"] += minutes
        elif is_presentation_entry(rec):
            bucket["presentation"] = True
            bucket["confirmed"] += minutes
        elif is_pencil_entry(rec):
            bucket["pencil"] += minutes
            proj = pencil_project_name(rec)
            if proj:
                bucket["pencil_projects"].append(proj)
        else:
            bucket["confirmed"] += minutes

    results: list[PersonAvailability] = []
    for user in users:
        if (user.get("userStatus") or {}).get("name") != "Active":
            continue
        dept = department_for_user(user)
        if not dept:
            continue
        uid = user["id"]
        person = PersonAvailability(
            user_id=uid, name=short_name(user), department=dept
        )
        per_day = by_user.get(uid, {})
        start, end = user_windows.get(uid, (None, None))
        for d in working_days_for_user(user, year, month):
            # Outside the user's active window (e.g. before their START DATE marker).
            if start and d < start:
                continue
            if end and d > end:
                continue
            day = per_day.get(
                d,
                {"confirmed": 0.0, "pencil": 0.0, "non_booking": 0.0,
                 "presentation": False, "pencil_projects": []},
            )
            # Skip bank-holiday / annual-leave days entirely — not available, not booked.
            if day["non_booking"] >= half_day_max_min and day["confirmed"] == 0:
                continue
            # Presentation day → whole day busy regardless of hours.
            if day["presentation"]:
                continue
            confirmed = day["confirmed"]
            if confirmed <= full_day_max_min:
                fraction = 1.0
            elif confirmed <= half_day_max_min:
                fraction = 0.5
            else:
                fraction = 0.0
            if fraction > 0:
                person.available_days += fraction
                person.available_dates.append(d)
                if day["pencil"] > 0:
                    person.pencil_days += fraction
                    person.pencil_projects.extend(day["pencil_projects"])
        results.append(person)
    return results
