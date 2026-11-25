"""Data coordinator for 松果能源 charging piles."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ACTIVE_STATES,
    GUN_STATUS_4G,
    PineconeAuthError,
    PineconeClient,
    PineconeError,
    _to_float,
)
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

_ENERGY_KEYS = (
    "totalElect",
    "elect",
    "chargeElect",
    "totalEnergy",
    "totalElectricity",
    "totalKwh",
    "energyKwh",
    "totalChargeElect",
    "energy",
    "kwh",
)
_COUNT_KEYS = ("totalCount", "totalNum", "orderCount", "count")
_DURATION_KEYS = ("totalDurationMinutes", "totalDuration", "durationMinutes", "totalMinutes")


def _pick(data: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = data.get(key)
        if value in (None, ""):
            continue
        number = _to_float(value)
        if number is not None:
            return number
    return None


def _period_totals(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise a charge/record/stats payload, whatever its shape."""
    if not isinstance(raw, dict):
        return {}

    energy = _pick(raw, _ENERGY_KEYS)
    count = _pick(raw, _COUNT_KEYS)
    duration = _pick(raw, _DURATION_KEYS)

    if energy is None:
        for key in ("data", "result", "body"):
            inner = raw.get(key)
            if isinstance(inner, dict):
                nested = _period_totals(inner)
                if nested:
                    return nested

    if energy is None:
        # Some stats responses only carry a per-day series.
        series = None
        for key in ("chartBuckets", "echarts", "list", "resultSet", "dataList", "rows", "data"):
            value = raw.get(key)
            if isinstance(value, list):
                series = value
                break
        if series:
            energy = 0.0
            count = 0.0
            duration = 0.0
            for item in series:
                if not isinstance(item, dict):
                    continue
                energy += _pick(item, _ENERGY_KEYS) or 0.0
                count += _pick(item, _COUNT_KEYS) or 0.0
                duration += _pick(item, _DURATION_KEYS) or 0.0

    if energy is None:
        return {}

    return {
        "total_energy": round(energy, 3),
        "total_count": count or 0.0,
        "total_duration": duration or 0.0,
    }



class PineconeCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Poll the cloud for every configured charging pile."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: PineconeClient,
        pile_codes: list[str],
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
        config_entry: ConfigEntry | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({client.phone})",
            update_interval=timedelta(seconds=max(10, scan_interval)),
            config_entry=config_entry,
        )
        self.client = client
        self.pile_codes = list(dict.fromkeys(pile_codes))
        self._detail_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
        self._stats_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
        self._charging_cache: dict[str, dict[str, Any]] = {}
        self._signal_cache: dict[str, int | None] = {}
        self._plan_cache: dict[str, dict[str, Any]] = {}
        self._strategy_cache: dict[str, dict[str, Any]] = {}
        self._period_cache: dict[str, dict[str, tuple[str, datetime, dict[str, Any]]]] = {}
        self._week_fallback = False
        self.entry: Any = None
        self._tick = 0

    @property
    def devices(self) -> list[str]:
        return self.pile_codes

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            return await self._fetch_all()
        except PineconeAuthError as err:
            # A newer login elsewhere (app / another client) invalidates this
            # token, so hand HA the reauth flow instead of a plain retry.
            _LOGGER.warning("登录状态已失效，触发重新登录流程: %s", err)
            if self.entry is not None:
                self.entry.async_start_reauth(self.hass)
            raise ConfigEntryAuthFailed(str(err)) from err

    async def _fetch_all(self) -> dict[str, dict[str, Any]]:
        self._tick += 1
        result: dict[str, dict[str, Any]] = {}
        try:
            devices = await self.client.list_devices()
        except PineconeAuthError:
            raise
        except PineconeError as err:
            raise UpdateFailed(str(err)) from err

        name_map = {}
        for item in devices:
            code = str(item.get("pileCode") or "").strip()
            if code:
                name_map[code] = item

        tasks = {}
        for code in self.pile_codes:
            tasks[code] = asyncio.gather(
                self._safe(self.client.gun_status(code), (None, "")),
                self._safe(self._detail(code), {}),
                self._safe(self._charging(code), {}),
                self._safe(self._signal(code), None),
                self._safe(self._today_stats(code), {}),
                self._safe(self._plan(code), {}),
                self._safe(self._strategy(code), {}),
                self._safe(self._period_stats(code, "WEEK"), {}),
                self._safe(self._period_stats(code, "MONTH"), {}),
                self._safe(self._period_stats(code, "YEAR"), {}),
            )
        gathered = await asyncio.gather(*tasks.values(), return_exceptions=False)
        for code, (status, detail, charging, signal, stats, plan, strategy, week, month, year) in zip(tasks, gathered):
            raw_code = status[0] if isinstance(status, tuple) else None
            status_text = status[1] if isinstance(status, tuple) else ""
            merged_charging = dict(charging or {})
            if not merged_charging.get("order_code"):
                merged_charging["order_code"] = self._charging_cache.get(code, {}).get("order_code", "")
            self._charging_cache[code] = merged_charging
            result[code] = {
                "pile_code": code,
                "relation": name_map.get(code, {}),
                "detail": detail or {},
                "status_code": raw_code,
                "status": status_text or GUN_STATUS_4G.get(raw_code, "未知"),
                "signal": signal,
                "charging": merged_charging,
                "stats": stats or {},
                "plan": plan or {},
                "strategy": strategy or {},
                "week": week or {},
                "month": month or {},
                "year": year or {},
                "online": raw_code is not None and raw_code != 5,
            }
        return result

    async def _safe(self, coro, default):
        try:
            return await coro
        except PineconeAuthError:
            raise
        except PineconeError as err:
            _LOGGER.debug("optional request failed: %s", err)
            return default

    async def _detail(self, code: str) -> dict[str, Any]:
        cached = self._detail_cache.get(code)
        if cached and datetime.now() - cached[0] < timedelta(minutes=10):
            return cached[1]
        detail = await self.client.pile_detail(code)
        self._detail_cache[code] = (datetime.now(), detail)
        return detail

    async def _charging(self, code: str) -> dict[str, Any]:
        try:
            raw = await self.client.current_charging(code)
        except PineconeError:
            return {}
        return PineconeClient.normalise_charging(raw)

    async def _signal(self, code: str) -> int | None:
        if self._tick % 3 != 0 and code in self._signal_cache:
            return self._signal_cache.get(code)
        try:
            level = await self.client.signal_level(code)
        except PineconeError:
            return self._signal_cache.get(code)
        self._signal_cache[code] = level
        return level

    async def _plan(self, code: str) -> dict[str, Any]:
        if self._tick % 6 != 0 and code in self._plan_cache:
            return self._plan_cache[code]
        try:
            raw = await self.client.get_plan(code)
        except PineconeError:
            return self._plan_cache.get(code, {})
        plan = PineconeClient.normalise_plan(raw)
        self._plan_cache[code] = plan
        return plan

    async def _strategy(self, code: str) -> dict[str, Any]:
        if code in self._strategy_cache and self._tick % 6 != 0:
            return self._strategy_cache[code]
        try:
            raw = await self.client.ac_current_limit(code)
        except PineconeError:
            return self._strategy_cache.get(code, {})
        self._strategy_cache[code] = raw
        return raw

    async def _today_stats(self, code: str) -> dict[str, Any]:
        """Today's totals.

        The cloud rejects periodType=DAY for charge/record/stats (verified
        online: MONTH/YEAR work, DAY/WEEK come back as 参数缺失), but the
        daily records pagination endpoint already returns the day's totals.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        cached = self._stats_cache.get(code)
        if cached and cached[1].get("_date") == today and datetime.now() - cached[0] < timedelta(minutes=5):
            return cached[1]
        try:
            raw = await self.client.day_records(code, today)
        except PineconeError:
            return cached[1] if cached else {}
        from .api import _to_float

        data = {
            "total_count": _to_float(raw.get("totalCount")),
            "total_energy": _to_float(raw.get("totalElect")),
            "total_duration": _to_float(raw.get("totalDurationMinutes")),
            "records": ((raw.get("page") or {}).get("resultSet") or []),
        }
        data["_date"] = today
        self._stats_cache[code] = (datetime.now(), data)
        return data

    async def _period_stats(self, code: str, period: str) -> dict[str, Any]:
        """Weekly / monthly / yearly energy totals.

        MONTH and YEAR come straight from charge/record/stats. WEEK is not
        accepted by the cloud (same 参数缺失 error as DAY), so it is summed
        from the seven daily record endpoints of the current ISO week.
        """
        now = datetime.now()
        if period == "WEEK":
            iso_year, iso_week, _ = now.isocalendar()
            cache_key = f"{iso_year}-W{iso_week:02d}"
            date_arg = now.strftime("%Y-%m-%d")
        elif period == "MONTH":
            cache_key = date_arg = now.strftime("%Y-%m")
        else:
            cache_key = date_arg = now.strftime("%Y")

        entry = (self._period_cache.get(code) or {}).get(period)
        if entry and entry[0] == cache_key and now - entry[1] < timedelta(minutes=15):
            return entry[2]

        data: dict[str, Any] = entry[2] if entry else {}
        try:
            if period == "WEEK" and self._week_fallback:
                raise PineconeError("week period unsupported, using records")
            raw = await self.client.stats(
                code,
                date_arg,
                period=period,
                week=now.isocalendar()[1] if period == "WEEK" else None,
            )
            data = _period_totals(raw)
        except PineconeError as err:
            _LOGGER.debug("stats %s failed for %s: %s", period, code, err)
            if period == "WEEK":
                self._week_fallback = True
                try:
                    data = await self._week_from_records(code, now)
                except PineconeError as err2:
                    _LOGGER.debug("week fallback failed for %s: %s", code, err2)
                    return entry[2] if entry else {}
            elif not data:
                return {}

        cache = self._period_cache.setdefault(code, {})
        cache[period] = (cache_key, datetime.now(), data)
        return data

    async def _week_from_records(self, code: str, now: datetime) -> dict[str, Any]:
        """Sum the daily totals of the current week (Monday..today)."""
        monday = now - timedelta(days=now.weekday())
        energy = count = duration = 0.0
        for offset in range(now.weekday() + 1):
            day = (monday + timedelta(days=offset)).strftime("%Y-%m-%d")
            raw = await self.client.day_records(code, day, size=1)
            energy += _to_float(raw.get("totalElect")) or 0.0
            count += _to_float(raw.get("totalCount")) or 0.0
            duration += _to_float(raw.get("totalDurationMinutes")) or 0.0
        return {
            "total_energy": round(energy, 3),
            "total_count": count,
            "total_duration": duration,
            "records": [],
            "_source": "day/page",
        }

    # ---------------------------------------------------------- helpers
    def details(self, code: str) -> dict[str, Any]:
        return (self.data or {}).get(code, {})

    def pile_type(self, code: str) -> str:
        """'ac' or 'dc'."""
        data = self.details(code)
        detail = data.get("detail") or {}
        relation = data.get("relation") or {}
        equipment = detail.get("equipmentType") or relation.get("equipmentType")
        if isinstance(equipment, dict):
            if equipment.get("code") == 2:
                return "ac"
            if equipment.get("code") == 1:
                return "dc"
        guns = detail.get("guns")
        if isinstance(guns, list) and guns and isinstance(guns[0], dict):
            gun_type = str(guns[0].get("gunType") or "")
            if "慢充" in gun_type:
                return "ac"
            if "快充" in gun_type:
                return "dc"
        raw = detail.get("pileType") or relation.get("pileType")
        try:
            num = int(raw)
        except (TypeError, ValueError):
            num = None
        if num == 10:
            return "ac"
        if num == 13:
            return "dc"
        return "dc"

    def device_name(self, code: str) -> str:
        data = self.details(code)
        relation = data.get("relation") or {}
        detail = data.get("detail") or {}
        for candidate in (
            relation.get("pileName"),
            relation.get("sharePileName"),
            detail.get("pileName"),
        ):
            if candidate:
                return str(candidate)
        return f"松果充电桩 {code[-4:]}"

    def is_charging(self, code: str) -> bool:
        data = self.details(code)
        return data.get("status_code") in (2, 6, 7, 9, 10, 11) or data.get("status") in (
            "充电中",
            "等待充电中",
            "充电完成",
            "已充满",
            "充电暂停",
        )
