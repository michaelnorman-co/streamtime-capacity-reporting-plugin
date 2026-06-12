"""Minimal client for the StreamTime v2 public API.

Only the routes we need: list users, search logged-time records by month.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import requests

# SearchView IDs (from probing the API)
SEARCH_VIEW_LOGGED_TIME = 8

# FilterGroupType IDs for the LoggedTime view
FGT_DATE = 5
FGT_MONTH = 220
FGT_TODO_STATUS = 85  # 1 = Scheduled, 2 = Logged
FGT_TEAM_MEMBER_STATUS = 127  # 1 = Active, 2 = Hibernated

# ConditionMatchType: 1 = AND
COND_AND = 1
# ValueMatchType: 1 = equals
VMT_EQUALS = 1

PAGE_SIZE = 500  # maxResults — API rejects much larger.


@dataclass
class StreamTimeClient:
    token: str
    base_url: str = "https://api.streamtime.net/v2"
    timeout: float = 30.0
    _job_cache: dict[int, dict] = field(default_factory=dict, repr=False)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, **params: Any) -> Any:
        r = requests.get(
            f"{self.base_url}{path}",
            headers=self._headers(),
            params=params,
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict[str, Any], **params: Any) -> Any:
        r = requests.post(
            f"{self.base_url}{path}",
            headers=self._headers(),
            params=params,
            json=body,
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    # --- Users ---------------------------------------------------------------

    def list_users(self) -> list[dict]:
        """v1 users endpoint includes role + labels (we need those for dept mapping)."""
        v1 = self.base_url.rsplit("/", 1)[0] + "/v1"
        r = requests.get(
            f"{v1}/users",
            headers=self._headers(),
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    # --- Logged time (= scheduled OR logged work) ----------------------------

    def search_logged_time_for_month(
        self,
        year: int,
        month: int,
        scheduled_only: bool = False,
        active_users_only: bool = True,
    ) -> list[dict]:
        """Return all loggedTime records for the given calendar month.

        By default includes both Scheduled (future) and Logged (past) entries — a person's
        capacity for a past month is determined by what's already logged against them, not
        what was once scheduled.
        """
        filter_groups: list[dict] = [
            {
                "filterGroupTypeId": FGT_MONTH,
                "conditionMatchTypeId": COND_AND,
                "filters": [
                    {"valueMatchTypeId": VMT_EQUALS, "value": f"{year:04d}-{month:02d}"}
                ],
            }
        ]
        if scheduled_only:
            filter_groups.append(
                {
                    "filterGroupTypeId": FGT_TODO_STATUS,
                    "conditionMatchTypeId": COND_AND,
                    "filters": [{"valueMatchTypeId": VMT_EQUALS, "value": 1}],
                }
            )
        if active_users_only:
            filter_groups.append(
                {
                    "filterGroupTypeId": FGT_TEAM_MEMBER_STATUS,
                    "conditionMatchTypeId": COND_AND,
                    "filters": [{"valueMatchTypeId": VMT_EQUALS, "value": 1}],
                }
            )

        # NOTE: the API used to populate each record's `job` object via
        # `additional_data=job`, but that param now makes /search return a 500
        # (server-side regression, ~May 2026). We fetch records without it and
        # join job details ourselves via GET /jobs/{id}, keyed on loggedTime.jobId.
        records = list(self._paginate_search(filter_groups))
        self._attach_jobs(records)
        return records

    # --- Jobs ----------------------------------------------------------------

    def get_job(self, job_id: int) -> dict | None:
        """Fetch a single job (cached). Returns None if it can't be retrieved."""
        if job_id in self._job_cache:
            return self._job_cache[job_id]
        try:
            job = self._get(f"/jobs/{job_id}")
        except requests.HTTPError:
            job = None
        self._job_cache[job_id] = job
        return job

    def _attach_jobs(self, records: list[dict]) -> None:
        """Populate each record's `job` field (which search no longer fills in).

        Capacity logic reads record["job"]["name"/"fullName"] to detect pencil
        and non-booking bookings, so this join keeps that behaviour intact.
        """
        job_ids = {
            r["loggedTime"]["jobId"]
            for r in records
            if r.get("loggedTime") and r["loggedTime"].get("jobId")
        }
        for jid in job_ids:
            if jid not in self._job_cache:
                self.get_job(jid)
                time.sleep(0.2)  # polite pause on real network fetches only
        for r in records:
            lt = r.get("loggedTime") or {}
            jid = lt.get("jobId")
            if jid:
                r["job"] = self._job_cache.get(jid)

    def _paginate_search(
        self, filter_groups: list[dict], additional_data: str | None = None
    ) -> Iterable[dict]:
        offset = 0
        while True:
            params: dict[str, Any] = {"search_view": SEARCH_VIEW_LOGGED_TIME}
            if additional_data:
                params["additional_data"] = additional_data
            body = {
                "maxResults": PAGE_SIZE,
                "offset": offset,
                "filterGroupCollection": {
                    "conditionMatchTypeId": COND_AND,
                    "filterGroups": filter_groups,
                },
            }
            page = self._post("/search", body, **params)
            if not isinstance(page, list):
                raise RuntimeError(f"Unexpected search response: {page!r}")
            yield from page
            if len(page) < PAGE_SIZE:
                return
            offset += PAGE_SIZE
            # Polite rate-limit pause: 60 req/min.
            time.sleep(1.05)
