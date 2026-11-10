# python-grohm

Async Python client for [Grohm](https://grohm.it) EV chargers, through the
Grohm cloud API. It is the library behind the Grohm integration of
Home Assistant, and is maintained by Emme Esse S.p.A., the maker of Grohm.

## Install

```bash
pip install grohm
```

## Usage

The API authenticates with a session cookie. Give the client an
`aiohttp.ClientSession` with a cookie jar of its own, so the session of one
account is never shared. The client logs in again by itself when the session
expires.

```python
import asyncio

import aiohttp

from grohm import GrohmClient


async def main() -> None:
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        client = GrohmClient(session, "user@example.com", "password")
        user = await client.login()
        print(user.account_type)

        devices = await client.get_devices(await client.get_device_ids())
        for device in devices.values():
            session_data = device.session
            print(
                device.name,
                device.status,
                device.nominal_power_kw,
                session_data.power if session_data else None,
            )

        await client.logout()


asyncio.run(main())
```

### What it covers

- Log in, log out, and the current user, with its account type: private,
  manager or installer.
- The chargers of the account. For a manager, the chargers of all its
  networks, plus the ones outside a network. For an installer, the chargers
  it maintains.
- The state of many chargers in one request: status, power, current,
  voltage, energy of the session, total energy, Wi-Fi signal.
- Commands: pause and resume charging (`ChargingSession.command_to` picks the
  right command), lock and unlock, forced charge (manager accounts only).

### Notes on the data

- `ChargingSession.power` is 0 when the charger is not charging: the
  charger keeps reporting a stale residual power otherwise.
- `Device.nominal_power_kw` is the commercial rating: 7.4, 11 or 22 kW.
- `Device.charging_phases` is the number of phases the car uses, on three
  phase chargers while charging.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/mypy src
```

A release on GitHub publishes the package to PyPI, from the
`release.yml` workflow, with PyPI trusted publishing. The tag must match the
version in `pyproject.toml`.

## License

Apache License 2.0, see [LICENSE](LICENSE).

The license covers only the code in this repository. It does not
cover the Grohm cloud service and its API, the my-Grohm apps, the firmware of
the chargers, or any other software of Emme Esse S.p.A., and it grants no
right to use the Grohm trademarks. Access to the Grohm cloud is provided by
Emme Esse S.p.A., and may be changed, limited or discontinued at any time.

Grohm is a registered trademark of Emme Esse S.p.A.
