"""Helpers for the scheduled charging (计划充电) feature."""

from __future__ import annotations

from typing import Any

from .api import PineconeClient, build_plan_window
from .coordinator import PineconeCoordinator

DEFAULT_START = "22:00"
DEFAULT_END = "08:00"
DEFAULT_REPEAT = "DAILY"


def current_plan(coordinator: PineconeCoordinator, code: str) -> dict[str, Any]:
    plan = (coordinator.details(code).get("plan") or {}) if coordinator.data else {}
    return {
        "enabled": bool(plan.get("enabled")),
        "start": plan.get("start") or DEFAULT_START,
        "end": plan.get("end") or DEFAULT_END,
        "repeat": plan.get("repeat") or DEFAULT_REPEAT,
    }


async def async_apply_plan(
    coordinator: PineconeCoordinator,
    code: str,
    *,
    enabled: bool | None = None,
    start: str | None = None,
    end: str | None = None,
    repeat: str | None = None,
) -> None:
    plan = current_plan(coordinator, code)
    new_enabled = plan["enabled"] if enabled is None else enabled
    new_start = start or plan["start"]
    new_end = end or plan["end"]
    new_repeat = (repeat or plan["repeat"]).upper()

    if new_start == new_end:
        raise ValueError("开始时间不能与结束时间相同")

    start_dt, end_dt = build_plan_window(new_repeat, new_start, new_end)
    await coordinator.client.set_plan(
        code, start_dt, end_dt, new_enabled, repeat=new_repeat
    )
    await coordinator.async_request_refresh()
