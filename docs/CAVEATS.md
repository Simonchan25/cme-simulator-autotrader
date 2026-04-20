# Caveats and Known Fragilities

## UI drift

The CME Simulator is under active development and its DOM has changed
at least twice in 2025-2026.  Specific selectors most likely to break:

| What | Where in code | Symptom if it changes |
|---|---|---|
| Row cell order (`cells[0]=name, cells[2]=last, cells[5]=position`) | `read_prices`, `read_positions` | Wrong column picked → `None` prices / positions |
| Submit-button labels (`SUBMIT` vs `BUY Order`) | `_execute_market_order` step 5 | Trade dialog stays open; no order placed |
| Confirm-Order button text (`Confirm Order` vs `CONFIRM`) | `_execute_market_order` step 6 | Confirmation modal never cleared, next trade collides |
| Direction-tab labels (`Buy` vs `BUY`) | `_execute_market_order` step 2 | SUBMIT stays disabled (no direction selected) |
| Category dropdown options | `ensure_category` | Wrong category selected or no change |
| Alert modal title (`Alert`) | `dismiss_stale_session_alert` | Modal never dismissed → bot stalls |

When something stops working, open Chrome DevTools on the simulator tab
and inspect the actual DOM around the selector.

## Fills do not match displayed Last Price

The Markets widget "Last Price" column is delayed (consistent with
CME's advertised 15-minute-delay for free market data).  Market orders
however fill against a more up-to-date internal orderbook.  This means:

* Strategies that measure a "delta" between an external real-time
  quote (Yahoo Finance, Alpaca GLD/SLV) and the displayed Last Price
  are measuring an illusion — the fill will clear at a current price,
  not the displayed stale price.
* Expected slippage per round-trip is roughly 1-2 ticks of bid/ask
  plus the drift during the 10-15 s the bot spends navigating the
  Trade dialog.  In practice $100-$250 per round-trip for a
  full-size Gold (GCM6) contract.

The `sma_trend.py` example therefore uses a slower signal (SMA20/SMA50
on 20-second samples = ~30 min half-cycle) to dilute per-trade
slippage below the expected per-trade edge.

## US cash-session dependency of Alpaca

If you use Alpaca GLD/SLV quotes as a real-time proxy for gold/silver,
beware that those are **stock ETFs**.  They only stream real-time
quotes during US equity market hours (13:30-20:00 UTC, M-F).  Outside
those hours the API returns a frozen last trade from the prior close,
so any "delta vs simulator" signal will be pure noise.

## Mac sleep kills the bot

`sleep()` in a Python script resumes from where it was paused when the
machine wakes, so a 5-minute timer can end up firing 8 hours late if
the Mac slept.  Mitigations:

```bash
caffeinate -dimsu &              # keeps display + CPU + system awake
# or: System Settings → Battery → "Prevent automatic sleeping ..."
```

For production bots, use `launchd` plists or `at`/`cron` rather than a
Python-level sleep.

## Multiple CDP clients trigger the Alert modal

Attaching two CDP clients to the same Chrome (e.g. DevTools open +
this bot) causes CME to periodically show the "please refresh the
page" modal.  The bot auto-dismisses it, but:

* Each dismiss reloads the page.  Any in-flight Trade dialog is lost.
* The Markets widget resets to the default (Equity Index) category —
  we re-assert the intended category every tick to recover.
* Open positions are preserved (they live server-side), but the bot's
  in-memory state of "what's open" may briefly diverge from CME until
  the next `read_positions` call.

If you need to debug, first stop the bot, attach DevTools, finish
debugging, close DevTools, then restart the bot.

## Fragility to account-state edge cases

* If the account is close to its margin limit, the Confirm Order modal
  may show a warning that the bot's selector logic does not recognise.
  The current implementation assumes `Confirm Order` / `Cancel` are
  the only buttons.
* On a fresh reset, the simulator sometimes shows an onboarding
  tooltip that occludes the Markets widget.  Dismiss it manually the
  first time and the cookie persists.
* If two instruments share the same row-label prefix (Gold + Micro
  Gold), the selector uses strict equality and will not confuse them —
  but do double-check in `read_positions` that you got the symbol
  column right.

## No SL / TP order types yet

`buy_market` and `sell_market` only submit Market orders.  Limit / Stop
/ Stop-Limit require a few extra fields in the Trade dialog and are
not implemented.  A contribution adding them would be welcome.

## Not audited, not production-ready

This is a research tool.  It will at some point fail to close a
position you thought it had closed, or double-fire an entry.  Do not
use it for anything you can't afford to lose in a paper account.
Real-money automation against a brokerage account is a significantly
different problem and out of scope.
