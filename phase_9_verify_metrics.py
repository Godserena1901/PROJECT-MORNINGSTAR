#!/usr/bin/env python3
"""Phase 9.1 - Verify Beta Metrics Recorder (offline).

Offline verification of phase_9_metrics.MetricsRecorder against synthetic but
realistic paper trades + cycle reports.  No network, no orders, no credentials,
and no change to DRY_RUN / LIVE_TRADING_ENABLED.

Checks:
  Part A/B - Fees, net P&L, win/loss, win rate, profit factor.
  Part C   - Balances (starting / ending / max).
  Part D   - Equity curve & max drawdown.
  Part E   - Exposure average / max.
  Part F   - Entry/exit prices, side, strategy, entry & exit reasons.
  Part G   - Daily P&L series.
  Part H   - Error / emergency-halt events + durable counters.
  Part I   - Restart detection (restart_count increments on a new session).
  Part J   - Round-trip: summary + JSONL output files are written/re-readable.

Usage:
    python3 phase_9_verify_metrics.py
Exit code 0 = all checks passed; 1 = one or more checks failed.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from phase_9_metrics import MetricsRecorder  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    print(("PASS" if condition else "FAIL"), "-", name, detail)
    if not condition:
        FAILURES.append(name)


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def _trade(symbol, side, entry, exit_p, qty, status, pnl, reason="", conf=75,
           opened="", closed=""):
    return {
        "symbol": symbol, "side": side, "entry": entry, "exit": exit_p,
        "quantity": qty, "pnl": pnl, "status": status, "reason": reason,
        "confidence": conf, "opened_at": opened, "closed_at": closed,
    }


# ============================================================================
# PART A/B - fees, net P&L, win/loss
# ============================================================================
def test_pnl_and_winloss(tmpdir):
    print("=" * 70)
    print("PART A/B - Fees, net P&L, win/loss aggregation")
    print("=" * 70)
    rec = MetricsRecorder(path=os.path.join(tmpdir, "m.jsonl"),
                          fee_rate=0.001, initial_balance=1000.0)
    # W1: gross 10 ; fees = (100+110)*0.001 = 0.21 ; net = 9.79
    rec.record_trade(_trade("BTC", "BUY", 100.0, 110.0, 1.0,
                            "TAKE_PROFIT", 10.0, reason="bullish macd"))
    # W2: gross 5 ; fees = (100+95)*0.001 = 0.195 ; net = 4.805
    rec.record_trade(_trade("ETH", "SELL", 200.0, 190.0, 0.5,
                            "TAKE_PROFIT", 5.0))
    # L : gross -3 ; fees = (100+96)*0.001 = 0.196 ; net = -3.196
    rec.record_trade(_trade("SOL", "BUY", 100.0, 96.0, 1.0,
                            "STOP_LOSS", -3.0))
    wl = rec.win_loss()

    check("two wins counted", wl["wins"] == 2, "(wins=%s)" % wl["wins"])
    check("one loss counted", wl["losses"] == 1, "(losses=%s)" % wl["losses"])
    check("trade count is three", wl["trades"] == 3, "(trades=%s)" % wl["trades"])
    check("win rate 66.67%", approx(wl["win_rate_pct"], 66.6667, 1e-3),
          "(=%.2f%%)" % wl["win_rate_pct"])
    # gross = 10+5-3 = 12 ; net = 9.79+4.805-3.196 = 11.399
    check("gross P&L is 12.0", approx(wl["gross_pnl"], 12.0, 1e-6),
          "(=%.6f)" % wl["gross_pnl"])
    check("total fees 0.601", approx(wl["fees"], 0.601, 1e-6),
          "(=%.6f)" % wl["fees"])
    check("net P&L 11.399", approx(wl["net_pnl"], 11.399, 1e-6),
          "(=%.6f)" % wl["net_pnl"])
    check("avg win 7.2975", approx(wl["avg_win"], 7.2975, 1e-4),
          "(=%.6f)" % wl["avg_win"])
    check("avg loss -3.196", approx(wl["avg_loss"], -3.196, 1e-4),
          "(=%.6f)" % wl["avg_loss"])


# ============================================================================
# Part C - balances
# ============================================================================
def test_balances(tmpdir):
    print()
    print("=" * 70)
    print("PART C - Balances (starting / ending / max)")
    print("=" * 70)
    rec = MetricsRecorder(path=os.path.join(tmpdir, "b.json"), fee_rate=0.0,
                          initial_balance=5000.0)
    rec.record_trade(_trade("BTC", "BUY", 100.0, 110.0, 1.0,
                            "TAKE_PROFIT", 10.0))
    rec.record_cycle({"equity": 5010.0, "exposure_used_usd": 100.0,
                      "open_positions": 1, "status": "RUNNING"})
    b = rec.balances()
    check("starting balance 5000", approx(b["starting"], 5000.0),
          "(=%.2f)" % b["starting"])
    check("ending balance 5010 (net, zero fees)", approx(b["ending"], 5010.0),
          "(=%.2f)" % b["ending"])
    check("max equity tracked from curve", approx(b["max"], 5010.0),
          "(=%.2f)" % b["max"])

# ============================================================================
# Part D - drawdown
# ============================================================================
def test_drawdown(tmpdir):
    print()
    print("=" * 70)
    print("PART D - Equity curve & max drawdown")
    print("=" * 70)
    rec = MetricsRecorder(path=os.path.join(tmpdir, "d.jsonl"))
    for eq in (1000.0, 1050.0, 980.0, 1020.0):
        rec.record_cycle({"equity": eq, "exposure_used_usd": 50.0,
                          "open_positions": 1, "status": "RUNNING"})
    dd = rec.drawdown()
    # peak 1050, trough 980 -> (1050-980)/1050 = 6.6667%
    check("max drawdown 6.6667%", approx(dd["max_drawdown_pct"], 6.6667, 1e-3),
          "(=%.4f%%)" % dd["max_drawdown_pct"])
    check("peak 1050", approx(dd["peak"], 1050.0), "(=%.2f)" % dd["peak"])
    check("trough 980", approx(dd["trough"], 980.0), "(=%.2f)" % dd["trough"])
    check("current 1020", approx(dd["current"], 1020.0),
          "(=%.2f)" % dd["current"])


# ============================================================================
# Part E - exposure
# ============================================================================
def test_exposure(tmpdir):
    print()
    print("=" * 70)
    print("PART E - Exposure average / max")
    print("=" * 70)
    rec = MetricsRecorder(path=os.path.join(tmpdir, "e.jsonl"))
    for exp in (100.0, 0.0, 150.0):
        rec.record_cycle({"equity": 1000.0, "exposure_used_usd": exp,
                          "open_positions": 1, "status": "RUNNING"})
    ex = rec.exposure_stats()
    check("avg exposure 83.3333", approx(ex["avg"], 83.3333, 1e-3),
          "(=%.4f)" % ex["avg"])
    check("max exposure 150", approx(ex["max"], 150.0), "(=%.2f)" % ex["max"])
    check("three samples", ex["sample_count"] == 3,
          "(=%d)" % ex["sample_count"])


# ============================================================================
# Part F - prices, side, strategy, reasons
# ============================================================================
def test_trade_fields(tmpdir):
    print()
    print("=" * 70)
    print("PART F - Entry/exit prices, side, strategy, reasons")
    print("=" * 70)
    rec = MetricsRecorder(path=os.path.join(tmpdir, "f.jsonl"))
    rec.record_trade(_trade("BTC", "BUY", 100.0, 110.0, 1.0, "TAKE_PROFIT",
                            10.0, reason="bullish signal"))
    t = [r for r in rec.rows if r.get("type") == "trade"][0]
    check("entry 100", approx(t["entry"], 100.0), "(=%.2f)" % t["entry"])
    check("exit 110", approx(t["exit"], 110.0), "(=%.2f)" % t["exit"])
    check("side BUY", t["side"] == "BUY", "(=%s)" % t["side"])
    check("strategy tagged", t["strategy"] == "ALLMIGHTSEE_PRIME",
          "(=%s)" % t["strategy"])
    check("entry reason captured", t["entry_reason"] == "bullish signal",
          "(=%r)" % t["entry_reason"])
    check("exit reason TAKE_PROFIT", t["exit_reason"] == "TAKE_PROFIT",
          "(=%s)" % t["exit_reason"])


# ============================================================================
# Part G - daily P&L
# ============================================================================
def test_daily_pnl(tmpdir):
    print()
    print("=" * 70)
    print("PART G - Daily P&L series")
    print("=" * 70)
    rec = MetricsRecorder(path=os.path.join(tmpdir, "g.jsonl"))
    rec.record_trade(_trade("BTC", "BUY", 100.0, 110.0, 1.0, "TAKE_PROFIT",
                            10.0, closed="2026-08-11T10:00:00"))
    rec.record_trade(_trade("ETH", "SELL", 200.0, 190.0, 0.5, "STOP_LOSS",
                            -2.0, closed="2026-08-12T09:00:00"))
    d = rec.daily_pnl_series()
    check("two days recorded", list(d.keys()) == ["2026-08-11", "2026-08-12"],
          "(days=%s)" % list(d.keys()))
    check("day-one net correct", approx(d.get("2026-08-11", 0.0), 9.79, 1e-4),
          "(=%.4f)" % d.get("2026-08-11", 0.0))

# ============================================================================
# Part H - error / emergency events
# ============================================================================
def test_events(tmpdir):
    print()
    print("=" * 70)
    print("PART H - Error & emergency-halt events + counters")
    print("=" * 70)
    rec = MetricsRecorder(path=os.path.join(tmpdir, "h.jsonl"))
    rec.record_cycle({"equity": 1000.0, "exposure_used_usd": 0.0,
                      "open_positions": 0, "status": "RUNNING",
                      "errors": ["analyze_symbol(BTC) failed: xyz",
                                 "duplicate position not opened"]})
    rec.record_cycle({"equity": 995.0, "exposure_used_usd": 50.0,
                      "open_positions": 1, "status": "EMERGENCY_HALT",
                      "halted_reason": "emergency stop file present",
                      "offline": True})
    check("two errors counted", rec.errors_count == 2,
          "(count=%d)" % rec.errors_count)
    check("one emergency counted", rec.emergency_count == 1,
          "(count=%d)" % rec.emergency_count)
    check("offline sample counted", rec.offline_count == 1,
          "(count=%d)" % rec.offline_count)
    check("error events are durable", len(rec.error_events()) == 2,
          "(n=%d)" % len(rec.error_events()))
    check("emergency event recorded", len(rec.emergency_events()) == 1,
          "(n=%d)" % len(rec.emergency_events()))


# ============================================================================
# Part I - restarts
# ============================================================================
def test_restart(tmpdir):
    print()
    print("=" * 70)
    print("PART I - Restart detection")
    print("=" * 70)
    path = os.path.join(tmpdir, "r.jsonl")
    rec1 = MetricsRecorder(path=path)
    rec1.start_session()
    rec1.write_summary()
    check("first session restart_count 0", rec1.restart_count == 0,
          "(restart=%s)" % rec1.restart_count)
    # A new recorder on the same summary file detects a restart.
    rec2 = MetricsRecorder(path=path)
    rec2.start_session()
    check("restart detected (count incremented)",
          rec2.restart_count == 1,
          "(prev=%s new=%s)" % (rec1.restart_count, rec2.restart_count))


# ============================================================================
# Part J - output files round-trip
# ============================================================================
def test_output_files(tmpdir):
    print()
    print("=" * 70)
    print("PART J - Summary + JSONL files written")
    print("=" * 70)
    path = os.path.join(tmpdir, "out.jsonl")
    rec = MetricsRecorder(path=path, initial_balance=1000.0)
    rec.record_trade(_trade("BTC", "BUY", 100.0, 110.0, 1.0, "TAKE_PROFIT",
                            10.0))
    rec.record_cycle({"equity": 1010.0, "exposure_used_usd": 100.0,
                      "open_positions": 1, "status": "RUNNING"})
    summary = rec.write_summary()
    check("summary file created", os.path.exists(rec.summary_path))
    check("jsonl file created", os.path.exists(path))
    check("summary has balances+trades keys",
          "balances" in summary and "win_loss" in summary
          and "trades" in summary["win_loss"])
    with open(path, "r", encoding="utf-8") as fh:
        lines = [json.loads(l) for l in fh if l.strip()]
    types = [l["type"] for l in lines]
    check("jsonl has 1 trade + 1 cycle",
          types.count("trade") == 1 and types.count("cycle") == 1,
          "(types=%s)" % types)


# ============================================================================
# Runner
# ============================================================================
def main():
    print("Phase 9.1 - Verify Beta Metrics Recorder (offline)")
    print("No orders, no network, DRY_RUN / live trading untouched.")
    with tempfile.TemporaryDirectory() as tmpdir:
        tests = [
            test_pnl_and_winloss, test_balances, test_drawdown, test_exposure,
            test_trade_fields, test_daily_pnl, test_events, test_restart,
            test_output_files,
        ]
        for fn in tests:
            try:
                fn(tmpdir)
            except Exception as e:  # noqa: BLE001
                check(fn.__name__ + " completed", False, repr(e))
    print()
    print("=" * 70)
    if FAILURES:
        print("VERIFY METRICS: %d FAILURE(S) -> %s" % (len(FAILURES), FAILURES))
        return 1
    print("VERIFY METRICS: ALL CHECKS PASSED (Phase 9.1)")
    return 0


if __name__ == "__main__":
    sys.exit(main())