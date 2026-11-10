"""Tests of the data models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from grohm import ChargerStatus, ChargingSession, Device, StatusCommand

from .conftest import device_payload


def device(**overrides: Any) -> Device:
    return Device.from_api(device_payload("1", **overrides))


def session(**overrides: Any) -> ChargingSession:
    data = device_payload("1")["currentSession"]
    data.update(overrides)
    return ChargingSession.from_api(data)


def test_device_fields() -> None:
    d = device()
    assert d.serial_number == "AA:BB:CC:DD:EE:01"
    assert d.three_phase
    assert d.energy_price == 0.25
    assert d.total_energy_kwh == 1234.5
    assert d.session is not None
    assert d.session.created_at == datetime(2026, 11, 10, 8, tzinfo=UTC)
    assert d.session.energy_kwh == 5.4
    assert d.session.car_connected is True


def test_device_without_session() -> None:
    d = device(currentSession=None)
    assert d.session is None
    assert d.status is None
    assert d.charging_phases is None


@pytest.mark.parametrize(
    ("phase", "raw", "expected"),
    [
        ("tri", 22.2, 22.0),
        ("tri", 11.1, 11.0),
        ("mono", 7.36, 7.4),
        ("mono", 3.0, 3.0),
        ("tri", None, None),
    ],
)
def test_nominal_power(phase: str, raw: float | None, expected: float | None) -> None:
    assert device(fase=phase, maxPowerKw=raw).nominal_power_kw == expected


def test_power_is_zero_when_not_charging() -> None:
    assert session(status="READY", energyCar=1500).power == 0
    assert session(maxCurrentCar=0).power == 0
    assert session().power == 22150
    assert session(energyCar=None).power is None


def test_voltage_from_millivolts() -> None:
    assert session(voltage="231500").voltage == 231.5
    assert session(voltage="n/a").voltage is None


def test_unknown_values() -> None:
    s = session(status="EXPLODED", createdAt="yesterday")
    assert s.status is None
    assert s.created_at is None
    assert s.car_connected is None
    assert session(createdAt="2026-11-10T08:00:00").created_at == datetime(
        2026, 11, 10, 8, tzinfo=UTC
    )


@pytest.mark.parametrize(("power", "phases"), [(7360, 1), (14720, 2), (22080, 3)])
def test_charging_phases(power: int, phases: int) -> None:
    assert (
        device(
            currentSession={
                **device_payload("1")["currentSession"],
                "energyCar": power,
                "voltage": "230000",
            }
        ).charging_phases
        == phases
    )


def test_charging_phases_single_phase_or_idle() -> None:
    assert device(fase="mono").charging_phases is None
    idle = {**device_payload("1")["currentSession"], "status": "READY"}
    assert device(currentSession=idle).charging_phases is None
    odd_voltage = {
        **device_payload("1")["currentSession"],
        "voltage": "5000",
        "energyCar": 7360,
    }
    assert device(currentSession=odd_voltage).charging_phases == 1


@pytest.mark.parametrize(
    ("status", "charging", "command"),
    [
        ("CHARGING", False, StatusCommand.PAUSE),
        ("PAUSED", True, StatusCommand.RUN),
        ("WAITING", True, StatusCommand.RESUME),
        ("CHARGING", True, None),
        ("READY", True, None),
        ("PAUSED", False, None),
    ],
)
def test_command_to(status: str, charging: bool, command: StatusCommand | None) -> None:
    assert session(status=status).command_to(charging) is command


def test_command_without_status() -> None:
    assert ChargingSession.from_api({}).command_to(True) is None
    assert ChargingSession.from_api({}).status is None
    assert ChargerStatus("READY") is ChargerStatus.READY
