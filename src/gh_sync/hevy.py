"""Thin client for the Hevy public API (https://api.hevyapp.com).

Auth is a single ``api-key`` header; there is no OAuth dance. Pagination is
1-based with a ``pageSize`` whose documented maximum varies per endpoint and is
not advertised in the spec, so :meth:`HevyClient._paginate` starts optimistic
and halves on a 400 rather than hardcoding a guess.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import requests

BASE_URL = "https://api.hevyapp.com"
logger = logging.getLogger("gh_sync.hevy")


class HevyError(RuntimeError):
    pass


def extract_workout_id(response: Any) -> str | None:
    """Pull the workout id out of a POST /v1/workouts response.

    The spec does not pin the response shape and the live API wraps the created
    workout in a *list* under "workout", not an object. Accept every plausible
    shape rather than betting on one: losing the id means the ledger cannot
    record the import and the loop-prevention cross-mark never happens.
    """
    candidate = response
    if isinstance(candidate, dict):
        candidate = candidate.get("workout", candidate)
    if isinstance(candidate, list):
        candidate = candidate[0] if candidate else None
    if isinstance(candidate, dict):
        workout_id = candidate.get("id")
        return str(workout_id) if workout_id else None
    return None


def _json_or_empty(response: requests.Response) -> dict[str, Any]:
    """Body of an already-successful write, or {} when it carries no JSON.

    Only ever call this once the status code has been checked. It exists so a
    write that Hevy has already accepted is never turned into an exception by
    the shape of its reply.
    """
    try:
        return response.json()
    except ValueError:  # requests raises a JSONDecodeError subclass of this
        return {}


class HevyClient:
    def __init__(self, api_key: str, timeout: int = 30) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"api-key": api_key, "Accept": "application/json"}
        )

    # ---------------------------------------------------------------- plumbing

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = f"{BASE_URL}{path}"
        for attempt in range(4):
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            # 429 and 5xx are transient; back off and retry.
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2**attempt
                logger.warning(
                    "Hevy %s %s -> %s, retrying in %ss", method, path, resp.status_code, wait
                )
                time.sleep(wait)
                continue
            return resp
        return resp

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._request("GET", path, params=params)
        if not resp.ok:
            raise HevyError(f"GET {path} -> {resp.status_code}: {resp.text[:400]}")
        return resp.json()

    def _paginate(self, path: str, key: str, page_size: int = 10) -> Iterator[dict]:
        """Yield every item across pages, shrinking pageSize if the API rejects it."""
        page = 1
        while True:
            try:
                payload = self._get(path, {"page": page, "pageSize": page_size})
            except HevyError as exc:
                if "400" in str(exc) and page_size > 1:
                    page_size = max(1, page_size // 2)
                    logger.info("Hevy rejected pageSize, retrying %s with %s", path, page_size)
                    continue
                raise
            items = payload.get(key) or []
            for item in items:
                yield item
            page_count = payload.get("page_count")
            if not items or (page_count is not None and page >= page_count):
                return
            page += 1

    # ------------------------------------------------------------------ reads

    def user_info(self) -> dict[str, Any]:
        return self._get("/v1/user/info")

    def workout_count(self) -> int:
        return int(self._get("/v1/workouts/count").get("workout_count", 0))

    def iter_workouts(self, page_size: int = 10) -> Iterator[dict]:
        """Workouts newest-first."""
        return self._paginate("/v1/workouts", "workouts", page_size)

    def iter_exercise_templates(self, page_size: int = 100) -> Iterator[dict]:
        return self._paginate("/v1/exercise_templates", "exercise_templates", page_size)

    def iter_routines(self, page_size: int = 10) -> Iterator[dict]:
        return self._paginate("/v1/routines", "routines", page_size)

    def iter_body_measurements(self, page_size: int = 10) -> Iterator[dict]:
        return self._paginate("/v1/body_measurements", "body_measurements", page_size)

    # ----------------------------------------------------------------- writes

    def create_workout(self, workout: dict[str, Any]) -> dict[str, Any]:
        resp = self._request("POST", "/v1/workouts", json={"workout": workout})
        if resp.status_code not in (200, 201):
            raise HevyError(f"POST /v1/workouts -> {resp.status_code}: {resp.text[:600]}")
        # Same hazard as create_body_measurement: the workout exists on Hevy by
        # now, so an unreadable body must degrade to "created, id unknown"
        # rather than blowing up the run. extract_workout_id returns None for
        # {} and flow B already handles a missing id.
        return _json_or_empty(resp)

    def create_body_measurement(self, measurement: dict[str, Any]) -> dict[str, Any] | None:
        """Returns None when Hevy already has an entry for that date (409).

        The live API answers a successful POST with an empty body, so the JSON
        is parsed opportunistically. Letting the decode error escape would be
        the worst outcome available: the measurement is already stored by the
        time the body is read, so the caller would abort *after* the write and
        never reach the ledger, orphaning the row and stalling the whole run.
        An empty dict still reads as "created" against the None-means-409 test.
        """
        resp = self._request("POST", "/v1/body_measurements", json=measurement)
        if resp.status_code == 409:
            return None
        if resp.status_code not in (200, 201):
            raise HevyError(
                f"POST /v1/body_measurements -> {resp.status_code}: {resp.text[:400]}"
            )
        return _json_or_empty(resp)
