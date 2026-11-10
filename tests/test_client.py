"""Tests of the client against the fake server."""

from __future__ import annotations

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from grohm import (
    AccountType,
    ChargerStatus,
    GrohmAuthenticationError,
    GrohmClient,
    GrohmCommandError,
    GrohmConnectionError,
    GrohmError,
    StatusCommand,
)

from .conftest import FakeBackend


async def test_login(client: GrohmClient) -> None:
    user = await client.login()
    assert user.id == "42"
    assert user.account_type is AccountType.PRIVATE
    assert user.last_name is None
    assert client.user == user


async def test_login_wrong_password(client: GrohmClient, backend: FakeBackend) -> None:
    backend.password = "other"
    with pytest.raises(GrohmAuthenticationError, match="Invalid email or password"):
        await client.login()
    assert client.user is None


@pytest.mark.parametrize("raw", [None, "", "9"])
async def test_unknown_account_type(
    client: GrohmClient, backend: FakeBackend, raw: str | None
) -> None:
    backend.account_type = raw
    assert (await client.login()).account_type is None


async def test_device_ids_private(client: GrohmClient) -> None:
    assert await client.get_device_ids() == ["1", "2"]


async def test_device_ids_manager(client: GrohmClient, backend: FakeBackend) -> None:
    backend.account_type = "1"
    assert await client.get_device_ids() == ["1", "3", "2"]


async def test_device_ids_installer(client: GrohmClient, backend: FakeBackend) -> None:
    backend.account_type = "2"
    assert await client.get_device_ids() == ["3"]


async def test_device_ids_paginated(
    client: GrohmClient, backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("grohm.client._PAGE_SIZE", 1)
    backend.private_ids = ["1", "2", "3", "1"]
    assert await client.get_device_ids() == ["1", "2", "3"]


async def test_get_devices(client: GrohmClient, backend: FakeBackend) -> None:
    devices = await client.get_devices(["1", "2", "missing"])
    assert set(devices) == {"1", "2"}
    device = devices["1"]
    assert device.name == "Charger 1"
    assert device.status is ChargerStatus.CHARGING
    assert device.session is not None
    assert device.session.voltage == 231.0
    assert device.session.power == 22150
    assert await client.get_devices([]) == {}
    assert (await client.get_device("2")) is not None


async def test_relogin_after_expired_session(
    client: GrohmClient, backend: FakeBackend
) -> None:
    await client.login()
    client._session.cookie_jar.clear()  # the server forgot the session
    assert "1" in await client.get_devices(["1"])
    assert [op for op, _ in backend.operations].count("Login") == 2


async def test_relogin_fails_with_new_password(
    client: GrohmClient, backend: FakeBackend
) -> None:
    await client.login()
    client._session.cookie_jar.clear()
    backend.password = "changed"
    with pytest.raises(GrohmAuthenticationError):
        await client.get_devices(["1"])


async def test_get_user_renews_session(
    client: GrohmClient, backend: FakeBackend
) -> None:
    await client.login()
    client._session.cookie_jar.clear()
    assert (await client.get_user()).id == "42"
    assert [op for op, _ in backend.operations] == ["Login", "Me", "Login"]


async def test_commands(client: GrohmClient, backend: FakeBackend) -> None:
    await client.set_locked("1", True)
    assert backend.devices["1"]["locked"] is True
    await client.force_charge("1")
    assert backend.devices["1"]["forceCharge"] is True
    await client.change_status("1", StatusCommand.PAUSE)
    assert backend.operations[-1] == (
        "ChangeDeviceStatus",
        {"deviceId": "1", "status": "PAUSED"},
    )
    await client.notify_viewing("1")
    assert backend.operations[-1][0] == "NotifyViewing"


async def test_command_refused(client: GrohmClient, backend: FakeBackend) -> None:
    backend.edit_error = "Not allowed"
    with pytest.raises(GrohmCommandError, match="Not allowed"):
        await client.force_charge("1")


async def test_logout(client: GrohmClient, backend: FakeBackend) -> None:
    await client.logout()  # not logged in: nothing to do
    assert backend.operations == []
    await client.login()
    await client.logout()
    assert client.user is None
    assert backend.operations[-1][0] == "Logout"


async def test_server_error(client: GrohmClient, backend: FakeBackend) -> None:
    backend.http_status = 504
    with pytest.raises(GrohmConnectionError, match="504"):
        await client.login()


async def test_http_auth_error(client: GrohmClient, backend: FakeBackend) -> None:
    backend.http_status = 403
    with pytest.raises(GrohmAuthenticationError):
        await client.login()


async def test_unreachable() -> None:
    async with ClientSession() as session:
        client = GrohmClient(session, "a", "b", api_url="http://localhost:1/graphql")
        with pytest.raises(GrohmConnectionError):
            await client.login()


async def test_get_user_with_valid_session(
    client: GrohmClient, backend: FakeBackend
) -> None:
    await client.login()
    backend.account_type = "1"  # the account type changed meanwhile
    assert (await client.get_user()).account_type is AccountType.MANAGER
    assert [op for op, _ in backend.operations] == ["Login", "Me"]


async def test_get_user_when_me_is_rejected(
    client: GrohmClient, backend: FakeBackend
) -> None:
    await client.login()
    backend.http_status = 401
    with pytest.raises(GrohmAuthenticationError):
        await client.get_user()


async def test_logout_ignores_errors(client: GrohmClient, backend: FakeBackend) -> None:
    await client.login()
    backend.http_status = 500
    await client.logout()
    assert client.user is None


@pytest.mark.parametrize(
    ("body", "content_type", "error"),
    [
        ("not json", "text/plain", GrohmConnectionError),
        ("[1, 2]", "application/json", GrohmConnectionError),
        (
            '{"data": null, "errors": [{"message": "Boom"}]}',
            "application/json",
            GrohmError,
        ),
    ],
)
async def test_invalid_responses(
    body: str, content_type: str, error: type[Exception]
) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(text=body, content_type=content_type)

    app = web.Application()
    app.router.add_post("/graphql", handler)
    server = TestServer(app, host="localhost")
    await server.start_server()
    try:
        async with ClientSession() as session:
            client = GrohmClient(
                session, "a", "b", api_url=str(server.make_url("/graphql"))
            )
            with pytest.raises(error):
                await client.login()
    finally:
        await server.close()


async def test_partial_errors_keep_data(
    client: GrohmClient, backend: FakeBackend
) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "data": {"login": {"__typename": "LoginError", "message": "x"}},
                "errors": [{"message": "minor"}],
            }
        )

    app = web.Application()
    app.router.add_post("/graphql", handler)
    server = TestServer(app, host="localhost")
    await server.start_server()
    try:
        async with ClientSession() as session:
            other = GrohmClient(
                session, "a", "b", api_url=str(server.make_url("/graphql"))
            )
            with pytest.raises(GrohmAuthenticationError, match="x"):
                await other.login()
    finally:
        await server.close()
