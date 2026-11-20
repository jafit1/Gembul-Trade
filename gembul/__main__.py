"""Entry point. `python -m gembul <command>`."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import uvicorn

from gembul.api.server import build_app
from gembul.core.engine import Engine
from gembul.settings import LOGS, get_settings
from gembul.telegram.bot import TelegramUI
from gembul.venues.dex.wallet import BrowserWalletRelay


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOGS / "gembul.log", encoding="utf-8"),
        ],
    )
    for noisy in ("httpx", "aiogram.event", "web3", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def cmd_run() -> None:
    s = get_settings()
    engine = Engine(s)
    relay = BrowserWalletRelay()

    tg = TelegramUI(engine, s) if s.telegram_token else None
    if tg is not None:
        engine.notifier = tg.notify
    app = build_app(engine, relay)
    server = uvicorn.Server(uvicorn.Config(
        app, host="0.0.0.0", port=8000, log_level="warning", access_log=False,
    ))

    await engine.start()
    tasks = [
        asyncio.create_task(engine.run(), name="engine"),
        asyncio.create_task(server.serve(), name="api"),
    ]
    if tg is not None:
        tasks.append(asyncio.create_task(tg.start_polling(), name="telegram"))

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        for t in tasks:
            t.cancel()
        if tg is not None:
            await tg.stop()
        await engine.stop()


async def cmd_status() -> None:
    s = get_settings()
    engine = Engine(s)
    await engine.start()
    snap = await engine.snapshot()
    import json
    print(json.dumps(snap, indent=2, default=str))
    await engine.stop()


async def cmd_scan(chain: str) -> None:
    s = get_settings()
    engine = Engine(s)
    await engine.start()
    sigs = await engine.meme.discover(chain, limit=40)
    for sig in sigs[:12]:
        f = sig.features
        print(f"{sig.score:.3f}  {sig.symbol:14s} liq=${f['liquidity_usd']:>12,.0f} "
              f"age={f['age_minutes']:>6.0f}m  1h={f['chg_1h']:>7.1f}%  {sig.address}")
        print(f"        {', '.join(f.get('reasons') or [])}")
    await engine.stop()


async def cmd_doctor() -> None:
    s = get_settings()
    engine = Engine(s)
    print(f"mode={s.mode} live={s.real_trading_enabled} base={s.base_currency}")
    await engine.cex.start()
    print(f"cex          : {engine.cex.ex.id if engine.cex.ex else '-'}")
    print(f"freqtrade    : {'online' if await engine.ft.ping() else 'offline'}")
    print(f"llm          : {'siap' if engine.llm.ready else 'belum dikonfigurasi'}")

    for name in s.meme.allow_chains:
        try:
            bal = await engine.dex.native_balance(name)
            print(f"chain {name:9s}: native {bal:.6f}")
        except Exception as exc:
            print(f"chain {name:9s}: error {exc}")

    try:
        sigs = await engine.meme.discover("solana", limit=40)
        print(f"scanner      : {len(sigs)} sinyal solana")
    except Exception as exc:
        print(f"scanner      : error {exc}")

    try:
        from gembul.venues.dex.chains import get_chain
        ch = get_chain("solana")
        price = await engine.dex.price("solana", ch.usdc)
        print(f"jupiter      : USDC harga {price:.4f}")
    except Exception as exc:
        print(f"jupiter      : error {exc}")

    await engine.stop()


async def cmd_reflect() -> None:
    s = get_settings()
    engine = Engine(s)
    await engine.start()
    n = await engine.reflector.run_once(limit=50)
    st = await engine.memory.stats()
    print(f"{n} episode diproses | total pelajaran {st['lessons']}")
    await engine.stop()


def cmd_backtest(argv: list[str]) -> None:
    import subprocess
    from gembul.settings import ROOT

    cmd = [str(ROOT / ".venv" / "bin" / "freqtrade"), "backtesting", *argv]
    print(" ".join(cmd))
    subprocess.run(cmd, check=False)


def main() -> None:
    s = get_settings()
    setup_logging(s.log_level)

    parser = argparse.ArgumentParser(prog="gembul")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run")
    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("reflect")

    p_scan = sub.add_parser("scan")
    p_scan.add_argument("chain", nargs="?", default="solana")

    p_bt = sub.add_parser("backtest")
    p_bt.add_argument("args", nargs=argparse.REMAINDER)

    args = parser.parse_args()

    if args.cmd == "run":
        asyncio.run(cmd_run())
    elif args.cmd == "status":
        asyncio.run(cmd_status())
    elif args.cmd == "doctor":
        asyncio.run(cmd_doctor())
    elif args.cmd == "reflect":
        asyncio.run(cmd_reflect())
    elif args.cmd == "scan":
        asyncio.run(cmd_scan(args.chain))
    elif args.cmd == "backtest":
        cmd_backtest(args.args)


if __name__ == "__main__":
    main()
