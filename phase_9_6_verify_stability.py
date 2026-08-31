#!/usr/bin/env python3
"""Phase 9.6 - Verify Stability & Endurance Validation (offline, NON-MUTATING).

Verifies the Phase 9.6 `StabilityEnduranceValidator` (phase_9_6_stability.py)
and its integration with the validated Phase 9.4/9.5 layers.  Uses synthetic-
but-realistic Phase 9.3-schema evidence.  Technical verification only; trading
conclusions remain a Phase 9.7 decision.

PROVEN:
  Part A - Safety/config contract (DRY_RUN=True, live=False, analysis only,
           main.py paper state untouched, no real orders).
  Part B - Repeatability: Phase 9.4 & 9.5 reports are byte-identical across
           repeated runs on identical evidence (determinism).
  Part C - State consistency: Phase 9.4 observed metrics + attribution sums
           reconcile to Phase 9.3 collected totals (no layer drift).
  Part D - Error stability: malformed/partial/empty evidence degrades without
           crashing and without fabricating claims; deterministic.
  Part E - Extended run: 200 simulated cycles aggregate; analyzers produce
           stable, sufficient-only conclusions; no real-order routes.
  Part F - Handoff to 9.7 + verdict GO_NOGO_DEFERRED (no invented thresholds).
  Part G - Regression gate: Phase 9.5 verification still PASSES unchanged.
"""

import json
import os
import sys
import subprocess
import tempfile

os.environ["DRY_RUN"] = "True"
os.environ["LIVE_TRADING_ENABLED"] = "False"

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import main as bot_main  # noqa: E402
from phase_9_4_analysis import PerformanceAnalyzer  # noqa: E402
from phase_9_5_validation import StrategyMarketValidator  # noqa: E402
from phase_9_6_stability import StabilityEnduranceValidator  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    print(("PASS" if condition else "FAIL"), "-", name, detail)
    if not condition:
        FAILURES.append(name)


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ===========================================================================
# Deterministic synthetic evidence (Phase 9.3 schema)
# ===========================================================================
def _trade(symbol, side, signal, status, gross, fees, net, opened, closed):
    return {
        "symbol": symbol, "side": side, "signal": signal,
        "entry": 100.0, "exit": 110.0, "quantity": 1.0,
        "gross_pnl": gross, "fees": fees, "net_pnl": net, "status": status,
        "exit_reason": status, "strategy": "ALLMIGHTSEE_PRIME",
        "opened_at": opened, "closed_at": closed,
        "reason": "verify signal", "win": 1 if net > 0 else 0,
    }


_TRADES = [
    _trade("BTC", "SELL", "SELL", "TAKE_PROFIT", 12.0, 0.22, 11.78,
           "2026-08-01T09:04:00Z", "2026-08-01T09:10:00Z"),
    _trade("ETH", "BUY", "BUY", "TAKE_PROFIT", 8.0, 0.16, 7.84,
           "2026-08-01T10:00:00Z", "2026-08-01T10:12:00Z"),
    _trade("BTC", "BUY", "BUY", "STOP_LOSS", -5.0, 0.10, -5.10,
           "2026-08-01T11:00:00Z", "2026-08-01T11:20:00Z"),
]
# totals: 3 trades, 2 wins, 1 loss, gross 15.0, fees 0.48, net 14.52


def _build_evidence_rows():
    rows = []
    for tr in _TRADES:
        rows.append({"type": "trade", "ts": "2026-08-01T08:00:00Z",
                     "data": tr})
    rows.append({"type": "context", "ts": "2026-08-01T09:05:00Z",
                 "data": {"symbol": "BTC", "current_price": 60000.0,
                          "rsi": 45, "macd": 0.1, "ema20": 59800.0}})
    rows.append({"type": "error", "ts": "2026-08-01T12:00:00Z",
                 "data": {"message": "simulated error record", "level": "info"}})
    return rows


def write_evidence(tmp_dir, rows=None, summary_override=None):
    rows = rows if rows is not None else _build_evidence_rows()
    ev = os.path.join(tmp_dir, "beta_evidence.jsonl")
    sm = os.path.join(tmp_dir, "beta_evidence_summary.json")
    with open(ev, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    summary = dict(summary_override or {})
    summary.setdefault("collection_id", "p96_test")
    summary.setdefault("evidence_log", ev)
    summary.setdefault("metrics_summary", "")
    summary.setdefault("restart_count", 0)
    summary.setdefault("cycles", 0)
    # Phase 9.3 collected totals (authoritative for consistency check)
    summary.setdefault("trades", len(_TRADES))
    summary.setdefault("wins", sum(1 for t in _TRADES if t["win"]))
    summary.setdefault("losses", sum(1 for t in _TRADES if not t["win"]))
    summary.setdefault("gross_pnl", round(sum(t["gross_pnl"] for t in _TRADES), 4))
    summary.setdefault("net_pnl", round(sum(t["net_pnl"] for t in _TRADES), 4))
    summary.setdefault("fees_total", round(sum(t["fees"] for t in _TRADES), 4))
    summary.setdefault("equity", {"initial_balance": 1000.0,
                                  "ending_balance": 1014.52})
    with open(sm, "w", encoding="utf-8") as fh:
        json.dump(summary, fh)
    return ev, sm


# ============================================================================
# Part A - Safety/config contract
# ============================================================================
def test_safety():
    print("=" * 70)
    print("PART A - Safety/config contract")
    print("=" * 70)
    check("DRY_RUN=True", os.environ.get("DRY_RUN") == "True")
    check("LIVE_TRADING_ENABLED=False",
          os.environ.get("LIVE_TRADING_ENABLED") == "False")
    check("phase_9_6 module imports (analysis only)",
          StabilityEnduranceValidator is not None)
    bot_main.PAPER_BALANCE = bot_main.INITIAL_PAPER_BALANCE
    before = bot_main.PAPER_BALANCE
    with tempfile.TemporaryDirectory() as tmp:
        ev, sm = write_evidence(tmp)
        v = StabilityEnduranceValidator.from_files(ev, sm)
        v.analyze = v.validate
        v.validate()
    after = bot_main.PAPER_BALANCE
    check("main.py paper state untouched (balance before==after)",
          before == after, "(%.2f -> %.2f)" % (before, after))


# ============================================================================
# Part B - Repeatability (determinism)
# ============================================================================
def test_repeatability():
    print("=" * 70)
    print("PART B - Repeatability/determinism")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as tmp:
        ev, sm = write_evidence(tmp)
        rep94 = [json.dumps(PerformanceAnalyzer.from_files(ev, sm).analyze(),
                            sort_keys=True) for _ in range(3)]
        rep95 = [json.dumps(StrategyMarketValidator.from_files(ev, sm).analyze(),
                            sort_keys=True) for _ in range(3)]
        check("Phase 9.4 reports byte-identical across runs",
              all(r == rep94[0] for r in rep94))
        check("Phase 9.5 reports byte-identical across runs",
              all(r == rep95[0] for r in rep95))


# ============================================================================
# Part C - State consistency
# ============================================================================
def test_state_consistency():
    print("=" * 70)
    print("PART C - State consistency (9.4 vs 9.3 totals)")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as tmp:
        ev, sm = write_evidence(tmp)
        v = StabilityEnduranceValidator.from_files(ev, sm)
        res = v.state_consistency(ev, sm)
        checks = res["checks"]
        check("observed total_trades matches evidence", checks["trade_count_matches"],
              "(obs=%s, evidence=%d, attribution=%d, summary=%d)"
              % (res["observed_metrics"].get("total_trades"),
                 res["evidence_total_trades"], res["attribution_trades"],
                 res["summary_total_trades"]))
        check("wins match", checks["wins_match"])
        check("losses match", checks["losses_match"])
        check("gross P&L matches", checks["gross_matches"])
        check("net P&L matches", checks["net_matches"])
        check("fees match", checks["fees_match"])
        check("state consistency sufficient", checks["sufficient"])


# ============================================================================
# Part D - Error stability
# ============================================================================
def test_error_stability():
    print("=" * 70)
    print("PART D - Error handling stability (no crash, no fabrication)")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as tmp:
        v = StabilityEnduranceValidator(evidence_path="x", summary_path="y")
        res = v.error_stability(tmp)
        check("no crash on malformed/partial/empty evidence",
              res["no_crash"], "(cases=%s)" % list(res["cases"].keys()))
        check("error stability sufficient", res["sufficient"])
        # confirm each case explicitly did NOT crash
        for cname, c in res["cases"].items():
            check("case '%s' no crash" % cname, not c["crashed"])


# ============================================================================
# Part E - Extended run
# ============================================================================
def test_extended_run():
    print("=" * 70)
    print("PART E - Extended-run (200 cycles) stability")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as tmp:
        v = StabilityEnduranceValidator(evidence_path="x", summary_path="y")
        res = v.extended_run(tmp, cycles=200)
        check("extended run completed 200 cycles", res["cycles"] == 200)
        check("no real order route in extended run",
              res["no_real_order_proof"]["place_calls"] == 0,
              "(place_calls=%s signed_calls=%s)"
              % (res["no_real_order_proof"]["place_calls"],
                 res["no_real_order_proof"]["signed_calls"]))
        check("extended run net_pnl populated", res["net_pnl_populated"])
        check("extended run sufficient", res["sufficient"])


# ============================================================================
# Part F - Handoff to 9.7 + verdict
# ============================================================================
def test_handoff_and_verdict():
    print("=" * 70)
    print("PART F - Handoff structure + verdict (no invented thresholds)")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as tmp:
        ev, sm = write_evidence(tmp)
        v = StabilityEnduranceValidator.from_files(ev, sm)
        res = v.validate()
        h = res["handoff"]["to_9_7_final_go_nogo"]
        check("handoff to 9.7 present", "to_9_7_final_go_nogo" in res["handoff"])
        check("stability block contains required keys",
              set(h["stability"].keys()) == {
                  "phase_9_4_deterministic", "phase_9_5_deterministic",
                  "state_consistency", "error_handling_stable",
                  "extended_run_stable"})
        check("verdict GO_NOGO_DEFERRED (no invented thresholds)",
              res["verdict"]["decision"] == "GO_NOGO_DEFERRED")
        check("verdict reason mentions thresholds none established",
              "none established" in res["verdict"]["reason"].lower())


# ============================================================================
# Part G - Regression gate (Phase 9.5 still passes)
# ============================================================================
def test_regression_gate():
    print("=" * 70)
    print("PART G - Regression gate: Phase 9.5 verification still PASSES")
    print("=" * 70)
    r = subprocess.run(
        [os.path.join(_HERE, "venv", "bin", "python"),
         "phase_9_5_verify_validation.py"],
        cwd=_HERE, capture_output=True, text=True, timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    check("Phase 9.5 verification exit code 0", r.returncode == 0,
          "(rc=%s)" % r.returncode)
    check("Phase 9.5 reports PASS", "Phase 9.5 verification: PASS" in out)


# ============================================================================
# Runner
# ============================================================================
def run():
    print("Phase 9.6 - Verify Stability & Endurance Validation")
    print("DRY_RUN=True | LIVE_TRADING_ENABLED=False | analysis only, "
          "no real orders.")
    print("=" * 70)
    bot_main.PAPER_BALANCE = bot_main.INITIAL_PAPER_BALANCE
    with tempfile.TemporaryDirectory() as _tmp_unused:
        # safety check baseline reset
        bot_main.PAPER_POSITIONS = {}
        bot_main.PAPER_TRADE_HISTORY = []
        bot_main.ALERTS = {}
        for part in [test_safety, test_repeatability, test_state_consistency,
                     test_error_stability, test_extended_run,
                     test_handoff_and_verdict, test_regression_gate]:
            try:
                part()
            except Exception as e:  # noqa: BLE001
                check(getattr(part, "__name__", "part") + " completed",
                      False, "(raised: %r)" % e)
    print()
    print("=" * 70)
    if FAILURES:
        print("VERIFY STABILITY: %d FAILURE(S) -> %s" % (len(FAILURES),
                                                          FAILURES))
        print("Phase 9.6 verification: FAIL")
        sys.exit(1)
    print("VERIFY STABILITY: ALL CHECKS PASSED (Phase 9.6)")
    print("Technical verification of StabilityEnduranceValidator passed.")
    print("Phase 9.6 verification: PASS")
    sys.exit(0)


if __name__ == "__main__":
    run()



