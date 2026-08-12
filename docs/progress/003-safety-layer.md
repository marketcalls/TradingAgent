# 003 - Safety layer: RiskGuard and AuditLog

**Date:** 2026-08-12
**Plan step:** Part 9, step 3
**Status:** complete, 48/48 checks passing

## What went in

| Path | Purpose |
|---|---|
| `backend/app/safety/risk.py` | Deterministic order gate - layer 4 of the four safety layers |
| `backend/app/safety/audit.py` | Dual-sink audit trail, JSONL plus SQLite |
| `backend/tests/test_safety.py` | 48 checks, no network required |

## Why this comes before the order tools

The plan's four safety layers are instructions, tool scoping, human confirmation, and
RiskGuard. Only the last two actually hold, and only RiskGuard holds against a user who
approves without reading. It is plain Python and config with no prompt involved, so nothing
said in the chat can change the outcome. Building it first means the order tools have
something real to call on day one.

## What RiskGuard checks

Ordered so the cheapest and most absolute checks run first:

1. **Kill switch** - the presence of `data/KILL` blocks everything. A human can stop the agent
   without killing the process or waiting for a turn to finish.
2. **`TRADING_ENABLED`** - master switch.
3. **Analyzer mode** - with `REQUIRE_ANALYZER_MODE=true`, any order while OpenAlgo is live is
   refused outright.
4. **Symbol** - present, not denylisted.
5. **Exchange** - present, in the allowlist, and **not a quote-only index feed**. Trying to
   trade `NSE_INDEX` is refused with a message telling the model to use the future or option.
6. **Product** - in the allowlist.
7. **Quantity** - numeric, positive, under the cap.
8. **Session cap** - a runaway-loop backstop, counted per session.
9. **Duplicate suppression** - an identical order within 10 seconds is refused. Models retry;
   brokers do not deduplicate.
10. **Notional cap** - priced at LTP for market orders, at the limit price otherwise.
11. **Fat-finger guard** - a limit price more than 20 percent from LTP is refused. This is the
    check most likely to prevent a real loss.

Baskets run every leg through the same gate and then apply the notional cap to the combined
total, so ten small legs cannot slip past a limit one large leg would hit.

`check_destructive` covers `cancel_all_orders`, `close_all_positions` and GTT cancels, which
have no symbol or quantity but still must respect the kill switch, the master switch and
analyzer mode.

Every refusal returns a plain-language message naming the code and the reason, so the model can
explain it and propose a corrected order rather than retrying blindly.

## AuditLog

Written **twice** per order - `attempt` before the broker is touched, `result` after - so a
crash mid-flight still leaves evidence the attempt happened. Two sinks: a daily JSONL file for
grepping and a SQLite table for querying. Order ids are extracted from all three write-response
shapes (flat `orderid`, `results[]` from baskets, `trigger_id` from GTT).

Audit failures are logged and swallowed. Losing the log is bad; losing the ability to square off
a position because the log directory was unwritable would be worse.

## Validation output

```
=== RiskGuard ===
  [PASS] kill switch blocks - kill_switch
  [PASS] live mode blocked while REQUIRE_ANALYZER_MODE - live_mode_blocked
  [PASS] index exchange refused - index_not_tradable
  [PASS] notional cap refused - notional_cap
  [PASS] fat-finger limit price refused - price_deviation
  [PASS] identical repeat refused - duplicate
  [PASS] session cap refused after 3 - session_cap
  [PASS] basket combined notional cap refused - notional_cap
  [PASS] close_all blocked in live mode - live_mode_blocked
=== AuditLog ===
  [PASS] two rows written - 2 rows
  [PASS] order id extracted - ["250408000989443"]
  [PASS] audit failure is swallowed

  48 passed, 0 failed
```

Sample refusal the model would receive:

```
Order refused by the risk guard (price_deviation): limit price 150.00 is 50.0 percent
away from the last traded price 100.00, beyond the 20 percent guard. Check for a
fat-finger error
```

## Fixes during this iteration

- The test harness hit `WinError 32` tearing down its temp directory because Windows keeps a
  SQLite handle open past the last connection close. Every check had already passed; switched
  to `TemporaryDirectory(ignore_cleanup_errors=True)`.

## Next

Iteration 4: the read-only toolkits - market data, symbols, account, system - and the first
end-to-end agent turn against live quotes.
