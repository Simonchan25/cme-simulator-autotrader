#!/usr/bin/env python3
"""
Interactive CLI for the CME Simulator client.  Useful for manual
experimentation or as a sanity check that the automation still works
after a CME UI change.

    $ python examples/manual_trade.py
    > status
    funds=$99,995.00 pnl=-$5.00 margin=$50,178.00 available=$49,817.00
    Positions: [{'symbol': 'GCM6', 'position': 'Long 1', ...}]
    > category Metals
    > prices Gold "Micro Silver"
    {'Gold': 4810.3, 'Micro Silver': 79.945}
    > buy "Micro Gold"
    ...
    > flatten
    > quit
"""
import asyncio
import shlex
from cme_bot import CMESimulatorClient


HELP = """
Commands:
  status                   – banner + open positions
  category <name>          – switch Markets widget (Metals, Energy, ...)
  prices <name1> <name2>.. – read Last Price for one or more rows
  buy  <instrument name>   – BUY 1x market order (quote spaces)
  sell <instrument name>   – SELL 1x market order
  flatten                  – FLATTEN ALL POSITIONS
  help                     – this message
  quit                     – exit
"""


async def main() -> None:
    async with CMESimulatorClient() as cme:
        print(HELP)
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not line:
                continue
            parts = shlex.split(line)
            cmd = parts[0].lower()
            args = parts[1:]

            try:
                if cmd in ("quit", "exit", "q"):
                    return
                if cmd == "help":
                    print(HELP)
                elif cmd == "status":
                    banner = await cme.read_banner()
                    positions = await cme.read_positions()
                    print(f"funds=${banner['funds']:,.2f} pnl=${banner['pnl']:+,.2f} "
                          f"margin=${banner['margin']:,.2f} "
                          f"available=${banner['available']:,.2f}")
                    print(f"Positions: {positions}")
                elif cmd == "category":
                    if not args:
                        print("usage: category <name>")
                        continue
                    ok = await cme.ensure_category(args[0])
                    print("ok" if ok else "failed")
                elif cmd == "prices":
                    if not args:
                        print("usage: prices <name1> [name2] ...")
                        continue
                    print(await cme.read_prices(*args))
                elif cmd == "buy":
                    if not args:
                        print("usage: buy <name>")
                        continue
                    print("ok" if await cme.buy_market(args[0]) else "failed")
                elif cmd == "sell":
                    if not args:
                        print("usage: sell <name>")
                        continue
                    print("ok" if await cme.sell_market(args[0]) else "failed")
                elif cmd == "flatten":
                    print("ok" if await cme.flatten_all() else "nothing to flatten")
                else:
                    print(f"unknown command: {cmd}.  try 'help'.")
            except Exception as e:
                print(f"error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
