"""HTTP client for the 松果能源 (Pinecone Energy) charging-pile cloud API.

All endpoints, headers and payloads below were recovered from the official
Android app ``csg.pinecone`` v2.1.1 (uni-app bundle ``app-service.js``).
See ``docs/API提取报告.md`` for the full reverse-engineering notes.

The cloud API is the only thing used here - no Bluetooth / BLE code paths.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_BASE = "https://sg.csgkeji.com.cn:2408/pinecone-mobile/"

# Business codes returned in the JSON envelope.
CODE_NOT_LOGGED_IN = "030400"

GUN_STATUS_4G: dict[int, str] = {
    0: "空闲中",
    1: "已连接",
    2: "充电中",
    3: "预约充电中",
    4: "故障",
    5: "已离线",
    6: "等待充电中",
    7: "充电完成",
    9: "已充满",
    10: "充电暂停",
    11: "车辆预约中",
    13: "升级中",
    14: "待插枪",
    15: "充电模组已取下",
}

# Charging states where a live "current charging" payload is meaningful.
ACTIVE_STATES = {1, 2, 6, 7, 9, 10, 11, 14}


class PineconeError(Exception):
    """Base error for the Pinecone cloud API."""


class PineconeAuthError(PineconeError):
    """Token missing / expired."""


class PineconeApiError(PineconeError):
    """Business level error (envelope success == False)."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def _first(data: Any, *names: str, default: Any = None) -> Any:
    """Return the first non-empty value for the given (dotted) key names."""
    if not isinstance(data, dict):
        return default
    for name in names:
        value: Any = data
        for part in name.split("."):
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(part)
        if value is not None and value != "":
            return value
    return default


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _unwrap(payload: Any) -> Any:
    """Unwrap the nested ``{success, data:{success, data}}`` envelopes."""
    node = payload
    for _ in range(4):
        if isinstance(node, dict) and "data" in node and isinstance(node["data"], (dict, list)):
            inner = node["data"]
            if isinstance(inner, dict) and ("success" in inner or "code" in inner):
                node = inner
                continue
        if isinstance(node, dict) and isinstance(node.get("data"), dict) and "operateResult" in node["data"]:
            node = node["data"]
            continue
        break
    return node


def extract_operation_result(payload: Any) -> dict[str, Any] | None:
    """Return the deepest ``operateResult`` reported by the pile."""
    stack = [payload]
    seen = 0
    while stack and seen < 8:
        node = stack.pop()
        seen += 1
        if isinstance(node, dict):
            if isinstance(node.get("operateResult"), dict):
                return node["operateResult"]
            for key in ("data", "result", "status"):
                if isinstance(node.get(key), (dict, list)):
                    stack.append(node[key])
        elif isinstance(node, list):
            stack.extend(node)
    return None


def assert_operation_ok(payload: Any, message: str) -> None:
    """Raise PineconeError when the envelope or the operateResult failed."""
    if isinstance(payload, dict) and payload.get("success") is False:
        raise PineconeApiError(str(payload.get("msg") or message), payload.get("code"))
    inner = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(inner, dict) and inner.get("success") is False:
        raise PineconeApiError(str(inner.get("msg") or message))
    result = extract_operation_result(payload)
    if result is not None and result.get("success") is False:
        raise PineconeApiError(str(result.get("msg") or message))


def _hhmm(value: Any) -> str:
    """Extract HH:MM from '2026-11-25 22:00:00' or '22:00'."""
    match = re.search(r"(?:[ T]|^)(\d{1,2}):(\d{2})", str(value or ""))
    if not match:
        return ""
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return ""
    return f"{hour:02d}:{minute:02d}"


def build_plan_window(
    repeat: str, start_hhmm: str, end_hhmm: str, now: datetime | None = None
) -> tuple[str, str]:
    """Reproduce the app's plan window computation (datetime strings)."""
    now = now or datetime.now()
    sh, sm = (int(x) for x in start_hhmm.split(":"))
    eh, em = (int(x) for x in end_hhmm.split(":"))
    if start_hhmm == end_hhmm:
        raise PineconeApiError("开始时间不能与结束时间相同")
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    if repeat.upper() == "ONCE" and start <= now:
        start += timedelta(days=1)
    end = start.replace(hour=eh, minute=em)
    if end <= start:
        end += timedelta(days=1)
    if repeat.upper() == "DAILY":
        end = start.replace(hour=eh, minute=em)
        if end <= start:
            end += timedelta(days=1)
    return (
        start.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
    )


class PineconeClient:
    """Small async wrapper around the recovered REST endpoints."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str | None = None,
        phone: str | None = None,
        timeout: int = 15,
    ) -> None:
        self._session = session
        self.token = token
        self.phone = phone
        self._timeout = timeout

    # ------------------------------------------------------------------ core
    async def _request(
        self,
        path: str,
        data: dict[str, Any] | None = None,
        method: str | None = None,
        *,
        auth: bool = True,
    ) -> dict[str, Any]:
        url = API_BASE + path.lstrip("/")
        if method is None:
            method = "POST" if data is not None else "GET"

        headers: dict[str, str] = {}
        if auth and self.token:
            # The app sends the raw token, not a Bearer token, plus the login phone.
            headers["Authorization"] = self.token
            if self.phone:
                headers["Phone"] = self.phone

        kwargs: dict[str, Any] = {"headers": headers, "timeout": self._timeout}
        if data is not None:
            kwargs["json"] = data

        try:
            async with self._session.request(method, url, **kwargs) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise PineconeApiError(
                        f"HTTP {resp.status}: {text[:180]}", code=str(resp.status)
                    )
                try:
                    payload = await resp.json(content_type=None)
                except ValueError as err:
                    raise PineconeApiError(f"响应不是 JSON: {text[:180]}") from err
        except aiohttp.ClientError as err:
            raise PineconeError(f"网络请求失败: {err}") from err
        except asyncio.TimeoutError as err:
            raise PineconeError("网络请求超时") from err

        if not isinstance(payload, dict):
            raise PineconeApiError("响应格式异常")

        code = str(payload.get("code") or "")
        if code == CODE_NOT_LOGGED_IN and auth:
            raise PineconeAuthError(str(payload.get("msg") or "登录状态已失效"))
        if payload.get("success") is False:
            raise PineconeApiError(str(payload.get("msg") or "请求失败"), code)
        return payload

    # ----------------------------------------------------------------- auth
    async def send_code(self, phone: str) -> None:
        payload = await self._request(
            f"phone/code/send?phone={phone}", auth=False, method="GET"
        )
        if payload.get("success") is False:
            raise PineconeApiError(str(payload.get("msg") or "验证码发送失败"))

    async def login_with_code(self, phone: str, code: str) -> dict[str, Any]:
        payload = await self._request(
            "login/phone",
            {
                "phone": phone,
                "phoneCode": code,
                "nickName": "松果果" + phone[-4:],
            },
            auth=False,
        )
        data = payload.get("data") or {}
        token = data.get("token")
        if not token:
            raise PineconeApiError(str(payload.get("msg") or "登录失败"))
        self.token = str(token)
        self.phone = phone
        return data

    async def logout(self) -> None:
        try:
            await self._request("login/logout")
        except PineconeError:
            _LOGGER.debug("logout failed, ignoring")

    async def user_info(self) -> dict[str, Any]:
        payload = await self._request("user/info")
        return payload.get("data") or {}

    # ---------------------------------------------------------------- devices
    async def list_devices(self) -> list[dict[str, Any]]:
        """Bound charging piles of the logged in account."""
        payload = await self._request("user/pile/relation/query")
        data = payload.get("data")
        items: list[dict[str, Any]] = []
        if isinstance(data, list):
            items = [d for d in data if isinstance(d, dict)]
        elif isinstance(data, dict):
            for key in ("resultSet", "records", "list", "rows", "data"):
                inner = data.get(key)
                if isinstance(inner, list):
                    items = [d for d in inner if isinstance(d, dict)]
                    break
            else:
                if data.get("pileCode"):
                    items = [data]
        return [self._normalise_device(item) for item in items]

    @staticmethod
    def _normalise_device(item: dict[str, Any]) -> dict[str, Any]:
        """Flatten the pile sub-object and guarantee a pileCode key."""
        merged: dict[str, Any] = dict(item)
        nested = item.get("pile")
        if isinstance(nested, dict):
            for key, value in nested.items():
                merged.setdefault(key, value)
        code = _first(merged, "pileCode", "deviceCode", "code", "sn", "pileNo")
        merged["pileCode"] = str(code).strip() if code else ""
        name = _first(merged, "pileName", "sharePileName", "name", "deviceName")
        if name:
            merged.setdefault("pileName", str(name))
        return merged

    async def pile_detail(self, pile_code: str) -> dict[str, Any]:
        payload = await self._request(f"pile/query/one?pileCode={pile_code}")
        # Real payload: {"success":true,"data":{...pile fields...}}
        node = payload.get("data")
        if isinstance(node, dict) and isinstance(node.get("data"), dict):
            node = node["data"]
        return node if isinstance(node, dict) else {}

    async def gun_status(self, pile_code: str) -> tuple[int | None, str]:
        """Raw gun status code (0..15) and the server provided status text."""
        payload = await self._request(f"pile/available/gun?gunCode={pile_code}01")
        node = payload.get("data")
        if isinstance(node, list):
            node = node[0] if node else None
        if isinstance(node, dict) and isinstance(node.get("data"), (dict, list)):
            node = node["data"]
            if isinstance(node, list):
                node = node[0] if node else None
        if not isinstance(node, dict):
            node = {}
        raw = _first(
            node,
            "gunStatus.code",
            "gunStatus",
            "status.code",
            "status",
            "statusCode",
            "chargingStatus.code",
            "chargingStatus",
            "gun.status.code",
            "gun.status",
            "gun.statusCode",
        )
        code = _to_int(raw)
        text = _first(node, "statusName", "statusText", "status.name", "gunStatus.name")
        if not text:
            text = GUN_STATUS_4G.get(code, "") if code is not None else ""
        return code, str(text or "")

    async def current_charging(self, pile_code: str) -> dict[str, Any]:
        payload = await self._request(f"pile/query/current/pile?gunCode={pile_code}01")
        inner = _unwrap(payload)
        return inner if isinstance(inner, dict) else {}

    async def signal_level(self, pile_code: str) -> int | None:
        payload = await self._request(f"pile/query/signal?pileCode={pile_code}")
        node = payload.get("data")
        if isinstance(node, dict) and isinstance(node.get("data"), dict):
            node = node["data"]
        if isinstance(node, dict):
            raw = _first(node, "signalBars", "signalLevel", "signal", "grade")
        else:
            raw = node
        level = _to_int(raw)
        if level is not None and 0 <= level <= 4:
            return level
        return None

    # ---------------------------------------------------------------- control
    async def start_charge(self, pile_code: str) -> dict[str, Any]:
        payload = await self._request("private/start/charge", {"pileCode": pile_code})
        assert_operation_ok(payload, "充电启动失败")
        return payload

    async def stop_charge(self, pile_code: str, order_code: str) -> dict[str, Any]:
        if not order_code:
            raise PineconeApiError("未获取到充电订单号，无法停止充电")
        payload = await self._request(
            f"private/netty/stop?pileCode={pile_code}&orderCode={order_code}", method="GET"
        )
        assert_operation_ok(payload, "停止充电失败")
        return payload

    async def unlock_gun(self, pile_code: str) -> dict[str, Any]:
        payload = await self._request(
            f"private/gun/unlock?pileCode={pile_code}&gunCode={pile_code}01", method="GET"
        )
        assert_operation_ok(payload, "充电枪解锁失败")
        return payload

    async def ac_current_limit(self, pile_code: str) -> dict[str, Any]:
        """AC pile current limit (private/query/charge/strategy)."""
        payload = await self._request(
            "private/query/charge/strategy", {"pileCode": pile_code}
        )
        return _unwrap(payload) if isinstance(_unwrap(payload), dict) else {}

    async def set_ac_current_limit(self, pile_code: str, amps: int) -> dict[str, Any]:
        payload = await self._request(
            "private/set/charge/strategy",
            {
                "pileCode": pile_code,
                "operatorSchema": 1,
                "chargingStrategy": {"code": 4, "msg": ""},
                "parameter": int(amps),
            },
        )
        assert_operation_ok(payload, "电流调节失败")
        return payload

    async def set_dc_power_limit(self, pile_code: str, percent: int) -> dict[str, Any]:
        """DC pile power limitation, expressed as a percentage of nominal power."""
        now = datetime.now()
        start = now.strftime("%Y-%m-%d 00:00:00")
        end = (now + timedelta(days=3650)).strftime("%Y-%m-%d 23:59:59")
        payload = await self._request(
            "private/power",
            {
                "pileCode": pile_code,
                "pileCodes": [pile_code],
                "percent": int(percent),
                "presentPowerStatus": True,
                "startTime": start,
                "endTime": end,
            },
        )
        assert_operation_ok(payload, "功率限制失败")
        return payload

    # ------------------------------------------------------------ scheduled
    async def get_plan(self, pile_code: str) -> dict[str, Any]:
        payload = await self._request(f"private/plan/query?pileCode={pile_code}", method="GET")
        inner = _unwrap(payload)
        return inner if isinstance(inner, dict) else {}

    async def set_plan(
        self,
        pile_code: str,
        start_time: str,
        end_time: str,
        enabled: bool,
        repeat: str = "DAILY",
    ) -> dict[str, Any]:
        payload = await self._request(
            "private/plan/issue",
            {
                "gunCode": f"{pile_code}01",
                "pileCode": pile_code,
                "startTime": start_time,
                "endTime": end_time,
                "operationType": 1 if enabled else 0,
                "isTrue": 0,
                "repeatType": "DAILY" if repeat.upper() == "DAILY" else "ONCE",
            },
        )
        assert_operation_ok(payload, "计划充电设置失败")
        return payload

    # ------------------------------------------------------------- toggles
    async def _set_feature(self, endpoint: str, pile_code: str, status: int) -> dict[str, Any]:
        payload = await self._request(
            endpoint, {"pileCode": pile_code, "status": status, "isTrue": 1}
        )
        assert_operation_ok(payload, "设置失败")
        return payload

    async def set_silent_mode(self, pile_code: str, enabled: bool) -> dict[str, Any]:
        return await self._set_feature(
            "private/set/silent/status", pile_code, 1 if enabled else 0
        )

    async def set_self_recovery(self, pile_code: str, enabled: bool) -> dict[str, Any]:
        return await self._set_feature(
            "private/set/self/recovery", pile_code, 1 if enabled else 0
        )

    async def set_plug_and_charge(self, pile_code: str, enabled: bool) -> dict[str, Any]:
        # authStatus == 1 means authentication required -> plug&charge is off.
        return await self._set_feature("private/set/auth", pile_code, 0 if enabled else 1)

    async def set_public_pile(self, pile_code: str, shared: bool) -> dict[str, Any]:
        payload = await self._request("pile/share", {"pileCode": pile_code, "shared": shared})
        assert_operation_ok(payload, "公共充电桩设置失败")
        return payload

    # ---------------------------------------------------------------- panel
    async def panel_config(self, pile_code: str) -> dict[str, Any]:
        payload = await self._request(f"panel/query/config?pileCode={pile_code}", method="GET")
        inner = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        return inner or {}

    async def save_panel_config(self, pile_code: str, codes: list[str]) -> dict[str, Any]:
        payload = await self._request(
            "panel/save/config", {"pileCode": pile_code, "selectedCodes": list(codes)}
        )
        assert_operation_ok(payload, "自定义面板保存失败")
        return payload

    # -------------------------------------------------------------- records
    async def day_records(self, pile_code: str, date: str, page: int = 1, size: int = 10) -> dict[str, Any]:
        payload = await self._request(
            "charge/record/day/page",
            {
                "condition": {"date": date, "pileCode": pile_code},
                "pageNum": max(1, int(page)),
                "pageSize": max(1, int(size)),
            },
        )
        data = payload.get("data") or {}
        return data if isinstance(data, dict) else {}

    async def stats(self, pile_code: str, date: str, period: str = "DAY", week: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "date": date,
            "periodType": period.upper(),
            "pileCode": pile_code,
        }
        if week is not None:
            body["weekOfYear"] = int(week)
        payload = await self._request("charge/record/stats", body)
        data = payload.get("data") or {}
        return data if isinstance(data, dict) else {}

    async def charging_chart(self, pile_code: str, order_code: str) -> dict[str, Any]:
        payload = await self._request(
            f"pile/charging/data/echarts?gunCode={pile_code}01&orderCode={order_code}", method="GET"
        )
        inner = _unwrap(payload)
        return inner if isinstance(inner, dict) else {}

    async def locate(self, pile_code: str) -> dict[str, Any]:
        payload = await self._request(f"pile/locate/get?pileCode={pile_code}", method="GET")
        inner = _unwrap(payload)
        return inner if isinstance(inner, dict) else {}

    # ------------------------------------------------------------ normalise
    @staticmethod
    def normalise_plan(raw: dict[str, Any]) -> dict[str, Any]:
        """Normalise private/plan/query payload to enabled/repeat/HH:MM."""
        node: Any = raw
        if isinstance(node, list):
            node = node[0] if node else {}
        if isinstance(node, dict):
            for key in ("resultSet", "records", "list", "content"):
                if isinstance(node.get(key), list):
                    node = node[key][0] if node[key] else {}
                    break
            if isinstance(node.get("plan"), dict):
                node = node["plan"]
            if isinstance(node.get("data"), dict):
                node = node["data"]
        if not isinstance(node, dict):
            node = {}
        start = _first(node, "startDateTime", "startTime")
        end = _first(node, "endDateTime", "endTime")
        raw_repeat = str(_first(node, "repeat", "repeatType", default="")).lower()
        repeat = "DAILY" if raw_repeat in ("daily", "everyday", "1") else "ONCE"
        enabled_raw = _first(node, "operationType", "isOpen", "enabled", "status", default=0)
        enabled = enabled_raw in (1, "1", True, "true", "open", "enabled")
        start_hhmm = _hhmm(start)
        end_hhmm = _hhmm(end)
        return {
            "enabled": bool(enabled),
            "repeat": repeat,
            "start": start_hhmm,
            "end": end_hhmm,
            "raw_start": start,
            "raw_end": end,
        }

    @staticmethod
    def normalise_charging(raw: dict[str, Any]) -> dict[str, Any]:
        """Flatten the several payload shapes used for live charging data."""
        node = raw.get("chargingData") if isinstance(raw.get("chargingData"), dict) else raw.get("data", raw)
        if not isinstance(node, dict):
            node = {}
        energy = _first(node, "energyKwh", "energy")
        duration = _first(node, "length", "chargeDuration", "durationMinutes")
        power = _first(node, "currentPower", "chargingPower", "pilePower", "power")
        soc = _first(node, "soc", "bmsCurrentSoc")
        voltage = _first(node, "voltage", "currentVoltage")
        current = _first(node, "current", "currentAmpere")
        order = _first(node, "orderCode", "order.code")
        start = _first(node, "startTime", "beginTime", "chargeStartTime")
        return {
            "energy": _to_float(energy),
            "duration": _to_float(duration),
            "power": _to_float(power),
            "soc": _to_float(soc),
            "voltage": _to_float(voltage),
            "current": _to_float(current),
            "order_code": str(order) if order else "",
            "start_time": str(start) if start else "",
        }
