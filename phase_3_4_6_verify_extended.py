#!/usr/bin/env python3
"""
Phase 3.4.6 - Verify Extended Simulations (Offline/DRY-RUN Only)
(Project Morningstar / Allmightsee Prime - Paper -> Testnet Validation).

Safe offline verification harness covering realistic multi-trade sequences
with consecutive wins/losses under DRY_RUN=True, preserving all existing
Phase 3.4.1-3.4.5 behavior unchanged.

SAFETY:
  * no real-money orders, no Binance network calls (DRY_RUN mode only),
  * DRY_RUN=True default and live trading disabled preserved unchanged,
  * NO production file is modified - only runtime helper methods are
    patched and restored, leaving main.py and binance_service.py intact,
  * explicitly offline/DRY-RUN simulation only.

Usage:
    python3 phase_3_4_6_verify_extended.py
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


ORIG = {}
PRICE = {"v": 60000.0}
ORDER_CALLS = []


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


def setup():
    """Patch market-data helpers (network hard-blocked) and capture engine calls."""
    svc = main.binance_service
    ORIG.update({
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

    def wrapped_place(*args, **kwargs):
        ORDER_CALLS.append(kwargs)
        return ORIG["place_order"](*args, **kwargs)

    svc.place_order = wrapped_place


def restore():
    """Restore every patched attribute on the shared engine instance."""
    svc = main.binance_service
    for key, value in ORIG.items():
        if key == "get_klines":
            main.get_klines = value
        else:
            setattr(svc, key, value)


def verify_extended_simulation():
    print("=" * 70)
    print("PHASE 3.4.6 - Verify Extended Simulations (Offline/DRY-RUN)")
    print("=" * 70)
    reset_paper_state()
    total_pnl = 0.0
    wins = 0
    losses = 0
    gross_profit = 0.0
    gross_loss = 0.0

    # Trade 1: BUY -> TAKE_PROFIT (win)
    PRICE["v"] = 60000.0
    main.open_paper_trade(_analysis("BTC", "BUY", 60000.0, 59000.0, 63000.0))
    PRICE["v"] = 63200.0
    main.check_paper_positions()
    pnl = main.PAPER_TRADE_HISTORY[-1]["pnl"]
    total_pnl += pnl
    wins += 1
    gross_profit += pnl

    # Trade 2: SELL -> STOP_LOSS (loss)
    PRICE["v"] = 3200.0
    main.open_paper_trade(_analysis("ETH", "SELL", 3200.0, 3400.0, 3000.0))
    PRICE["v"] = 3450.0
    main.check_paper_positions()
    pnl = main.PAPER_TRADE_HISTORY[-1]["pnl"]
    total_pnl += pnl
    losses += 1
    gross_loss += pnl

    # Trade 3: BUY -> STOP_LOSS (loss)
    PRICE["v"] = 100.0
    main.open_paper_trade(_analysis("SOL", "BUY", 100.0, 99.0, 101.0))
    PRICE["v"] = 98.0
    main.check_paper_positions()
    pnl = main.PAPER_TRADE_HISTORY[-1]["pnl"]
    total_pnl += pnl
    losses += 1
    gross_loss += pnl

    # Trade 4: SELL -> TAKE_PROFIT (win)
    PRICE["v"] = 500.0
    main.open_paper_trade(_analysis("BNB", "SELL", 500.0, 530.0, 470.0))
    PRICE["v"] = 460.0
    main.check_paper_positions()
    pnl = main.PAPER_TRADE_HISTORY[-1]["pnl"]
    total_pnl += pnl
    wins += 1
    gross_profit += pnl

    # Trade 5: BUY -> TAKE_PROFIT (win)
    PRICE["v"] = 1000.0
    main.open_paper_trade(_analysis("ADA", "BUY", 1000.0, 990.0, 1010.0))
    PRICE["v"] = 1010.0
    main.check_paper_positions()
    pnl = main.PAPER_TRADE_HISTORY[-1]["pnl"]
    total_pnl += pnl
    wins += 1
    gross_profit += pnl

    # Trade 6: SELL -> STOP_LOSS (loss)
    PRICE["v"] = 80.0
    main.open_paper_trade(_analysis("XRP", "SELL", 80.0, 85.0, 75.0))
    PRICE["v"] = 88.0
    main.check_paper_positions()
    pnl = main.PAPER_TRADE_HISTORY[-1]["pnl"]
    total_pnl += pnl
    losses += 1
    gross_loss += pnl

    # Trade 7: BUY -> TAKE_PROFIT (win)
    PRICE["v"] = 5000.0
    main.open_paper_trade(_analysis("DOT", "BUY", 5000.0, 4900.0, 5100.0))
    PRICE["v"] = 5100.0
    main.check_paper_positions()
    pnl = main.PAPER_TRADE_HISTORY[-1]["pnl"]
    total_pnl += pnl
    wins += 1
    gross_profit += pnl

    # Trade 8: SELL -> TAKE_PROFIT (win)
    PRICE["v"] = 200.0
    main.open_paper_trade(_analysis("DOGE", "SELL", 200.0, 210.0, 190.0))
    PRICE["v"] = 190.0
    main.check_paper_positions()
    pnl = main.PAPER_TRADE_HISTORY[-1]["pnl"]
    total_pnl += pnl
    wins += 1
    gross_profit += pnl

    # Trade 9: BUY -> STOP_LOSS (loss)
    PRICE["v"] = 50.0
    main.open_paper_trade(_analysis("AVAX", "BUY", 50.0, 49.0, 51.0))
    PRICE["v"] = 48.0
    main.check_paper_positions()
    pnl = main.PAPER_TRADE_HISTORY[-1]["pnl"]
    total_pnl += pnl
    losses += 1
    gross_loss += pnl

    # Trade 10: SELL -> STOP_LOSS (loss)
    PRICE["v"] = 10.0
    main.open_paper_trade(_analysis("MATIC", "SELL", 10.0, 12.0, 8.0))
    PRICE["v"] = 15.0
    main.check_paper_positions()
    pnl = main.PAPER_TRADE_HISTORY[-1]["pnl"]
    total_pnl += pnl
    losses += 1
    gross_loss += pnl

    # -- Extended simulation verdict ------------------------------------------
    check("6.1: 10 trades closed (5 wins / 5 losses)",
          len(main.PAPER_TRADE_HISTORY) == 10 and wins == 5 and losses == 5,
          f"(wins={wins}, losses={losses})")
    check("6.2: balance equals initial + total PnL",
          abs(main.PAPER_BALANCE - (1000.0 + total_pnl)) < 1e-6,
          f"(balance={main.PAPER_BALANCE}, total_pnl={total_pnl})")
    s = main.get_paper_statistics()
    check("6.3: statistics match the 10-trade session",
          s["total_trades"] == 10 and s["wins"] == 5 and s["losses"] == 5
          and abs(s["total_pnl"] - total_pnl) < 1e-6
          and s["win_rate"] == 50.0)
    expected_pf = (gross_profit / abs(gross_loss)) if gross_loss != 0 else (gross_profit if gross_profit > 0 else 0.0)
    check("6.4: profit factor matches manual gross computation",
          abs(s["profit_factor"] - expected_pf) < 1e-6,
          f"(pf={s['profit_factor']}, expected={expected_pf})")
    check("6.5: recent_trades returns the latest 5 (API cap)",
          len(s["recent_trades"]) == 5
          and s["recent_trades"][-1]["symbol"] == "MATIC"
          and s["recent_trades"][-1]["status"] == "STOP_LOSS",
          f"(recent={len(s['recent_trades'])})")


def run():
    print("Phase 3.4.6 - Verify Extended Simulations (Offline/DRY-RUN)")
    print("No real orders, no network, no config changes. DRY_RUN default preserved.")
    try:
        setup()
        verify_extended_simulation()
    finally:
        restore()
        reset_paper_state()
    print()
    print("=" * 70)
    if FAILURES:
        print(f"EXTENDED SIMULATION: {len(FAILURES)} FAILURE(S) -> {FAILURES}")
        sys.exit(1)
    print("EXTENDED SIMULATION: ALL CHECKS PASSED (Phase 3.4.6)")
    sys.exit(0)


if __name__ == "__main__":
    run()