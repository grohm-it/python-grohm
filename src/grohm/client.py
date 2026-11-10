"""Async client for the Grohm cloud GraphQL API."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import aiohttp

from .exceptions import (
    GrohmAuthenticationError,
    GrohmCommandError,
    GrohmConnectionError,
    GrohmError,
)
from .models import AccountType, Device, StatusCommand, User

_LOGGER = logging.getLogger(__name__)

API_URL = "https://api.grohm.it/graphql"

_PAGE_SIZE = 50
_MAX_PAGES = 50
_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=15)

# Without a valid session the API answers HTTP 200 with this GraphQL error
# (`me` returns null instead).
_NOT_AUTHENTICATED = "not authenticated"

_USER_FIELDS = "id email firstName lastName accountType"

_LOGIN = f"""
mutation Login($email: String!, $password: String!) {{
  login(email: $email, password: $password) {{
    __typename
    ... on LoginSuccess {{ user {{ {_USER_FIELDS} }} }}
    ... on LoginError {{ message }}
  }}
}}
"""

_LOGOUT = "mutation Logout { logout }"

_ME = f"query Me {{ me {{ {_USER_FIELDS} }} }}"

_PAGED_IDS = """
query {name}($page: Int!, $first: Int!) {{
  {field}(page: $page, first: $first) {{
    items {{ {items} }}
    pageInfo {{ hasNextPage nextPage }}
  }}
}}
"""

_MY_DEVICES = _PAGED_IDS.format(name="MyDevices", field="myDevices", items="id")
_DEVICES_OUT_OF_NETWORK = _PAGED_IDS.format(
    name="DevicesOutOfNet", field="devicesOutOfNet", items="id"
)
_MY_NETWORKS = _PAGED_IDS.format(
    name="MyNetworks", field="myNetworks", items="devices { id }"
)
_MAINTAINER_DEVICES = "query MaintainerDevices { maintainerDevices { id } }"

_DEVICE_FRAGMENT = """
fragment GrohmDevice on Device {
  id
  name
  serialNumber
  isConnected
  locked
  fase
  forceCharge
  hourCost
  maxPowerKw
  totalKwhCharged
  currentSession {
    status
    voltage
    maxCurrent
    maxCurrentCar
    wattHoursCharged
    energyCar
    chargeSeconds
    wifiPower
    createdAt
  }
}
"""

_CHANGE_STATUS = """
mutation ChangeDeviceStatus($deviceId: ID!, $status: ChangeDeviceStatus!) {
  changeDeviceStatus(deviceId: $deviceId, status: $status) { device { id } }
}
"""

_EDIT_DEVICE = """
mutation EditDevice($id: ID!, $input: EditDeviceInput!) {
  editDevice(id: $id, input: $input) {
    __typename
    ... on EditDeviceError { message }
  }
}
"""

_NOTIFY_VIEWING = """
mutation NotifyViewing($id: ID!) {
  notifyDevice(id: $id, action: VIEWING) { device { id } }
}
"""


def _devices_query(count: int) -> str:
    """One query that reads several devices at once, with aliases d0, d1, ..."""
    params = ", ".join(f"$id{i}: ID!" for i in range(count))
    fields = "\n".join(
        f"  d{i}: device(id: $id{i}) {{ ...GrohmDevice }}" for i in range(count)
    )
    return f"query Devices({params}) {{\n{fields}\n}}\n{_DEVICE_FRAGMENT}"


class GrohmClient:
    """Client for the Grohm cloud.

    The API authenticates with a session cookie, so the client keeps the
    credentials and logs in again by itself when the session expires.

    Pass an `aiohttp.ClientSession` with a cookie jar of its own: the session
    cookie is the credential of the account, and must not be shared with
    other clients or accounts.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        *,
        api_url: str = API_URL,
        timeout: aiohttp.ClientTimeout = _DEFAULT_TIMEOUT,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._email = email
        self._password = password
        self._api_url = api_url
        self._timeout = timeout
        self._login_lock = asyncio.Lock()
        self.user: User | None = None
        """The user of the last successful login, or None."""

    # ----------------------------------------------------------- transport

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        # The backend is Django: send the CSRF token back when there is one.
        for cookie in self._session.cookie_jar:
            if cookie.key == "csrftoken" and cookie.value:
                headers["X-CSRFToken"] = cookie.value
                headers["Referer"] = self._api_url
        return headers

    async def _request(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send one GraphQL request and return its data."""
        try:
            async with self._session.post(
                self._api_url,
                json={"query": query, "variables": variables or {}},
                headers=self._headers(),
                timeout=self._timeout,
            ) as response:
                if response.status in (401, 403):
                    raise GrohmAuthenticationError(f"HTTP {response.status}")
                if response.status >= 400:
                    raise GrohmConnectionError(f"HTTP {response.status}")
                body = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GrohmConnectionError(str(err) or type(err).__name__) from err
        except ValueError as err:
            raise GrohmConnectionError("Invalid JSON in response") from err

        if not isinstance(body, dict):
            raise GrohmConnectionError("Unexpected response")
        data = body.get("data")
        errors = body.get("errors") or []
        if errors:
            message = "; ".join(str(error.get("message", error)) for error in errors)
            if _NOT_AUTHENTICATED in message.lower():
                raise GrohmAuthenticationError(message)
            # Data and errors together means partial data: keep it.
            if data is None:
                raise GrohmError(message)
            _LOGGER.debug("Partial GraphQL errors: %s", message)
        return data or {}

    async def _query(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Like `_request`, but logs in again once when the session expired."""
        if self.user is None:
            await self.login()
        try:
            return await self._request(query, variables)
        except GrohmAuthenticationError:
            _LOGGER.debug("Session expired, logging in again")
        await self.login()
        return await self._request(query, variables)

    # -------------------------------------------------------------- account

    async def login(self) -> User:
        """Log in, and return the user.

        Raises GrohmAuthenticationError when the credentials are rejected.
        """
        async with self._login_lock:
            self._session.cookie_jar.clear()
            data = await self._request(
                _LOGIN, {"email": self._email, "password": self._password}
            )
            payload = data.get("login") or {}
            if payload.get("__typename") != "LoginSuccess":
                self.user = None
                raise GrohmAuthenticationError(
                    payload.get("message") or "Invalid email or password"
                )
            self.user = User.from_api(payload["user"])
            return self.user

    async def logout(self) -> None:
        """End the session on the server. Errors are ignored."""
        if self.user is not None:
            with contextlib.suppress(GrohmError):
                await self._request(_LOGOUT)
        self.user = None
        self._session.cookie_jar.clear()

    async def get_user(self) -> User:
        """Return the current user, logging in again when the session expired."""
        if self.user is not None:
            try:
                data = await self._request(_ME)
            except GrohmAuthenticationError:
                data = {}
            if data.get("me"):
                self.user = User.from_api(data["me"])
                return self.user
        return await self.login()

    # -------------------------------------------------------------- devices

    async def _paged(self, query: str, field: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        for _ in range(_MAX_PAGES):
            data = await self._query(query, {"page": page, "first": _PAGE_SIZE})
            node = data.get(field) or {}
            items.extend(node.get("items") or [])
            page_info = node.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            page = int(page_info.get("nextPage") or page + 1)
        return items

    async def get_device_ids(self) -> list[str]:
        """IDs of every charger the account can see.

        Private accounts: their own chargers. Managers: the chargers of all
        their networks, plus the ones not in a network. Installers: the
        chargers they maintain.
        """
        user = await self.get_user()
        ids: list[str] = []
        if user.account_type is AccountType.MANAGER:
            for network in await self._paged(_MY_NETWORKS, "myNetworks"):
                ids.extend(str(device["id"]) for device in network.get("devices") or [])
            orphans = await self._paged(_DEVICES_OUT_OF_NETWORK, "devicesOutOfNet")
            ids.extend(str(device["id"]) for device in orphans)
        elif user.account_type is AccountType.INSTALLER:
            data = await self._query(_MAINTAINER_DEVICES)
            ids.extend(
                str(device["id"]) for device in data.get("maintainerDevices") or []
            )
        else:
            devices = await self._paged(_MY_DEVICES, "myDevices")
            ids.extend(str(device["id"]) for device in devices)
        return list(dict.fromkeys(ids))

    async def get_devices(self, device_ids: list[str]) -> dict[str, Device]:
        """Full state of the given chargers, in one request.

        Chargers the account cannot read are left out.
        """
        if not device_ids:
            return {}
        query = _devices_query(len(device_ids))
        variables = {f"id{i}": device_id for i, device_id in enumerate(device_ids)}
        data = await self._query(query, variables)
        devices: dict[str, Device] = {}
        for i, device_id in enumerate(device_ids):
            if raw := data.get(f"d{i}"):
                devices[device_id] = Device.from_api(raw)
        return devices

    async def get_device(self, device_id: str) -> Device | None:
        """Full state of one charger."""
        return (await self.get_devices([device_id])).get(device_id)

    # ------------------------------------------------------------- commands

    async def change_status(self, device_id: str, command: StatusCommand) -> None:
        """Pause, run or resume charging. See `ChargingSession.command_to`."""
        await self._query(
            _CHANGE_STATUS, {"deviceId": device_id, "status": command.value}
        )

    async def _edit_device(self, device_id: str, **fields: Any) -> None:
        data = await self._query(_EDIT_DEVICE, {"id": device_id, "input": fields})
        payload = data.get("editDevice") or {}
        if payload.get("__typename") == "EditDeviceError":
            raise GrohmCommandError(payload.get("message") or "Edit refused")

    async def set_locked(self, device_id: str, locked: bool) -> None:
        """Lock or unlock a charger. A locked charger does not charge."""
        await self._edit_device(device_id, locked=locked)

    async def force_charge(self, device_id: str) -> None:
        """Start charging without an RFID card. Manager accounts only.

        The charger keeps `force_charge` on for 300 seconds.
        """
        await self._edit_device(device_id, forceCharge=True)

    async def notify_viewing(self, device_id: str) -> None:
        """Ask the charger to report its state more often for a while."""
        await self._query(_NOTIFY_VIEWING, {"id": device_id})
