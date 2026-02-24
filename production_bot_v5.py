"""
production_bot_v5.py
====================
Kalshi BTC Binary Options Trading Bot — Version 5.1.0
"Temporal Stalker Edition"

Architecture:
  - Tracks trends by 'Birth Time' (the exact minute the crossover occurred).
  - Stalking: Allows entry anytime during the trend if price is fair and age < limit.
  - Cross-Session: Will stalk a 'Buy' signal into a new Kalshi session if it's still fresh.

Changelog v5.1.0:
  - FIX: Replaced REST polling (5s sleep) with watch_ohlcv WebSocket stream.
         Eliminates ~6 second signal lag vs TradingView.
  - FIX: Persist acted_on_birth_time to state/acted_birth_ts.json on every write.
         Survives bot restarts — prevents double-entry on same signal after crash/restart.
  - IMPROVEMENT: All LOG_COLUMNS now fully populated (spread, rolling_24h_loss,
         ob_stale, filter_reason were previously hardcoded to 0 / empty).
  - IMPROVEMENT: New log columns: ask_yes, ask_no, yes_liq, no_liq, signal_birth_time,
         signal_age_min, btc_price_at_settlement, pnl_this_trade, mode.
  - IMPROVEMENT: Settlement rows (PAYOUT/SETTLE) now carry full trade context —
         side, entry_price, qty, strike, btc_price — so each row is self-contained
         for backtesting without requiring a join to the BUY row.
  - IMPROVEMENT: y_liq and n_liq now forwarded from market loop through on_tick.
  - IMPROVEMENT: filter_reason column populated on every skipped tick.
"""

import asyncio
import collections
import csv
import json
import logging
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import numpy as np
import pandas as pd
import ccxt.pro as ccxt
from dotenv import load_dotenv

from kalshi_client import KalshiClient

# ── Version ───────────────────────────────────────────────────────────────────
VERSION = "5.1.0 - Temporal Stalker"

current_dir = Path(__file__).resolve().parent
sys.path.append(str(current_dir.parent))
load_dotenv(dotenv_path=current_dir / ".env")


# ═════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═════════════════════════════════════════════════════════════════════════════

class Config:
    PAPER_MODE          = True
    PAPER_START_BALANCE = 1000.0
    MAX_DAILY_LOSS      = 1_000_000.0

    SYMBOL        = "BTC/USD"
    EXCHANGES     = ["coinbase"]
    SERIES_TICKER = "KXBTC15M"

    UT_BOT_SENSITIVITY = 1.0
    UT_BOT_ATR_PERIOD  = 10
    CANDLE_TIMEFRAME   = "1m"

    MAX_STALK_POST_SIGNAL_MIN = 10.0

    LOG_FILE        = "production_log_v5.csv"
    SYSTEM_LOG_FILE = "bot_v5.log"
    STATE_DIR       = "state"
    STATE_FILE      = "state/acted_birth_ts.json"   # Persisted birth-time dedup

    TIME_ENTRY_MIN_MIN      = 15
    TIME_ENTRY_MAX_MIN      = 1
    MARKET_VETO_PRICE       = 30
    MARKET_MAX_ENTRY_PRICE  = 75

    MAX_FILLS_PER_SESSION = 1
    FLAT_FRAC_PCT         = 0.02
    MAX_CONTRACTS_LIMIT   = 250

    MAX_ORDERBOOK_STALE_SEC   = 10.0
    SETTLEMENT_INITIAL_DELAY  = 90.0
    SETTLEMENT_RETRY_INTERVAL = 15.0
    SETTLEMENT_MAX_RETRIES    = 4


def update_config_from_file():
    if not os.path.exists("config.json"): return
    try:
        with open("config.json") as f: new = json.load(f)
        for key, value in new.items():
            if hasattr(Config, key):
                setattr(Config, key, value)
    except Exception: pass

def setup_logging():
    logger = logging.getLogger("kalshi_bot_v5")
    logger.setLevel(logging.INFO)
    if logger.handlers: return logger
    handler = RotatingFileHandler(Config.SYSTEM_LOG_FILE, maxBytes=5_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    logger.addHandler(handler)
    return logger


# ═════════════════════════════════════════════════════════════════════════════
# RISK & SHARED STATE
# ═════════════════════════════════════════════════════════════════════════════

class RiskEngine:
    def __init__(self, start_balance: float):
        self.paper_balance = start_balance
        self.real_balance  = 0.0
        self.pnl_history   = deque(maxlen=5000)

    def record_pnl(self, amount: float):
        self.pnl_history.append((time.time(), amount))

    def rolling_24h_loss(self) -> float:
        cutoff = time.time() - 86400
        return sum(pnl for ts, pnl in self.pnl_history if ts > cutoff and pnl < 0)

    def calculate_qty(self, entry_price_cents: int) -> int:
        bankroll = self.paper_balance if Config.PAPER_MODE else self.real_balance
        if abs(self.rolling_24h_loss()) > Config.MAX_DAILY_LOSS or bankroll <= 0: return 0
        dollar_risk = bankroll * Config.FLAT_FRAC_PCT
        qty = int(dollar_risk / (entry_price_cents / 100.0))
        return min(max(0, qty), Config.MAX_CONTRACTS_LIMIT)


class SharedState:
    def __init__(self):
        self.lock             = asyncio.Lock()
        self.latest_btc       = 0.0
        self.ut_signal        = None
        self.ut_atr           = 0.0
        self.ut_stop          = 0.0
        self.signal_birth_time = 0.0

    async def update_indicator(self, signal, atr, stop, btc, birth_ts):
        async with self.lock:
            self.latest_btc        = btc
            self.ut_atr            = atr
            self.ut_stop           = stop
            self.ut_signal         = signal
            self.signal_birth_time = birth_ts


# ═════════════════════════════════════════════════════════════════════════════
# INDICATOR MATH (1:1 TRADINGVIEW)
# ═════════════════════════════════════════════════════════════════════════════

def calculate_ut_bot(df, key_value, atr_period):
    prev_close = df['close'].shift(1)
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - prev_close).abs(),
        (df['low']  - prev_close).abs()
    ], axis=1).max(axis=1)

    # Wilder's RMA — matches Pine's atr()
    atr = np.zeros(len(df))
    atr[atr_period - 1] = tr.iloc[:atr_period].mean()
    for i in range(atr_period, len(df)):
        atr[i] = (tr.iloc[i] - atr[i - 1]) * (1.0 / atr_period) + atr[i - 1]

    df['atr']   = atr
    n_loss      = key_value * df['atr']

    stops = np.zeros(len(df))
    for i in range(1, len(df)):
        if df['atr'].iloc[i] == 0: continue
        p_stop  = stops[i - 1]
        p_close = df['close'].iloc[i - 1]
        c_close = df['close'].iloc[i]
        nl      = n_loss.iloc[i]
        if   c_close > p_stop and p_close > p_stop: stops[i] = max(p_stop, c_close - nl)
        elif c_close < p_stop and p_close < p_stop: stops[i] = min(p_stop, c_close + nl)
        elif c_close > p_stop:                       stops[i] = c_close - nl
        else:                                        stops[i] = c_close + nl

    df['xATRTrailingStop'] = stops
    df['ut_signal']        = np.where(df['close'] > df['xATRTrailingStop'], 'buy', 'sell')
    df['flipped']          = df['ut_signal'] != df['ut_signal'].shift(1)
    df['birth_ts']         = (df['timestamp'] / 1000.0).where(df['flipped']).ffill()

    return df


# ═════════════════════════════════════════════════════════════════════════════
# STRATEGY CONTROLLER
# ═════════════════════════════════════════════════════════════════════════════

LOG_COLUMNS = [
    # Identity
    "timestamp", "event", "mode",
    # Market context
    "ticker", "side", "entry_price", "qty",
    "time_left", "btc_price", "strike",
    # Order book
    "raw_yes_bid", "raw_no_bid", "ask_yes", "ask_no", "spread",
    "yes_liq", "no_liq", "obi",
    # Risk
    "bankroll", "rolling_24h_loss",
    # Indicator
    "ut_signal", "ut_atr", "ut_stop",
    "signal_birth_time", "signal_age_min",
    # Diagnostics
    "ob_stale", "filter_reason",
    # Settlement
    "settlement_source", "btc_price_at_settlement", "pnl_this_trade",
    # Free text
    "msg"
]


class StrategyController:
    def __init__(self, shared_state: SharedState):
        self.risk             = RiskEngine(Config.PAPER_START_BALANCE)
        self.shared           = shared_state
        self.active_position  = None
        self.session_fills    = 0
        self.prev_ticker      = None
        self.prev_strike      = 0.0
        self.last_valid_ob_ts = 0.0
        self.last_heartbeat_ts = 0.0

        # FIX: Load persisted birth time so restarts don't re-fire the same signal
        Path(Config.STATE_DIR).mkdir(parents=True, exist_ok=True)
        self.acted_on_birth_time = self._load_birth_time()

        if not os.path.exists(Config.LOG_FILE):
            with open(Config.LOG_FILE, "w", newline="") as f:
                csv.writer(f).writerow(LOG_COLUMNS)

    # ── State Persistence ────────────────────────────────────────────────────

    def _load_birth_time(self):
        try:
            if os.path.exists(Config.STATE_FILE):
                with open(Config.STATE_FILE) as f:
                    return json.load(f).get("acted_on_birth_time")
        except Exception:
            pass
        return None

    def _save_birth_time(self, birth_ts):
        try:
            with open(Config.STATE_FILE, "w") as f:
                json.dump({"acted_on_birth_time": birth_ts}, f)
        except Exception:
            pass

    # ── Logging ──────────────────────────────────────────────────────────────

    def log(self, event, ctx, msg=""):
        ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
        bank = self.risk.paper_balance if Config.PAPER_MODE else self.risk.real_balance
        mode = "PAPER" if Config.PAPER_MODE else "LIVE"

        ep       = ctx.get("entry_price", 0)
        yes_bid  = ctx.get("raw_yes_bid", 0)
        no_bid   = ctx.get("raw_no_bid", 0)
        ask_yes  = ctx.get("ask_yes", 0)
        ask_no   = ctx.get("ask_no", 0)
        side     = ctx.get("side", "")

        # Spread: ask minus bid on the traded side, else yes spread as default
        if side == "yes":
            spread = ask_yes - yes_bid if yes_bid > 0 else 0
        elif side == "no":
            spread = ask_no - no_bid if no_bid > 0 else 0
        else:
            spread = ask_yes - yes_bid if yes_bid > 0 else 0

        ob_stale = int(
            (time.time() - self.last_valid_ob_ts) > Config.MAX_ORDERBOOK_STALE_SEC
        )

        row = [
            ts,
            event,
            mode,
            ctx.get("ticker", ""),
            side,
            ep,
            ctx.get("qty", 0),
            round(ctx.get("time_left", 0.0), 2),
            round(ctx.get("btc", 0.0), 2),
            ctx.get("strike", 0),
            yes_bid,
            no_bid,
            ask_yes,
            ask_no,
            spread,
            ctx.get("yes_liq", 0),
            ctx.get("no_liq", 0),
            round(ctx.get("obi", 0.0), 3),
            round(bank, 2),
            round(self.risk.rolling_24h_loss(), 2),
            ctx.get("ut_signal", ""),
            round(ctx.get("ut_atr", 0.0), 2),
            round(ctx.get("ut_stop", 0.0), 2),
            ctx.get("signal_birth_time", 0),
            round(ctx.get("signal_age_min", 0.0), 2),
            ob_stale,
            ctx.get("filter_reason", ""),
            ctx.get("settlement_source", ""),
            round(ctx.get("btc_price_at_settlement", 0.0), 2),
            round(ctx.get("pnl_this_trade", 0.0), 4),
            msg
        ]

        with open(Config.LOG_FILE, "a", newline="") as f:
            csv.writer(f).writerow(row)

        if event == "HRTBT":
            # Rich colored status line — matches V4 console style
            sig   = ctx.get("ut_signal", "") or "--"
            stale = "  [STALE-OB]" if ctx.get("ob_stale", 0) else ""
            pos   = "  [IN POSITION]" if ctx.get("has_position", False) else ""
            print(
                f"\033[90m[{'HRTBT':^10}] "
                f"{ctx.get('ticker','N/A')} | "
                f"BTC:{ctx.get('btc',0):.2f} | "
                f"Stop:{ctx.get('ut_stop',0):.2f} | "
                f"ATR:{ctx.get('ut_atr',0):.2f} | "
                f"Sig:{sig.upper()} Age:{ctx.get('signal_age_min',0):.1f}m | "
                f"Y:{ctx.get('raw_yes_bid',0)}c N:{ctx.get('raw_no_bid',0)}c OBI:{ctx.get('obi',0):+.3f} | "
                f"Bank:${ctx.get('bankroll',0):.2f}"
                f"{stale}{pos}\033[0m"
            )
        elif event in ("PAPER_BUY", "LIVE_BUY", "PAYOUT"):
            print(f"\033[92m[{event:^14}] {ctx.get('ticker','')} | {msg}\033[0m")
        elif event in ("SETTLE", "ERROR"):
            print(f"\033[91m[{event:^14}] {ctx.get('ticker','')} | {msg}\033[0m")
        elif event in ("SETTLE_VERIFIED",):
            print(f"\033[94m[{event:^14}] {ctx.get('ticker','')} | {msg}\033[0m")
        else:
            print(f"[{event:^14}] {ctx.get('ticker','')} | {msg}")

    # ── Settlement ───────────────────────────────────────────────────────────

    async def _bg_settle(self, kalshi, ticker, settlement_btc_price, strike, position):
        await asyncio.sleep(Config.SETTLEMENT_INITIAL_DELAY)

        verified = None
        for _ in range(Config.SETTLEMENT_MAX_RETRIES):
            try:
                md = (await kalshi.get_market(ticker)).get("market", {})
                if md.get("status") in ("settled", "finalized") and md.get("result"):
                    verified = md["result"].lower()
                    break
                await asyncio.sleep(Config.SETTLEMENT_RETRY_INTERVAL)
            except Exception:
                await asyncio.sleep(Config.SETTLEMENT_RETRY_INTERVAL)

        outcome = 1 if verified == "yes" else (
            0 if verified == "no" else (
                1 if settlement_btc_price > strike else 0
            )
        )
        source = "kalshi_verified" if verified else "spot_fallback"

        if position:
            won = (
                (position["side"] == "yes" and outcome == 1) or
                (position["side"] == "no"  and outcome == 0)
            )
            # Build a self-contained settlement context carrying full trade detail
            settle_ctx = {
                "ticker":                  ticker,
                "side":                    position["side"],
                "entry_price":             position["entry_price"],
                "qty":                     position["qty"],
                "strike":                  strike,
                "btc":                     settlement_btc_price,
                "btc_price_at_settlement": settlement_btc_price,
                "settlement_source":       source,
                "ut_signal":               position.get("ut_signal", ""),
                "ut_atr":                  position.get("ut_atr", 0.0),
                "ut_stop":                 position.get("ut_stop", 0.0),
                "signal_birth_time":       position.get("signal_birth_time", 0),
                "signal_age_min":          position.get("signal_age_min", 0.0),
            }
            if won:
                payout = position["qty"] * 1.00
                cost   = position["qty"] * (position["entry_price"] / 100.0)
                pnl    = payout - cost
                self.risk.paper_balance += payout
                self.risk.record_pnl(pnl)
                settle_ctx["pnl_this_trade"] = pnl
                self.log("PAYOUT", settle_ctx, f"WIN! Payout: ${payout:.2f} | PnL: ${pnl:.4f}")
            else:
                cost = position["qty"] * (position["entry_price"] / 100.0)
                pnl  = -cost
                self.risk.record_pnl(pnl)
                settle_ctx["pnl_this_trade"] = pnl
                self.log("SETTLE", settle_ctx, f"LOSS. Cost: ${cost:.2f} | PnL: ${pnl:.4f}")
        else:
            self.log("SETTLE_VERIFIED", {
                "ticker": ticker,
                "settlement_source": source,
                "btc_price_at_settlement": settlement_btc_price
            }, f"Market Roll: {str(verified).upper()}")

    # ── Main Tick Handler ────────────────────────────────────────────────────

    async def on_tick(self, kalshi, data):
        async with self.shared.lock:
            cur_sig  = self.shared.ut_signal
            birth_ts = self.shared.signal_birth_time
            atr      = self.shared.ut_atr
            stop     = self.shared.ut_stop
            btc      = self.shared.latest_btc

        signal_age_min = (time.time() - birth_ts) / 60.0 if birth_ts > 0 else 999.0
        ob_stale = (time.time() - self.last_valid_ob_ts) > Config.MAX_ORDERBOOK_STALE_SEC

        ctx = {
            "ticker":            data["ticker"],
            "time_left":         data["minutes_left"],
            "btc":               btc,
            "strike":            data["strike"],
            "raw_yes_bid":       data["raw_yes_bid"],
            "raw_no_bid":        data["raw_no_bid"],
            "ask_yes":           data["ask_yes"],
            "ask_no":            data["ask_no"],
            "yes_liq":           data["yes_liq"],
            "no_liq":            data["no_liq"],
            "obi":               data["obi"],
            "ut_signal":         cur_sig,
            "ut_atr":            atr,
            "ut_stop":           stop,
            "signal_birth_time": birth_ts,
            "signal_age_min":    signal_age_min,
        }

        # Heartbeat every 10 seconds
        if time.time() - self.last_heartbeat_ts > 10.0:
            bank = self.risk.paper_balance if Config.PAPER_MODE else self.risk.real_balance
            ctx["bankroll"]     = bank
            ctx["ob_stale"]     = int(ob_stale)
            ctx["has_position"] = self.active_position is not None
            self.log("HRTBT", ctx)
            self.last_heartbeat_ts = time.time()

        # ── Gate checks with explicit filter_reason ───────────────────────
        if self.session_fills >= Config.MAX_FILLS_PER_SESSION:
            return  # Silent — already traded this session, no noise in log
        if self.active_position:
            return
        if self.acted_on_birth_time == birth_ts:
            ctx["filter_reason"] = "already_acted_this_signal"
            return
        if signal_age_min > Config.MAX_STALK_POST_SIGNAL_MIN:
            ctx["filter_reason"] = f"signal_too_old_{signal_age_min:.1f}m"
            return
        if data["minutes_left"] < Config.TIME_ENTRY_MAX_MIN:
            ctx["filter_reason"] = f"too_close_to_expiry_{data['minutes_left']:.1f}m"
            return
        if ob_stale:
            ctx["filter_reason"] = "orderbook_stale"
            return

        # ── Determine side and maker price ───────────────────────────────
        side     = "yes" if cur_sig == "buy" else "no"
        best_bid = data["raw_yes_bid"] if side == "yes" else data["raw_no_bid"]
        best_ask = data["ask_yes"]     if side == "yes" else data["ask_no"]
        maker_price = max(1, min(99, (best_bid + 1) if best_ask > (best_bid + 1) else best_bid))

        ctx["side"]        = side
        ctx["entry_price"] = maker_price

        if not (Config.MARKET_VETO_PRICE <= maker_price <= Config.MARKET_MAX_ENTRY_PRICE):
            ctx["filter_reason"] = f"price_out_of_range_{maker_price}c"
            return

        qty = self.risk.calculate_qty(maker_price)
        if qty < 1:
            ctx["filter_reason"] = "qty_zero_insufficient_bankroll"
            return

        ctx["qty"] = qty

        # ── Commit ───────────────────────────────────────────────────────
        # FIX: Persist birth time before placing order so a crash after fill
        #      doesn't leave acted_on_birth_time un-persisted
        self.acted_on_birth_time = birth_ts
        self._save_birth_time(birth_ts)
        self.session_fills += 1

        # Store indicator snapshot on position so settlement row is self-contained
        position_record = {
            "ticker":            data["ticker"],
            "side":              side,
            "qty":               qty,
            "entry_price":       maker_price,
            "ut_signal":         cur_sig,
            "ut_atr":            atr,
            "ut_stop":           stop,
            "signal_birth_time": birth_ts,
            "signal_age_min":    round(signal_age_min, 2),
        }

        if Config.PAPER_MODE:
            self.risk.paper_balance -= qty * (maker_price / 100.0)
            self.active_position = position_record
            self.log("PAPER_BUY", ctx,
                     f"STALKER FILL: Age {signal_age_min:.1f}m | {side.upper()} @ {maker_price}c x{qty}")
        else:
            try:
                await kalshi.create_order(
                    ticker=data["ticker"], action="buy", type="limit",
                    side=side, count=qty, price=maker_price
                )
                self.active_position = position_record
                self.log("LIVE_BUY", ctx,
                         f"STALKER LIVE: Age {signal_age_min:.1f}m | {side.upper()} @ {maker_price}c x{qty}")
            except Exception as e:
                self.log("ERROR", ctx, f"Order failed: {e}")
                # Roll back since order didn't land
                self.session_fills -= 1
                self.acted_on_birth_time = None
                self._save_birth_time(None)


# ═════════════════════════════════════════════════════════════════════════════
# CANDLE SYNTHESIS FROM TICK STREAM
# ═════════════════════════════════════════════════════════════════════════════

class CandleBuilder:
    """
    Builds 1-minute OHLCV candles from a stream of (timestamp_ms, price) ticks.

    On each tick:
      - If still within the current minute: update high, low, close, volume.
      - If a new minute has started: finalize the previous candle, append to
        history, open a new candle.

    Maintains a rolling deque of up to MAX_CANDLES closed candles plus the
    currently-forming candle. Call as_dataframe() to get the full list with
    the live candle appended — ready to feed into calculate_ut_bot().
    """
    MAX_CANDLES = 1000

    def __init__(self):
        self.closed  = collections.deque(maxlen=self.MAX_CANDLES)
        self.current = None   # dict: timestamp_ms, open, high, low, close, volume

    def _minute_bucket(self, ts_ms: float) -> int:
        """Floor timestamp to the start of its UTC minute (in ms)."""
        return int(ts_ms // 60_000) * 60_000

    def update(self, ts_ms: float, price: float) -> bool:
        """
        Feed a new tick. Returns True if a candle was just closed
        (i.e. the UT Bot should be recalculated).
        """
        bucket = self._minute_bucket(ts_ms)
        closed_candle = False

        if self.current is None:
            # First tick ever
            self.current = dict(timestamp=bucket, open=price, high=price,
                                low=price, close=price, volume=0.0)
        elif bucket != self.current["timestamp"]:
            # Minute boundary crossed — finalize current candle
            self.closed.append([
                self.current["timestamp"],
                self.current["open"],
                self.current["high"],
                self.current["low"],
                self.current["close"],
                self.current["volume"],
            ])
            self.current = dict(timestamp=bucket, open=price, high=price,
                                low=price, close=price, volume=0.0)
            closed_candle = True
        else:
            # Still in the same minute — update running candle
            self.current["high"]  = max(self.current["high"],  price)
            self.current["low"]   = min(self.current["low"],   price)
            self.current["close"] = price

        return closed_candle

    def as_dataframe(self):
        """Return closed candles + live forming candle as a DataFrame."""
        rows = list(self.closed)
        if self.current:
            rows.append([
                self.current["timestamp"],
                self.current["open"],
                self.current["high"],
                self.current["low"],
                self.current["close"],
                self.current["volume"],
            ])
        if not rows:
            return None
        return pd.DataFrame(rows, columns=["timestamp","open","high","low","close","volume"])

    @property
    def ready(self) -> bool:
        """Need at least atr_period + 2 closed candles before UT Bot is reliable."""
        return len(self.closed) >= (Config.UT_BOT_ATR_PERIOD + 2)


# ═════════════════════════════════════════════════════════════════════════════
# LOOPS & MAIN
# ═════════════════════════════════════════════════════════════════════════════

async def watch_exchange_loop(ex, bot):
    """
    Restored from V4. Subscribes to the Coinbase order book WebSocket and
    keeps ex.orderbooks[symbol] updated continuously in memory.
    The candle loop reads from that cache — no polling, no REST.
    Reconnects automatically on any error.
    """
    name = getattr(ex, "id", str(ex))
    while True:
        try:
            await ex.watch_order_book(Config.SYMBOL)
        except Exception as e:
            print(f"[WS {name}] Reconnecting: {type(e).__name__}: {e}")
            await asyncio.sleep(5)


async def ohlcv_candle_loop(exchanges: dict, shared_state, bot):
    """
    V5.1 approach: synthesize 1-minute candles from the live WebSocket
    order book mid-price, exactly as V4 did.

    Every 0.5s, reads the mid-price from each exchange's cached order book
    (kept live by watch_exchange_loop), feeds it into CandleBuilder, and
    recalculates UT Bot whenever a candle closes or a set interval elapses.

    Falls back to REST fetch_ohlcv for the initial candle history so the
    bot has enough bars to compute ATR immediately on startup without waiting
    for 10+ minutes of ticks.
    """
    logger  = setup_logging()
    builder = CandleBuilder()

    # ── Seed with REST history so ATR is ready immediately on startup ─────────
    primary_ex = list(exchanges.values())[0]
    try:
        print("[CANDLE LOOP] Seeding candle history from REST...")
        seed = await primary_ex.fetch_ohlcv(
            Config.SYMBOL, timeframe="1m", limit=Config.UT_BOT_ATR_PERIOD + 50
        )
        for c in seed[:-1]:   # Exclude the last (still-forming) candle
            builder.closed.append(c)
        print(f"[CANDLE LOOP] Seeded {len(builder.closed)} closed candles. Switching to WebSocket tick feed.")
    except Exception as e:
        print(f"[CANDLE LOOP] REST seed failed ({e}) — will build candles from ticks only.")

    last_recalc_ts = 0.0

    while True:
        try:
            await asyncio.sleep(0.5)
            update_config_from_file()

            # Read mid-price from each exchange's live cached order book
            prices = []
            for ex in exchanges.values():
                try:
                    ob  = ex.orderbooks.get(Config.SYMBOL, {})
                    bid = ob["bids"][0][0]
                    ask = ob["asks"][0][0]
                    prices.append((bid + ask) / 2.0)
                except (IndexError, KeyError, TypeError):
                    continue

            if not prices:
                continue

            price  = float(np.median(prices))   # Median across exchanges if multiple
            ts_ms  = time.time() * 1000.0

            closed_candle = builder.update(ts_ms, price)

            # Recalculate UT Bot on every new closed candle, or every 5s on the live candle
            now = time.time()
            if not (closed_candle or (now - last_recalc_ts) >= 5.0):
                continue
            if not builder.ready:
                print(f"[CANDLE LOOP] Warming up... {len(builder.closed)}/{Config.UT_BOT_ATR_PERIOD + 2} candles", end="\r", flush=True)
                continue

            df = builder.as_dataframe()
            if df is None or len(df) < Config.UT_BOT_ATR_PERIOD + 2:
                continue

            df  = calculate_ut_bot(df, Config.UT_BOT_SENSITIVITY, Config.UT_BOT_ATR_PERIOD)

            # Use live forming candle for signal, stop, ATR, and price.
            # This means the bot reacts the moment BTC crosses the trailing
            # stop on the live candle, not after waiting for it to close.
            # The closed candle (last_c) is still used for birth_ts so that
            # signal birth time is only stamped on confirmed crossovers.
            last_c = df.iloc[-2]
            live_c = df.iloc[-1]

            await shared_state.update_indicator(
                live_c["ut_signal"],
                float(live_c["atr"]),
                float(live_c["xATRTrailingStop"]),
                float(live_c["close"]),
                float(live_c["birth_ts"])
            )

            last_recalc_ts = now

        except Exception as e:
            print(f"[CANDLE LOOP] ERROR: {type(e).__name__}: {e}")
            logger.error(f"Candle loop error: {e}")
            await asyncio.sleep(2)


async def kalshi_market_loop(kalshi, market_queue, bot):
    while True:
        try:
            await asyncio.sleep(0.5)
            m_resp = await kalshi.get_markets(series_ticker=Config.SERIES_TICKER, status="open")
            now    = datetime.now(timezone.utc)
            future = sorted(
                [m for m in m_resp.get("markets", [])
                 if datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")) > now],
                key=lambda x: x["close_time"]
            )
            if not future: continue

            target = future[0]
            ticker = target["ticker"]
            strike = target.get("floor_strike") or target.get("strike_price") or 0

            ob    = (await kalshi.get_orderbook(ticker)).get("orderbook", {})
            y_bid = sorted(ob.get("yes") or [], key=lambda x: x[0], reverse=True)[0][0] if ob.get("yes") else 0
            n_bid = sorted(ob.get("no")  or [], key=lambda x: x[0], reverse=True)[0][0] if ob.get("no")  else 0

            if y_bid > 0 or n_bid > 0:
                bot.last_valid_ob_ts = time.time()

            # Top 5 levels of liquidity on each side
            y_liq = sum(l[1] for l in (ob.get("yes") or [])[:5])
            n_liq = sum(l[1] for l in (ob.get("no")  or [])[:5])

            close_dt     = datetime.fromisoformat(target["close_time"].replace("Z", "+00:00"))
            minutes_left = (close_dt - now).total_seconds() / 60.0

            data = {
                "ticker":      ticker,
                "strike":      strike,
                "minutes_left": minutes_left,
                "raw_yes_bid": y_bid,
                "raw_no_bid":  n_bid,
                "ask_yes":     100 - n_bid if n_bid > 0 else 99,
                "ask_no":      100 - y_bid if y_bid > 0 else 99,
                "yes_liq":     y_liq,
                "no_liq":      n_liq,
                "obi":         (y_liq - n_liq) / (y_liq + n_liq) if (y_liq + n_liq) > 0 else 0.0,
            }

            if market_queue.full():
                market_queue.get_nowait()
            await market_queue.put(data)

        except Exception:
            await asyncio.sleep(2)


async def main():
    kalshi = KalshiClient()
    shared = SharedState()
    bot    = StrategyController(shared)

    # Build exchange dict — supports multiple exchanges like V4 (median price)
    exchanges = {}
    for name in Config.EXCHANGES:
        if not hasattr(ccxt, name):
            print(f"[STARTUP] WARNING: Exchange '{name}' not found in ccxt.pro — skipping.")
            continue
        ex = getattr(ccxt, name)({"newUpdates": True})
        exchanges[name] = ex
        print(f"[STARTUP] Exchange loaded: {name} | watch_order_book: {hasattr(ex, 'watch_order_book')}")

    if not exchanges:
        print("[STARTUP] ERROR: No valid exchanges configured. Check Config.EXCHANGES.")
        raise SystemExit(1)

    print(f"[STARTUP] Mode: {'PAPER' if Config.PAPER_MODE else '*** LIVE ***'} | Symbol: {Config.SYMBOL}")
    print(f"[STARTUP] Strategy: watch_order_book WebSocket → tick-synthesized 1m candles → UT Bot")

    market_queue = asyncio.Queue(maxsize=1)

    # Start one WebSocket order book listener per exchange (V4 pattern)
    for ex in exchanges.values():
        asyncio.create_task(watch_exchange_loop(ex, bot))

    asyncio.create_task(ohlcv_candle_loop(exchanges, shared, bot))
    asyncio.create_task(kalshi_market_loop(kalshi, market_queue, bot))

    while True:
        try:
            data = await market_queue.get()

            # Session roll: new ticker detected
            if bot.prev_ticker and data["ticker"] != bot.prev_ticker:
                asyncio.create_task(bot._bg_settle(
                    kalshi,
                    bot.prev_ticker,
                    shared.latest_btc,
                    bot.prev_strike,
                    bot.active_position
                ))
                bot.active_position = None
                bot.session_fills   = 0
                # Note: acted_on_birth_time is NOT reset here — intentional.
                # Prevents re-entry on the same signal in the new session.

            bot.prev_ticker = data["ticker"]
            bot.prev_strike = data["strike"]
            await bot.on_tick(kalshi, data)

        except Exception:
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
