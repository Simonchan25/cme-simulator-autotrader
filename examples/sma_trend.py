#!/usr/bin/env python3
"""
Minimal intraday SMA-trend bot built on top of CMESimulatorClient.

Strategy:
    SMA20 > SMA50  -> go / stay LONG
    SMA20 < SMA50  -> go / stay SHORT (or flat, see FLIP_TO_SHORT)
Entries fire on confirmed crossovers.  Exits fire on opposite crossover,
trailing stop, or hard timeout.

This is an example — don't trade it as-is on a live account without
understanding the limitations (CME simulator fill dynamics, slippage,
fragility to UI changes).

Usage::

    python examples/sma_trend.py
"""
from __future__ import annotations

import asyncio
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from cme_bot import CMESimulatorClient


# -- Configuration ----------------------------------------------------
POLL_INTERVAL = 20            # seconds between price polls
SMA20_WINDOW = 30             # samples (~10 min at 20s poll)
SMA50_WINDOW = 90             # samples (~30 min)
MIN_CONFIRM_BARS = 2          # require this many bars of new trend before flip
MAX_HOLD_SECONDS = 4 * 60 * 60
STOP_COOLDOWN_SECONDS = 300   # pause after adverse exit
FLIP_TO_SHORT = True          # if False, close longs on down-cross but don't short

# Trade these instruments (row label, category, trailing-stop $ retrace).
# Adjust to your risk tolerance; trailing stop is in raw price units.
INSTRUMENTS = [
    {"name": "Micro Gold", "category": "Metals", "trail": 6.0},
]


# -- Data classes -----------------------------------------------------
@dataclass
class Position:
    name: str
    side: str        # "BUY" or "SELL"
    entry_px: float
    opened_at: datetime
    peak: float


@dataclass
class State:
    price_history: deque = field(default_factory=lambda: deque(maxlen=SMA50_WINDOW))
    trend: Optional[str] = None     # 'UP' / 'DOWN'
    trend_streak: int = 0
    position: Optional[Position] = None
    stopped_out_at: Optional[datetime] = None


def compute_trend(history: deque) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """Return (sma20, sma50, 'UP'|'DOWN'|None) once history is warm."""
    if len(history) < SMA50_WINDOW:
        return None, None, None
    buf = list(history)
    sma20 = statistics.fmean(buf[-SMA20_WINDOW:])
    sma50 = statistics.fmean(buf[-SMA50_WINDOW:])
    if sma20 > sma50:
        return sma20, sma50, "UP"
    if sma20 < sma50:
        return sma20, sma50, "DOWN"
    return sma20, sma50, None


async def run_one_instrument(cme: CMESimulatorClient, cfg: dict, st: State) -> None:
    """One tick of entry/exit logic for one instrument."""
    now = datetime.now()
    prices = await cme.read_prices(cfg["name"])
    px = prices.get(cfg["name"])
    if px is None or px <= 0:
        return
    st.price_history.append(px)
    sma20, sma50, trend = compute_trend(st.price_history)

    if trend is None:
        return

    # ── Entry / flip ──
    if st.position is None:
        # cooldown?
        if st.stopped_out_at is not None:
            if (now - st.stopped_out_at).total_seconds() < STOP_COOLDOWN_SECONDS:
                return
            st.stopped_out_at = None

        # require trend confirmation for multiple bars
        if st.trend != trend:
            st.trend_streak += 1
            if st.trend_streak < MIN_CONFIRM_BARS:
                return
            st.trend = trend
            st.trend_streak = 0
        else:
            st.trend_streak = 0
            return

        side = "BUY" if trend == "UP" else "SELL"
        if side == "SELL" and not FLIP_TO_SHORT:
            return
        print(f"[{now:%H:%M:%S}] SIGNAL {cfg['name']} px={px:.4f} "
              f"sma20={sma20:.4f} sma50={sma50:.4f} -> {side}")
        ok = await (cme.buy_market(cfg["name"]) if side == "BUY"
                    else cme.sell_market(cfg["name"]))
        if not ok:
            print(f"  execute failed, will retry next tick")
            return
        st.position = Position(
            name=cfg["name"], side=side, entry_px=px, opened_at=now, peak=px,
        )
        return

    # ── Manage open position ──
    pos = st.position
    hold_sec = (now - pos.opened_at).total_seconds()
    if pos.side == "BUY":
        pos.peak = max(pos.peak, px)
    else:
        pos.peak = min(pos.peak, px)

    close = False
    reason = ""

    # opposite-cross exit
    if (pos.side == "BUY" and trend == "DOWN") or (pos.side == "SELL" and trend == "UP"):
        close = True
        reason = "opposite SMA cross"

    # trailing stop
    if not close:
        trail = cfg["trail"]
        if pos.side == "BUY" and (pos.peak - px) > trail:
            close = True
            reason = f"trailing stop (peak ${pos.peak:.4f})"
        elif pos.side == "SELL" and (px - pos.peak) > trail:
            close = True
            reason = f"trailing stop (peak ${pos.peak:.4f})"

    # timeout
    if not close and hold_sec > MAX_HOLD_SECONDS:
        close = True
        reason = "hard timeout"

    if close:
        print(f"[{now:%H:%M:%S}] CLOSE {pos.side} {cfg['name']} px={px:.4f}: {reason}")
        opp = "SELL" if pos.side == "BUY" else "BUY"
        ok = await (cme.sell_market(cfg["name"]) if opp == "SELL"
                    else cme.buy_market(cfg["name"]))
        if not ok:
            print("  exit failed, will retry")
            return
        approx_pnl = (px - pos.entry_px) if pos.side == "BUY" else (pos.entry_px - px)
        print(f"  approx P&L (in price units): {approx_pnl:+.4f}")
        st.position = None
        if reason.startswith("trailing") or reason == "hard timeout":
            st.stopped_out_at = now


async def main() -> int:
    async with CMESimulatorClient() as cme:
        # Ensure we're on the right category (assumes all INSTRUMENTS share one)
        cat = INSTRUMENTS[0]["category"]
        if not await cme.ensure_category(cat):
            print(f"FAIL: cannot switch to category {cat}")
            return 1

        states = {cfg["name"]: State() for cfg in INSTRUMENTS}
        print(f"Bot LIVE.  Warmup ~{SMA50_WINDOW * POLL_INTERVAL // 60} min "
              f"before first signal.  Ctrl-C to stop.")

        try:
            while True:
                await cme.ensure_category(cat)    # re-assert every tick
                for cfg in INSTRUMENTS:
                    try:
                        await run_one_instrument(cme, cfg, states[cfg["name"]])
                    except Exception as e:
                        print(f"error on {cfg['name']}: {e}")
                await asyncio.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\nBot stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
