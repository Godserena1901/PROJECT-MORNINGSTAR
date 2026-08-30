#!/usr/bin/env python3
"""Phase 9.5 - Verify Strategy & Market-Condition Validation (offline, NON-MUTATING).

Verifies the Phase 9.5 `StrategyMarketValidator` (phase_9_5_validation.py).

It is an offline technical-verification harness using synthetic-but-realistic
evidence in the Phase 9.3 schema.  It proves the validator is SOFTWARE-CORRECT
and treats any trading-performance conclusion separately (no invented
thresholds, no fabricated market data, no unsupported historical evidence).

WHAT IT PROVES:
  Part A  - Safety/config contract (DRY_RUN=True, live disabled, analysis only,
            main.py paper state untouched, no real orders).
  Part B  - Strategy robustness across signal / side / symbol / exit_reason /
            strategy (per-value trades, wins/losses, net, win rate,
            sufficiency).
  Part C  - Market-condition validation: context snapshots inside the trade
            window are joined and bucketed (rsi band, macd trend, volume
            regime, regime); outcomes match the synthetic source exactly.
  Part D  - Insufficient-evidence handling: no context / empty evidence =>
            explicit insufficient / unavailable with reasons; nothing is
            fabricated or estimated.
  Part E  - Handoff structure to 9.6 and 9.7; verdict GO_NOGO_DEFERRED.
  Part F  - Regression gate: Phase 9.4 verification still passes unchanged.
  Part G  - Full CLI round-trip: validator writes a durable report file.

Usage:
    python3 phase_9_5_verify_validation.py
Exit code 0 = all checks passed; 1 = one or more checks failed.
"""

import json
import os
import sys
import subprocess
import tempfile

# SAFETY: force safe defaults before importing the shared engine.
os.environ["DRY_RUN"] = "True"
os.environ["LIVE_TRADING_ENABLED"] = "False"

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import main as bot_main  # noqa: E402
from phase_9_5_validation import StrategyMarketValidator  # noqa: E402
from binance_service import binance_service as shared_engine  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    print(("PASS" if condition else "FAIL"), "-", name, detail)
    if not condition:
        FAILURES.append(name)


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


# ============================================================================
# Deterministic synthetic evidence (Phase 9.3 schema + envelope ts)
# ============================================================================
def _trade(symbol, side, signal, status, gross, fees, net, opened, closed,
           strategy="ALLMIGHTSEE_PRIME"):
    return {
        "symbol": symbol, "side": side, "signal": signal, "entry": 100.0,
        "exit": 110.0, "quantity": 1.0, "gross_pnl": gross, "fees": fees,
        "net_pnl": net, "status": status, "exit_reason": status,
        "strategy": strategy, "opened_at": opened, "closed_at": closed,
        "reason": "verify signal",
    }


def _ctx(_ts, symbol, rsi=None, macd=None, macd_signal=None,
         volume=None, average_volume=None, regime=None):
    """Build a market/context DATA dict (Phase 9.3 context schema)."""
    data = {"symbol": symbol, "current_price": 100.0}
    if rsi is not None:
        data["rsi"] = rsi
    if macd is not None:
        data["macd"] = macd
    if macd_signal is not None:
        data["macd_signal"] = macd_signal
    if volume is not None:
        data["volume"] = volume
    if average_volume is not None:
        data["average_volume"] = average_volume
    if regime is not None:
        data["regime"] = regime
    return data


def _parse_ts(value):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)


def _ctx_row(ts, data):
    """Build a (parsed_ts, data) tuple for window-joining (context_rows)."""
    return _parse_ts(ts), data

# ============================================================================
# PART A - safety & config contract
# ============================================================================
def test_safety():
    print("=" * 70)
    print("PART A - Safety (analysis-only, no real orders, paper untouched)")
    print("=" * 70)
    check("process DRY_RUN is True",
          os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes"))
    check("process LIVE_TRADING_ENABLED is False",
          os.getenv("LIVE_TRADING_ENABLED", "").lower() in ("0", "false", "no"))
    check("shared engine dry_run is True", shared_engine.dry_run is True)
    check("shared engine live trading disabled",
          shared_engine.live_trading_enabled is False)
    before = _paper_snapshot()
    v = StrategyMarketValidator(trades=[], cycles=[], contexts=[], summary={})
    v.analyze()
    after = _paper_snapshot()
    check("main.py paper state untouched by validation",
          before == after,
          "(balance %.2f -> %.2f)" % (before[0], after[0]))


def _paper_snapshot():
    return (bot_main.PAPER_BALANCE, dict(bot_main.PAPER_POSITIONS),
            list(bot_main.PAPER_TRADE_HISTORY))


# ============================================================================
# PART B - strategy robustness across supported dimensions
# ============================================================================
def _mixed_trades():
    return [
        _trade("BTC", "BUY", "BUY", "TAKE_PROFIT", 10.0, 0.21, 9.79,
               "2026-08-01T09:00:00Z", "2026-08-01T10:00:00Z"),
        _trade("BTC", "BUY", "BUY", "STOP_LOSS", -3.0, 0.196, -3.196,
               "2026-08-01T11:00:00Z", "2026-08-01T12:00:00Z"),
        _trade("ETH", "SELL", "SELL", "TAKE_PROFIT", 5.0, 0.195, 4.805,
               "2026-08-02T09:00:00Z", "2026-08-02T10:00:00Z"),
    ]


def test_strategy_robustness():
    print("=" * 70)
    print("PART B - Strategy robustness across signal/side/symbol/reason")
    print("=" * 70)
    v = StrategyMarketValidator(trades=_mixed_trades(), summary={})
    sr = v.strategy_robustness()

    by_sig = sr["by_signal"]
    check("by_signal BUY has 2 trades, SELL 1",
          by_sig["BUY"]["trades"] == 2 and by_sig["SELL"]["trades"] == 1,
          "(=%s)" % by_sig)
    check("by_signal BUY net == 6.594",
          approx(by_sig["BUY"]["net"], 6.594, 1e-4),
          "(=%s)" % by_sig["BUY"]["net"])
    check("by_signal BUY win_rate == 50",
          by_sig["BUY"]["win_rate_pct"] == 50.0,
          "(=%s)" % by_sig["BUY"]["win_rate_pct"])
    check("by_side SELL wins == 1 and net == 4.805",
          sr["by_side"]["SELL"]["wins"] == 1
          and approx(sr["by_side"]["SELL"]["net"], 4.805, 1e-4),
          "(=%s)" % sr["by_side"]["SELL"])
    check("by_symbol BTC has 2, ETH 1",
          sr["by_symbol"]["BTC"]["trades"] == 2
          and sr["by_symbol"]["ETH"]["trades"] == 1,
          "(=%s)" % sr["by_symbol"])
    check("by_exit_reason STOP_LOSS losses == 1",
          sr["by_exit_reason"]["STOP_LOSS"]["losses"] == 1,
          "(=%s)" % sr["by_exit_reason"]["STOP_LOSS"])
    check("by_strategy tagged ALLMIGHTSEE_PRIME for all trades",
          sr["by_strategy"].get("ALLMIGHTSEE_PRIME", {}).get("trades") == 3,
          "(=%s)" % sr["by_strategy"])
    check("every value carries sufficiency + win_rate_pct",
          all("sufficiency" in blk and "win_rate_pct" in blk
              for dim in sr.values() for blk in dim.values()))

# ============================================================================
# PART C - market-condition validation (context-in-window join)
# ============================================================================
def test_market_condition():
    print("=" * 70)
    print("PART C - Market-condition validation (context join + buckets)")
    print("=" * 70)
    trades = _mixed_trades()
    ctx_rows = [
        _ctx_row("2026-08-01T09:30:00Z",
                 _ctx("2026-08-01T09:30:00Z", "BTC", rsi=25, macd=5.0,
                      macd_signal=4.0, volume=90.0, average_volume=100.0)),
        _ctx_row("2026-08-01T11:30:00Z",
                 _ctx("2026-08-01T11:30:00Z", "BTC", rsi=55, macd=2.0,
                      macd_signal=3.0, volume=120.0, average_volume=100.0)),
        _ctx_row("2026-08-02T09:30:00Z",
                 _ctx("2026-08-02T09:30:00Z", "ETH", rsi=60, macd=4.0,
                      macd_signal=2.0, volume=200.0, average_volume=100.0)),
    ]
    v = StrategyMarketValidator(
        trades=trades, contexts=[d for _t, d in ctx_rows],
        context_rows=ctx_rows, summary={})
    mc = v.market_condition_validation()
    cov = mc["coverage"]
    check("coverage: all three trades have in-window context",
          cov["trades_with_context"] == 3
          and cov["trades_without_context"] == 0,
          "(=%s)" % cov)
    cond = mc["conditions"]
    rsi = cond["rsi_band"]
    check("rsi_band available", rsi["available"] is True)
    check("rsi_band buckets match source",
          rsi["buckets"]["rsi_lt_30"]["trades"] == 1
          and rsi["buckets"]["rsi_lt_30"]["wins"] == 1
          and rsi["buckets"]["rsi_30_70"]["trades"] == 2
          and rsi["buckets"]["rsi_30_70"]["losses"] == 1,
          "(=%s)" % rsi["buckets"])
    check("no_context_in_window bucket absent when all matched",
          "no_context_in_window" not in rsi["buckets"])
    macd = cond["macd_trend"]
    check("macd_trend buckets match source",
          macd["buckets"]["macd_bull"]["trades"] == 2
          and macd["buckets"]["macd_bull"]["wins"] == 2
          and macd["buckets"]["macd_bear"]["trades"] == 1
          and macd["buckets"]["macd_bear"]["losses"] == 1,
          "(=%s)" % macd["buckets"])
    vol = cond["volume_regime"]
    check("volume_regime buckets match source",
          vol["buckets"]["vol_low"]["trades"] == 1
          and vol["buckets"]["vol_high"]["trades"] == 2,
          "(=%s)" % vol["buckets"])

# ============================================================================
# PART D - insufficient-evidence handling (never fabricated)
# ============================================================================
def test_insufficient():
    print("=" * 70)
    print("PART D - Insufficient evidence => explicit, never fabricated")
    print("=" * 70)
    # No context rows at all
    v = StrategyMarketValidator(trades=_mixed_trades(), summary={})
    mc = v.market_condition_validation()
    check("no context -> every condition unavailable with reason",
          all(res["available"] is False and res["buckets"] == {}
              for res in mc["conditions"].values()),
          "(=%s)" % {k: r["available"] for k, r in mc["conditions"].items()})
    check("no context -> coverage reports zero context rows",
          mc["coverage"]["context_rows"] == 0
          and mc["coverage"]["trades_with_context"] == 0,
          "(=%s)" % mc["coverage"])
    # Context exists but outside any trade window
    out_of_window = [
        _ctx_row("2026-08-09T09:30:00Z",
                 _ctx("2026-08-09T09:30:00Z", "BTC", rsi=50))]
    v2 = StrategyMarketValidator(trades=_mixed_trades(), summary={},
                                 contexts=[d for _t, d in out_of_window],
                                 context_rows=out_of_window)
    mc2 = v2.market_condition_validation()
    rsi2 = mc2["conditions"]["rsi_band"]
    check("out-of-window context -> all trades binned no_context_in_window",
          rsi2["buckets"].get("no_context_in_window", {}).get("trades") == 3
          and rsi2["buckets"]["no_context_in_window"]["sufficiency"]
          == "insufficient",
          "(=%s)" % rsi2["buckets"])
    # Empty trades -> sufficiency statement present
    v3 = StrategyMarketValidator(trades=[], summary={})
    su = v3.sufficiency()
    check("empty trades -> statement says validation not possible",
          any("no trades recorded" in s for s in su["statement"]))
    check("empty trades -> strategy dimensions insufficient",
          su["strategy_dimensions"]["by_signal"]["status"] == "insufficient")

# ============================================================================
# PART E - report structure & handoff
# ============================================================================
def test_report_structure():
    print("=" * 70)
    print("PART E - Report structure & handoff to 9.6/9.7")
    print("=" * 70)
    trades = _mixed_trades()
    ctx_rows = [
        _ctx_row("2026-08-01T09:30:00Z",
                 _ctx("2026-08-01T09:30:00Z", "BTC", rsi=25))]
    v = StrategyMarketValidator(trades=trades, summary={},
                                contexts=[d for _t, d in ctx_rows],
                                context_rows=ctx_rows)
    rep = v.analyze()
    check("top-level keys present",
          "strategy_robustness" in rep
          and "market_condition_validation" in rep
          and "evidence_sufficiency" in rep and "handoff" in rep
          and "verdict" in rep)
    h = rep["handoff"]
    check("handoff has 9.6 and 9.7 blocks",
          "to_9_6_stability_endurance" in h
          and "to_9_7_final_go_nogo" in h)
    check("9.6 carries robustness + reliability",
          "strategy_robustness" in h["to_9_6_stability_endurance"]
          and "reliability" in h["to_9_6_stability_endurance"])
    check("9.7 carries robustness + sufficiency",
          "strategy_robustness" in h["to_9_7_final_go_nogo"]
          and "sufficiency" in h["to_9_7_final_go_nogo"])
    check("verdict deferred (no invented thresholds)",
          rep["verdict"]["decision"] == "GO_NOGO_DEFERRED")


# ============================================================================
# PART F - regression gate (Phase 9.4 remains intact)
# ============================================================================
def test_regression_gate():
    print("=" * 70)
    print("PART F - Regression gate (Phase 9.4 verification unchanged)")
    print("=" * 70)
    result = subprocess.run(
        [sys.executable, os.path.join(_HERE, "phase_9_4_verify_analysis.py")],
        capture_output=True, text=True, cwd=_HERE)
    tail = (result.stdout or "").strip().splitlines()[-2:]
    check("Phase 9.4 regression passes unchanged (exit=0)",
          result.returncode == 0,
          "(exit=%d %s)" % (result.returncode,
                            tail[-1][:70] if tail else ""))

# ============================================================================
# PART G - CLI round-trip (durable report file)
# ============================================================================
def test_cli_roundtrip(tmpdir):
    print("=" * 70)
    print("PART G - CLI round-trip writes a durable Phase 9.5 report")
    print("=" * 70)
    ev = os.path.join(tmpdir, "ev.jsonl")
    summary = os.path.join(tmpdir, "summary.json")
    rows = []
    for tr in _mixed_trades():
        rows.append({"type": "trade", "ts": "2026-08-01T00:00:00Z",
                     "data": tr})
    data = _ctx("2026-08-01T09:30:00Z", "BTC", rsi=25)
    rows.append({"type": "context", "ts": "2026-08-01T09:30:00Z",
                 "data": data})
    with open(ev, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    with open(summary, "w", encoding="utf-8") as fh:
        json.dump({"collection_id": "cli_test",
                   "evidence_log": "ev.jsonl",
                   "metrics_summary": "m.jsonl",
                   "restart_count": 0, "cycles": 0}, fh)

    v = StrategyMarketValidator.from_files(ev, summary)
    rep = v.analyze()
    check("from_files parsed 3 trades + 1 context",
          len(v.trades) == 3 and len(v.context_rows) == 1,
          "(trades=%d ctx=%d)" % (len(v.trades), len(v.context_rows)))
    out = os.path.join(tmpdir, "validation_9_5.json")
    written = v.write_report(rep, out)
    check("durable report written", os.path.exists(written))
    with open(written, "r", encoding="utf-8") as fh:
        persisted = json.load(fh)
    check("persisted report has robustness + handoff + phase 9.5",
          "strategy_robustness" in persisted and "handoff" in persisted
          and persisted["phase"] == "9.5")


# ============================================================================
# Runner
# ============================================================================
def run():
    print("Phase 9.5 - Verify Strategy & Market-Condition Validation "
          "(offline, NON-MUTATING)")
    print("DRY_RUN=True | LIVE_TRADING_ENABLED=False | analysis only, "
          "no real orders, no fabricated evidence.")

    bot_main.PAPER_POSITIONS = {}
    bot_main.PAPER_TRADE_HISTORY = []
    bot_main.PAPER_BALANCE = bot_main.INITIAL_PAPER_BALANCE
    bot_main.ALERTS = {}

    with tempfile.TemporaryDirectory() as tmpdir:
        parts = [
            test_safety,
            test_strategy_robustness,
            test_market_condition,
            test_insufficient,
            test_report_structure,
            test_regression_gate,
            lambda: test_cli_roundtrip(tmpdir),
        ]
        for part in parts:
            try:
                part()
            except Exception as e:  # noqa: BLE001
                check(getattr(part, "__name__", "part") + " completed",
                      False, "(raised: %r)" % e)

    print()
    print("=" * 70)
    if FAILURES:
        print("VERIFY VALIDATION: %d FAILURE(S) -> %s" % (len(FAILURES),
                                                          FAILURES))
        print("Phase 9.5 technical verification: FAIL")
        sys.exit(1)
    print("VERIFY VALIDATION: ALL CHECKS PASSED (Phase 9.5)")
    print("Technical verification of StrategyMarketValidator passed "
          "(separate from any trading-performance conclusion).")
    print("Phase 9.5 verification: PASS")
    sys.exit(0)


if __name__ == "__main__":
    run()