# Architecture

The tool is a thin automation shell around the public CME Simulator web
UI.  Everything happens in the user's own Chrome session via the Chrome
DevTools Protocol (CDP).

```
┌────────────────────────┐            ┌──────────────────────────┐
│  Your Python process   │            │  Chrome (headed, macOS /  │
│  ──────────────────    │            │  Linux, with --remote-    │
│  cme_bot.CMESimulator  │  CDP       │  debugging-port=9222)     │
│  Client                │ ◀───────▶  │                           │
│  - read_prices()       │  ws://     │  Tab: cmegroup.com/       │
│  - buy_market()        │  127.0.0.1 │        trading_tools/     │
│  - flatten_all()       │   :9222    │        simulator          │
│  - ensure_category()   │            │  (logged in manually)     │
└────────────────────────┘            └──────────────────────────┘
```

## Why CDP and not requests / a REST API?

CME does not expose a REST API for the free paper simulator.  All
interaction goes through the React UI, which in turn speaks a private
WebSocket to CME.  We drive the UI through Playwright's CDP client
because:

1. Playwright dispatches *trusted* pointer events (`Input.dispatchMouseEvent`).
   MUI Select popovers and ripple-wrapped buttons ignore synthetic
   `element.click()` calls, so `el.click()` inside `page.evaluate`
   silently fails.  `page.mouse.click(x, y)` works.
2. The Chrome session doubles as the visual cockpit — you can watch
   the bot work.
3. Attaching to *your* existing Chrome lets the simulator keep its
   login cookie via `--user-data-dir`.

## Flow: submitting a market order

`CMESimulatorClient.buy_market("Micro Gold")` executes this sequence:

1. **Dismiss Alert** — if the "please refresh the page" modal is
   visible, click REFRESH and wait for the reload.  (See
   `ui_events.dismiss_stale_session_alert`.)
2. **Clear leftover dialogs** — press Escape in case a prior dialog
   is still open (common when two trades fire back-to-back).
3. **Find row** — scan `[role="row"]` in the Markets widget for the
   cell whose text matches the instrument name ("Micro Gold").  Grab
   that row's `TRADE` button's center coordinates.
4. **Click TRADE** — `page.mouse.click(x, y)`.  The Trade dialog
   opens.
5. **Click the *Buy* or *Sell* tab** (title-case; MUI renders them as
   tabs at ~y=659).  Required for SUBMIT to enable.
6. **Open the order-type combobox**, pick "Market" from the listbox.
   Both clicks are trusted CDP.
7. **Click SUBMIT** — wide button near the bottom of the dialog.
8. **Click Confirm Order** — modal with `Cancel` / `Confirm Order`
   buttons.  If 1-Click mode is enabled this step is skipped by CME.

All coordinate lookups happen inline with `page.evaluate` so the
library doesn't cache stale DOM references.

## Why the stale-session Alert exists

Empirically, CME's frontend tolerates a single long-running CDP
consumer but shows the "please refresh the page" modal whenever two
clients are attached simultaneously (e.g. Chrome DevTools open plus
this bot).  We believe it is also triggered when the tab has been
backgrounded long enough for some websocket state to go stale.  The
library auto-dismisses the modal, but each dismiss reloads the page
and clears state:

* The Markets widget resets to the default category.
* Any mid-flow Trade dialog is discarded.
* Your open positions are *not* affected (they live server-side).

Callers should be ready to re-run `ensure_category()` after any
dismiss.

## Virtual rendering

The Markets widget uses `MuiDataGrid-virtualScroller`, so only visible
rows are in the DOM.  For instruments that render inside the widget
viewport (`Gold`, `Micro Silver`, etc.) this is fine, but if you want
to trade an instrument below the fold you need to scroll the grid
first.  None of the current examples hit this case.
