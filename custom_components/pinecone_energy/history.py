"""Rolling day/week/month/year energy history for 松果能源 charging piles.

The cloud only exposes the *current* period totals (today / this week / this
month / this year), so the integration keeps its own cache:

* every coordinator refresh upserts the current day/week/month/year total;
* on start-up and once per day the cloud ``chartBuckets`` of
  ``charge/record/stats`` are harvested to back-fill periods the cache has
  never seen (months from the yearly buckets, days from the monthly ones,
  weeks summed from the daily ones);
* everything is stored in ``.storage`` so it survives restarts, and the four
  energy sensors expose it through their ``history`` attribute.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_HISTORY_DAYS,
    DOMAIN,
    MAX_HISTORY_DAYS,
    MIN_HISTORY_DAYS,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
SAVE_DELAY = 30

PERIODS = ("day", "week", "month", "year")

#: Attribute name used for each period inside the ``history`` list.
KEY_FIELD: dict[str, str] = {
    "day": "date",
    "week": "week",
    "month": "month",
    "year": "year",
}

#: How many rows each sensor exposes; the cache itself keeps the whole period.
#: This keeps the state attributes (and the recorder database) small.
ATTRIBUTE_ROWS: dict[str, int] = {
    "day": 120,
    "week": 104,
    "month": 60,
    "year": 20,
}


def _num(value: Any) -> float | None:
    """Round to three decimals, tolerating strings and empty values."""
    try:
        if value is None or value == "":
            return None
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _count(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return round(float(value))
    except (TypeError, ValueError):
        return None


def _parse_day(value: Any) -> date | None:
    """Parse ``2026-11-28`` / ``2026-11`` / ``2026`` into a date."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if len(text) < 4:
        return None
    try:
        year = int(text[:4])
        month = int(text[5:7]) if len(text) >= 7 else 1
        day = int(text[8:10]) if len(text) >= 10 else 1
        return date(year, month, day)
    except ValueError:
        return None


def iso_week_key(day: date) -> str:
    """``2026-W48`` for the ISO week containing ``day``."""
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def week_bounds(day: date) -> tuple[str, str]:
    """Monday and Sunday (ISO strings) of the week containing ``day``."""
    monday = day - timedelta(days=day.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def week_bounds_from_key(key: str) -> tuple[date, date] | None:
    """Monday / Sunday of an ``2026-W48`` key."""
    try:
        year_text, week_text = key.split("-W")
        monday = date.fromisocalendar(int(year_text), int(week_text), 1)
    except (ValueError, AttributeError):
        return None
    return monday, monday + timedelta(days=6)


def current_key(period: str, now: datetime | None = None) -> str:
    """Cache key of the period that is running right now."""
    now = now or dt_util.now()
    today = now.date()
    if period == "day":
        return today.isoformat()
    if period == "week":
        return iso_week_key(today)
    if period == "month":
        return now.strftime("%Y-%m")
    return str(today.year)


def stats_of(source: Any) -> dict[str, Any]:
    """Map a coordinator period payload to (energy, count, duration)."""
    if not isinstance(source, dict):
        return {}
    return {
        "energy": source.get("total_energy", source.get("energy")),
        "count": source.get("total_count", source.get("count")),
        "duration": source.get("total_duration", source.get("duration")),
    }


class PineconeHistory:
    """Persistent per-pile period cache, shared by all sensors."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        retention_days: int = DEFAULT_HISTORY_DAYS,
    ) -> None:
        self.hass = hass
        self.retention_days = _clamp_days(retention_days)
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.history")
        self._piles: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ lifecycle
    async def async_load(self) -> None:
        """Load the cache from disk and drop entries outside the window."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            piles = stored.get("piles")
            if isinstance(piles, dict):
                self._piles = {
                    str(code): node
                    for code, node in piles.items()
                    if isinstance(node, dict)
                }
        # ``retention_days`` comes from the config entry option, never from the
        # file: shrinking / growing the window stays under the user's control.
        self._prune()
        _LOGGER.debug(
            "历史缓存已载入：%s 台桩，周期 %s 天",
            len(self._piles),
            self.retention_days,
        )

    async def async_save(self) -> None:
        """Write the cache now."""
        await self._store.async_save(self._payload())

    def _payload(self) -> dict[str, Any]:
        return {"retention_days": self.retention_days, "piles": self._piles}

    def _schedule_save(self) -> None:
        """Debounce writes; the store flushes on HA shutdown as well."""
        self._store.async_delay_save(self._payload, SAVE_DELAY)

    # --------------------------------------------------------------- writing
    def _pile(self, code: str) -> dict[str, Any]:
        pile = self._piles.setdefault(code, {})
        for period in PERIODS:
            if not isinstance(pile.get(period), dict):
                pile[period] = {}
        if not isinstance(pile.get("backfill"), dict):
            pile["backfill"] = {}
        return pile

    def record(
        self,
        code: str,
        period: str,
        key: str | int,
        *,
        energy: Any = None,
        count: Any = None,
        duration: Any = None,
        extra: dict[str, Any] | None = None,
        source: str = "poll",
    ) -> None:
        """Upsert one period bucket; unchanged values are not touched.

        Skipping no-op writes keeps the ``updated_at`` stamp stable, so an
        idle pile produces no state writes at all.
        """
        if period not in PERIODS or key in (None, ""):
            return
        key = str(key)
        energy_v = _num(energy)
        count_v = _count(count)
        duration_v = _num(duration)
        if energy_v is None and count_v is None and duration_v is None:
            return

        pile = self._pile(code)
        bucket = pile.setdefault(period, {})
        entry = dict(bucket.get(key) or {})
        changed = not entry
        for field, value in (
            ("energy", energy_v),
            ("count", count_v),
            ("duration", duration_v),
        ):
            if value is not None and entry.get(field) != value:
                entry[field] = value
                changed = True
        for field, value in (extra or {}).items():
            if value is not None and entry.get(field) != value:
                entry[field] = value
                changed = True
        if entry.get("source") != source:
            entry["source"] = source
            changed = True
        if not changed:
            return

        entry["updated_at"] = dt_util.now().isoformat(timespec="seconds")
        bucket[key] = entry
        pile["updated_at"] = entry["updated_at"]
        self._schedule_save()

    async def async_record_all(self, result: dict[str, dict[str, Any]]) -> None:
        """Store the current totals carried by one coordinator refresh."""
        now = dt_util.now()
        today = now.date()
        for code, data in result.items():
            self.record(
                code,
                "day",
                today.isoformat(),
                **stats_of(data.get("stats")),
                source="live",
            )
            self.record(
                code,
                "week",
                iso_week_key(today),
                **stats_of(data.get("week")),
                extra=dict(zip(("start", "end"), week_bounds(today))),
                source="live",
            )
            self.record(
                code,
                "month",
                now.strftime("%Y-%m"),
                **stats_of(data.get("month")),
                extra={"start": today.replace(day=1).isoformat()},
                source="live",
            )
            self.record(
                code,
                "year",
                str(today.year),
                **stats_of(data.get("year")),
                extra={
                    "start": f"{today.year}-01-01",
                    "end": f"{today.year}-12-31",
                },
                source="live",
            )
        self._prune(today)

    # --------------------------------------------------------------- reading
    def entries(self, code: str, period: str) -> list[dict[str, Any]]:
        """All cached buckets of one period, oldest first."""
        bucket = (self._piles.get(code) or {}).get(period) or {}
        now = dt_util.now()
        cutoff = now.date() - timedelta(days=self.retention_days)
        running = current_key(period, now)
        rows: list[dict[str, Any]] = []
        for key, entry in bucket.items():
            if not isinstance(entry, dict):
                continue
            # The period that is running right now is always part of the list.
            if key != running and not self._in_window(period, key, entry, cutoff):
                continue
            rows.append(self._row(period, key, entry))
        rows.sort(key=lambda row: str(row[KEY_FIELD[period]]))
        return rows

    @staticmethod
    def _row(period: str, key: str, entry: dict[str, Any]) -> dict[str, Any]:
        row: dict[str, Any] = {KEY_FIELD[period]: int(key) if period == "year" else key}
        for field in ("energy", "count", "duration", "start", "end"):
            if entry.get(field) is not None:
                row[field] = entry[field]
        for field in ("source", "updated_at"):
            if entry.get(field):
                row[field] = entry[field]
        return row

    @staticmethod
    def _period_start(period: str, key: str, entry: dict[str, Any]) -> date | None:
        if period == "day":
            return _parse_day(key)
        if period == "week":
            return _parse_day(entry.get("start")) or _parse_day(key)
        if period == "month":
            return _parse_day(f"{key}-01")
        return _parse_day(f"{key}-01-01")

    def _in_window(
        self, period: str, key: str, entry: dict[str, Any], cutoff: date
    ) -> bool:
        start = self._period_start(period, key, entry)
        return start is None or start >= cutoff

    def attributes(self, code: str, period: str) -> dict[str, Any]:
        """The ``history`` attribute payload for one sensor."""
        rows = self.entries(code, period)
        field = KEY_FIELD.get(period, "period")
        limit = ATTRIBUTE_ROWS.get(period, 30)
        total = len(rows)
        shown = rows[-limit:] if total > limit else rows
        payload: dict[str, Any] = {
            "history": shown,
            "history_count": total,
            "history_shown": len(shown),
            "cache_period_days": self.retention_days,
        }
        if total > len(shown):
            payload["history_truncated"] = True
        if not shown:
            return payload
        payload[f"oldest_{field}"] = shown[0][field]
        payload[f"newest_{field}"] = shown[-1][field]
        energies = [row["energy"] for row in shown if row.get("energy") is not None]
        if energies:
            payload["history_total_energy"] = round(sum(energies), 3)
        updated = (self._piles.get(code) or {}).get("updated_at")
        if updated:
            payload["updated_at"] = updated
        return payload

    def merged_energy(
        self, code: str, period: str, key: str | int
    ) -> float | None:
        """Energy already cached for one specific bucket."""
        entry = ((self._piles.get(code) or {}).get(period) or {}).get(str(key))
        if not isinstance(entry, dict):
            return None
        return _num(entry.get("energy"))

    # ------------------------------------------------------------- backfill
    def backfill_state(self, code: str) -> dict[str, Any]:
        """What the cloud harvest already covered for this pile."""
        return dict((self._piles.get(code) or {}).get("backfill") or {})

    def mark_backfill(
        self,
        code: str,
        *,
        through: date | None = None,
        years: tuple[int, ...] | list[int] = (),
        retention: int | None = None,
    ) -> None:
        node = self._pile(code)["backfill"]
        if through is not None:
            node["through"] = through.isoformat()
        if retention is not None:
            node["retention"] = int(retention)
        if years:
            existing = {int(y) for y in node.get("years") or []}
            existing.update(int(y) for y in years)
            node["years"] = sorted(existing)
        self._schedule_save()

    def has_history(self, code: str) -> bool:
        pile = self._piles.get(code) or {}
        return any(pile.get(period) for period in PERIODS)

    # ------------------------------------------------------------- retention
    def _prune(self, today: date | None = None) -> None:
        """Drop buckets older than the configured cache period."""
        today = today or dt_util.now().date()
        cutoff = today - timedelta(days=self.retention_days)
        for pile in self._piles.values():
            for period in PERIODS:
                bucket = pile.get(period)
                if not isinstance(bucket, dict):
                    continue
                running = current_key(period, dt_util.now())
                for key in list(bucket):
                    if key == running:
                        continue
                    entry = bucket[key]
                    if not isinstance(entry, dict) or not self._in_window(
                        period, key, entry, cutoff
                    ):
                        bucket.pop(key, None)


def _clamp_days(value: Any) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        return DEFAULT_HISTORY_DAYS
    return max(MIN_HISTORY_DAYS, min(MAX_HISTORY_DAYS, days))
