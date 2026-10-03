"""Thin client for the Hevy public API (https://api.hevyapp.com).

Auth is a single ``api-key`` header; there is no OAuth dance. Pagination is
1-based with a ``pageSize`` whose documented maximum varies per endpoint and is
not advertised in the spec, so :meth:`HevyClient._paginate` starts optimistic
and halves on a 400 rather than hardcoding a guess.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any

import requests

from . import __version__

BASE_URL = "https://api.hevyapp.com"
logger = logging.getLogger("gh_sync.hevy")

# Transient failures are retried this many times with exponential backoff.
MAX_ATTEMPTS = 4
# Never sleep longer than this on a single Retry-After, whatever the server says.
MAX_BACKOFF_SECONDS = 60


class HevyError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _retry_after(resp: requests.Response, attempt: int) -> float:
    header = resp.headers.get("Retry-After", "")
    if header.isdigit():
        return min(float(header), MAX_BACKOFF_SECONDS)
    return float(2**attempt)


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
            {
                "api-key": api_key,
                "Accept": "application/json",
                "User-Agent": f"garmin-hevy-sync/{__version__}",
            }
        )

    # ---------------------------------------------------------------- plumbing

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """One API call, retrying transient failures where that is safe.

        A GET is retried on dropped connections, timeouts, 429 and 5xx. A POST
        is retried only when Hevy provably did not process it: the connection
        was never made, or Hevy answered 429. After a read timeout or a 5xx the
        workout may already exist, and sending it again would create a second
        copy. Those raise instead, the activity is recorded as failed, and the
        next run's overlap check sees the workout if it was created after all.
        """
        url = f"{BASE_URL}{path}"
        idempotent = method.upper() == "GET"
        for attempt in range(MAX_ATTEMPTS):
            last = attempt == MAX_ATTEMPTS - 1
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                never_sent = isinstance(exc, requests.ConnectTimeout)
                if last or not (idempotent or never_sent):
                    raise HevyError(f"{method} {path}: {exc}") from exc
                wait = 2**attempt
                logger.warning("Hevy %s %s failed (%s), retrying in %ss", method, path, exc, wait)
                time.sleep(wait)
                continue
            transient = resp.status_code == 429 or (idempotent and resp.status_code >= 500)
            if transient and not last:
                wait = _retry_after(resp, attempt)
                logger.warning(
                    "Hevy %s %s -> %s, retrying in %ss", method, path, resp.status_code, wait
                )
                time.sleep(wait)
                continue
            return resp
        raise AssertionError("unreachable")  # pragma: no cover

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._request("GET", path, params=params)
        if not resp.ok:
            raise HevyError(
                f"GET {path} -> {resp.status_code}: {resp.text[:400]}", status=resp.status_code
            )
        return resp.json()

    def _paginate(self, path: str, key: str, page_size: int = 10) -> Iterator[dict]:
        """Yield every item across pages, shrinking pageSize if the API rejects it."""
        page = 1
        while True:
            try:
                payload = self._get(path, {"page": page, "pageSize": page_size})
            except HevyError as exc:
                if exc.status == 400 and page_size > 1:
                    page_size = max(1, page_size // 2)
                    logger.info("Hevy rejected pageSize, retrying %s with %s", path, page_size)
                    continue
                raise
            items = payload.get(key) or []
            yield from items
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
            raise HevyError(
                f"POST /v1/workouts -> {resp.status_code}: {resp.text[:600]}",
                status=resp.status_code,
            )
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
                f"POST /v1/body_measurements -> {resp.status_code}: {resp.text[:400]}",
                status=resp.status_code,
            )
        return _json_or_empty(resp)
