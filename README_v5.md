# Kalshi BTC Binary Options Bot — V5
## "Temporal Stalker Edition"
### `production_bot_v5.py` + `dashboard_v5.py`

---

## Table of Contents

1. [What This Bot Does](#what-this-bot-does)
2. [The Theory: How Kalshi BTC Binary Options Work](#the-theory-how-kalshi-btc-binary-options-work)
3. [The Strategy: UT Bot + Temporal Stalker](#the-strategy-ut-bot--temporal-stalker)
4. [Architecture: How the Code Works](#architecture-how-the-code-works)
5. [Setup & Running](#setup--running)
6. [Configuration Reference](#configuration-reference)
7. [Log File Reference](#log-file-reference)
8. [The Dashboard](#the-dashboard)
9. [Understanding the Console Output](#understanding-the-console-output)
10. [Risk Management](#risk-management)
11. [Known Limitations](#known-limitations)

---

## What This Bot Does

V5 monitors BTC price in real time via a Coinbase WebSocket feed, runs the UT Bot technical indicator on 1-minute candles, and uses that signal to decide whether to buy YES or NO contracts on Kalshi's 15-minute BTC binary options market. It operates in one of two modes:

- **PAPER mode** (default): Simulates trades against a virtual $1,000 bankroll. No real money moves. Use this to validate the strategy before going live.
- **LIVE mode**: Places real orders on Kalshi using your API credentials.

The bot runs continuously, handling every 15-minute market session automatically. When one session expires, it rolls to the next, attempts to settle the previous position via Kalshi's API, and resets for a new potential trade.

---

## The Theory: How Kalshi BTC Binary Options Work

### Binary Options 101

A Kalshi binary option is a contract that settles at exactly **$1.00 (100¢) if YES** or **$0.00 (0¢) if NO** at expiry. There is no partial payout — it is a binary outcome.

For BTC 15-minute contracts (`KXBTC15M`), the question is: **"Will BTC be at or above [strike price] at [expiry time]?"**

- If you buy **YES at 60¢** and BTC is above the strike at expiry: you receive 100¢. Profit = 40¢ per contract.
- If you buy **YES at 60¢** and BTC is below the strike: you receive 0¢. Loss = 60¢ per contract.
- If you buy **NO at 40¢** and BTC is below the strike at expiry: you receive 100¢. Profit = 60¢ per contract.

The price of the contract (e.g. 60¢) reflects the market's implied probability that BTC will be above the strike. A 60¢ YES price means the market thinks there's roughly a 60% chance of finishing above the strike.

### The Math of Profitability

For a strategy to be profitable over time, your win rate must exceed the breakeven threshold implied by the prices you pay. At 50¢ entry, you need >50% win rate. At 60¢ entry, you need >60% win rate. At 40¢ entry, you only need >40% win rate.

This is why **entry price matters enormously** and why the bot has price filters (`MARKET_VETO_PRICE`, `MARKET_MAX_ENTRY_PRICE`). Buying a 90¢ YES contract means you're betting on something the market thinks is almost certain — you need to be right very often to overcome the slim profit margin on wins.

### The 15-Minute Window

Each session has a defined close time. The strike price is typically the current BTC price (or nearby round number) at session open. With 15 minutes of runway, a BTC move of even $200-300 can flip the result. This gives technical signals enough time to play out, but is short enough that major macro moves don't overwhelm the trade.

### Edge: When Does the Market Misprice?

The market price (60¢ YES, 40¢ NO) represents the crowd's aggregate view. The bot's edge — if it has one — comes from identifying moments when momentum indicators suggest the market's current implied probability is wrong. Specifically: if UT Bot says BTC is in a strong uptrend and the YES contract is only 55¢, the market may be underpricing the probability of finishing above the strike.

---

## The Strategy: UT Bot + Temporal Stalker

### UT Bot — The Indicator

UT Bot (UT Bot Alerts by HPotter) is a trend-following indicator built on an **ATR-based trailing stop**. It works as follows:

**Step 1: Calculate ATR (Average True Range)**
ATR measures how much BTC typically moves per candle. On a 1-minute candle, ATR might be $50. A high ATR means volatile conditions; low ATR means quiet.

The bot uses **Wilder's smoothing** (RMA), which is how TradingView computes it:
- First ATR value = simple average of first `ATR_PERIOD` true ranges
- Each subsequent: `ATR[i] = ATR[i-1] + (TR[i] - ATR[i-1]) / ATR_PERIOD`

**Step 2: Calculate the Trailing Stop**
The trailing stop sits at `key_value × ATR` away from price, and only moves in the direction of the trend:

- If price is above the stop and moving up: stop rises (but never falls)
- If price is below the stop and moving down: stop falls (but never rises)
- If price crosses the stop: the stop jumps to the other side and the signal flips

In code:
```
if close > prev_stop and prev_close > prev_stop:
    stop = max(prev_stop, close - n_loss)
elif close < prev_stop and prev_close < prev_stop:
    stop = min(prev_stop, close + n_loss)
elif close > prev_stop:
    stop = close - n_loss   # Signal just flipped to buy
else:
    stop = close + n_loss   # Signal just flipped to sell
```

**Step 3: The Signal**
- `ut_signal = "buy"` when `close > trailing_stop` (BTC above the stop — uptrend)
- `ut_signal = "sell"` when `close < trailing_stop` (BTC below the stop — downtrend)

The signal **flips** at the moment price crosses the trailing stop. The exact timestamp of that flip is the **birth time**.

### Why UT Bot Works for This Application

UT Bot is a momentum/trend-following indicator. It answers: "Is BTC currently in an uptrend or downtrend, as measured by recent volatility?" This is meaningful for a 15-minute binary because:

1. Trends have persistence — if BTC was rising when the signal fired, it's more likely to still be above the strike 5-10 minutes later.
2. The ATR-based stop adapts to volatility — in choppy markets, the stop widens and filters out noise. In trending markets, the stop tightens and keeps you close to price.
3. The crossover is a clear event — you know exactly when momentum shifted and can measure how old that signal is.

### The Temporal Stalker

The "Stalker" part is the logic for **when to enter a Kalshi trade** after the signal fires.

The problem: UT Bot fires a signal at, say, 10:03am. But the current Kalshi session expires at 10:15am and it's already 10:12am — only 3 minutes left, which is too tight. The next session opens at 10:15am with a new 15-minute window.

A naive bot would miss this trade entirely. The Stalker waits.

**The Stalker Pattern:**
1. UT Bot fires a BUY signal at 10:03am. `birth_ts = 10:03:00`.
2. At 10:03am, time remaining in the current session is <1 min. Bot skips — `time_too_close_to_expiry`.
3. At 10:15am, new session opens with 15 minutes left. Signal age = 12 minutes.
4. If 12 minutes < `MAX_STALK_POST_SIGNAL_MIN` (10 min): Bot enters. 
5. If signal age > `MAX_STALK_POST_SIGNAL_MIN`: Bot skips — signal too old.

**Why Cap the Stalk Window?**

A signal that fired 20 minutes ago is much less reliable than one that fired 2 minutes ago. BTC conditions change. The `MAX_STALK_POST_SIGNAL_MIN = 10.0` means the bot will only act on signals that are less than 10 minutes old — fresh enough that the momentum is likely still intact.

**Signal Dedup via Birth Time**

The bot persists the `birth_ts` of every signal it acted on to disk (`state/acted_birth_ts.json`). This prevents:
- Double-entry on the same signal in the same session (e.g., the strategy tick fires twice before the order lands)
- Re-entry after a bot restart (if it crashed after placing an order, it won't place again on the same signal when it restarts)

### Live Candle Signal

The signal is read from the **live forming candle**, not the last closed candle. This means the moment BTC crosses the trailing stop on the current tick, the signal flips immediately — you don't wait 30-60 seconds for the current candle to close.

This was the fix for a significant lag issue in earlier versions where signals appeared 30+ seconds after the crossover on TradingView.

---

## Architecture: How the Code Works

The bot runs three concurrent async loops that communicate through shared state.

```
┌─────────────────────────────────┐
│   Coinbase WebSocket            │
│   watch_order_book()            │
│   → ex.orderbooks[BTC/USD]      │  ← continuously updated in memory
└─────────────────┬───────────────┘
                  │ mid-price every 0.5s
                  ▼
┌─────────────────────────────────┐
│   ohlcv_candle_loop             │
│   CandleBuilder → 1m OHLCV      │
│   calculate_ut_bot()            │
│   → SharedState.update()        │  ← signal, ATR, stop, birth_ts
└─────────────────┬───────────────┘
                  │
                  ▼
┌─────────────────────────────────┐    ┌─────────────────────────────┐
│   kalshi_market_loop            │    │   StrategyController        │
│   REST polling every 0.5s       │───▶│   on_tick()                 │
│   → market_queue                │    │   Guards → Entry → Log      │
└─────────────────────────────────┘    └─────────────────────────────┘
```

### Loop 1: `watch_exchange_loop`

Subscribes to Coinbase's Level 2 order book WebSocket. Each update keeps `ex.orderbooks["BTC/USD"]` current in memory. This loop just maintains the connection and auto-reconnects on any error. No logic lives here.

### Loop 2: `ohlcv_candle_loop`

Every 0.5 seconds:
1. Reads `(bid + ask) / 2` from the cached order book
2. Feeds the mid-price into `CandleBuilder`
3. If a new minute boundary is crossed, `CandleBuilder` finalizes the previous candle
4. Recalculates UT Bot on every new closed candle, or at least every 5 seconds on the live forming candle
5. Writes signal, ATR, stop, and birth_ts into `SharedState`

On startup, seeds ~60 closed candles via a single REST call so ATR is ready immediately instead of waiting 10+ minutes for ticks to build up.

### Loop 3: `kalshi_market_loop`

Every 0.5 seconds:
1. Fetches all open KXBTC15M markets from Kalshi's REST API
2. Sorts by expiry, takes the nearest-expiring future market
3. Fetches the order book for that market
4. Computes yes/no bids, asks, liquidity, OBI
5. Pushes to `market_queue` (size 1 — always the freshest data)

### Main Loop

Reads from `market_queue` continuously. On each tick:
1. Detects session roll (ticker changed) → triggers background settlement of previous position
2. Calls `bot.on_tick(kalshi, data)` with combined market data + shared state

### `on_tick` — The Decision Engine

Reads the current signal from `SharedState`, applies all guards in order, and either places a trade or returns with a logged `filter_reason`. Guards are applied in priority order — the first failure returns immediately:

1. Session fill limit reached (`session_fills >= MAX_FILLS_PER_SESSION`)
2. Already in a position
3. Already acted on this birth_ts
4. Signal too old (`signal_age_min > MAX_STALK_POST_SIGNAL_MIN`)
5. Too close to expiry (`minutes_left < TIME_ENTRY_MAX_MIN`)
6. Order book stale (`ob_stale`)
7. Price out of range (`< MARKET_VETO_PRICE` or `> MARKET_MAX_ENTRY_PRICE`)
8. Quantity would be zero (insufficient bankroll)

If all guards pass → commit trade, deduct from paper balance, log `PAPER_BUY` or `LIVE_BUY`.

### Settlement

When a session rolls (new ticker detected), the bot fires `_bg_settle()` as a background task. It:
1. Waits `SETTLEMENT_INITIAL_DELAY` seconds (90s) for Kalshi to finalize the result
2. Queries Kalshi for the market result up to `SETTLEMENT_MAX_RETRIES` times
3. If Kalshi confirms result: logs `PAYOUT` (win) or `SETTLE` (loss)
4. Fallback: compares BTC spot price at settlement time against strike (less reliable)
5. Updates paper balance accordingly

---

## Setup & Running

### Prerequisites

```
Python 3.11+
```

### Installation

```bash
cd /path/to/Kalshi-Bot-V5
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Environment Variables

Create a `.env` file in the same directory as `production_bot_v5.py`:

```
KALSHI_API_KEY=your_api_key_here
KALSHI_PRIVATE_KEY=your_private_key_here
```

Your `kalshi_client.py` reads these at startup. In Paper mode, the bot initializes without making authenticated API calls (prices and market data are public).

### Running in Paper Mode (default)

```bash
source .venv/bin/activate
python3 production_bot_v5.py
```

### Running the Dashboard (separate terminal)

```bash
source .venv/bin/activate
python3 dashboard_v5.py
# Open http://localhost:5000 in your browser
```

### Switching to Live Mode

In `config.json`:
```json
{
  "PAPER_MODE": false
}
```

The bot hot-reloads `config.json` every 0.5 seconds, so you can flip this without restarting. **Make sure your API credentials are in `.env` before going live.**

### Live Config Overrides

Any key in `Config` class can be overridden via `config.json` without restarting the bot. The candle loop reads it every cycle. Example:

```json
{
  "PAPER_MODE": true,
  "FLAT_FRAC_PCT": 0.015,
  "MAX_STALK_POST_SIGNAL_MIN": 8.0,
  "MARKET_VETO_PRICE": 35
}
```

---

## Configuration Reference

### Mode & Balance

| Setting | Default | Description |
|---|---|---|
| `PAPER_MODE` | `true` | `true` = simulate trades, no real money. `false` = live trading. |
| `PAPER_START_BALANCE` | `1000.0` | Starting virtual bankroll in dollars for paper mode. |
| `MAX_DAILY_LOSS` | `1000000.0` | If rolling 24h losses exceed this, bot stops placing orders. Set lower in live mode (e.g. 50.0). |

### Market & Data

| Setting | Default | Description |
|---|---|---|
| `SYMBOL` | `"BTC/USD"` | The trading pair to monitor on Coinbase. Don't change this. |
| `EXCHANGES` | `["coinbase"]` | List of exchanges for price feed. Supports multiple — bot takes the median price. |
| `SERIES_TICKER` | `"KXBTC15M"` | Kalshi market series. `KXBTC15M` = 15-minute BTC binary options. |
| `CANDLE_TIMEFRAME` | `"1m"` | Candle timeframe for UT Bot. 1-minute candles fed from tick synthesis. |

### UT Bot Indicator

| Setting | Default | Description |
|---|---|---|
| `UT_BOT_SENSITIVITY` | `1.0` | Multiplier on ATR for the trailing stop width. Higher = wider stop, fewer signals, less sensitive to noise. Lower = tighter stop, more signals, more whipsaws. Range: 0.5–3.0. |
| `UT_BOT_ATR_PERIOD` | `10` | Number of candles for ATR calculation. Higher = smoother ATR, slower to react. Lower = faster ATR, more volatile. Default of 10 matches TradingView's UT Bot defaults. |

**Tuning guidance:** `SENSITIVITY` and `ATR_PERIOD` together control how frequently the signal flips. At 1.0/10, you'll see signal flips roughly every 5-20 minutes in normal BTC conditions. Raising `SENSITIVITY` to 2.0 will give you fewer but more sustained signals — better for trending markets, worse for ranging ones.

### Stalker Window

| Setting | Default | Description |
|---|---|---|
| `MAX_STALK_POST_SIGNAL_MIN` | `10.0` | Maximum age (in minutes) of a UT Bot signal before the bot stops stalking it. A signal older than this is considered stale and will be skipped even if a good session opens. |

**Tuning guidance:** Setting this lower (5-7 min) means you only trade on very fresh signals — higher quality but fewer trades. Setting it higher (15+ min) risks entering on signals whose momentum has faded. 10 minutes is a reasonable default for 1-minute UT Bot signals.

### Entry Filters

| Setting | Default | Description |
|---|---|---|
| `TIME_ENTRY_MIN_MIN` | `15` | Only enter if the current session has at least this many minutes remaining. Since KXBTC15M sessions are 15 minutes total, this means the bot enters in the first window of a fresh session. |
| `TIME_ENTRY_MAX_MIN` | `1` | Don't enter if fewer than this many minutes remain until expiry. Prevents buying into a session that's about to close. |
| `MARKET_VETO_PRICE` | `30` | Minimum contract price in cents. Protects against buying contracts the market thinks have very low probability. A 29¢ YES contract means only ~29% chance of winning — hard to overcome. |
| `MARKET_MAX_ENTRY_PRICE` | `75` | Maximum contract price in cents. Prevents buying contracts where the profit margin on a win is too thin. A 76¢ YES contract only pays 24¢ on a win — requires very high accuracy to be profitable. |
| `MAX_FILLS_PER_SESSION` | `1` | Maximum trades per 15-minute session. Set to 1 to prevent doubling into a position. |
| `MAX_ORDERBOOK_STALE_SEC` | `10.0` | If the Kalshi order book hasn't refreshed in this many seconds, skip entry. Stale order book data can lead to entries at bad prices. |

**Price range intuition:** The range 30¢–75¢ targets contracts where the market sees roughly 30-75% probability. This zone tends to have reasonable liquidity and meaningful profit margins on both YES and NO outcomes. Contracts outside this range are either near-certainties (bad risk/reward) or longshots (low win rate).

### Position Sizing

| Setting | Default | Description |
|---|---|---|
| `FLAT_FRAC_PCT` | `0.02` | Fraction of bankroll to risk per trade. `0.02` = 2%. On a $1,000 bankroll at a 50¢ entry: 2% = $20 at risk → 40 contracts. |
| `MAX_CONTRACTS_LIMIT` | `250` | Hard cap on contracts per trade, regardless of bankroll size. Prevents oversizing on large bankrolls. |

**Sizing math:**
```
dollar_risk = bankroll × FLAT_FRAC_PCT
contracts   = int(dollar_risk / (entry_price_cents / 100))
```

At $1,000 bankroll, 2%, and 50¢ entry: `$20 / $0.50 = 40 contracts`. Each contract costs 50¢, wins 100¢ (profit 50¢) or loses 50¢. Max gain = $20, max loss = $20.

### Settlement

| Setting | Default | Description |
|---|---|---|
| `SETTLEMENT_INITIAL_DELAY` | `90.0` | Seconds to wait after session expiry before querying Kalshi for the result. Kalshi typically finalizes within 60-90 seconds. |
| `SETTLEMENT_RETRY_INTERVAL` | `15.0` | Seconds between retry attempts if the result isn't available yet. |
| `SETTLEMENT_MAX_RETRIES` | `4` | Maximum number of times to retry settlement before falling back to spot price comparison. |

### Files

| Setting | Default | Description |
|---|---|---|
| `LOG_FILE` | `"production_log_v5.csv"` | Main trade and activity log. Every event is written here. |
| `SYSTEM_LOG_FILE` | `"bot_v5.log"` | Python logging output — errors, warnings, stack traces. Rotates at 5MB, keeps 3 backups. |
| `STATE_FILE` | `"state/acted_birth_ts.json"` | Persists the last-acted birth_ts across restarts. Prevents double-entry after crash. |

---

## Log File Reference

Every event the bot handles writes a row to `production_log_v5.csv`. The columns:

| Column | Description |
|---|---|
| `timestamp` | UTC ISO timestamp of the event |
| `event` | `HRTBT`, `PAPER_BUY`, `LIVE_BUY`, `PAYOUT`, `SETTLE`, `SETTLE_VERIFIED`, `ERROR`, `SYSTEM` |
| `mode` | `PAPER` or `LIVE` |
| `ticker` | Kalshi market ticker (e.g. `KXBTC15M-26FEB231945-45`) |
| `side` | `yes` or `no` |
| `entry_price` | Contract price in cents |
| `qty` | Number of contracts |
| `time_left` | Minutes remaining in session at entry |
| `btc_price` | BTC spot price at time of event |
| `strike` | Session strike price in dollars |
| `raw_yes_bid` | Best YES bid in the Kalshi order book (cents) |
| `raw_no_bid` | Best NO bid in the Kalshi order book (cents) |
| `ask_yes` | Best YES ask (100 - no_bid) |
| `ask_no` | Best NO ask (100 - yes_bid) |
| `spread` | Bid-ask spread on the traded side |
| `yes_liq` | Top 5 levels YES liquidity (contract count) |
| `no_liq` | Top 5 levels NO liquidity (contract count) |
| `obi` | Order Book Imbalance: `(yes_liq - no_liq) / (yes_liq + no_liq)`. +1 = all YES liquidity, -1 = all NO liquidity. |
| `bankroll` | Current paper (or real) balance at time of event |
| `rolling_24h_loss` | Sum of losses in the past 24 hours |
| `ut_signal` | `buy` or `sell` — UT Bot signal direction |
| `ut_atr` | ATR value at time of event |
| `ut_stop` | Trailing stop level at time of event |
| `signal_birth_time` | Unix timestamp of the candle that triggered the signal flip |
| `signal_age_min` | Age of signal in minutes at time of entry |
| `ob_stale` | `1` if order book was stale at event time, `0` if fresh |
| `filter_reason` | Why the bot skipped entry on a given tick |
| `settlement_source` | `kalshi_verified` or `spot_fallback` |
| `btc_price_at_settlement` | BTC price when settlement was checked |
| `pnl_this_trade` | Profit/loss on the trade in dollars |
| `msg` | Human-readable description of the event |

### Event Types

- **`HRTBT`** — Emitted every 10 seconds. Shows current state of all indicators and filters. Not a trade event — used for monitoring.
- **`PAPER_BUY`** — A simulated trade was placed in paper mode.
- **`LIVE_BUY`** — A real order was submitted to Kalshi.
- **`PAYOUT`** — The trade won. Balance increased.
- **`SETTLE`** — The trade lost. Balance decreased.
- **`SETTLE_VERIFIED`** — Session expired but no position was held. Kalshi result logged for record.
- **`ERROR`** — An order failed or an exception was caught.
- **`SYSTEM`** — Startup and shutdown messages.

---

## The Dashboard

Run `dashboard_v5.py` in a separate terminal. Access at `http://localhost:5000`.

The dashboard reads `production_log_v5.csv` live and refreshes every 10 seconds. It shows:

- **Realized P&L** — Cumulative profit/loss from closed trades only. The entry cost of an open position is never shown here — only what has actually been won or lost.
- **UT Bot Signal** — Current signal direction (BUY/SELL) and how old it is.
- **Win Rate** — Wins/(Wins+Losses) across all settled trades.
- **Trade Quality** — Average entry price, spread, signal age at entry, and average PnL per trade.
- **Market** — Current Kalshi ticker, strike, time to expiry, OBI.
- **Order Book** — Live yes/no bids, asks, and liquidity.
- **P&L Chart** — Cumulative realized P&L over time. Color dynamically transitions from green (above zero) to red (below zero). Filter by 1H, 6H, 24H, or ALL.
- **Activity Log** — Last 20 non-heartbeat events.

---

## Understanding the Console Output

```
[  HRTBT   ] KXBTC15M-26FEB231945-45 | BTC:64833.99 | Stop:64750.00 | ATR:52.3 | Sig:BUY Age:3.2m | Y:53c N:46c OBI:+0.625 | Bank:$1000.00
```

- `KXBTC15M-26FEB231945-45` — Current Kalshi market ticker. The number at the end (45) is the strike in hundreds (i.e. $64,500).
- `BTC:64833.99` — Current BTC mid-price from Coinbase.
- `Stop:64750.00` — UT Bot trailing stop level. Signal is BUY because BTC (64833) > Stop (64750).
- `ATR:52.3` — Current ATR. Stop moves 52.3 × 1.0 (sensitivity) = 52.3 points per candle at most.
- `Sig:BUY Age:3.2m` — Signal is BUY, fired 3.2 minutes ago.
- `Y:53c N:46c` — Yes bid is 53¢, No bid is 46¢.
- `OBI:+0.625` — Order book leans YES. Positive = more YES liquidity.
- `Bank:$1000.00` — Current paper balance.

---

## Risk Management

V5 uses flat fractional sizing — the same percentage of bankroll on every trade regardless of signal quality. Key controls:

**Drawdown protection:** `MAX_DAILY_LOSS` halts trading if rolling 24-hour losses exceed the threshold. Set this to something meaningful in live mode — e.g. 5% of your bankroll.

**Position limits:** `MAX_FILLS_PER_SESSION = 1` means one trade per 15-minute window. This prevents the bot from doubling down if a position goes wrong.

**Price filters:** `MARKET_VETO_PRICE` and `MARKET_MAX_ENTRY_PRICE` prevent entering contracts with unfavorable implied probabilities.

**Signal staleness:** `MAX_STALK_POST_SIGNAL_MIN` ensures you only trade on fresh signals. An old signal has less predictive power.

---

## Known Limitations

**No position awareness relative to strike:** The bot doesn't consider where BTC is relative to the strike when deciding to enter. A BUY signal when BTC is $500 below the strike with 3 minutes left is a very different bet than when BTC is $10 above the strike. This is the biggest gap in the current logic.

**MFI and OBI not used for sizing:** Order book imbalance and money flow data are logged but not incorporated into position sizing or filtering decisions.

**1-minute candles only:** UT Bot on 1-minute candles generates signals more frequently than on higher timeframes. Some signals are noise that reverses within the same 15-minute session window.

**Flat sizing:** Every trade risks the same fraction of bankroll regardless of signal confidence. A more sophisticated system would size larger on higher-confidence signals.

**Settlement fallback:** If Kalshi doesn't finalize within the retry window, the bot falls back to comparing BTC spot price against strike. This is generally accurate but not authoritative.
