#!/usr/bin/env python3
"""
Phase 3.4.5 - Verify Reconnection & Connectivity Resilience
(Project Morningstar / Allmightsee Prime - Paper -> Testnet Validation).

Safe paper/test validation of the EXISTING connectivity/recovery layer:

  Part 1 - BinanceService connectivity primitives (stubbed transport):
           * ping(): ONLINE on HTTP 200, ERROR on HTTP 500,
             OFFLINE on network exceptions (with latency/error fields),
           * get_server_time(): success (server time + drift) and failure,
           * _signed_request(): network failure -> structured Network Error,
           * get_current_price(): success and failure (False, 0.0, error),
           * get_symbol_rules(): network failure -> structured error dict
             (drives the format_* fallbacks used by the paper flow).

  Part 2 - Paper-flow resilience (fallbacks when market data fails):
           * check_paper_positions(): if the ticker call fails, it falls back
             to the last candle close and monitoring continues (TP close still
             executes through the engine),
           * empty or failing fallback leaves positions OPEN without crashing,
           * open_paper_trade(): unavailable exchange rules fall back to raw
             entry price / quantity and the order still routes through the
             engine (DRY_RUN simulated).

  Part 3 - Reconnect cycle end-to-end:
           * connectivity outage -> monitoring survives, position stays OPEN,
           * connectivity restored -> normal monitoring resumes,
           * market reaches TP after reconnect -> engine-routed close works.

SAFETY (identical to Phases 3.4.1-3.4.4):
  * no real-money orders, no real Binance network calls (transport stubbed and
    network sink hard-blocked for the paper flow),
  * DRY_RUN=True default and live trading disabled preserved unchanged,
  * NO production file is modified - only runtime helpers are patched and
    restored, so Phase 3.4.1-3.4.4 behavior is untouched,
  * explicitly OUT of scope: extended multi-day simulations (Phase 3.4.6).

Usage:
    python3 phase_3_4_5_verify_reconnect.py
Exit code 0 = all checks passed; 1 = one or more checks failed.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402  (for realistic transport exceptions)
import pandas as pd  # noqa: E402  (for the kline fallback frames)

from binance_service import BinanceService  # noqa: E402
import main  # noqa: E402  (paper flow under test)

FAILURES = []


def check(name, condition, detail=""):
    """Record a single verification result."""
    print(("PASS" if condition else "FAIL"), "-", name, detail)
    if not condition:
        FAILURES.append(name)


class FakeResponse:
    """Minimal stand-in for requests.Response used by the stubbed transport."""

    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code} error")

    def json(self):
        if self._json is None:
            raise ValueError("No JSON body")
        return self._json


class FakeSession:
    """Stubbed transport: per-method response queues and exception sources."""

    def __init__(self, responses=None, exceptions=None):
        self.responses = responses or {}   # method -> list[FakeResponse]
        self.exceptions = exceptions or {}  # method -> Exception | list[Exception]

    def get(self, url, **kwargs):
        return self._dispatch("GET")

    def post(self, url, **kwargs):
        return self._dispatch("POST")

    def delete(self, url, **kwargs):
        return self._dispatch("DELETE")

    def _dispatch(self, method):
        if method in self.exceptions:
            exc = self.exceptions[method]
            if isinstance(exc, list):
                exc = exc.pop(0)
            raise exc
        queue = self.responses.get(method, [])
        if isinstance(queue, list) and queue:
            return queue.pop(0)
        return FakeResponse(200, {}, "")


def _service_with(session):
    """Fresh BinanceService with a stubbed transport (dummy keys only)."""
    svc = BinanceService(api_key="VERIFY_KEY", api_secret="VERIFY_SECRET",
                         dry_run=True, live_trading_enabled=False)
    svc.session = session
    return svc


# ============================================================================
# PART 1 - BinanceService connectivity primitives (stubbed transport)
# ============================================================================
def verify_connectivity_primitives():
    print("=" * 70)
    print("PART 1 - Connectivity primitives (stubbed transport)")
    print("=" * 70)

    # -- 1.1 ping ONLINE on HTTP 200 ---------------------------------------
    svc = _service_with(FakeSession(responses={"GET": [FakeResponse(200, {}, "")]}))
    p = svc.ping()
    check("1.1: ping ONLINE on 200", p.get("status") == "ONLINE"
          and p.get("latency_ms") is not None and p.get("error") is None,
          f"(status={p.get('status')})")

    # -- 1.2 ping ERROR on HTTP 500 ----------------------------------------
    svc = _service_with(FakeSession(responses={"GET": [FakeResponse(500, {}, "Internal error")]}))
    p = svc.ping()
    check("1.2: ping ERROR on HTTP 500", p.get("status") == "ERROR"
          and "HTTP 500" in p.get("error", ""),
          f"(error={p.get('error')})")

    # -- 1.3 ping OFFLINE on network exception ------------------------------
    svc = _service_with(FakeSession(exceptions={"GET": requests.ConnectionError("simulated connection reset")}))
    p = svc.ping()
    check("1.3: ping OFFLINE on network exception", p.get("status") == "OFFLINE"
          and p.get("latency_ms") is None
          and "simulated connection reset" in p.get("error", ""),
          f"(error={p.get('error')})")

    # -- 1.4 get_server_time success + drift --------------------------------
    svc = _service_with(FakeSession(responses={"GET": [FakeResponse(200, {"serverTime": 1700000000000}, "")]}))
    t = svc.get_server_time()
    check("1.4: server time success + drift", t.get("server_time") == 1700000000000
          and t.get("local_time") is not None and t.get("drift_ms") is not None
          and t.get("error") is None)

    # -- 1.5 get_server_time failure ----------------------------------------
    svc = _service_with(FakeSession(exceptions={"GET": requests.Timeout("timed out")}))
    t = svc.get_server_time()
    check("1.5: server time failure structured", t.get("server_time") is None
          and t.get("drift_ms") is None and "timed out" in t.get("error", "").lower())

    # -- 1.6 _signed_request network failure -> structured error -------------
    svc = _service_with(FakeSession(exceptions={"POST": requests.ConnectionError("connection refused")}))
    r = svc._signed_request("POST", "/api/v3/order", {"symbol": "BTCUSDT"})
    check("1.6: signed request network failure structured",
          r.get("success") is False and r.get("code") is None
          and r.get("http_status") is None
          and "Network Error" in r.get("error", "")
          and "connection refused" in r.get("error", ""),
          f"(error={r.get('error')})")

    # -- 1.7 get_current_price success and failure ---------------------------
    svc = _service_with(FakeSession(responses={"GET": [FakeResponse(200, {"price": "61000.5"}, "")]}))
    ok, price, err = svc.get_current_price("BTC")
    check("1.7: current price success", ok is True and price == 61000.5 and err is None)

    svc = _service_with(FakeSession(exceptions={"GET": requests.Timeout("slow")}))
    ok, price, err = svc.get_current_price("BTC")
    check("1.7: current price failure structured", ok is False and price == 0.0
          and "Failed to fetch price" in err)

    # -- 1.8 get_symbol_rules network failure -> structured error ------------
    svc = _service_with(FakeSession(exceptions={"GET": requests.ConnectionError("offline")}))
    rules = svc.get_symbol_rules("BTC")
    check("1.8: symbol rules failure structured", rules.get("success") is False
          and "Failed to fetch rules" in rules.get("error", ""))


# ============================================================================
# Shared isolation helpers for the paper-flow scenarios (main.py)
# ============================================================================
PRICE = {"v": 60000.0}
ORDER_CALLS = []      # kwargs captured from every engine place_order() call
FALLBACK_CALLS = []   # record when the kline fallback is used
_ORIG = {}


def _analysis(symbol, signal, entry, stop_loss, take_profit, confidence=85):
    return {
        "symbol": symbol, "signal": signal, "entry": entry,
        "stop_loss": stop_loss, "take_profit": take_profit,
        "confidence": confidence,
    }


def reset_paper_state():
    main.PAPER_POSITIONS = {}
    main.PAPER_BALANCE = 1000.0
    main.PAPER_TRADE_HISTORY = []
    ORDER_CALLS.clear()
    FALLBACK_CALLS.clear()


def setup():
    """Patch market-data helpers (network hard-blocked) and capture engine calls."""
    svc = main.binance_service
    _ORIG.update({
        "format_price": svc.format_price,
        "format_quantity": svc.format_quantity,
        "get_current_price": svc.get_current_price,
        "place_order": svc.place_order,
        "session": svc.session,
        "live_trading_enabled": svc.live_trading_enabled,
        "dry_run": svc.dry_run,
        "get_klines": main.get_klines,
    })
    svc.format_price = lambda s, p: (True, round(float(p), 2), None)
    svc.format_quantity = lambda s, q: (True, round(float(q), 6), None)
    svc.get_current_price = lambda s: (True, PRICE["v"], None)
    svc.session = _NoNet()  # hard-block any network
    main.get_klines = _recorded_klines

    def wrapped_place(*args, **kwargs):
        ORDER_CALLS.append(kwargs)
        return _ORIG["place_order"](*args, **kwargs)

    svc.place_order = wrapped_place


def restore():
    svc = main.binance_service
    for key, value in _ORIG.items():
        if key == "get_klines":
            main.get_klines = value
        else:
            setattr(svc, key, value)


def _recorded_klines(symbol, interval="15m", limit=100):
    FALLBACK_CALLS.append((symbol, interval, limit))
    return _ORIG["get_klines"](symbol, interval=interval, limit=limit)


class _NoNet:
    """Network sink that fails loudly - proves zero real Binance contact."""

    def get(self, *a, **k):
        raise AssertionError("NETWORK GET ATTEMPTED during verification")

    def post(self, *a, **k):
        raise AssertionError("NETWORK POST ATTEMPTED during verification")

    def delete(self, *a, **k):
        raise AssertionError("NETWORK DELETE ATTEMPTED during verification")


def _klines_frame(last_close):
    """Small synthetic 1m kline frame with the given last close price."""
    return pd.DataFrame({"close": [last_close - 100.0, last_close]})


# ============================================================================
# PART 2 - Paper-flow resilience (fallbacks when market data fails)
# ============================================================================
def verify_paper_flow_resilience():
    print()
    print("=" * 70)
    print("PART 2 - Paper-flow resilience (fallbacks on market-data failure)")
    print("=" * 70)

    svc = main.binance_service

    # -- 2.1 ticker fails -> kline fallback rescues monitoring ---------------
    reset_paper_state()
    PRICE["v"] = 60000.0
    main.open_paper_trade(_analysis("BTC", "BUY", 60000.0, 59000.0, 63000.0))
    fallback_used = {"v": False}

    def fake_klines(symbol, interval="15m", limit=100):
        fallback_used["v"] = True
        return _klines_frame(63200.0)

    svc.get_current_price = lambda s: (False, 0.0, "Connection reset by peer")
    main.get_klines = fake_klines
    main.check_paper_positions()
    hist = main.PAPER_TRADE_HISTORY
    check("2.1: kline fallback used after ticker failure",
          fallback_used["v"] is True
          and "BTC" not in main.PAPER_POSITIONS
          and len(hist) == 1 and hist[0]["status"] == "TAKE_PROFIT"
          and hist[0]["exit"] == 63200.0,
          f"(history={len(hist)}, fallback_used={fallback_used['v']})")
    check("2.1: fallback TP close still engine-routed (SELL, simulated)",
          len(ORDER_CALLS) == 2 and ORDER_CALLS[1]["side"] == "SELL"
          and str(hist[0].get("close_order_id", "")).startswith("SIM-")
          and hist[0].get("close_execution_status") == "SIMULATED")
    check("2.1: balance updated after rescue close",
          abs(main.PAPER_BALANCE - (1000.0 + hist[0]["pnl"])) < 1e-6)
    svc.get_current_price = lambda s: (True, PRICE["v"], None)

    # -- 2.2 empty fallback -> position stays OPEN, no crash -----------------
    reset_paper_state()
    PRICE["v"] = 60000.0
    main.open_paper_trade(_analysis("BTC", "BUY", 60000.0, 59000.0, 63000.0))
    svc.get_current_price = lambda s: (False, 0.0, "offline")
    main.get_klines = lambda symbol, interval="15m", limit=100: pd.DataFrame()
    main.check_paper_positions()
    check("2.2: empty fallback keeps position OPEN without crashing",
          "BTC" in main.PAPER_POSITIONS
          and main.PAPER_POSITIONS["BTC"]["status"] == "OPEN"
          and main.PAPER_TRADE_HISTORY == []
          and len(ORDER_CALLS) == 1)  # entry only
    svc.get_current_price = lambda s: (True, PRICE["v"], None)

    # -- 2.3 failing fallback -> position stays OPEN, no crash ---------------
    reset_paper_state()
    PRICE["v"] = 60000.0
    main.open_paper_trade(_analysis("BTC", "BUY", 60000.0, 59000.0, 63000.0))
    svc.get_current_price = lambda s: (False, 0.0, "offline")

    def failing_klines(symbol, interval="15m", limit=100):
        raise RuntimeError("kline fetch failed")

    main.get_klines = failing_klines
    main.check_paper_positions()
    check("2.3: failing fallback keeps position OPEN without crashing",
          "BTC" in main.PAPER_POSITIONS and main.PAPER_TRADE_HISTORY == []
          and len(ORDER_CALLS) == 1)
    svc.get_current_price = lambda s: (True, PRICE["v"], None)

    # -- 2.4 unavailable exchange rules -> raw entry/qty fallback ------------
    reset_paper_state()
    svc.format_price = lambda s, p: (False, None, "exchange rules unavailable")
    svc.format_quantity = lambda s, q: (False, None, "exchange rules unavailable")
    PRICE["v"] = 60000.0
    opened = main.open_paper_trade(_analysis("BTC", "BUY", 60000.0, 59000.0, 63000.0))
    pos = main.PAPER_POSITIONS.get("BTC", {})
    raw_qty = 100.0 / 60000.0
    check("2.4: open succeeds with raw entry price + raw quantity",
          opened is True and pos.get("entry") == 60000.0
          and abs(pos.get("quantity") - raw_qty) < 1e-12,
          f"(entry={pos.get('entry')}, qty={pos.get('quantity')})")
    check("2.4: order still routed through engine (simulated)",
          len(ORDER_CALLS) == 1 and ORDER_CALLS[0]["side"] == "BUY"
          and pos.get("execution_status") == "SIMULATED")
    svc.format_price = lambda s, p: (True, round(float(p), 2), None)
    svc.format_quantity = lambda s, q: (True, round(float(q), 6), None)


# ============================================================================
# PART 3 - Reconnect cycle end-to-end
# ============================================================================
def verify_reconnect_cycle():
    print()
    print("=" * 70)
    print("PART 3 - Reconnect cycle end-to-end (outage -> resume -> close)")
    print("=" * 70)

    svc = main.binance_service
    reset_paper_state()
    PRICE["v"] = 60000.0
    main.open_paper_trade(_analysis("BTC", "BUY", 60000.0, 59000.0, 63000.0))

    # -- 3.1 connectivity outage: ticker + klines both fail ------------------
    svc.get_current_price = lambda s: (False, 0.0, "connection reset")

    def dead_klines(symbol, interval="15m", limit=100):
        raise RuntimeError("connection lost")

    main.get_klines = dead_klines
    main.check_paper_positions()
    check("3.1: outage survives monitoring without crashing or closing",
          "BTC" in main.PAPER_POSITIONS
          and main.PAPER_POSITIONS["BTC"]["status"] == "OPEN"
          and main.PAPER_TRADE_HISTORY == []
          and len(ORDER_CALLS) == 1)

    # -- 3.2 connectivity restored: monitoring resumes -----------------------
    svc.get_current_price = lambda s: (True, 62000.0, None)  # mid price
    main.check_paper_positions()
    check("3.2: resumed monitoring leaves position OPEN at mid price",
          "BTC" in main.PAPER_POSITIONS
          and main.PAPER_POSITIONS["BTC"]["status"] == "OPEN"
          and len(ORDER_CALLS) == 1)

    # -- 3.3 market reaches TP after reconnect -> engine-routed close --------
    svc.get_current_price = lambda s: (True, 63200.0, None)  # above TP
    main.check_paper_positions()
    hist = main.PAPER_TRADE_HISTORY
    check("3.3: TP close after reconnect via engine SELL order",
          "BTC" not in main.PAPER_POSITIONS and len(hist) == 1
          and hist[0]["status"] == "TAKE_PROFIT"
          and len(ORDER_CALLS) == 2 and ORDER_CALLS[1]["side"] == "SELL"
          and str(hist[0].get("close_order_id", "")).startswith("SIM-"))
    check("3.3: balance reflects the post-reconnect win",
          abs(main.PAPER_BALANCE - (1000.0 + hist[0]["pnl"])) < 1e-6)


def run():
    print("Phase 3.4.5 - Verify Reconnection & Connectivity Resilience (paper/test)")
    print("No real orders, no network, no config changes. DRY_RUN default preserved.")
    try:
        verify_connectivity_primitives()
        setup()
        verify_paper_flow_resilience()
        verify_reconnect_cycle()
    finally:
        restore()
        reset_paper_state()
    print()
    print("=" * 70)
    if FAILURES:
        print(f"VERIFY RECONNECT: {len(FAILURES)} FAILURE(S) -> {FAILURES}")
        sys.exit(1)
    print("VERIFY RECONNECT: ALL CHECKS PASSED (Phase 3.4.5)")
    sys.exit(0)


if __name__ == "__main__":
    run()