"""
UI-event helpers for driving CME Simulator via Playwright over CDP.

CME Simulator is a MUI (Material-UI) React app with three quirks that
defeat naive `page.click()`:

1. **MUI Select** popovers only open on *pointer* events, not simulated
   clicks.  `page.mouse.click(x, y)` dispatches trusted CDP pointer events
   and works; `el.click()` inside `page.evaluate` does not.
2. The app periodically shows a blocking modal ``Alert — To fetch the
   latest updates, please refresh the page`` with a single REFRESH button.
   Any in-progress trade flow must first dismiss this.
3. Dialog layouts changed recently from ``BUY``/``SELL`` labelled submit
   buttons to a separate ``SUBMIT`` button, and the direction tab labels
   are title-case ``Buy``/``Sell`` (not uppercase).

The helpers here encapsulate all three quirks so the rest of the library
can stay strategy-agnostic.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable


async def click_xy(page, xy: dict | None, *, label: str = "") -> bool:
    """Trusted CDP click at a viewport coordinate.  No-op + warn if xy is None."""
    if not xy:
        return False
    await page.mouse.click(xy["x"], xy["y"])
    return True


async def locate_element(page, js_selector: str, arg=None) -> dict | None:
    """
    Run a JS snippet that returns either an Element's bounding-box center
    ``{x, y}`` or null.  The snippet is an arrow function; ``arg`` is
    passed as its only argument when non-None.

    Example::

        xy = await locate_element(page, '''(label) => {
            for (const t of document.querySelectorAll('[role="tab"]'))
                if (t.textContent.trim() === label) {
                    const r = t.getBoundingClientRect();
                    return {x: r.left + r.width/2, y: r.top + r.height/2};
                }
            return null;
        }''', "Buy")
    """
    if arg is None:
        return await page.evaluate(js_selector)
    return await page.evaluate(js_selector, arg)


async def dismiss_stale_session_alert(page) -> bool:
    """
    If the CME ``Alert — please refresh the page`` modal is visible, click
    its REFRESH button.  Returns True if an alert was dismissed.  The
    caller should assume the page has reloaded and re-initialise state
    (e.g. re-select the Markets category) afterwards.
    """
    refresh_xy = await page.evaluate(
        """() => {
            for (const d of document.querySelectorAll('[role="dialog"], [role="alertdialog"]')) {
                const title = d.querySelector('h1, h2, h3')?.textContent?.trim() || '';
                if (title === 'Alert' || d.textContent.includes('refresh the page')) {
                    for (const b of d.querySelectorAll('button')) {
                        if (b.textContent.trim() === 'REFRESH') {
                            const r = b.getBoundingClientRect();
                            return {x: r.left + r.width/2, y: r.top + r.height/2};
                        }
                    }
                }
            }
            return null;
        }"""
    )
    if not refresh_xy:
        return False
    await page.mouse.click(refresh_xy["x"], refresh_xy["y"])
    # Wait for the reload-induced re-render; caller typically runs
    # ensure_category + re-authenticates any context it relied on.
    await asyncio.sleep(6)
    return True


async def press_escape(page, times: int = 1) -> None:
    """Dismiss any leftover popup / listbox between steps."""
    for _ in range(times):
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
        except Exception:
            pass
