"""Cursor dashboard API client (unofficial endpoints)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

BASE_URL = "https://cursor.com"
USAGE_SUMMARY_PATH = "/api/usage-summary"
FILTERED_EVENTS_PATH = "/api/dashboard/get-filtered-usage-events"
CURRENT_PERIOD_PATH = "/api/dashboard/get-current-period-usage"
AGGREGATED_USAGE_PATH = "/api/dashboard/get-aggregated-usage-events"
ORIGIN = "https://cursor.com"


class CursorAuthError(Exception):
    """Raised when Cursor session cookie is missing, invalid, or expired."""


class CursorAPIError(Exception):
    """Raised for non-auth Cursor API failures (HTTP, network, unexpected body)."""

    def __init__(
        self,
        message: str,
        *,
        endpoint: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.status_code = status_code


class CursorSchemaError(CursorAPIError):
    """Raised when response JSON is missing expected fields (API may have changed)."""


class CursorClient:
    """HTTP client for usage-summary and filtered-usage-events."""

    def __init__(self, session_token: str, timeout: float = 30.0) -> None:
        token = (session_token or "").strip()
        if not token:
            raise CursorAuthError("Cursor session_token is empty")

        self._timeout = timeout
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            headers={
                "User-Agent": "cursor-usage-reporter/0.1",
                "Accept": "application/json",
            },
            cookies={"WorkosCursorSessionToken": token},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CursorClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def get_usage_summary(self) -> dict[str, Any]:
        """GET /api/usage-summary — billing cycle, plan, usage totals."""
        data = self._request_json("GET", USAGE_SUMMARY_PATH)
        for field in ("billingCycleStart", "billingCycleEnd", "membershipType"):
            if field not in data:
                raise CursorSchemaError(
                    f"usage-summary missing required field '{field}' "
                    "(dashboard API may have changed)",
                    endpoint=USAGE_SUMMARY_PATH,
                )
        return data

    def get_current_period_usage(self) -> dict[str, Any]:
        """POST /api/dashboard/get-current-period-usage — pool percents the dashboard shows."""
        data = self._request_json("POST", CURRENT_PERIOD_PATH, json={})
        plan = data.get("planUsage")
        if not isinstance(plan, dict):
            raise CursorSchemaError(
                "current-period-usage missing 'planUsage' "
                "(dashboard API may have changed)",
                endpoint=CURRENT_PERIOD_PATH,
            )
        for field in (
            "totalSpend",
            "autoPercentUsed",
            "apiPercentUsed",
            "totalPercentUsed",
        ):
            if field not in plan:
                raise CursorSchemaError(
                    f"planUsage missing required field '{field}' "
                    "(dashboard API may have changed)",
                    endpoint=CURRENT_PERIOD_PATH,
                )
        return data

    def get_aggregated_usage(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        """POST aggregated usage for [start_ms, end_ms]. Rows include the dashboard tier."""
        if start_ms > end_ms:
            raise ValueError("start_ms must be <= end_ms")
        data = self._request_json(
            "POST",
            AGGREGATED_USAGE_PATH,
            json={"startDate": start_ms, "endDate": end_ms},
        )
        rows = data.get("aggregations")
        if not isinstance(rows, list):
            raise CursorSchemaError(
                "aggregated-usage-events missing 'aggregations' "
                "(dashboard API may have changed)",
                endpoint=AGGREGATED_USAGE_PATH,
            )
        return [row for row in rows if isinstance(row, dict)]

    def iter_usage_events(
        self,
        start_ms: int,
        end_ms: int,
        page_size: int = 100,
    ) -> Iterator[dict[str, Any]]:
        """Yield usage events for [start_ms, end_ms], paginating until complete."""
        if page_size < 1:
            raise ValueError("page_size must be >= 1")
        if start_ms > end_ms:
            raise ValueError("start_ms must be <= end_ms")

        page = 1
        yielded = 0
        total: int | None = None

        while True:
            body = {
                "startDate": str(start_ms),
                "endDate": str(end_ms),
                "page": page,
                "pageSize": page_size,
            }
            data = self._request_json("POST", FILTERED_EVENTS_PATH, json=body)

            if "usageEventsDisplay" not in data:
                raise CursorSchemaError(
                    "filtered-usage-events missing 'usageEventsDisplay' "
                    "(dashboard API may have changed)",
                    endpoint=FILTERED_EVENTS_PATH,
                )
            if "totalUsageEventsCount" not in data:
                raise CursorSchemaError(
                    "filtered-usage-events missing 'totalUsageEventsCount' "
                    "(dashboard API may have changed)",
                    endpoint=FILTERED_EVENTS_PATH,
                )

            events = data["usageEventsDisplay"]
            if not isinstance(events, list):
                raise CursorSchemaError(
                    "usageEventsDisplay must be a list",
                    endpoint=FILTERED_EVENTS_PATH,
                )

            total = int(data["totalUsageEventsCount"])
            if not events:
                break

            for event in events:
                if isinstance(event, dict):
                    yield event
                    yielded += 1

            if yielded >= total or len(events) < page_size:
                break
            page += 1

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if method.upper() == "POST":
            headers["Origin"] = ORIGIN
            headers["Content-Type"] = "application/json"

        try:
            response = self._client.request(method, path, headers=headers, json=json)
        except httpx.TimeoutException as exc:
            raise CursorAPIError(
                f"Timeout calling Cursor {path}",
                endpoint=path,
            ) from exc
        except httpx.RequestError as exc:
            raise CursorAPIError(
                f"Network error calling Cursor {path}: {exc}",
                endpoint=path,
            ) from exc

        self._raise_for_status(response, path)

        try:
            data = response.json()
        except ValueError as exc:
            raise CursorSchemaError(
                f"Cursor {path} returned non-JSON body "
                "(dashboard API may have changed)",
                endpoint=path,
                status_code=response.status_code,
            ) from exc

        if not isinstance(data, dict):
            raise CursorSchemaError(
                f"Cursor {path} JSON root must be an object "
                "(dashboard API may have changed)",
                endpoint=path,
                status_code=response.status_code,
            )

        self._raise_if_auth_payload(data)
        return data

    def _raise_for_status(self, response: httpx.Response, path: str) -> None:
        if response.status_code == 401:
            raise CursorAuthError(
                "Cursor session unauthorized (cookie invalid or expired). "
                "Update cursor.session_token in config.yaml"
            )

        # Some responses return 200 with {"error": "not_authenticated"}
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            self._raise_if_auth_payload(payload)

        if response.is_success:
            return

        snippet = (response.text or "")[:300]
        raise CursorAPIError(
            f"Cursor {path} returned HTTP {response.status_code}: {snippet}",
            endpoint=path,
            status_code=response.status_code,
        )

    @staticmethod
    def _raise_if_auth_payload(data: dict[str, Any]) -> None:
        error = data.get("error")
        if error == "not_authenticated" or (
            isinstance(error, str) and "not_authenticated" in error.lower()
        ):
            raise CursorAuthError(
                "Cursor session not_authenticated (cookie invalid or expired). "
                "Update cursor.session_token in config.yaml"
            )
