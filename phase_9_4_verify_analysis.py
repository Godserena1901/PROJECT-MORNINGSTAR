#!/usr/bin/env python3
"""Phase 9.4 - Verify Performance Analysis (offline, NON-MUTATING).

Verifies the Phase 9.4 `PerformanceAnalyzer` (phase_9_4_analysis.py).

It is an offline technical-verification harness using synthetic-but-realistic
evidence in the Phase 9.3 schema.  It SEPARATES two concerns per contract:

  * software correctness of the analyzer (calculation + sufficiency logic)
  * trading-performance conclusions

The analyzer may PASS its technical verification regardless of whether the
trading numbers are good; and it must not fabricate data, claim profitability
on a small sample, or introduce numeric thresholds that the repository does
not establish.

WHAT IT PROVES:
  Part A  - Observed metrics: counts, win rate, gross/net P&L, fees,
            avg per-trade, avg win, avg loss, equity movement, drawdown,
            exposure, errors.
  Part B  - Profit factor: computed when the loss side supports it, and
            reported unavailable (not estimated) when it does not.
  Part C  - Attribution: by signal / side / symbol / exit reason.
  Part D  - Evidence sufficiency: small/empty samples => insufficient;
            unsupported => unavailable; no fabricated history.
  Part E  - Report structure & handoff to 9.5 / 9.6 / 9.7; verdict is
            GO_NOGO_DEFERRED (no invented thresholds).
  Part F  - Safety: no real-order route, DRY_RUN=True, live disabled,
            main.py paper state untouched, analysis-only.
  Part G  - Technical PASS is independent of trading performance.

Usage:
    python3 phase_9_4_verify_analysis.py
Exit code 0 = all checks passed; 1 = one or more checks failed.
"""

import json
import os
import sys
import tempfile

# SAFETY: force safe defaults before importing the shared engine.
os.environ["DRY_RUN"] = "True"
os.environ["LIVE_TRADING_ENABLED"] = "False"

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import main as bot_main  # noqa: E402
from phase_9_4_analysis import PerformanceAnalyzer  # noqa: E402
from binance_service import binance_service as shared_engine  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    print(("PASS" if condition else "FAIL"), "-", name, detail)
    if not condition:
        FAILURES.append(name)


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


# ============================================================================
# Deterministic synthetic evidence in the Phase 9.3 schema
# ============================================================================
def _trade(symbol, side, entry, exit_p, qty, status, gross, fees, net):
    return {
        "symbol": symbol, "side": side, "entry": entry, "exit": exit_p,
        "quantity": qty, "gross_pnl": gross, "fees": fees, "net_pnl": net,
        "status": status, "exit_reason": status,
        "signal": "BUY" if side == "BUY" else "SELL",
        "strategy": "ALLMIGHTSEE_PRIME",
        "opened_at": "2026-08-01T10:00:00Z",
        "closed_at": "2026-08-01T11:00:00Z",
        "reason": "verify signal",
    }


def _cycles():
    return [
        {"equity": 1000.0, "exposure_used_usd": 100.0,
         "open_positions": 1, "status": "RUNNING", "offline": False},
        {"equity": 1015.0, "exposure_used_usd": 150.0,
         "open_positions": 2, "status": "RUNNING", "offline": False},
        {"equity": 990.0, "exposure_used_usd": 0.0,
         "open_positions": 0, "status": "RUNNING", "offline": True},
        {"equity": 1005.0, "exposure_used_usd": 100.0,
         "open_positions": 1, "status": "RUNNING", "offline": False},
    ]


def _contexts():
    return [
        {"symbol": "BTC", "current_price": 100.0, "rsi": 60.0,
         "macd": 2.0},
        {"symbol": "ETH", "current_price": 200.0, "rsi": 45.0,
         "macd": -1.0},
    ]


def _errors():
    return [{"src": "shadow_engine", "msg": "analyze(SOL) failed: x"}]


def _summary(cycles=None):
    cycles = cycles if cycles is not None else _cycles()
    curve = [1000.0] + [c["equity"] for c in cycles]
    peak = max(curve)
    max_dd = max((peak - v) / peak for v in curve)
    return {
        "collection_id": "c9_4_test",
        "evidence_log": "beta_evidence.jsonl",
        "metrics_summary": "beta_metrics_summary.json",
        "restart_count": 0,
        "cycles": len(cycles),
        "equity": {
            "initial_balance": 1000.0,
            "ending_balance": 1005.0,
            "shadow_balance": 1005.0,
            "last_sample": 1005.0,
        },
        "max_drawdown": {"max_drawdown_pct": round(max_dd * 100.0, 4),
                         "peak": peak, "trough": min(curve),
                         "samples": len(curve)},
        "signals": {}, "strategies": {}, "symbols": {},
    }

# ============================================================================
# PART A - observed metrics correctness on a deterministic dataset
# ============================================================================
def test_observed_metrics():
    print("=" * 70)
    print("PART A - Observed metrics correctness")
    print("=" * 70)
    # W1: net 9.79 ; W2: net 4.805 ; L: net -3.196
    trades = [
        _trade("BTC", "BUY", 100.0, 110.0, 1.0, "TAKE_PROFIT",
               10.0, 0.21, 9.79),
        _trade("ETH", "SELL", 200.0, 190.0, 0.5, "TAKE_PROFIT",
               5.0, 0.195, 4.805),
        _trade("SOL", "BUY", 100.0, 96.0, 1.0, "STOP_LOSS",
               -3.0, 0.196, -3.196),
    ]
    a = PerformanceAnalyzer(trades=trades, cycles=_cycles(),
                            contexts=_contexts(), errors=_errors(),
                            summary=_summary())
    rep = a.analyze()
    obs = rep["observed_metrics"]

    check("total trades == 3", obs["total_trades"] == 3,
          "(=%d)" % obs["total_trades"])
    check("wins == 2", obs["wins"] == 2, "(=%d)" % obs["wins"])
    check("losses == 1", obs["losses"] == 1, "(=%d)" % obs["losses"])
    check("win rate == 66.6667", approx(obs["win_rate_pct"], 66.6667, 1e-3),
          "(=%.4f)" % obs["win_rate_pct"])
    check("gross == 12.0", approx(obs["gross_pnl"], 12.0, 1e-6),
          "(=%.6f)" % obs["gross_pnl"])
    check("fees == 0.601", approx(obs["fees_total"], 0.601, 1e-6),
          "(=%.6f)" % obs["fees_total"])
    check("net == 11.399", approx(obs["net_pnl"], 11.399, 1e-6),
          "(=%.6f)" % obs["net_pnl"])
    check("avg per-trade == 3.7997",
          approx(obs["avg_pnl_per_trade"], 11.399 / 3, 1e-6),
          "(=%.6f)" % (obs["avg_pnl_per_trade"] or 0.0))
    check("avg win == 7.2975", approx(obs["avg_win"], (9.79 + 4.805) / 2, 1e-6),
          "(=%.6f)" % (obs["avg_win"] or 0.0))
    check("avg loss == -3.196", approx(obs["avg_loss"], -3.196, 1e-6),
          "(=%.6f)" % (obs["avg_loss"] or 0.0))
    check("profit factor available + correct (gross)",
          obs["profit_factor_available"] is True
          and approx(obs["profit_factor"], 15.0 / 3.0, 1e-4),
          "(pf=%.4f)" % (obs["profit_factor"] or 0.0))
    eq = obs["equity"]
    check("equity movement == +5.0", approx(eq["movement"], 5.0, 1e-6),
          "(=%.4f)" % (eq["movement"] or 0.0))
    dd = obs["max_drawdown"]
    check("max drawdown == 2.4631% (peak 1015, trough 990)",
          approx(dd["max_drawdown_pct"], (1015 - 990) / 1015 * 100, 1e-3),
          "(pct=%.4f)" % (dd["max_drawdown_pct"] or 0.0))
    ex = obs["exposure"]
    check("exposure max == 150", approx(ex["exposure_max_usd"], 150.0, 1e-6))
    check("exposure avg == 87.5", approx(ex["exposure_avg_usd"], 87.5, 1e-6))
    rel = obs["reliability"]
    check("errors counted == 1", rel["errors"] == 1)
    check("offline samples == 1", rel["offline_samples"] == 1)


# ============================================================================
# PART B - profit factor support boundary
# ============================================================================
def test_profit_factor_boundary():
    print("=" * 70)
    print("PART B - Profit factor: computed vs unavailable (never estimated)")
    print("=" * 70)
    # All wins, no loss -> profit factor unavailable (divisor zero)
    only_wins = [_trade("BTC", "BUY", 100.0, 110.0, 1.0, "TAKE_PROFIT",
                        10.0, 0.21, 9.79)]
    a1 = PerformanceAnalyzer(trades=only_wins, summary=_summary())
    pf1 = a1.profit_factor()
    check("no-loss evidence -> profit factor unavailable",
          pf1["available"] is False and pf1["profit_factor"] is None,
          "(available=%s)" % pf1["available"])
    check("unavailable reason is explicit",
          "divisor is zero" in pf1["reason"] or "no losing" in pf1["reason"],
          "(=%s)" % pf1["reason"])
    # No trades -> unavailable with 'no trades' reason
    a2 = PerformanceAnalyzer(trades=[], summary=_summary())
    pf2 = a2.profit_factor()
    check("empty evidence -> profit factor unavailable",
          pf2["available"] is False and "no trades" in pf2["reason"],
          "(=%s)" % pf2["reason"])
    # Mixed wins+losses -> computed
    mixed = [_trade("B", "BUY", 100.0, 110.0, 1.0, "TP", 10.0, 0.21, 9.79),
             _trade("E", "BUY", 100.0, 96.0, 1.0, "SL", -3.0, 0.196, -3.196)]
    a3 = PerformanceAnalyzer(trades=mixed, summary=_summary())
    pf3 = a3.profit_factor()
    check("wins+losses -> profit factor computed",
          pf3["available"] is True and approx(pf3["profit_factor"],
                                              10.0 / 3.0, 1e-4),
          "(pf=%.4f)" % (pf3["profit_factor"] or 0.0))


def _trade_short(symbol, side, status, gross, fees, net):
    return {"symbol": symbol, "side": side, "entry": 100.0, "exit": 110.0,
            "quantity": 1.0, "gross_pnl": gross, "fees": fees,
            "net_pnl": net, "status": status, "exit_reason": status,
            "signal": side, "strategy": "ALLMIGHTSEE_PRIME",
            "closed_at": "2026-08-01T10:00:00Z"}

# ============================================================================
# PART C - attribution
# ============================================================================
def test_attribution():
    print("=" * 70)
    print("PART C - Attribution by signal / side / symbol / exit reason")
    print("=" * 70)
    trades = [
        _trade("BTC", "BUY", 100.0, 110.0, 1.0, "TAKE_PROFIT",
               10.0, 0.21, 9.79),
        _trade("ETH", "SELL", 200.0, 190.0, 0.5, "TAKE_PROFIT",
               5.0, 0.195, 4.805),
        _trade("SOL", "BUY", 100.0, 96.0, 1.0, "STOP_LOSS",
               -3.0, 0.196, -3.196),
    ]
    a = PerformanceAnalyzer(trades=trades, summary=_summary())
    att = a.attribution()
    check("by_signal BUY has 2 trades, SELL 1",
          att["by_signal"]["BUY"]["trades"] == 2
          and att["by_signal"]["SELL"]["trades"] == 1,
          "(=%s)" % att["by_signal"])
    check("by_side BUY net == 6.594",
          approx(att["by_side"]["BUY"]["net"], 9.79 - 3.196, 1e-4),
          "(=%s)" % att["by_side"]["BUY"])
    check("by_symbol SOL losses == 1",
          att["by_symbol"]["SOL"]["losses"] == 1,
          "(=%s)" % att["by_symbol"]["SOL"])
    check("by_exit_reason STOP_LOSS loss==1",
          att["by_exit_reason"]["STOP_LOSS"]["losses"] == 1,
          "(=%s)" % att["by_exit_reason"]["STOP_LOSS"])


# ============================================================================
# PART D - evidence sufficiency (no fabrication, no invented thresholds)
# ============================================================================
def test_sufficiency():
    print("=" * 70)
    print("PART D - Evidence sufficiency (small sample = insufficient)")
    print("=" * 70)
    # Empty evidence set
    empty = PerformanceAnalyzer(trades=[], cycles=[], contexts=[],
                                summary={})
    rep = empty.analyze()
    su = rep["evidence_sufficiency"]
    check("empty evidence -> all trade metrics insufficient",
          su["per_metric"]["total_trades"]["status"] == "insufficient"
          and su["per_metric"]["win_loss"]["status"] == "insufficient"
          and su["per_metric"]["drawdown"]["status"] == "insufficient",
          "(t=%s)" % su["per_metric"]["total_trades"]["status"])
    check("empty evidence -> no fabrication (trades stays 0)",
          rep["observed_metrics"]["total_trades"] == 0
          and rep["observed_metrics"]["net_pnl"] == 0.0)
    check("sufficiency statement present (no profitability claim)",
          len(su["statement"]) > 0 and any(
              "no profitability claim" in s for s in su["statement"]))
    # Profit factor unavailable propagates to sufficiency
    only_wins = [_trade("B", "BUY", 100.0, 110.0, 1.0, "TP", 10.0, 0.21, 9.79)]
    a2 = PerformanceAnalyzer(trades=only_wins, summary=_summary())
    check("profit factor sufficiency reflects unavailability",
          a2.analyze()["evidence_sufficiency"]["per_metric"]["profit_factor"][
              "status"] == "insufficient")

# ============================================================================
# PART E - report structure & handoff
# ============================================================================
def test_report_structure():
    print("=" * 70)
    print("PART E - Report structure & handoff to 9.5/9.6/9.7")
    print("=" * 70)
    trades = [_trade("B", "BUY", 100.0, 110.0, 1.0, "TP", 10.0, 0.21, 9.79)]
    a = PerformanceAnalyzer(trades=trades, cycles=_cycles(),
                            contexts=_contexts(), errors=_errors(),
                            summary=_summary())
    rep = a.analyze()
    check("top-level keys present",
          "observed_metrics" in rep and "attribution" in rep
          and "evidence_sufficiency" in rep and "handoff" in rep
          and "verdict" in rep)
    h = rep["handoff"]
    check("handoff has 9.5, 9.6, 9.7 blocks",
          "to_9_5_strategy_market" in h
          and "to_9_6_stability_endurance" in h
          and "to_9_7_final_go_nogo" in h)
    check("9.5 carries attribution + context count",
          "attribution" in h["to_9_5_strategy_market"]
          and h["to_9_5_strategy_market"]["market_context_snapshot_count"] == 2)
    check("9.6 carries reliability + drawdown",
          "reliability" in h["to_9_6_stability_endurance"]
          and "max_drawdown" in h["to_9_6_stability_endurance"])
    check("9.7 carries observed metrics + sufficiency",
          "observed_metrics" in h["to_9_7_final_go_nogo"]
          and "sufficiency" in h["to_9_7_final_go_nogo"])
    check("verdict deferred (no invented thresholds)",
          rep["verdict"]["decision"] == "GO_NOGO_DEFERRED")


# ============================================================================
# PART F - safety
# ============================================================================
def test_safety():
    print("=" * 70)
    print("PART F - Safety (analysis-only, no real orders, paper untouched)")
    print("=" * 70)
    check("process DRY_RUN is True",
          os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes"))
    check("process LIVE_TRADING_ENABLED is False",
          os.getenv("LIVE_TRADING_ENABLED", "").lower() in ("0", "false", "no"))
    check("shared engine dry_run is True", shared_engine.dry_run is True)
    check("shared engine live trading disabled",
          shared_engine.live_trading_enabled is False)
    before = _paper_snapshot()
    trades = [_trade("B", "BUY", 100.0, 110.0, 1.0, "TP", 10.0, 0.21, 9.79)]
    PerformanceAnalyzer(trades=trades, cycles=_cycles(), summary=_summary())\
        .analyze()
    after = _paper_snapshot()
    check("main.py paper state untouched by analysis",
          before == after,
          "(balance %.2f -> %.2f)" % (before[0], after[0]))


def _paper_snapshot():
    return (bot_main.PAPER_BALANCE, dict(bot_main.PAPER_POSITIONS),
            list(bot_main.PAPER_TRADE_HISTORY))


# ============================================================================
# PART G - technical PASS is independent of trading performance
# ============================================================================
def test_technical_independence():
    print("=" * 70)
    print("PART G - Technical PASS independent of trading performance")
    print("=" * 70)
    # A losing dataset must still produce a technically-correct analysis
    # (analyzer correctness, not trading GO).
    losing = [
        _trade("B", "BUY", 100.0, 96.0, 1.0, "STOP_LOSS", -4.0, 0.196, -4.196),
        _trade("E", "BUY", 100.0, 95.0, 1.0, "STOP_LOSS", -5.0, 0.195, -5.195),
    ]
    a = PerformanceAnalyzer(trades=losing, summary=_summary())
    rep = a.analyze()
    check("losing dataset analysis completes",
          approx(rep["observed_metrics"]["net_pnl"], -9.391, 1e-3),
          "(net=%.4f)" % rep["observed_metrics"]["net_pnl"])
    check("losing dataset still deferred verdict",
          rep["verdict"]["decision"] == "GO_NOGO_DEFERRED")

# ============================================================================
# Runner
# ============================================================================
def run():
    print("Phase 9.4 - Verify Performance Analysis (offline, NON-MUTATING)")
    print("DRY_RUN=True | LIVE_TRADING_ENABLED=False | analysis only, "
          "no real orders, no network.")

    # Clean paper baseline for the non-mutation proof.
    bot_main.PAPER_POSITIONS = {}
    bot_main.PAPER_TRADE_HISTORY = []
    bot_main.PAPER_BALANCE = bot_main.INITIAL_PAPER_BALANCE
    bot_main.ALERTS = {}

    parts = [
        test_observed_metrics,
        test_profit_factor_boundary,
        test_attribution,
        test_sufficiency,
        test_report_structure,
        test_safety,
        test_technical_independence,
    ]
    for part in parts:
        try:
            part()
        except Exception as e:  # noqa: BLE001
            check(getattr(part, "__name__", "part") + " completed", False,
                  "(raised: %r)" % e)

    print()
    print("=" * 70)
    if FAILURES:
        print("VERIFY ANALYSIS: %d FAILURE(S) -> %s" % (len(FAILURES),
                                                        FAILURES))
        print("Phase 9.4 technical verification: FAIL")
        sys.exit(1)
    print("VERIFY ANALYSIS: ALL CHECKS PASSED (Phase 9.4)")
    print("Technical verification of PerformanceAnalyzer passed "
          "(separate from any trading-performance conclusion).")
    print("Phase 9.4 verification: PASS ")
    sys.exit(0)


if __name__ == "__main__":
    run()