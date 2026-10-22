"""Web routes: control."""

from __future__ import annotations

from ..context import state

from dataclasses import asdict

from fastapi import APIRouter, HTTPException

from pydantic import BaseModel

from datetime import datetime

from ..control import DEFAULT_CHARGE_TARGET, DEFAULT_PACE_MARGIN, ControlSettings, Controller, Schedule, happy_slots

router = APIRouter()


class ScheduleIn(BaseModel):
    action: str = "charge"
    start: str = "00:30"
    end: str = "05:30"
    days: list[int] = [0, 1, 2, 3, 4, 5, 6]
    power_w: int = 1500
    target_soc: int = DEFAULT_CHARGE_TARGET
    enabled: bool = True


class HappyHourIn(BaseModel):
    date: str
    start: str
    end: str


class ControlIn(BaseModel):
    enabled: bool = False
    hold_cheap: bool = True
    grid_charge: bool = False
    charge_power_w: int = 1500
    charge_target_soc: int = DEFAULT_CHARGE_TARGET
    charge_dispatch: bool = True
    charge_happy: bool = True
    auto_balance: bool = False
    balance_days: int = 28
    pace_charge: bool = False
    pace_margin_min: int = DEFAULT_PACE_MARGIN
    happy_hours: list[HappyHourIn] = []
    schedules: list[ScheduleIn] = []


class ModeIn(BaseModel):
    mode: int


class LimitsIn(BaseModel):
    charging_limit_soc: int | None = None
    discharge_limit_soc: int | None = None


class OverrideIn(BaseModel):
    action: str  # charge, hold, discharge, or off to cancel
    minutes: int = 60
    power_w: int | None = None
    target_soc: int | None = None


@router.get("/api/control")
def get_control() -> dict:
    return state.controller.status()


@router.post("/api/control")
async def save_control(body: ControlIn) -> dict:
    try:
        data = body.model_dump()
        data["schedules"] = tuple(Schedule.from_dict(x) for x in data["schedules"])
        settings = ControlSettings(**data).validate()
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    state.connection_store.save_section("control", asdict(settings))
    controller: Controller = state.controller
    storage = state.storage
    storage.set_free_slots(happy_slots(settings.happy_hours, storage.tz), datetime.now(storage.tz).date().isoformat())
    controller.update_settings(settings)
    await controller.tick()
    return controller.status()


@router.post("/api/control/override")
async def quick_override(body: OverrideIn) -> dict:
    """Charge, hold or discharge for the next while, whatever the schedules say; "off" cancels it."""
    controller: Controller = state.controller
    if body.action == "off":
        await controller.clear_override()
        return controller.status()
    if not state.collectors["battery"].connected:
        raise HTTPException(status_code=409, detail="The battery isn't connected")
    try:
        await controller.set_override(body.action, body.minutes, body.power_w, body.target_soc)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return controller.status()


@router.post("/api/control/mode")
async def set_battery_mode(body: ModeIn) -> dict:
    """Switch the battery to one of the Anker app's modes now."""
    controller: Controller = state.controller
    battery = state.collectors["battery"]
    if not battery.connected:
        raise HTTPException(status_code=409, detail="The battery isn't connected")
    try:
        await controller.set_mode(body.mode)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    except Exception as err:
        raise HTTPException(status_code=502, detail=f"The battery didn't accept the change: {err}") from err
    return controller.status()


@router.post("/api/control/limits")
async def set_battery_limits(body: LimitsIn) -> dict:
    """Set the battery's own charge and discharge limits now."""
    controller: Controller = state.controller
    if not state.collectors["battery"].connected:
        raise HTTPException(status_code=409, detail="The battery isn't connected")
    limits = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        await controller.set_limits(limits)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    except Exception as err:
        raise HTTPException(status_code=502, detail=f"The battery didn't accept the change: {err}") from err
    return controller.status()
