#!/usr/bin/env python3
"""Phase 4 - Verify Market-Data Reading: BTC & ETH (safe handling).

Read-only, network-blocked verification of the Phase 4 market-data layer that
was missing in Requirement #10:

  * get_klines() validates supported symbols & intervals before any request;
  * empty / None / non-list / HTTP-failure responses return a clean
    (False, None, error) tuple instead of crashing the caller;
  * the newest candle is recency-checked and surfaced as "stale" rather than
    trusted;
  * main.get_klines() delegates to the service and raises a catchable
    requests.RequestException on every failure path, preserving the exact
    happy-path DataFrame + indicator columns.

SAFETY:
  * no real-money orders, no Binance network calls (all transport stubbed),
  * no changes to trading logic, order execution, indicators or live-trading
    behavior,
  * DRY_RUN=True and live_trading_enabled=False remain the defaults.

Usage:
    python3 phase_4_verify_market_data.py
Exit code 0 = all checks passed; 1 = one or more checks failed.
"""

import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd  # noqa: E402
from binance_service import (  # noqa: E402
    BinanceService,
    binance_service,
    SUPPORTED_KLINE_INTERVALS,
)
import main as bot_main  # noqa: E402  (aliased to avoid clash with this harness' own main())


FAILURES = []


def check(name, condition, detail=""):
    """Record a single verification result."""
    print(("PASS" if condition else "FAIL"), "-", name, detail)
    if not condition:
        FAILURES.append(name)


NOW_MS = int(time.time() * 1000)


def _kline_rows(count, start_offset_ms=0, close_price=60000.0):
    """Build `count` synthetic Binance kline rows (oldest -> newest)."""
    rows = []
    for i in range(count):
        open_ms = NOW_MS - (count - 1 - i) * 60000 + start_offset_ms
        close_ms = open_ms + 60000
        rows.append([
            open_ms, str(close_price), str(close_price), str(close_price),
            str(close_price), "1.0", close_ms, "1.0", 1, "1.0", "1.0", "0",
        ])
    return rows


class _FakeResp:
    def __init__(self, payload, status_code=200, exc=None):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)
        self._exc = exc

    def raise_for_status(self):
        if self._exc:
            raise self._exc
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")
        return None

    def json(self):
        if self._exc:
            raise self._exc
        return self._payload


class _FakeSession:
    """Stub transport that records GETs - proves zero Binance contact."""

    def __init__(self, payload, status_code=200, exc=None):
        self._payload = payload
        self.status_code = status_code
        self._exc = exc
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return _FakeResp(self._payload, self.status_code, self._exc)


def _make_svc(payload=None, exc=None, status_code=200):
    svc = BinanceService(api_key="VERIFY_ONLY_KEY", api_secret="VERIFY_ONLY_SECRET")
    svc.session = _FakeSession(payload, status_code=status_code, exc=exc)
    return svc


# 1. Happy path: BTC & ETH klines -------------------------------------------
def test_happy_path():
    for symbol in ("BTC", "ETH"):
        svc = _make_svc(_kline_rows(5))
        ok, df, err = svc.get_klines(symbol, interval="15m", limit=5)
        check(f"happy {symbol}: success", ok is True, f"({err})")
        check(f"happy {symbol}: no error", err is None)
        check(f"happy {symbol}: dataframe returned", df is not None)
        expected_cols = {"open_time", "open", "high", "low", "close", "volume", "close_time"}
        check(f"happy {symbol}: has OHLCV columns",
              df is not None and expected_cols.issubset(set(df.columns)))
        check(f"happy {symbol}: close is numeric & parseable",
              df is not None and float(df["close"].iloc[-1]) == 60000.0)
        check(f"happy {symbol}: exactly one kline request", len(svc.session.calls) == 1)
        check(f"happy {symbol}: request targets /api/v3/klines",
              len(svc.session.calls) == 1 and "/api/v3/klines" in svc.session.calls[0][0])
        check(f"happy {symbol}: symbol pair correct in params",
              svc.session.calls[0][1].get("symbol") == f"{symbol}USDT")


# 2. Supported-interval + supported-symbol validation (no network) -----------
def test_validation():
    svc = _make_svc(_kline_rows(5))

    ok, df, err = svc.get_klines("BTC", interval="7x", limit=5)
    check("unsupported interval rejected", ok is False and err and "Unsupported interval" in err)
    check("unsupported interval -> no network", len(svc.session.calls) == 0)
    check("supported intervals advertised", "1m" in SUPPORTED_KLINE_INTERVALS and "4h" in SUPPORTED_KLINE_INTERVALS)

    for bad in ("", "   ", "bt coin!", "123!@#", "BTC/USDT"):
        ok, df, err = svc.get_klines(bad, interval="1m", limit=5)
        check(f"invalid symbol '{bad}' rejected", ok is False and err and "Invalid symbol" in err)
    check("invalid symbols -> no network calls", len(svc.session.calls) == 0)

    ok, df, err = svc.get_klines("BTCUSDT", interval="1m", limit=5)
    check("BTCUSDT already-prefixed accepted", ok is True, f"({err})")
    check("BTCUSDT pair in request", svc.session.calls[-1][1].get("symbol") == "BTCUSDT")

    ok2, df2, err2 = svc.get_klines("ETH", interval="1h", limit=5)
    check("ETH accepted on 1h", ok2 is True, f"({err2})")
    check("ETH pair in request", svc.session.calls[-1][1].get("symbol") == "ETHUSDT")


# 3. Empty / None / non-list / malformed ------------------------------------
def test_empty_and_malformed():
    svc = _make_svc([])
    ok, df, err = svc.get_klines("BTC", interval="1m", limit=5)
    check("empty list -> failure", ok is False and err and "No klines data" in err, f"({err})")
    check("empty list -> df None", df is None)

    svc2 = _make_svc({"unexpected": True})
    ok2, df2, err2 = svc2.get_klines("BTC", interval="1m", limit=5)
    check("non-list payload -> failure", ok2 is False and err2 and "No klines data" in err2, f"({err2})")
    check("non-list payload -> df None", df2 is None)

    svc3 = _make_svc(None)
    ok3, df3, err3 = svc3.get_klines("BTC", interval="1m", limit=5)
    check("None payload -> failure", ok3 is False and err3 and "No klines data" in err3, f"({err3})")
    check("None payload -> df None", df3 is None)


# 4. HTTP failure -----------------------------------------------------------
def test_http_failure():
    svc = _make_svc(exc=requests.HTTPError("503 Service Unavailable"))
    ok, df, err = svc.get_klines("BTC", interval="1m", limit=5)
    check("http error -> failure", ok is False and err and "Failed to fetch klines" in err, f"({err})")
    check("http error -> df None", df is None)


# 5. Stale data detection ----------------------------------------------------
def test_stale_data():
    # newest candle open_time = 3 days ago; interval 1h -> threshold 2h -> stale
    rows = _kline_rows(5, start_offset_ms=-3 * 86400 * 1000, close_price=61000.0)
    svc = _make_svc(rows)
    ok, df, err = svc.get_klines("BTC", interval="1h", limit=5)
    check("stale data flagged", ok is False and err and "Stale data" in err, f"({err})")
    check("stale data still returns df for inspection", df is not None)

    # fresh data must NOT be flagged stale
    svc2 = _make_svc(_kline_rows(5, close_price=61000.0))
    ok2, df2, err2 = svc2.get_klines("BTC", interval="1h", limit=5)
    check("fresh data accepted", ok2 is True and err2 is None, f"({err2})")


# 6. main.get_klines() delegation -------------------------------------------
def _raw_df(rows=None):
    if rows is None:
        rows = _kline_rows(34)
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_asset_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"]
    df = pd.DataFrame(rows, columns=cols)
    for c in ("open_time", "close_time", "close", "high", "low", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def test_main_delegation():
    original = bot_main.binance_service.get_klines

    # Happy path: indicator columns preserved
    try:
        bot_main.binance_service.get_klines = lambda symbol, interval="15m", limit=100: (True, _raw_df(), None)
        result = bot_main.get_klines("BTC")
        check("main.get_klines happy path returns df", result is not None)
        base_cols = {"open_time", "open", "high", "low", "close", "volume", "close_time"}
        ind_cols = {"ema20", "rsi", "macd", "macd_signal", "macd_histogram", "atr"}
        check("main.get_klines keeps base OHLCV columns", base_cols.issubset(set(result.columns)))
        check("main.get_klines keeps indicator columns", ind_cols.issubset(set(result.columns)))
    finally:
        bot_main.binance_service.get_klines = original

    # Failure paths: raises RequestException (callers already catch)
    try:
        bot_main.binance_service.get_klines = lambda symbol, interval="15m", limit=100: (False, None, "No klines data returned.")
        raised = False
        try:
            bot_main.get_klines("BTC")
        except requests.RequestException:
            raised = True
        check("main.get_klines raises on empty", raised)

        def _stale(symbol, interval="15m", limit=100):
            return (False, _raw_df(), "Stale data ...")
        bot_main.binance_service.get_klines = _stale
        raised = False
        try:
            bot_main.get_klines("BTC")
        except requests.RequestException:
            raised = True
        check("main.get_klines raises on stale", raised)
    finally:
        bot_main.binance_service.get_klines = original


# 7. Safety defaults preserved ----------------------------------------------
def test_safety_defaults():
    old_dry = os.environ.pop("DRY_RUN", None)
    old_live = os.environ.pop("LIVE_TRADING_ENABLED", None)
    try:
        svc = BinanceService()
        check("DRY_RUN default is True (safe)", svc.dry_run is True)
        check("live_trading_enabled default is False (safe)", svc.live_trading_enabled is False)
    finally:
        if old_dry is not None:
            os.environ["DRY_RUN"] = old_dry
        if old_live is not None:
            os.environ["LIVE_TRADING_ENABLED"] = old_live
    print("INFO  module instance -> dry_run=%r live_trading_enabled=%r"
          % (binance_service.dry_run, binance_service.live_trading_enabled))


def main():
    print("Phase 4 - Verify Market Data (BTC & ETH, safe handling)")
    print("Network blocked, no real orders, DRY_RUN default preserved.")
    test_happy_path()
    test_validation()
    test_empty_and_malformed()
    test_http_failure()
    test_stale_data()
    test_main_delegation()
    test_safety_defaults()
    print()
    print("=" * 70)
    if FAILURES:
        print(f"VERIFY MARKET DATA: {len(FAILURES)} FAILURE(S) -> {FAILURES}")
        sys.exit(1)
    print("VERIFY MARKET DATA: ALL CHECKS PASSED (Phase 4)")
    sys.exit(0)


if __name__ == "__main__":
    main()
