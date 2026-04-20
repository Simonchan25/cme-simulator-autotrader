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

    async def get_position_qty(self, contract: str) -> int:
        """
        Return the *signed* net open quantity for ``contract`` (e.g.
        ``"MGCM6"``).  +N for LONG N, -N for SHORT N, 0 if flat or not
        found.

        Intended use: snapshot before a trade, compare after the Confirm
        click, and treat "qty unchanged" as a failed fill even if the
        Confirm button appeared to be clicked.  See ``buy_market`` /
        ``sell_market`` for the built-in verification path.
        """
        assert self.page is not None
        try:
            return await self.page.evaluate(
                """(contract) => {
                    const rows = Array.from(
                        document.querySelectorAll('[role="row"]')
                    ).filter(r => r.offsetParent);
                    for (const r of rows) {
                        const cells = Array.from(
                            r.querySelectorAll('[role="gridcell"], [role="cell"], td')
                        ).filter(c => c.offsetParent).map(c => c.textContent.trim());
                        if (cells.length < 6) continue;
                        // Open Positions rows: [contract, name, month, openPnL,
                        // totalPnL, "Long N"|"Short N", ...].  Order rows start
                        // with "Buy"/"Sell", so cells[0] !== contract skips them.
                        if (cells[0] !== contract) continue;
                        const m = cells[5].match(/^(Long|Short)\\s*(\\d+)$/);
                        if (!m) return 0;
                        const qty = parseInt(m[2], 10);
                        return m[1] === 'Long' ? qty : -qty;
                    }
                    return 0;
                }""",
                contract,
            )
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------
    async def _execute_market_order(
        self,
        instrument_name: str,
        side: str,
        verify_contract: str | None = None,
    ) -> bool:
        """
        Drive the Trade dialog to submit a MARKET order for *instrument_name*
        on the given *side* ("BUY" or "SELL").

        Returns True **only** if the full flow succeeded: SUBMIT was enabled,
        the Confirm Order dialog appeared and was clicked, and — when
        ``verify_contract`` is given — the CME Open Positions qty for that
        contract actually changed.

        The flow:
          1. Escape + alert-dismiss to ensure a clean state.
          2. Click TRADE on the instrument's row.
          3. Click the ``Buy`` or ``Sell`` direction tab (title-case).
          4. Open the order-type combobox and pick ``Market``.
          5. (Guard) Verify SUBMIT button is enabled, then click it.
          6. (Guard) Poll up to 5 s for Confirm Order dialog, click Confirm.
          7. (Optional guard) Poll up to 10 s for the CME position qty to
             change.  Only active when ``verify_contract`` is provided.

        The three guards exist because a disabled SUBMIT, a missed Confirm
        dialog, or a silently-rejected order would otherwise produce a
        false-positive "fill" — the bot thinks it traded, the CME side
        hasn't moved.  When ``verify_contract`` is not provided, steps 5
        and 6 are still enforced but 7 is skipped.
        """
        assert self.page is not None
        page = self.page
        if side not in ("BUY", "SELL"):
            raise ValueError("side must be 'BUY' or 'SELL'")

        await dismiss_stale_session_alert(page)
        await press_escape(page)

        # Pre-snapshot (used in step 7 if verify_contract)
        qty_before: int | None = None
        if verify_contract is not None:
            qty_before = await self.get_position_qty(verify_contract)

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

        # 5a. Pre-submit guard: abort if SUBMIT is disabled (form incomplete).
        # Clicking a disabled button silently no-ops and we'd falsely think
        # the order was placed.
        submit_enabled = await page.evaluate(
            """(side) => {
                const ok = ['SUBMIT', side, side + ' Order', 'Submit', 'Place Order'];
                for (const b of document.querySelectorAll('button, [role="button"]')) {
                    const r = b.getBoundingClientRect();
                    const t = b.textContent.trim();
                    if (ok.includes(t) && r.width > 200 && r.top > 400) {
                        if (b.disabled || b.getAttribute('aria-disabled') === 'true') return false;
                        return true;
                    }
                }
                return false;
            }""",
            side,
        )
        if not submit_enabled:
            await press_escape(page)
            return False

        await page.mouse.click(sub_xy["x"], sub_xy["y"])
        await asyncio.sleep(1.5)

        # 6. Confirm Order modal — poll up to 5 s.  SUBMIT → modal transition
        # can lag, but if no modal appears at all, the SUBMIT click didn't
        # trigger a real submission and we must NOT treat it as success.
        conf_xy = None
        for _ in range(10):
            conf_xy = await page.evaluate(
                """() => {
                    const ok = ['Confirm Order', 'CONFIRM ORDER', 'Confirm'];
                    for (const d of document.querySelectorAll('[role="dialog"], [role="alertdialog"]')) {
                        if (!d.offsetParent) continue;
                        if (!d.textContent.includes('Confirm Order') && !d.textContent.includes('Confirm')) continue;
                        for (const b of d.querySelectorAll('button')) {
                            if (ok.includes(b.textContent.trim())) {
                                const r = b.getBoundingClientRect();
                                return {x: r.left + r.width/2, y: r.top + r.height/2};
                            }
                        }
                    }
                    return null;
                }"""
            )
            if conf_xy:
                break
            await asyncio.sleep(0.5)
        if not conf_xy:
            await press_escape(page)
            return False
        await page.mouse.click(conf_xy["x"], conf_xy["y"])
        await asyncio.sleep(1.5)

        # 7. Optional real-fill verification: poll CME Open Positions qty
        # for the contract until it differs from the pre-submit snapshot.
        # Without this, a silently-rejected order (margin limit, contract
        # expired, etc.) still "succeeds" above because all the clicks
        # landed.  Only active when the caller supplied verify_contract.
        if verify_contract is not None:
            assert qty_before is not None
            qty_after = qty_before
            for _ in range(20):  # up to 10 s
                qty_after = await self.get_position_qty(verify_contract)
                if qty_after != qty_before:
                    break
                await asyncio.sleep(0.5)
            if qty_after == qty_before:
                return False
        return True

    async def buy_market(
        self,
        instrument_name: str,
        qty: int = 1,
        verify_contract: str | None = None,
    ) -> bool:
        """
        Open a MARKET BUY ``qty``-contract order.

        Parameters
        ----------
        instrument_name : str
            Row name in the Markets widget (e.g. ``"Micro Gold"``).
        qty : int, default 1
            Must be 1 for now.  The Trade dialog's quantity input is not
            currently driven by this client; submit ``qty=1`` in a loop if
            you need more.
        verify_contract : str, optional
            When provided (e.g. ``"MGCM6"``), the method snapshots the
            signed CME Open Positions qty for that contract before the
            SUBMIT click and polls up to 10 s after Confirm for it to
            change.  If the qty never changes, the method returns False —
            catching silently-rejected orders.  Without this argument the
            method only checks that the UI clicks landed, which can still
            produce false positives.
        """
        if qty != 1:
            raise NotImplementedError("qty>1 not implemented; open qty=1 in a loop")
        return await self._execute_market_order(
            instrument_name, "BUY", verify_contract=verify_contract
        )

    async def sell_market(
        self,
        instrument_name: str,
        qty: int = 1,
        verify_contract: str | None = None,
    ) -> bool:
        """
        Open a MARKET SELL ``qty``-contract order (opens a short).

        See :py:meth:`buy_market` for parameter docs including the
        ``verify_contract`` real-fill guard.
        """
        if qty != 1:
            raise NotImplementedError("qty>1 not implemented; open qty=1 in a loop")
        return await self._execute_market_order(
            instrument_name, "SELL", verify_contract=verify_contract
        )

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
