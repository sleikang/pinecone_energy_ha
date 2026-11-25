#!/usr/bin/env python3
"""Read-only probe that mimics the coordinator snapshot for one/all piles.

Prints exactly the values the Home Assistant entities would expose, so the
field mapping can be verified against the real cloud payloads.

Usage: python3 tools/pinecone_probe.py [pileCode]
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import aiohttp

API_FILE = Path(__file__).resolve().parents[1] / "custom_components" / "pinecone_energy" / "api.py"
spec = importlib.util.spec_from_file_location("pinecone_api", API_FILE)
m = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(m)


def pile_type(detail: dict, relation: dict) -> str:
    equipment = detail.get("equipmentType") or relation.get("equipmentType")
    if isinstance(equipment, dict):
        if equipment.get("code") == 2:
            return "ac"
        if equipment.get("code") == 1:
            return "dc"
    guns = detail.get("guns")
    if isinstance(guns, list) and guns and isinstance(guns[0], dict):
        if "慢充" in str(guns[0].get("gunType") or ""):
            return "ac"
    return "dc"


def flag(value):
    if value is None:
        return None
    try:
        num = int(value)
    except (TypeError, ValueError):
        return None
    if num == 255:
        return None
    return num in (1, 170)


async def probe(pile: str | None) -> None:
    token = json.load(open(Path.home() / ".pinecone_energy.json"))
    async with aiohttp.ClientSession() as session:
        client = m.PineconeClient(session, token["token"], token["phone"])
        devices = await client.list_devices()
        if pile:
            devices = [d for d in devices if d.get("pileCode") == pile]
        for dev in devices:
            code = dev["pileCode"]
            detail = await client.pile_detail(code)
            gun_code, gun_text = await client.gun_status(code)
            charging = m.PineconeClient.normalise_charging(await client.current_charging(code))
            signal = await client.signal_level(code)
            plan = m.PineconeClient.normalise_plan(await client.get_plan(code))
            strategy = await client.ac_current_limit(code)
            stats = await client.day_records(code, datetime.now().strftime("%Y-%m-%d"))
            kind = pile_type(detail, dev)

            node = strategy.get("data", strategy)
            if isinstance(node, list):
                node = node[0] if node else {}
            amps = node.get("parameter") if isinstance(node, dict) else None

            guns = detail.get("guns") if isinstance(detail.get("guns"), list) else []
            rated = guns[0].get("ratedCurrent") if guns and isinstance(guns[0], dict) else None

            print(f"\n=== {code} | {dev.get('pileName')} | {detail.get('pileName')} | type={kind}")
            print(f"在线: {dev.get('online')}  桩状态: {dev.get('pileStatus')}")
            print(f"状态: {gun_text} (code={gun_code}) / 映射={m.GUN_STATUS_4G.get(gun_code)}")
            print(
                f"功率={charging['power']} 电量={charging['energy']} 时长={charging['duration']} "
                f"SOC={charging['soc'] or detail.get('soc')} 电压={charging['voltage']} 电流={charging['current']}"
            )
            print(f"信号={signal} 计划={plan}")
            print(f"电流上限 parameter={amps} ratedCurrent={rated}")
            print(f"静音={flag(detail.get('silentMode'))} 智联续充={flag(detail.get('selfRecoveryStatus'))} "
                  f"即插即充={not bool(flag(detail.get('authStatus')))} authStatus={detail.get('authStatus')} "
                  f"plugAndChargeStatus={detail.get('plugAndChargeStatus')}")
            print(
                f"今日: 次数={stats.get('totalCount')} 电量={stats.get('totalElect')} "
                f"时长={stats.get('totalDurationMinutes')}"
            )
            print(f"面板: {[c.get('name') for c in (await client.panel_config(code)).get('cards', [])]}")


if __name__ == "__main__":
    import sys

    asyncio.run(probe(sys.argv[1] if len(sys.argv) > 1 else None))
