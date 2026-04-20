#!/usr/bin/env python3
"""
One-shot script: flatten whatever is open, then buy a fixed basket of
contracts and hold.  Useful as a structural "bet" with zero active
trading (no slippage drag until exit).

Usage::

    python examples/buy_and_hold.py
"""
import asyncio
from cme_bot import CMESimulatorClient

# Edit to taste.  ``name`` must match the row label shown in the Markets
# widget (click on a row header to confirm).  Try:
#   "Gold"         (GCM6, 100 oz)
#   "Micro Gold"   (MGCM6, 10 oz)
#   "Silver"       (SIK6, 5000 oz)
#   "Micro Silver" (SILK6, 1000 oz)
BASKET = [
    {"name": "Gold", "qty": 1, "category": "Metals"},
    {"name": "Micro Silver", "qty": 1, "category": "Metals"},
]


async def main() -> int:
    async with CMESimulatorClient() as cme:
        banner0 = await cme.read_banner()
        print(f"Before: funds=${banner0['funds']:,.2f} pnl=${banner0['pnl']:+,.2f}")

        print("Flatten all existing positions ...")
        await cme.flatten_all()

        for leg in BASKET:
            print(f"Switching to category {leg['category']} ...")
            if not await cme.ensure_category(leg["category"]):
                print(f"  FAIL: could not switch to {leg['category']}")
                return 1
            for i in range(leg["qty"]):
                print(f"  BUY 1x {leg['name']} ...")
                if not await cme.buy_market(leg["name"], qty=1):
                    print(f"    failed")
                    return 2
                await asyncio.sleep(1.5)

        positions = await cme.read_positions()
        banner1 = await cme.read_banner()
        print(f"\nAfter:")
        print(f"  Positions: {positions}")
        print(f"  Account:  funds=${banner1['funds']:,.2f} "
              f"margin=${banner1['margin']:,.2f} "
              f"pnl=${banner1['pnl']:+,.2f}")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
