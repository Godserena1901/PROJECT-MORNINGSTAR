#!/usr/bin/env python3
"""
Phase 3.4.4 - Verify Balance & PnL Accounting
(Project Morningstar / Allmightsee Prime - Paper -> Testnet Validation).

Safe paper/test validation of the EXISTING paper accounting layer in main.py
(PAPER_BALANCE updates in check_paper_positions + get_paper_statistics):

  Part 1 - Balance/PnL accounting through the engine-routed paper flow:
           * TP close credits the paper balance by the exact PnL,
           * SL close debits the paper balance,
           * sequential trades accumulate balance and history consistently,
           * statistics reflect the closed trades (wins/losses/total PnL).

  Part 2 - get_paper_statistics() correctness (synthetic closed-trade data):
           * empty history -> zeroed statistics,
           * wins/losses/win_rate/gross profit/loss/profit factor,
           * best/worst trade, average win/loss,
           * BUY/SELL directional breakdown,
           * PnL fallback when trade has no stored 'pnl' (computed from
             entry/exit/side),
           * recent_trades returns the latest 5,
           * profit-factor edge cases (all wins / all losses).

  Part 3 - Full-cycle integration: open -> monitor -> close (win+loss mix),
           then verify the recorded balance equals initial + summed PnL and
           statistics match the manually computed numbers.

SAFETY (identical to Phases 3.4.1-3.4.3):
  * no real-money orders, no Binance network calls (network sink hard-blocked),
  * DRY_RUN=True default and live trading disabled preserved unchanged,
  * NO production file is modified - this harness only imports main.py and
    patches runtime helper methods for isolation (restored afterwards),
  * explicitly OUT of scope: reconnection testing, extended simulations.

Usage:
    python3 phase_3_4_4_verify_balance.py
Exit code 0 = all checks passed; 1 = one or more checks failed.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main  # noqa: E402  (paper accounting under test)

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
# PART 1 - Balance/PnL accounting through the engine-routed paper flow
# ============================================================================
def verify_balance_via_paper_flow():
    print("=" * 70)
    print("PART 1 - Balance/PnL accounting through the engine-routed paper flow")
    print("=" * 70)

    # -- 1.1 BUY long -> TAKE_PROFIT: balance credited exactly by PnL -------
    reset_paper_state()
    PRICE["v"] = 60000.0
    main.open_paper_trade(_analysis("BTC", "BUY", 60000.0, 59000.0, 63000.0))
    PRICE["v"] = 63200.0  # above TP
    main.check_paper_positions()
    hist = main.PAPER_TRADE_HISTORY
    pnl = hist[0]["pnl"]
    check("1.1: TP trade records positive PnL", pnl > 0, f"(pnl={pnl})")
    check("1.1: balance credited by exactly the PnL",
          abs(main.PAPER_BALANCE - (1000.0 + pnl)) < 1e-6,
          f"(balance={main.PAPER_BALANCE})")
    stats = main.get_paper_statistics()
    check("1.1: stats reflect the win", stats["total_trades"] == 1
          and stats["wins"] == 1 and stats["losses"] == 0
          and stats["win_rate"] == 100.0
          and abs(stats["total_pnl"] - pnl) < 1e-6)

    # -- 1.2 BUY long -> STOP_LOSS: balance debited by exactly the PnL -------
    reset_paper_state()
    PRICE["v"] = 60000.0
    main.open_paper_trade(_analysis("BTC", "BUY", 60000.0, 59000.0, 63000.0))
    PRICE["v"] = 58500.0  # below SL
    main.check_paper_positions()
    hist = main.PAPER_TRADE_HISTORY
    pnl = hist[0]["pnl"]
    check("1.2: SL trade records negative PnL", pnl < 0, f"(pnl={pnl})")
    check("1.2: balance debited by exactly the PnL",
          abs(main.PAPER_BALANCE - (1000.0 + pnl)) < 1e-6,
          f"(balance={main.PAPER_BALANCE})")
    stats = main.get_paper_statistics()
    check("1.2: stats reflect the loss", stats["total_trades"] == 1
          and stats["losses"] == 1 and stats["wins"] == 0
          and abs(stats["total_pnl"] - pnl) < 1e-6)

    # -- 1.3 sequential win then loss: balance + stats accumulate -------------
    reset_paper_state()
    PRICE["v"] = 60000.0
    main.open_paper_trade(_analysis("BTC", "BUY", 60000.0, 59000.0, 63000.0))
    PRICE["v"] = 63200.0
    main.check_paper_positions()
    pnl_win = main.PAPER_TRADE_HISTORY[0]["pnl"]

    PRICE["v"] = 3200.0
    main.open_paper_trade(_analysis("ETH", "SELL", 3200.0, 3400.0, 3000.0))
    PRICE["v"] = 3450.0
    main.check_paper_positions()
    pnl_loss = main.PAPER_TRADE_HISTORY[1]["pnl"]

    check("1.3: two trades closed sequentially",
          len(main.PAPER_TRADE_HISTORY) == 2
          and pnl_win > 0 and pnl_loss < 0)
    check("1.3: balance accumulates net PnL",
          abs(main.PAPER_BALANCE - (1000.0 + pnl_win + pnl_loss)) < 1e-6,
          f"(balance={main.PAPER_BALANCE})")
    stats = main.get_paper_statistics()
    check("1.3: stats match manual sums",
          stats["total_trades"] == 2 and stats["wins"] == 1
          and stats["losses"] == 1 and stats["win_rate"] == 50.0
          and abs(stats["total_pnl"] - (pnl_win + pnl_loss)) < 1e-6
          and abs(stats["gross_profit"] - pnl_win) < 1e-6
          and abs(stats["gross_loss"] - pnl_loss) < 1e-6)


# ============================================================================
# PART 2 - get_paper_statistics() correctness (synthetic closed trades)
# ============================================================================
def _trade(symbol, side, entry, exit_price, status, pnl=None):
    t = {"symbol": symbol, "side": side, "entry": entry, "exit": exit_price,
         "status": status}
    if pnl is not None:
        t["pnl"] = pnl
    return t


def verify_statistics_synthetic():
    print()
    print("=" * 70)
    print("PART 2 - get_paper_statistics() correctness (synthetic history)")
    print("=" * 70)

    # -- 2.1 empty history -> zeroed statistics -----------------------------
    main.PAPER_TRADE_HISTORY = []
    s = main.get_paper_statistics()
    check("2.1: empty history returns zeroed stats",
          s["total_trades"] == 0 and s["wins"] == 0 and s["losses"] == 0
          and s["win_rate"] == 0.0 and s["total_pnl"] == 0.0
          and s["profit_factor"] == 0.0 and s["recent_trades"] == []
          and s["buy_trades"] == 0 and s["sell_trades"] == 0)

    # -- 2.2 mixed history: core metrics ------------------------------------
    history = [
        _trade("BTC", "BUY", 100.0, 110.0, "TAKE_PROFIT", 10.0),
        _trade("BTC", "BUY", 100.0, 95.0, "STOP_LOSS", -5.0),
        _trade("ETH", "SELL", 50.0, 42.0, "TAKE_PROFIT", 8.0),
        _trade("ETH", "SELL", 50.0, 52.0, "STOP_LOSS", -2.0),
        _trade("SOL", "BUY", 10.0, 14.0, "TAKE_PROFIT", 4.0),
        _trade("SOL", "SELL", 30.0, 33.0, "STOP_LOSS", -3.0),
    ]
    main.PAPER_TRADE_HISTORY = list(history)
    s = main.get_paper_statistics()
    check("2.2: core counts", s["total_trades"] == 6 and s["wins"] == 3
          and s["losses"] == 3 and s["win_rate"] == 50.0)
    check("2.2: PnL aggregates", abs(s["total_pnl"] - 12.0) < 1e-9
          and abs(s["average_pnl"] - 2.0) < 1e-9
          and abs(s["gross_profit"] - 22.0) < 1e-9
          and abs(s["gross_loss"] - (-10.0)) < 1e-9
          and abs(s["profit_factor"] - 2.2) < 1e-9)
    check("2.2: best/worst + averages",
          s["best_trade"] == 10.0 and s["worst_trade"] == -5.0
          and abs(s["avg_win"] - (22.0 / 3)) < 1e-9
          and abs(s["avg_loss"] - (-10.0 / 3)) < 1e-9)
    check("2.2: directional breakdown", s["buy_trades"] == 3
          and s["buy_wins"] == 2 and s["sell_trades"] == 3
          and s["sell_wins"] == 1)

    # -- 2.3 recent_trades returns the latest 5 -----------------------------
    check("2.3: recent_trades keeps last 5",
          len(s["recent_trades"]) == 5
          and s["recent_trades"][-1]["symbol"] == "SOL"
          and s["recent_trades"][-1]["status"] == "STOP_LOSS")

    # -- 2.4 PnL fallback when 'pnl' is absent ------------------------------
    # Classification uses status (TAKE_PROFIT/STOP_LOSS); PnL amounts fall
    # back to entry/exit/side computation (real flow always stores pnl).
    fallback = [
        _trade("BTC", "BUY", 100.0, 110.0, "TAKE_PROFIT"),  # +10 by (exit-entry)
        _trade("ETH", "SELL", 50.0, 45.0, "TAKE_PROFIT"),   # +5  by (entry-exit)
        _trade("SOL", "BUY", 100.0, 90.0, "STOP_LOSS"),     # -10
        _trade("BNB", "SELL", 50.0, 55.0, "STOP_LOSS"),     # -5
    ]
    main.PAPER_TRADE_HISTORY = fallback
    s = main.get_paper_statistics()
    check("2.4: PnL computed from entry/exit/side when 'pnl' missing",
          abs(s["total_pnl"] - 0.0) < 1e-9
          and abs(s["gross_profit"] - 15.0) < 1e-9
          and abs(s["gross_loss"] - (-15.0)) < 1e-9
          and abs(s["profit_factor"] - 1.0) < 1e-9)
    check("2.4: win/loss classified by status when 'pnl' missing",
          s["wins"] == 2 and s["losses"] == 2
          and s["best_trade"] == 10.0 and s["worst_trade"] == -10.0)

    # -- 2.5 profit-factor edge cases ---------------------------------------
    main.PAPER_TRADE_HISTORY = [
        _trade("BTC", "BUY", 1.0, 3.0, "TAKE_PROFIT", 2.0),
        _trade("ETH", "BUY", 1.0, 4.0, "TAKE_PROFIT", 3.0),
        _trade("SOL", "BUY", 1.0, 5.0, "TAKE_PROFIT", 4.0),
    ]
    s = main.get_paper_statistics()
    check("2.5: all-wins profit factor = gross profit",
          abs(s["profit_factor"] - 9.0) < 1e-9 and s["losses"] == 0
          and s["wins"] == 3)

    main.PAPER_TRADE_HISTORY = [
        _trade("BTC", "BUY", 10.0, 8.0, "STOP_LOSS", -2.0),
        _trade("ETH", "BUY", 10.0, 7.0, "STOP_LOSS", -3.0),
    ]
    s = main.get_paper_statistics()
    check("2.5: all-losses profit factor = 0",
          s["profit_factor"] == 0.0 and s["wins"] == 0 and s["losses"] == 2
          and s["worst_trade"] == -3.0)


# ============================================================================
# PART 3 - Full-cycle integration: balance + stats after a mixed session
# ============================================================================
def verify_full_cycle():
    print()
    print("=" * 70)
    print("PART 3 - Full-cycle integration (open -> monitor -> close, mixed)")
    print("=" * 70)

    reset_paper_state()
    pnls = []

    # Trade 1: BUY long -> TAKE_PROFIT (win)
    PRICE["v"] = 60000.0
    main.open_paper_trade(_analysis("BTC", "BUY", 60000.0, 59000.0, 63000.0))
    PRICE["v"] = 63200.0
    main.check_paper_positions()
    pnls.append(main.PAPER_TRADE_HISTORY[-1]["pnl"])

    # Trade 2: SELL short -> STOP_LOSS (loss)
    PRICE["v"] = 3200.0
    main.open_paper_trade(_analysis("ETH", "SELL", 3200.0, 3400.0, 3000.0))
    PRICE["v"] = 3450.0
    main.check_paper_positions()
    pnls.append(main.PAPER_TRADE_HISTORY[-1]["pnl"])

    # Trade 3: BUY long -> STOP_LOSS (loss)
    PRICE["v"] = 100.0
    main.open_paper_trade(_analysis("SOL", "BUY", 100.0, 99.0, 101.0))
    PRICE["v"] = 98.0
    main.check_paper_positions()
    pnls.append(main.PAPER_TRADE_HISTORY[-1]["pnl"])

    # Trade 4: SELL short -> TAKE_PROFIT (win)
    PRICE["v"] = 500.0
    main.open_paper_trade(_analysis("BNB", "SELL", 500.0, 530.0, 470.0))
    PRICE["v"] = 460.0
    main.check_paper_positions()
    pnls.append(main.PAPER_TRADE_HISTORY[-1]["pnl"])

    total_pnl = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = sum(p for p in pnls if p < 0)

    check("3: four trades closed (2 wins / 2 losses)",
          len(main.PAPER_TRADE_HISTORY) == 4 and wins == 2 and losses == 2
          and pnls[0] > 0 and pnls[1] < 0 and pnls[2] < 0 and pnls[3] > 0,
          f"(pnls={[round(p, 4) for p in pnls]})")
    check("3: balance equals initial + summed PnL",
          abs(main.PAPER_BALANCE - (1000.0 + total_pnl)) < 1e-6,
          f"(balance={main.PAPER_BALANCE}, total_pnl={round(total_pnl, 4)})")

    s = main.get_paper_statistics()
    check("3: statistics match the recorded session",
          s["total_trades"] == 4 and s["wins"] == 2 and s["losses"] == 2
          and s["win_rate"] == 50.0
          and abs(s["total_pnl"] - total_pnl) < 1e-6
          and abs(s["gross_profit"] - gross_profit) < 1e-6
          and abs(s["gross_loss"] - gross_loss) < 1e-6)
    check("3: directional breakdown correct",
          s["buy_trades"] == 2 and s["buy_wins"] == 1
          and s["sell_trades"] == 2 and s["sell_wins"] == 1)
    if gross_loss != 0:
        expected_pf = gross_profit / abs(gross_loss)
        check("3: profit factor matches manual computation",
              abs(s["profit_factor"] - expected_pf) < 1e-6,
              f"(pf={s['profit_factor']}, expected={expected_pf})")


def run():
    print("Phase 3.4.4 - Verify Balance & PnL Accounting (paper/test)")
    print("No real orders, no network, no config changes. DRY_RUN default preserved.")
    try:
        setup()
        verify_balance_via_paper_flow()
        verify_statistics_synthetic()
        verify_full_cycle()
    finally:
        restore()
        reset_paper_state()
    print()
    print("=" * 70)
    if FAILURES:
        print(f"VERIFY BALANCE: {len(FAILURES)} FAILURE(S) -> {FAILURES}")
        sys.exit(1)
    print("VERIFY BALANCE: ALL CHECKS PASSED (Phase 3.4.4)")
    sys.exit(0)


if __name__ == "__main__":
    run()