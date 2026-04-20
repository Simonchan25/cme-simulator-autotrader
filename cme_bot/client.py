"""
High-level client for the CME Trading Simulator.

Example
-------
>>> import asyncio
>>> from cme_bot.client import CMESimulatorClient
>>> async def demo():
...     async with CMESimulatorClient() as cme:
...         await cme.ensure_category("Metals")
...         prices = await cme.read_prices("Gold", "Silver")
...         print(prices)                         # {'Gold': 4810.3, 'Silver': 80.02}
...         banner = await cme.read_banner()
...         print(banner)                         # {'funds': 100000.0, 'pnl': 0.0, ...}
...         await cme.buy_market("Micro Gold", qty=1)
...         print(await cme.read_positions())
...         await cme.flatten_all()
>>> asyncio.run(demo())

Preconditions
-------------
The simulator runs inside a regular Chrome tab.  Launch Chrome with the
DevTools protocol enabled and log in manually before using this client::

    /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\
        --remote-debugging-port=9222 --user-data-dir=/tmp/cme-profile

Then visit https://www.cmegroup.com/trading_tools/simulator and log in.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from playwright.async_api import async_playwright, Page, Browser

from .ui_events import (
    dismiss_stale_session_alert,
    press_escape,
)


DEFAULT_CDP_URL = "http://127.0.0.1:9222"


class CMESimulatorClient:
    """
    A playwright-driven client for https://www.cmegroup.com/trading_tools/simulator.

    All methods that touch the UI dismiss the periodic "please refresh"
    modal first and press Escape to clear any leftover popover so that
    back-to-back operations (open → close) don't collide.
    """

    def __init__(self, cdp_url: str = DEFAULT_CDP_URL) -> None:
        self.cdp_url = cdp_url
        self._pw_cm = None
        self._browser: Browser | None = None
        self.page: Page | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        self._pw_cm = async_playwright()
        pw = await self._pw_cm.__aenter__()
        self._browser = await pw.chromium.connect_over_cdp(self.cdp_url)
        self.page = None
        for p in self._browser.contexts[0].pages:
            if "cmegroup" in p.url or "simulator" in p.url:
                self.page = p
                break
        if self.page is None:
            raise RuntimeError(
                "CME Simulator tab not found.  Open "
                "https://www.cmegroup.com/trading_tools/simulator in the "
                "Chrome session attached to CDP first."
            )
        await self.page.bring_to_front()
        await asyncio.sleep(0.5)

    async def close(self) -> None:
        if self._pw_cm is not None:
            await self._pw_cm.__aexit__(None, None, None)
            self._pw_cm = None
            self._browser = None
            self.page = None

    async def __aenter__(self) -> "CMESimulatorClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Category (Markets widget dropdown)
    # ------------------------------------------------------------------
    async def ensure_category(self, category: str) -> bool:
        """
        Select *category* (e.g. ``"Metals"``, ``"Energy"``, ``"Equity Index"``)
        in the Markets widget so the rows we want to trade are visible.

        Returns True on success.  Uses trusted CDP clicks because MUI Select
        popovers ignore synthetic JS clicks.
        """
        assert self.page is not None
        await dismiss_stale_session_alert(self.page)

        already = await self.page.evaluate(
            """(cat) => {
                for (const c of document.querySelectorAll('[role="combobox"]')) {
                    const v = (c.getAttribute('value') || c.textContent || '').trim();
                    if (v === cat) return true;
                }
                return false;
            }""",
            category,
        )
        if already:
            return True

        combo_xy = await self.page.evaluate(
            """() => {
                const c = document.querySelectorAll('[role="combobox"]')[0];
                if (!c) return null;
                const r = c.getBoundingClientRect();
                return {x: r.left + r.width/2, y: r.top + r.height/2};
            }"""
        )
        if not combo_xy:
            return False
        await self.page.mouse.click(combo_xy["x"], combo_xy["y"])
        await asyncio.sleep(0.8)

        opt_xy = await self.page.evaluate(
            """(cat) => {
                for (const lb of document.querySelectorAll('[role="listbox"]')) {
                    for (const li of lb.querySelectorAll('li, [role="option"]')) {
                        if (li.textContent.trim() === cat) {
                            const r = li.getBoundingClientRect();
                            return {x: r.left + r.width/2, y: r.top + r.height/2};
                        }
                    }
                }
                return null;
            }""",
            category,
        )
        if not opt_xy:
            await press_escape(self.page)
            return False
        await self.page.mouse.click(opt_xy["x"], opt_xy["y"])
        await asyncio.sleep(1.2)
        return True

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    async def read_prices(self, *names: str) -> dict[str, float]:
        """
        Return the Last Price for each instrument whose row NAME matches
        one of *names*.  Names are the visible strings in the Markets
        widget ("Gold", "Silver", "Micro Gold", "Crude Oil", etc.).

        Keys in the returned dict are the names that were found; missing
        instruments (dash-placeholder Last Price) are omitted.
        """
        assert self.page is not None
        wanted = list(names)
        rows = await self.page.evaluate(
            """(wanted) => {
                const out = {};
                for (const row of document.querySelectorAll('[role="row"]')) {
                    const cells = row.querySelectorAll('[role="gridcell"]');
                    if (cells.length < 3) continue;
                    const name = cells[0]?.textContent?.trim();
                    if (!wanted.includes(name)) continue;
                    const priceText = cells[2]?.textContent?.trim();
                    const price = parseFloat(priceText);
                    if (!isNaN(price)) out[name] = price;
                }
                return out;
            }""",
            wanted,
        )
        return rows

    async def read_banner(self) -> dict:
        """
        Return ``{'funds', 'pnl', 'margin', 'available', 'one_click'}``
        parsed from the top banner.  Numeric values are floats.
        """
        assert self.page is not None
        raw = await self.page.evaluate(
            """() => document.querySelector('[class*="banner"], header, [class*="MuiToolbar"]')?.textContent?.trim() || ''"""
        )

        def _extract(label: str) -> float:
            # The banner is a single string like
            #   "PRACTICE FUNDS$100,000.00PROFIT/LOSS-$5.00MARGIN$50,178.00AVAILABLE$49,817.00..."
            i = raw.find(label)
            if i < 0:
                return float("nan")
            tail = raw[i + len(label):]
            buf = ""
            started = False
            for ch in tail:
                if ch.isdigit() or ch == "." or ch == "-":
                    buf += ch
                    started = True
                elif ch == "," or ch == "$":
                    continue
                elif started:
                    break
            try:
                return float(buf) if buf else float("nan")
            except ValueError:
                return float("nan")

        return {
            "funds": _extract("PRACTICE FUNDS"),
            "pnl": _extract("PROFIT/LOSS"),
            "margin": _extract("MARGIN"),
            "available": _extract("AVAILABLE"),
            "one_click": "1-Click" in raw,
            "raw": raw[:300],
        }

    async def read_positions(self) -> list[dict]:
        """
        Return a list of open positions::

            [{'symbol': 'GCM6', 'position': 'Long 1',
              'avg_px': 4804.7, 'pnl': ...}, ...]
        """
        assert self.page is not None
        return await self.page.evaluate(
            """() => {
                const out = [];
                for (const row of document.querySelectorAll('[role="row"]')) {
                    const cells = row.querySelectorAll('[role="gridcell"]');
                    if (cells.length >= 10) {
                        const sym = cells[0]?.textContent?.trim();
                        const pos = cells[5]?.textContent?.trim();
                        if (sym && /^[A-Z]{2,4}[A-Z]\\d$/.test(sym) && pos && pos !== '') {
                            out.push({
                                symbol: sym,
                                position: pos,
                                avg_px: parseFloat(cells[8]?.textContent?.trim()) || null,
                                pnl: cells[9]?.textContent?.trim() || null,
                            });
                        }
                    }
                }
                return out;
            }"""
        )

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------
    async def _execute_market_order(self, instrument_name: str, side: str) -> bool:
        """
        Drive the Trade dialog to submit a MARKET order for *instrument_name*
        on the given *side* ("BUY" or "SELL").  Returns True if the flow
        reached the final Confirm click without errors.

        The flow:
          1. Escape + alert-dismiss to ensure a clean state.
          2. Click TRADE on the instrument's row.
          3. Click the ``Buy`` or ``Sell`` direction tab (title-case).
          4. Open the order-type combobox and pick ``Market``.
          5. Click SUBMIT.
          6. Click Confirm Order on the confirmation modal (if any).
        """
        assert self.page is not None
        page = self.page
        if side not in ("BUY", "SELL"):
            raise ValueError("side must be 'BUY' or 'SELL'")

        await dismiss_stale_session_alert(page)
        await press_escape(page)

        # 1. TRADE button on the matching row
        trade_xy = await page.evaluate(
            """(name) => {
                for (const row of document.querySelectorAll('[role="row"]')) {
                    const cells = row.querySelectorAll('[role="gridcell"]');
                    if (cells.length >= 3 && cells[0]?.textContent?.trim() === name) {
                        for (const b of row.querySelectorAll('button')) {
                            if (b.textContent.trim() === 'TRADE') {
                                const r = b.getBoundingClientRect();
                                return {x: r.left + r.width/2, y: r.top + r.height/2};
                            }
                        }
                    }
                }
                return null;
            }""",
            instrument_name,
        )
        if not trade_xy:
            return False
        await page.mouse.click(trade_xy["x"], trade_xy["y"])
        await asyncio.sleep(2)
        if await dismiss_stale_session_alert(page):
            return False

        # 2. Direction tab (title-case 'Buy' / 'Sell')
        tab_label = "Buy" if side == "BUY" else "Sell"
        tab_xy = await page.evaluate(
            """(label) => {
                for (const t of document.querySelectorAll('[role="tab"]')) {
                    if (t.textContent.trim() === label) {
                        const r = t.getBoundingClientRect();
                        return {x: r.left + r.width/2, y: r.top + r.height/2};
                    }
                }
                return null;
            }""",
            tab_label,
        )
        if not tab_xy:
            return False
        await page.mouse.click(tab_xy["x"], tab_xy["y"])
        await asyncio.sleep(0.8)

        # 3. Order-type combobox (currently shows Limit/Market/...)
        ot_xy = await page.evaluate(
            """() => {
                for (const c of document.querySelectorAll('[role="combobox"]')) {
                    const v = (c.getAttribute('value') || c.textContent || '').trim();
                    if (['Limit','Market','Stop','Stop Limit'].some(x => v.includes(x))) {
                        const r = c.getBoundingClientRect();
                        return {x: r.left + r.width/2, y: r.top + r.height/2};
                    }
                }
                return null;
            }"""
        )
        if not ot_xy:
            return False
        await page.mouse.click(ot_xy["x"], ot_xy["y"])
        await asyncio.sleep(0.7)

        # 4. 'Market' option
        mkt_xy = await page.evaluate(
            """() => {
                for (const lb of document.querySelectorAll('[role="listbox"]')) {
                    for (const li of lb.querySelectorAll('li, [role="option"]')) {
                        if (li.textContent.trim() === 'Market') {
                            const r = li.getBoundingClientRect();
                            return {x: r.left + r.width/2, y: r.top + r.height/2};
                        }
                    }
                }
                return null;
            }"""
        )
        if not mkt_xy:
            return False
        await page.mouse.click(mkt_xy["x"], mkt_xy["y"])
        await asyncio.sleep(0.8)

        # 5. SUBMIT button (wide, bottom half of viewport)
        sub_xy = await page.evaluate(
            """(side) => {
                const ok = ['SUBMIT', side, side + ' Order', 'Submit', 'Place Order'];
                let best = null, bestW = 0;
                for (const b of document.querySelectorAll('button, [role="button"]')) {
                    const r = b.getBoundingClientRect();
                    const t = b.textContent.trim();
                    if (ok.includes(t) && r.width > 200 && r.top > 400 && r.width > bestW) {
                        bestW = r.width;
                        best = {x: r.left + r.width/2, y: r.top + r.height/2};
                    }
                }
                return best;
            }""",
            side,
        )
        if not sub_xy:
            return False
        await page.mouse.click(sub_xy["x"], sub_xy["y"])
        await asyncio.sleep(1.5)

        # 6. Confirm Order modal (may be skipped if 1-Click is on)
        conf_xy = await page.evaluate(
            """() => {
                const ok = ['Confirm Order', 'CONFIRM ORDER', 'Confirm'];
                for (const b of document.querySelectorAll('button')) {
                    if (ok.includes(b.textContent.trim())) {
                        const r = b.getBoundingClientRect();
                        return {x: r.left + r.width/2, y: r.top + r.height/2};
                    }
                }
                return null;
            }"""
        )
        if conf_xy:
            await page.mouse.click(conf_xy["x"], conf_xy["y"])
            await asyncio.sleep(1.5)
        return True

    async def buy_market(self, instrument_name: str, qty: int = 1) -> bool:
        """Open a MARKET BUY ``qty``-contract order.  (Qty>1 not yet wired —
        currently always submits with the dialog's default 1.)"""
        if qty != 1:
            raise NotImplementedError("qty>1 not implemented; open qty=1 in a loop")
        return await self._execute_market_order(instrument_name, "BUY")

    async def sell_market(self, instrument_name: str, qty: int = 1) -> bool:
        """Open a MARKET SELL ``qty``-contract order (opens a short)."""
        if qty != 1:
            raise NotImplementedError("qty>1 not implemented; open qty=1 in a loop")
        return await self._execute_market_order(instrument_name, "SELL")

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------
    async def flatten_all(self) -> bool:
        """Click FLATTEN ALL POSITIONS and confirm.  Returns True on success."""
        assert self.page is not None
        page = self.page
        await press_escape(page)

        flat_xy = await page.evaluate(
            """() => {
                for (const el of document.querySelectorAll('button, [role="button"], span, div')) {
                    if (el.textContent?.trim() === 'FLATTEN ALL POSITIONS' && el.offsetParent) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 0) return {x: r.left + r.width/2, y: r.top + r.height/2};
                    }
                }
                return null;
            }"""
        )
        if not flat_xy:
            return False  # probably already flat
        await page.mouse.click(flat_xy["x"], flat_xy["y"])
        await asyncio.sleep(1.5)

        conf_xy = await page.evaluate(
            """() => {
                for (const b of document.querySelectorAll('button')) {
                    if (['Flatten','OK','Yes','Confirm'].includes(b.textContent.trim()) && b.offsetParent) {
                        const r = b.getBoundingClientRect();
                        return {x: r.left + r.width/2, y: r.top + r.height/2};
                    }
                }
                return null;
            }"""
        )
        if conf_xy:
            await page.mouse.click(conf_xy["x"], conf_xy["y"])
            await asyncio.sleep(3)
        return True
