"""A fake Grohm GraphQL server, run locally with aiohttp."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp import ClientSession, CookieJar, web
from aiohttp.test_utils import TestServer

from grohm import GrohmClient

SESSION_COOKIE = "abc123"


def device_payload(device_id: str, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": device_id,
        "name": f"Charger {device_id}",
        "serialNumber": f"AA:BB:CC:DD:EE:0{device_id}",
        "isConnected": True,
        "locked": False,
        "fase": "tri",
        "forceCharge": False,
        "hourCost": 0.25,
        "maxPowerKw": 22.2,
        "totalKwhCharged": 1234.5,
        "currentSession": {
            "status": "CHARGING",
            "voltage": "231000",
            "maxCurrent": 32,
            "maxCurrentCar": 32,
            "wattHoursCharged": 5400,
            "energyCar": 22150,
            "chargeSeconds": 900,
            "wifiPower": -61,
            "createdAt": "2026-11-10T08:00:00+00:00",
        },
    }
    data.update(overrides)
    return data


@dataclass
class FakeBackend:
    """State of the fake server, changed by the tests."""

    password: str = "secret"
    account_type: str | None = "0"
    http_status: int = 200
    devices: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {i: device_payload(i) for i in ("1", "2", "3")}
    )
    private_ids: list[str] = field(default_factory=lambda: ["1", "2"])
    networks: list[list[str]] = field(default_factory=lambda: [["1"], ["3"]])
    orphan_ids: list[str] = field(default_factory=lambda: ["2"])
    maintained_ids: list[str] = field(default_factory=lambda: ["3"])
    page_size: int = 50
    edit_error: str | None = None
    operations: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def user(self) -> dict[str, Any]:
        return {
            "id": "42",
            "email": "user@example.com",
            "firstName": "Ada",
            "lastName": "",
            "accountType": self.account_type,
        }

    def page(self, ids: list[Any], variables: dict[str, Any]) -> dict[str, Any]:
        page, first = variables["page"], variables["first"]
        chunk = ids[(page - 1) * first : page * first]
        has_next = page * first < len(ids)
        return {
            "items": chunk,
            "pageInfo": {
                "hasNextPage": has_next,
                "nextPage": page + 1 if has_next else None,
            },
        }

    async def handle(self, request: web.Request) -> web.Response:
        if self.http_status != 200:
            return web.Response(status=self.http_status, text="<html>error</html>")
        body = await request.json()
        query: str = body["query"]
        variables: dict[str, Any] = body.get("variables") or {}
        operation = query.split("(")[0].split("{")[0].split()[1]
        self.operations.append((operation, variables))
        authenticated = request.cookies.get("sessionid") == SESSION_COOKIE

        if operation == "Login":
            if variables["password"] != self.password:
                return web.json_response(
                    {
                        "data": {
                            "login": {
                                "__typename": "LoginError",
                                "message": "Invalid email or password",
                            }
                        }
                    }
                )
            response = web.json_response(
                {"data": {"login": {"__typename": "LoginSuccess", "user": self.user()}}}
            )
            response.set_cookie("sessionid", SESSION_COOKIE, httponly=True)
            response.set_cookie("csrftoken", "token")
            return response
        if operation == "Logout":
            response = web.json_response({"data": {"logout": None}})
            response.del_cookie("sessionid")
            return response
        if operation == "Me":
            return web.json_response(
                {"data": {"me": self.user() if authenticated else None}}
            )
        if not authenticated:
            return web.json_response(
                {"data": None, "errors": [{"message": "User is not authenticated"}]}
            )
        assert request.headers.get("X-CSRFToken") == "token"

        data: dict[str, Any]
        if operation == "MyDevices":
            data = {
                "myDevices": self.page([{"id": i} for i in self.private_ids], variables)
            }
        elif operation == "MyNetworks":
            nets = [{"devices": [{"id": i} for i in ids]} for ids in self.networks]
            data = {"myNetworks": self.page(nets, variables)}
        elif operation == "DevicesOutOfNet":
            data = {
                "devicesOutOfNet": self.page(
                    [{"id": i} for i in self.orphan_ids], variables
                )
            }
        elif operation == "MaintainerDevices":
            data = {"maintainerDevices": [{"id": i} for i in self.maintained_ids]}
        elif operation == "Devices":
            data = {
                f"d{i}": copy.deepcopy(self.devices.get(variables[f"id{i}"]))
                for i in range(len(variables))
            }
        elif operation == "ChangeDeviceStatus":
            data = {"changeDeviceStatus": {"device": {"id": variables["deviceId"]}}}
        elif operation == "EditDevice":
            if self.edit_error:
                data = {
                    "editDevice": {
                        "__typename": "EditDeviceError",
                        "message": self.edit_error,
                    }
                }
            else:
                self.devices[variables["id"]].update(variables["input"])
                data = {"editDevice": {"__typename": "EditDeviceSuccess"}}
        elif operation == "NotifyViewing":
            data = {"notifyDevice": {"device": {"id": variables["id"]}}}
        else:
            return web.json_response({"data": None, "errors": [{"message": "Unknown"}]})
        return web.json_response({"data": data})


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
async def client(backend: FakeBackend) -> AsyncIterator[GrohmClient]:
    app = web.Application()
    app.router.add_post("/graphql", backend.handle)
    # aiohttp does not store cookies for IP addresses, so use a host name.
    server = TestServer(app, host="localhost")
    await server.start_server()
    async with ClientSession(cookie_jar=CookieJar()) as session:
        yield GrohmClient(
            session,
            "user@example.com",
            "secret",
            api_url=str(server.make_url("/graphql")),
        )
    await server.close()
