"""Data coordinator for 松果能源 charging piles."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    ACTIVE_STATES,
    GUN_STATUS_4G,
    PineconeAuthError,
    PineconeClient,
    PineconeError,
    _to_float,
)
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .history import (
    PineconeHistory,
    _parse_day,
    iso_week_key,
    week_bounds_from_key,
)

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


def _chart_buckets(raw: Any) -> list[dict[str, Any]]:
    """The per-day / per-month series of a charge/record/stats response."""
    if not isinstance(raw, dict):
        return []
    for key in ("chartBuckets", "echarts", "buckets"):
        value = raw.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _month_keys(start: date, end: date) -> list[str]:
    """``["2026-01", ... "2026-11"]`` for every month between the two dates."""
    keys: list[str] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        keys.append(cursor.strftime("%Y-%m"))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return keys


# Daily buckets are only harvested this many months back (weekly history follows
# them); monthly / yearly history still honours the configured cache period.
_HISTORY_MONTH_LIMIT = 24
# On incremental runs only re-read the months touched by the last few weeks.
_BACKFILL_GAP_DAYS = 40


class PineconeCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Poll the cloud for every configured charging pile."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: PineconeClient,
        pile_codes: list[str],
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
        config_entry: ConfigEntry | None = None,
        history: PineconeHistory | None = None,
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
        self.history = history or PineconeHistory(hass, "default")
        self._detail_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
        self._stats_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
        self._charging_cache: dict[str, dict[str, Any]] = {}
        self._signal_cache: dict[str, int | None] = {}
        self._plan_cache: dict[str, dict[str, Any]] = {}
        self._strategy_cache: dict[str, dict[str, Any]] = {}
        self._period_cache: dict[str, dict[str, tuple[str, datetime, dict[str, Any]]]] = {}
        self._week_fallback = False
        self._backfill_day: date | None = None
        self._backfill_running = False
        self.entry: Any = None
        self._tick = 0

    @property
    def devices(self) -> list[str]:
        return self.pile_codes

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            data = await self._fetch_all()
        except PineconeAuthError as err:
            # A newer login elsewhere (app / another client) invalidates this
            # token, so hand HA the reauth flow instead of a plain retry.
            _LOGGER.warning("登录状态已失效，触发重新登录流程: %s", err)
            if self.entry is not None:
                self.entry.async_start_reauth(self.hass)
            raise ConfigEntryAuthFailed(str(err)) from err

        # Cache today / this week / this month / this year before the entities
        # read their attributes, then hit the cloud for older periods once a day.
        await self.history.async_record_all(data)
        self._schedule_history_backfill()
        return data

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

    # ------------------------------------------------------ period history
    def _schedule_history_backfill(self) -> None:
        """Queue the daily cloud harvest; never blocks the poll itself."""
        today = dt_util.now().date()
        if self._backfill_day == today or self._backfill_running:
            return
        self._backfill_day = today
        coro = self.async_backfill_history()
        name = f"{DOMAIN} 历史回填"
        try:
            if self.config_entry is not None:
                self.config_entry.async_create_background_task(
                    self.hass, coro, name, eager_start=False
                )
            else:
                self.hass.async_create_background_task(
                    coro, name, eager_start=False
                )
        except (RuntimeError, AttributeError) as err:
            coro.close()
            _LOGGER.debug("无法启动历史回填任务: %s", err)

    async def async_backfill_history(self, force: bool = False) -> None:
        """Harvest cloud chart buckets so the sensors get a real history."""
        if self._backfill_running:
            return
        self._backfill_running = True
        try:
            now = dt_util.now()
            for code in self.pile_codes:
                try:
                    await self._backfill_pile(code, now, force=force)
                except PineconeAuthError:
                    _LOGGER.debug("历史回填中止：登录状态已失效")
                    break
                except PineconeError as err:
                    _LOGGER.debug("历史回填失败 %s: %s", code, err)
            if self.data:
                self.async_update_listeners()
        finally:
            self._backfill_running = False

    async def _backfill_pile(
        self, code: str, now: datetime, force: bool = False
    ) -> None:
        """One pile: yearly buckets -> months, monthly buckets -> days/weeks."""
        today = now.date()
        retention_start = today - timedelta(days=self.history.retention_days)
        state = self.history.backfill_state(code)
        through = _parse_day(state.get("through"))
        if through == today and not force and state.get("retention") == self.history.retention_days:
            return
        if state.get("retention") != self.history.retention_days:
            # Cache period changed: re-harvest so the new window is filled.
            through = None
            state = {}
        years_done = {int(y) for y in state.get("years") or []}
        this_month = now.strftime("%Y-%m")
        harvested: list[int] = []

        # YEAR buckets give the monthly series: month + year history in one call.
        for year in range(retention_start.year, today.year + 1):
            if through is not None and year != today.year and year in years_done:
                continue
            raw = await self._backfill_stats(code, str(year), "YEAR")
            if raw is None:
                continue
            buckets = _chart_buckets(raw)
            if buckets and not any(
                str(b.get("bucketKey") or "")[:4] == str(year) for b in buckets
            ):
                # The server answered with another year's buckets: it ignored the
                # ``date`` we sent, so neither the totals nor the series are
                # trustworthy for this year.
                _LOGGER.debug("历史回填 %s：%s 年的分桶不属于该年，跳过", code, year)
                continue
            harvested.append(year)
            self._record_totals(
                code,
                "year",
                str(year),
                raw,
                extra={"start": f"{year}-01-01", "end": f"{year}-12-31"},
            )
            for bucket in buckets:
                month_key = str(bucket.get("bucketKey") or "")[:7]
                if len(month_key) != 7 or month_key >= this_month:
                    continue
                self._record_bucket(
                    code,
                    "month",
                    month_key,
                    bucket,
                    extra={"start": f"{month_key}-01"},
                )

        # MONTH buckets give the daily series; weekly totals are summed from it.
        if through is not None and not force:
            day_start = max(
                retention_start, through - timedelta(days=_BACKFILL_GAP_DAYS)
            )
        else:
            day_start = retention_start
        for month_key in _month_keys(day_start, today)[-_HISTORY_MONTH_LIMIT:]:
            raw = await self._backfill_stats(code, month_key, "MONTH")
            if raw is None:
                continue
            buckets = _chart_buckets(raw)
            if buckets and not any(
                str(b.get("bucketKey") or "")[:7] == month_key for b in buckets
            ):
                # Same guard as the YEAR pass: a mismatched series means the
                # request's ``date`` was ignored, so skip this month entirely.
                _LOGGER.debug("历史回填 %s：%s 的分桶不属于该月，跳过", code, month_key)
                continue
            if month_key != this_month:
                self._record_totals(
                    code,
                    "month",
                    month_key,
                    raw,
                    extra={"start": f"{month_key}-01"},
                )
            for bucket in buckets:
                day_key = str(bucket.get("bucketKey") or "")[:10]
                if len(day_key) != 10 or day_key >= today.isoformat():
                    continue
                self._record_bucket(code, "day", day_key, bucket)

        self._rebuild_weeks(code, day_start, today)
        self.history.mark_backfill(
            code,
            through=today,
            years=harvested,
            retention=self.history.retention_days,
        )

    async def _backfill_stats(
        self, code: str, date_arg: str, period: str
    ) -> dict[str, Any] | None:
        """One stats call for the backfill, tolerating per-call failures."""
        try:
            raw = await self.client.stats(code, date_arg, period=period)
        except PineconeAuthError:
            raise
        except PineconeError as err:
            _LOGGER.debug("回填 %s %s 失败: %s", period, date_arg, err)
            return None
        await asyncio.sleep(0.1)  # stay friendly to the cloud API
        return raw

    def _record_totals(
        self,
        code: str,
        period: str,
        key: str,
        raw: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> None:
        totals = _period_totals(raw)
        if not totals:
            return
        energy = totals.get("total_energy") or 0.0
        count = totals.get("total_count") or 0.0
        if not energy and not count:
            return
        self.history.record(
            code,
            period,
            key,
            energy=energy,
            count=count,
            duration=totals.get("total_duration"),
            extra=extra,
            source="cloud",
        )

    def _record_bucket(
        self,
        code: str,
        period: str,
        key: str,
        bucket: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> None:
        energy = _to_float(bucket.get("elect"))
        count = _to_float(bucket.get("count"))
        duration = _to_float(bucket.get("durationMinutes"))
        if not energy and not count and not duration:
            return
        self.history.record(
            code,
            period,
            key,
            energy=energy,
            count=count,
            duration=duration,
            extra=extra,
            source="cloud",
        )

    def _rebuild_weeks(self, code: str, day_start: date, today: date) -> None:
        """Sum the harvested days into ISO weeks (the current week stays live)."""
        rows = self.history.entries(code, "day")
        current_week = iso_week_key(today)
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            day = _parse_day(row.get("date"))
            if day is None or day < day_start or day >= today:
                continue
            groups.setdefault(iso_week_key(day), []).append(row)
        for week, week_rows in groups.items():
            if week == current_week:
                continue
            bounds = week_bounds_from_key(week)
            if bounds is None:
                continue
            monday, sunday = bounds
            if monday < day_start or sunday >= today:
                continue
            energy = sum(row.get("energy") or 0.0 for row in week_rows)
            count = sum(row.get("count") or 0 for row in week_rows)
            duration = sum(row.get("duration") or 0.0 for row in week_rows)
            if not energy and not count:
                continue
            self.history.record(
                code,
                "week",
                week,
                energy=round(energy, 3),
                count=count,
                duration=duration,
                extra={"start": monday.isoformat(), "end": sunday.isoformat()},
                source="cloud",
            )

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
