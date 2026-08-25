#!/usr/bin/env python3
"""
Phase 3.4.3 - Verify Position Tracking & TP/SL Exits
(Project Morningstar / Allmightsee Prime - Paper -> Testnet Validation).

Safe paper/test validation of the EXISTING engine-routed paper flow
(main.py open_paper_trade / check_paper_positions, Phase 3.3):

  Part 1 - Position tracking:
           * a BUY entry routed through the engine records a paper position
             (order_id, order_type, execution_status, execution_price),
           * duplicate-symbol and HOLD signals are refused,
           * no TP/SL touch keeps the position OPEN (no premature close).

  Part 2 - TP/SL exits routed through the engine:
           * BUY long  -> TAKE_PROFIT  (SELL close order), STOP_LOSS (SELL),
           * SELL short -> TAKE_PROFIT (BUY close order),  STOP_LOSS (BUY),
           * offsetting side + matching quantity, simulated close order IDs,
           * position removed, history appended, PnL recorded, balance updated.

  Part 3 - Safety guards preserved:
           * paper flow blocked when live trading is enabled,
           * paper flow blocked when DRY_RUN is off,
           * non-simulated engine result is refused.

SAFETY (identical to Phases 3.4.1/3.4.2):
  * no real-money orders, no Binance network calls (network sink hard-blocked),
  * DRY_RUN=True default and live trading disabled preserved unchanged,
  * NO production file is modified - this harness only imports main.py and
    patches runtime helper methods for isolation (restored afterwards),
  * explicitly OUT of scope: dedicated balance/PnL accounting suites,
    reconnection testing, extended simulations.

Usage:
    python3 phase_3_4_3_verify_positions.py
Exit code 0 = all checks passed; 1 = one or more checks failed.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main  # noqa: E402  (paper engine under test)

FAILURES = []


def check(name, condition, detail=""):
    """Record a single verification result."""
    print(("PASS" if condition else "FAIL"), "-", name, detail)
    if not condition:
        FAILURES.append(name)


class _NoNet:
    """Network sink that fails loudly - proves zero Binance contact."""

    def get(self, *a, **k):
        raise AssertionError("NETWORK GET ATTEMPTED during verification")

    def post(self, *a, **k):
        raise AssertionError("NETWORK POST ATTEMPTED during verification")

    def delete(self, *a, **k):
        raise AssertionError("NETWORK DELETE ATTEMPTED during verification")


# Runtime state shared between the paper engine and the harness.
PRICE = {"v": 60000.0}
ORDER_CALLS = []  # kwargs captured from every engine place_order() call
_ORIG = {}


def _analysis(symbol, signal, entry, stop_loss, take_profit, confidence=85):
    return {
        "symbol": symbol, "signal": signal, "entry": entry,
        "stop_loss": stop_loss, "take_profit": take_profit,
        "confidence": confidence,
    }


def reset_paper_state():
    """Isolate each scenario: clean paper book/balance/history."""
    main.PAPER_POSITIONS = {}
    main.PAPER_BALANCE = 1000.0
    main.PAPER_TRADE_HISTORY = []
    ORDER_CALLS.clear()


def setup():
    """Patch runtime market-data helpers so no network is touched."""
    svc = main.binance_service
    _ORIG.update({
        "format_price": svc.format_price,
        "format_quantity": svc.format_quantity,
        "get_current_price": svc.get_current_price,
        "place_order": svc.place_order,
        "session": svc.session,
        "live_trading_enabled": svc.live_trading_enabled,
        "dry_run": svc.dry_run,
    })
    svc.format_price = lambda s, p: (True, round(float(p), 2), None)
    svc.format_quantity = lambda s, q: (True, round(float(q), 6), None)
    svc.get_current_price = lambda s: (True, PRICE["v"], None)
    svc.session = _NoNet()  # hard-block any network

    def wrapped_place(*args, **kwargs):
        ORDER_CALLS.append(kwargs)
        return _ORIG["place_order"](*args, **kwargs)

    svc.place_order = wrapped_place


def restore():
    """Restore every patched attribute on the shared engine instance."""
    svc = main.binance_service
    for key, value in _ORIG.items():
        setattr(svc, key, value)


# ============================================================================
# PART 1 - Position tracking
# ============================================================================
def verify_position_tracking():
    print("=" * 70)
    print("PART 1 - Position tracking (engine-routed paper flow)")
    print("=" * 70)

    reset_paper_state()
    svc = main.binance_service
    check("1.1: default DRY_RUN=True preserved", svc.dry_run is True, f"(dry_run={svc.dry_run})")
    check("1.1: live trading disabled preserved", svc.live_trading_enabled is False)

    PRICE["v"] = 60000.0
    opened = main.open_paper_trade(_analysis("BTC", "BUY", 60000.0, 59000.0, 63000.0))
    check("1.2: BUY entry accepted", opened is True)
    pos = main.PAPER_POSITIONS.get("BTC", {})
    check("1.2: position recorded with engine order fields",
          pos.get("symbol") == "BTC" and pos.get("side") == "BUY"
          and pos.get("status") == "OPEN"
          and str(pos.get("order_id", "")).startswith("SIM-")
          and pos.get("execution_status") == "SIMULATED"
          and pos.get("order_type") == "MARKET"
          and pos.get("execution_price") == 60000.0,
          f"(order_id={pos.get('order_id')}, execution_price={pos.get('execution_price')})")
    check("1.2: position has SL/TP + size",
          pos.get("stop_loss") == 59000.0 and pos.get("take_profit") == 63000.0
          and pos.get("quantity", 0) > 0 and pos.get("position_size_usd", 0) > 0)
    check("1.2: entry order went through the engine once",
          len(ORDER_CALLS) == 1 and ORDER_CALLS[0]["side"] == "BUY"
          and ORDER_CALLS[0]["symbol"] == "BTC")

    dup = main.open_paper_trade(_analysis("BTC", "BUY", 60000.0, 59000.0, 63000.0))
    check("1.3: duplicate symbol refused", dup is False
          and len(main.PAPER_POSITIONS) == 1
          and len(ORDER_CALLS) == 1)  # no second engine order

    hold = main.open_paper_trade(_analysis("SOL", "HOLD", 100.0, 99.0, 101.0))
    check("1.4: HOLD signal refused", hold is False and "SOL" not in main.PAPER_POSITIONS)

    PRICE["v"] = 62000.0  # between SL and TP
    main.check_paper_positions()
    check("1.5: no TP/SL touch keeps position OPEN",
          "BTC" in main.PAPER_POSITIONS
          and main.PAPER_POSITIONS["BTC"]["status"] == "OPEN"
          and main.PAPER_TRADE_HISTORY == []
          and main.PAPER_BALANCE == 1000.0
          and len(ORDER_CALLS) == 1)  # entry order only - no exit order generated


# ============================================================================
# PART 2 - TP/SL exits routed through the engine
# ============================================================================
def verify_tp_sl_exits():
    print()
    print("=" * 70)
    print("PART 2 - TP/SL exits (offsetting engine orders, DRY_RUN simulated)")
    print("=" * 70)

    # -- 2.1 BUY long -> TAKE_PROFIT (SELL close) --------------------------
    reset_paper_state()
    PRICE["v"] = 60000.0
    main.open_paper_trade(_analysis("BTC", "BUY", 60000.0, 59000.0, 63000.0))
    qty = main.PAPER_POSITIONS["BTC"]["quantity"]
    PRICE["v"] = 63200.0  # above TP
    main.check_paper_positions()
    hist = main.PAPER_TRADE_HISTORY
    check("2.1: BUY position closed at TP", "BTC" not in main.PAPER_POSITIONS
          and len(hist) == 1 and hist[0]["status"] == "TAKE_PROFIT"
          and hist[0]["exit"] == 63200.0 and hist[0]["pnl"] > 0)
    check("2.1: exit was engine SELL order (offsetting, same qty)",
          len(ORDER_CALLS) == 2 and ORDER_CALLS[1]["side"] == "SELL"
          and abs(ORDER_CALLS[1]["quantity"] - qty) < 1e-9
          and abs(ORDER_CALLS[1]["price"] - 63200.0) < 1e-9)
    check("2.1: close order simulated + recorded",
          str(hist[0].get("close_order_id", "")).startswith("SIM-")
          and hist[0].get("close_execution_status") == "SIMULATED")
    check("2.1: balance credited with TP PnL",
          abs(main.PAPER_BALANCE - (1000.0 + hist[0]["pnl"])) < 1e-9)

    # -- 2.2 BUY long -> STOP_LOSS (SELL close) ----------------------------
    reset_paper_state()
    PRICE["v"] = 60000.0
    main.open_paper_trade(_analysis("BTC", "BUY", 60000.0, 59000.0, 63000.0))
    PRICE["v"] = 58500.0  # below SL
    main.check_paper_positions()
    hist = main.PAPER_TRADE_HISTORY
    check("2.2: BUY position closed at SL",
          "BTC" not in main.PAPER_POSITIONS and len(hist) == 1
          and hist[0]["status"] == "STOP_LOSS" and hist[0]["pnl"] < 0)
    check("2.2: SL exit via engine SELL order",
          len(ORDER_CALLS) == 2 and ORDER_CALLS[1]["side"] == "SELL"
          and abs(ORDER_CALLS[1]["price"] - 58500.0) < 1e-9)

    # -- 2.3 SELL short -> TAKE_PROFIT (BUY close) -------------------------
    reset_paper_state()
    PRICE["v"] = 3200.0
    main.open_paper_trade(_analysis("ETH", "SELL", 3200.0, 3400.0, 3000.0))
    PRICE["v"] = 2950.0  # at/below TP for a short
    main.check_paper_positions()
    hist = main.PAPER_TRADE_HISTORY
    check("2.3: SELL position closed at TP",
          "ETH" not in main.PAPER_POSITIONS and len(hist) == 1
          and hist[0]["status"] == "TAKE_PROFIT" and hist[0]["pnl"] > 0)
    check("2.3: short TP exit via engine BUY order",
          len(ORDER_CALLS) == 2 and ORDER_CALLS[1]["side"] == "BUY"
          and abs(ORDER_CALLS[1]["price"] - 2950.0) < 1e-9)

    # -- 2.4 SELL short -> STOP_LOSS (BUY close) ---------------------------
    reset_paper_state()
    PRICE["v"] = 3200.0
    main.open_paper_trade(_analysis("ETH", "SELL", 3200.0, 3400.0, 3000.0))
    PRICE["v"] = 3450.0  # at/above SL for a short
    main.check_paper_positions()
    hist = main.PAPER_TRADE_HISTORY
    check("2.4: SELL position closed at SL",
          "ETH" not in main.PAPER_POSITIONS and len(hist) == 1
          and hist[0]["status"] == "STOP_LOSS" and hist[0]["pnl"] < 0)
    check("2.4: short SL exit via engine BUY order",
          len(ORDER_CALLS) == 2 and ORDER_CALLS[1]["side"] == "BUY"
          and abs(ORDER_CALLS[1]["price"] - 3450.0) < 1e-9)


# ============================================================================
# PART 3 - Safety guards preserved
# ============================================================================
def verify_safety_guards():
    print()
    print("=" * 70)
    print("PART 3 - Safety guards preserved")
    print("=" * 70)

    reset_paper_state()
    svc = main.binance_service

    # -- 3.1 paper flow blocked when live trading is enabled ---------------
    svc.live_trading_enabled = True
    blocked = main.open_paper_trade(_analysis("BTC", "BUY", 60000.0, 59000.0, 63000.0))
    svc.live_trading_enabled = False
    check("3.1: paper flow blocked when live trading enabled", blocked is False
          and main.PAPER_POSITIONS == {} and ORDER_CALLS == [])

    # -- 3.2 paper flow blocked when DRY_RUN is off ------------------------
    svc.dry_run = False
    blocked = main.open_paper_trade(_analysis("BTC", "BUY", 60000.0, 59000.0, 63000.0))
    svc.dry_run = True
    check("3.2: paper flow blocked when DRY_RUN off", blocked is False
          and main.PAPER_POSITIONS == {} and ORDER_CALLS == [])

    # -- 3.3 non-simulated engine result is refused ------------------------
    def fake_place(**kwargs):
        return {"status": "FILLED", "dry_run": False, "order_id": 42,
                "order_type": "MARKET", "execution_price": 60000.0}

    orig_place = svc.place_order
    svc.place_order = fake_place
    refused = main.open_paper_trade(_analysis("BTC", "BUY", 60000.0, 59000.0, 63000.0))
    svc.place_order = orig_place
    check("3.3: non-simulated engine result refused",
          refused is False and main.PAPER_POSITIONS == {})

    check("3.4: DRY_RUN=True and live-disabled restored after guard tests",
          svc.dry_run is True and svc.live_trading_enabled is False)


def run():
    print("Phase 3.4.3 - Verify Position Tracking & TP/SL Exits (paper/test)")
    print("No real orders, no network, no config changes. DRY_RUN default preserved.")
    try:
        setup()
        verify_position_tracking()
        verify_tp_sl_exits()
        verify_safety_guards()
    finally:
        restore()
        reset_paper_state()
    print()
    print("=" * 70)
    if FAILURES:
        print(f"VERIFY POSITIONS: {len(FAILURES)} FAILURE(S) -> {FAILURES}")
        sys.exit(1)
    print("VERIFY POSITIONS: ALL CHECKS PASSED (Phase 3.4.3)")
    sys.exit(0)


if __name__ == "__main__":
    run()