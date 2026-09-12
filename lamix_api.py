"""Lamix agent API client.

Endpoints used, per the panel's REST API reference:
- GET  /api/v1/clients          - client roster (sync/list)
- GET  /api/v1/ranges           - ranges held, with free-number counts
- GET  /api/v1/numbers          - numbers held, filterable by range/assigned
- POST /api/v1/numbers/assign   - assign specific MSISDNs to a client
- POST /api/v1/numbers/unassign - release MSISDNs back from a client
"""

import asyncio

import httpx

from config import LAMIX_API_TOKEN, LAMIX_BASE_URL


class LamixApiError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


def _headers() -> dict:
    if not LAMIX_API_TOKEN:
        raise LamixApiError("missing_token", "LAMIX_API_TOKEN is not configured.")
    return {"Authorization": f"Bearer {LAMIX_API_TOKEN}"}


def _raise_for_error(response: httpx.Response) -> None:
    if response.status_code == 429:
        try:
            retry_after = response.json().get("retryAfterSeconds", "a bit")
        except ValueError:
            retry_after = "a bit"
        raise LamixApiError("rate_limited", f"Rate limited, retry after {retry_after}s")

    if response.status_code >= 400:
        try:
            code = response.json().get("error", "unknown_error")
        except ValueError:
            code = f"http_{response.status_code}"
        raise LamixApiError(code)


async def _get(path: str, params: dict) -> dict:
    url = f"{LAMIX_BASE_URL.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, headers=_headers(), params=params)
    _raise_for_error(response)
    return response.json()


async def _post(path: str, body: dict) -> dict:
    url = f"{LAMIX_BASE_URL.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, headers=_headers(), json=body)
    _raise_for_error(response)
    return response.json()


async def fetch_clients(limit: int = 500) -> list:
    data = await _get("/api/v1/clients", {"limit": limit})
    return data.get("clients", [])


async def fetch_messages(from_iso: str = None, to_iso: str = None, limit: int = 10) -> list:
    params = {"limit": limit}
    if from_iso:
        params["from"] = from_iso
    if to_iso:
        params["to"] = to_iso
    data = await _get("/api/v1/messages", params)
    return data.get("records", [])


async def fetch_ranges() -> list:
    data = await _get("/api/v1/ranges", {})
    return data.get("ranges", [])


async def fetch_free_numbers(range_id: str, limit: int) -> list:
    """Return up to `limit` MSISDNs in `range_id` that are not yet assigned."""
    numbers: list = []
    after = None
    while len(numbers) < limit:
        params = {
            "rangeId": range_id,
            "assigned": "false",
            "limit": min(limit - len(numbers), 500),
        }
        if after:
            params["after"] = after
        data = await _get("/api/v1/numbers", params)
        records = data.get("records", [])
        numbers.extend(r["number"] for r in records)
        after = data.get("nextCursor")
        if not after or not records:
            break
    return numbers[:limit]


async def fetch_all_numbers(assigned: str = "true") -> list:
    """All numbers in the agent's inventory, paginated (no client filter - see fetch_client_numbers)."""
    numbers: list = []
    after = None
    while True:
        params = {"assigned": assigned, "limit": 500}
        if after:
            params["after"] = after
        data = await _get("/api/v1/numbers", params)
        records = data.get("records", [])
        numbers.extend(records)
        after = data.get("nextCursor")
        if not after or not records:
            break
        await asyncio.sleep(1.1)
    return numbers


async def fetch_client_numbers(client_id: str) -> list:
    """All numbers currently held by this client.

    /api/v1/numbers has no server-side client filter (only range/assigned are
    supported - an unrecognized `client`/`clientId` param is silently ignored
    and the full agent-wide list comes back instead). Fetch everything and
    filter by the `client` field on each record here.
    """
    all_numbers = await fetch_all_numbers(assigned="true")
    return [n for n in all_numbers if (n.get("client") or "").lower() == client_id.lower()]


async def fetch_cdrs(number: str = None, from_iso: str = None, to_iso: str = None, limit: int = 500) -> list:
    params = {"limit": limit}
    if number:
        params["number"] = number
    if from_iso:
        params["from"] = from_iso
    if to_iso:
        params["to"] = to_iso
    data = await _get("/api/v1/cdrs", params)
    return data.get("records", [])


async def assign_numbers(client: str, numbers: list, client_payout_rate: str = "0") -> dict:
    return await _post(
        "/api/v1/numbers/assign",
        {"client": client, "numbers": numbers, "clientPayoutRate": client_payout_rate},
    )


async def unassign_numbers(numbers: list) -> dict:
    return await _post("/api/v1/numbers/unassign", {"numbers": numbers})
