"""Data models of the Grohm cloud API."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class AccountType(StrEnum):
    """Type of a Grohm account (`User.accountType`, a string on the wire)."""

    PRIVATE = "0"
    """Private owner of one or more chargers."""

    MANAGER = "1"
    """Fleet manager: owns chargers grouped in networks, with RFID cards."""

    INSTALLER = "2"
    """Installer: read-only access to the chargers of its customers."""


class ChargerStatus(StrEnum):
    """Status of the current charging session (`DeviceLogStatus`)."""

    READY = "READY"
    CHARGING = "CHARGING"
    FINISHED = "FINISHED"
    PAUSED = "PAUSED"
    WAITING = "WAITING"


class StatusCommand(StrEnum):
    """Values accepted by the `changeDeviceStatus` mutation."""

    PAUSE = "PAUSED"
    """Pause a session that is charging."""

    RUN = "RUNNING"
    """Resume a paused session."""

    RESUME = "RESUMED"
    """Start a session that is waiting. Does nothing on a paused session."""


# Commercial power ratings, per phase type: single phase 7.4 kW, three phase
# 11 kW and 22 kW.
_RATINGS_SINGLE_PHASE = (7.4,)
_RATINGS_THREE_PHASE = (11.0, 22.0)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class User:
    """The account that is logged in."""

    id: str
    email: str
    first_name: str | None
    last_name: str | None
    account_type: AccountType | None
    """None when the account type is not set yet, or unknown."""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> User:
        """Create a user from the GraphQL `User` type."""
        raw_type = data.get("accountType")
        try:
            account_type = AccountType(str(raw_type)) if raw_type is not None else None
        except ValueError:
            account_type = None
        return cls(
            id=str(data["id"]),
            email=str(data.get("email") or ""),
            first_name=data.get("firstName") or None,
            last_name=data.get("lastName") or None,
            account_type=account_type,
        )


@dataclass(frozen=True, slots=True)
class ChargingSession:
    """Live data of the current session of a charger (`DeviceLog`)."""

    status: ChargerStatus | None
    voltage_mv: int | None
    max_current: int | None
    """Maximum current the charger allows, in A."""
    car_current: int | None
    """Current drawn by the car, in A (`maxCurrentCar`)."""
    energy_wh: int | None
    """Energy charged in this session, in Wh."""
    raw_power_w: int | None
    """Power reported by the charger (`energyCar`, a power despite its name)."""
    charge_seconds: int | None
    wifi_dbm: int | None
    created_at: datetime | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> ChargingSession:
        """Create a session from the GraphQL `DeviceLog` type."""
        try:
            status = ChargerStatus(data["status"]) if data.get("status") else None
        except ValueError:
            status = None
        return cls(
            status=status,
            voltage_mv=_to_int(data.get("voltage")),
            max_current=_to_int(data.get("maxCurrent")),
            car_current=_to_int(data.get("maxCurrentCar")),
            energy_wh=_to_int(data.get("wattHoursCharged")),
            raw_power_w=_to_int(data.get("energyCar")),
            charge_seconds=_to_int(data.get("chargeSeconds")),
            wifi_dbm=_to_int(data.get("wifiPower")),
            created_at=_to_datetime(data.get("createdAt")),
        )

    @property
    def voltage(self) -> float | None:
        """Voltage in V. The API sends millivolts, as a string."""
        return None if self.voltage_mv is None else self.voltage_mv / 1000

    @property
    def power(self) -> float | None:
        """Power delivered to the car, in W.

        When it is not charging, the charger keeps reporting a stale residual
        power (around 1500 W). Without current there is no power, so this
        returns 0 unless the status is charging and the car draws current.
        """
        if self.raw_power_w is None:
            return None
        if self.status is not ChargerStatus.CHARGING or self.car_current == 0:
            return 0.0
        return float(self.raw_power_w)

    @property
    def energy_kwh(self) -> float | None:
        """Energy charged in this session, in kWh."""
        return None if self.energy_wh is None else self.energy_wh / 1000

    @property
    def car_connected(self) -> bool | None:
        """Whether a car is plugged in, derived from the status."""
        if self.status is None:
            return None
        return self.status is not ChargerStatus.READY

    def command_to(self, charging: bool) -> StatusCommand | None:
        """Return the command that starts or stops charging, or None if not possible.

        Mirrors the official app: charging → pause, paused → run,
        waiting → resume.
        """
        if charging:
            return (
                {
                    ChargerStatus.PAUSED: StatusCommand.RUN,
                    ChargerStatus.WAITING: StatusCommand.RESUME,
                }.get(self.status)
                if self.status
                else None
            )
        return StatusCommand.PAUSE if self.status is ChargerStatus.CHARGING else None


@dataclass(frozen=True, slots=True)
class Device:
    """A Grohm charger."""

    id: str
    name: str
    serial_number: str
    """Formatted like a MAC address, AA:BB:CC:DD:EE:FF."""
    is_connected: bool
    locked: bool
    phase: str | None
    """"mono" or "tri"."""
    force_charge: bool
    """True for 300 seconds after a forced charge is started."""
    energy_price: float | None
    """Price per kWh set by the owner."""
    max_power_kw: float | None
    total_energy_kwh: float | None
    session: ChargingSession | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Device:
        """Create a device from the GraphQL `Device` type."""
        session = data.get("currentSession")
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or ""),
            serial_number=str(data.get("serialNumber") or ""),
            is_connected=bool(data.get("isConnected")),
            locked=bool(data.get("locked")),
            phase=data.get("fase"),
            force_charge=bool(data.get("forceCharge")),
            energy_price=_to_float(data.get("hourCost")),
            max_power_kw=_to_float(data.get("maxPowerKw")),
            total_energy_kwh=_to_float(data.get("totalKwhCharged")),
            session=ChargingSession.from_api(session) if session else None,
        )

    @property
    def three_phase(self) -> bool:
        """Whether the charger is three phase. Anything else counts as single phase."""
        return self.phase == "tri"

    @property
    def status(self) -> ChargerStatus | None:
        """Status of the current session."""
        return self.session.status if self.session else None

    @property
    def nominal_power_kw(self) -> float | None:
        """Rated power, snapped to the commercial rating (7.4, 11 or 22 kW)."""
        if self.max_power_kw is None:
            return None
        best, best_distance = self.max_power_kw, math.inf
        ratings = _RATINGS_THREE_PHASE if self.three_phase else _RATINGS_SINGLE_PHASE
        for rating in ratings:
            distance = abs(self.max_power_kw - rating)
            if distance < best_distance and distance <= 1.0:
                best, best_distance = rating, distance
        return best

    @property
    def charging_phases(self) -> int | None:
        """Phases the car actually uses (1 to 3), only for three phase chargers.

        Derived from the reported power: phases = P / (V_phase × I_car), with
        the measured voltage when plausible, else 230 V.
        """
        if not self.three_phase or self.session is None:
            return None
        power = self.session.power
        amps = self.session.car_current
        if not power or not amps or power <= 0 or amps <= 0:
            return None
        volts = self.session.voltage
        if volts is None or not 180 <= volts <= 270:
            volts = 230.0
        return max(1, min(3, round(power / (volts * amps))))
