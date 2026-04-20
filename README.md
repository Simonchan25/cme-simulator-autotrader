# cme-simulator-autotrader

Playwright-driven Python client and example bots for the free
[CME Group Trading Simulator](https://www.cmegroup.com/trading_tools/simulator).

> ⚠️  **Paper trading only.**  This tool automates the public browser-based
> paper-trading simulator.  It is **not** connected to live markets and
> cannot place real orders.  Use at your own risk for research and
> education.

## What it does

The simulator is a React / MUI (Material-UI) SPA with several anti-automation
quirks that defeat naive `page.click()`:

* MUI Select popovers only open on *trusted* pointer events;
* A periodic "please refresh the page" modal blocks the UI until dismissed;
* Submit / tab labels are case-sensitive and have changed across UI revisions.

This library wraps all three into a small async client so you can read
prices, submit market orders, flatten positions, and build your own
strategies on top.

```python
import asyncio
from cme_bot import CMESimulatorClient

async def demo():
    async with CMESimulatorClient() as cme:
        await cme.ensure_category("Metals")
        print(await cme.read_prices("Gold", "Silver"))
        # {'Gold': 4810.3, 'Silver': 80.02}
        print(await cme.read_banner())
        # {'funds': 100000.0, 'pnl': 0.0, 'margin': 0.0, ...}

        # verify_contract makes buy_market return True ONLY if the CME
        # Open Positions qty for MGCM6 actually changed — catches
        # silently-rejected orders (0.2.0+).
        ok = await cme.buy_market("Micro Gold", verify_contract="MGCM6")
        assert ok, "order didn't land on CME"
        print(await cme.get_position_qty("MGCM6"))   # 1

        print(await cme.read_positions())
        await cme.flatten_all()

asyncio.run(demo())
```

## Setup

### 1. Install

```bash
git clone https://github.com/Simonchan25/cme-simulator-autotrader.git
cd cme-simulator-autotrader
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # one-time browser download (not strictly needed if attaching to system Chrome)
```

### 2. Launch Chrome with the DevTools Protocol open

The client attaches to an existing Chrome session over CDP.  On macOS:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
    --remote-debugging-port=9222 \
    --user-data-dir=/tmp/cme-profile
```

On Linux:

```bash
google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/cme-profile
```

Then in that Chrome window:

1. Go to https://www.cmegroup.com/trading_tools/simulator
2. Click **Sign In** and log into your CME Simulator account.
3. Leave the tab open.

The client will attach to port 9222 and find the simulator tab automatically.

### 3. Run an example

```bash
python examples/buy_and_hold.py       # open and hold a small basket
python examples/sma_trend.py          # intraday SMA20/SMA50 trend follower
python examples/manual_trade.py       # interactive CLI
```

## Project layout

```
cme_bot/
    client.py       CMESimulatorClient — connect, read prices/banner/positions,
                    buy/sell market, flatten.
    ui_events.py    Alert dismiss + UI helpers.

examples/
    buy_and_hold.py One-shot basket purchase.
    sma_trend.py    Minimal intraday trend-follower template.
    manual_trade.py Interactive CLI for experimentation.

docs/
    ARCHITECTURE.md How the automation works under the hood.
    CAVEATS.md      Risks and known fragilities.
```

## Caveats

* **UI can change at any time.**  CME updates the simulator without
  warning; selectors and button labels here have already changed twice
  in 2025-2026.  If an example stops working, check
  [CAVEATS.md](docs/CAVEATS.md) and the `ui_events.py` / `client.py`
  selectors first.
* **Displayed "Last Price" is delayed; fills are not.**  The Markets
  widget uses delayed quotes but market orders fill against a live-ish
  orderbook.  Simple "arb" strategies based on the delta between YF /
  Alpaca real-time data and the displayed Last Price do not work.
* **Multiple CDP clients trigger an Alert modal.**  If you have Chrome
  DevTools or another CDP tool attached at the same time as this bot,
  CME will periodically show the "please refresh" modal.  The client
  auto-dismisses it, but each dismiss reloads the page and requires a
  re-category step.
* **Session cookies.**  The `/tmp/cme-profile` directory stores your CME
  login cookie.  Do not commit it to git; `.gitignore` excludes
  `chrome-profile/` and `bot_chrome_profile/` by default.
* **Mac sleep will kill your bot.**  Run ``caffeinate -dimsu`` in a
  separate terminal while the bot is live, or disable sleep in System
  Settings.

## Contributing

Bug reports and small PRs welcome.  The selectors in `client.py` and
`ui_events.py` are the main moving parts when CME updates the UI — if
you find and fix a selector that broke, please send a PR.

## License

MIT — see [LICENSE](LICENSE).
