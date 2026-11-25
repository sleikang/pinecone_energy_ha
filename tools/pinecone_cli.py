#!/usr/bin/env python3
"""Command line client for the 松果能源 cloud API.

Handy for verifying the recovered endpoints without Home Assistant.

Usage:
    python3 pinecone_cli.py send-code --phone 138xxxxxxxx
    python3 pinecone_cli.py login --phone 138xxxxxxxx --code 123456
    python3 pinecone_cli.py devices
    python3 pinecone_cli.py status --pile <pileCode>
    python3 pinecone_cli.py watch --pile <pileCode>
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path

import aiohttp

API_FILE = Path(__file__).resolve().parents[1] / "custom_components" / "pinecone_energy" / "api.py"
spec = importlib.util.spec_from_file_location("pinecone_api", API_FILE)
_module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(_module)

GUN_STATUS_4G = _module.GUN_STATUS_4G
PineconeClient = _module.PineconeClient
PineconeError = _module.PineconeError

TOKEN_FILE = Path(os.path.expanduser("~/.pinecone_energy.json"))


def load_session() -> dict:
    if TOKEN_FILE.exists():
        try:
            return json.loads(TOKEN_FILE.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def save_session(data: dict) -> None:
    TOKEN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


async def run(args: argparse.Namespace) -> None:
    stored = load_session()
    async with aiohttp.ClientSession() as session:
        client = PineconeClient(
            session,
            token=stored.get("token") if not args.phone else None,
            phone=stored.get("phone") if not args.phone else args.phone,
        )
        if args.command == "send-code":
            await client.send_code(args.phone)
            print("验证码已发送")
            return
        if args.command == "login":
            data = await client.login_with_code(args.phone, args.code)
            save_session({"phone": args.phone, "token": client.token})
            print(json.dumps({k: v for k, v in data.items() if k != "token"}, ensure_ascii=False, indent=2))
            print("已保存 token 到", TOKEN_FILE)
            return

        if not client.token:
            raise SystemExit("尚未登录，请先执行 login")

        if args.command == "devices":
            devices = await client.list_devices()
            for item in devices:
                print(f"{item.get('pileCode')}\t{item.get('pileName') or ''}\t{json.dumps(item, ensure_ascii=False)}")
        elif args.command == "detail":
            print(json.dumps(await client.pile_detail(args.pile), ensure_ascii=False, indent=2))
        elif args.command == "status":
            code, text = await client.gun_status(args.pile)
            print(f"状态码 {code} / {text}  (映射: {GUN_STATUS_4G.get(code)})")
        elif args.command == "charging":
            raw = await client.current_charging(args.pile)
            print(json.dumps(raw, ensure_ascii=False, indent=2))
            print("归一化:", json.dumps(PineconeClient.normalise_charging(raw), ensure_ascii=False))
        elif args.command == "signal":
            print(await client.signal_level(args.pile))
        elif args.command == "plan":
            print(json.dumps(await client.get_plan(args.pile), ensure_ascii=False, indent=2))
        elif args.command == "strategy":
            print(json.dumps(await client.ac_current_limit(args.pile), ensure_ascii=False, indent=2))
        elif args.command == "panel":
            print(json.dumps(await client.panel_config(args.pile), ensure_ascii=False, indent=2))
        elif args.command == "records":
            print(json.dumps(await client.day_records(args.pile, args.date), ensure_ascii=False, indent=2))
        elif args.command == "stats":
            print(json.dumps(await client.stats(args.pile, args.date), ensure_ascii=False, indent=2))
        elif args.command == "start":
            await client.start_charge(args.pile)
            print("已下发启动充电")
        elif args.command == "stop":
            order = args.order
            if not order:
                raw = await client.current_charging(args.pile)
                order = PineconeClient.normalise_charging(raw).get("order_code") or ""
            await client.stop_charge(args.pile, order)
            print("已下发停止充电")
        elif args.command == "unlock":
            await client.unlock_gun(args.pile)
            print("已下发解锁枪")
        elif args.command == "watch":
            for _ in range(args.count):
                code, text = await client.gun_status(args.pile)
                raw = await client.current_charging(args.pile)
                charging = PineconeClient.normalise_charging(raw)
                signal = await client.signal_level(args.pile)
                print(
                    f"[{text}/{code}] power={charging['power']} energy={charging['energy']} "
                    f"dur={charging['duration']} soc={charging['soc']} signal={signal} "
                    f"order={charging['order_code']}"
                )
                await asyncio.sleep(args.interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="松果能源 API 调试工具")
    parser.add_argument("command")
    parser.add_argument("--phone")
    parser.add_argument("--code")
    parser.add_argument("--pile")
    parser.add_argument("--order", default="")
    parser.add_argument("--date", default="")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except PineconeError as err:
        raise SystemExit(f"请求失败: {err}")


if __name__ == "__main__":
    main()
