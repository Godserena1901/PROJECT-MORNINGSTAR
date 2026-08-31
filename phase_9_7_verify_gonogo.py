#!/usr/bin/env python3
"""Phase 9.7 - Verify Final Phase 9 GO/NO-GO Validation (offline, NON-MUTATING).

Verifies the Phase 9.7 `Phase9FinalGoNoGo` consolidation layer
(phase_9_7_gonogo.py) and its integration with the validated Phase 9.4/9.5/9.6
handoff contracts.  Uses synthetic-but-realistic Phase 9.3-schema evidence.
Technical verification only: the GO/NO-GO verdict itself remains
GO_NOGO_DEFERRED because the repository establishes no thresholds.

PROVEN:
  Part A - Safety/config contract (DRY_RUN=True, live=False, analysis only,
           main.py paper state untouched, no real orders).
  Part B - Consolidation: 9.4/9.5/9.6 handoff blocks consumed intact; observed
           metrics reconcile with hand-computed totals (no layer drift).
  Part C - Verdict: GO_NOGO_DEFERRED with explicit no-thresholds policy
           (no invented profitability gates).
  Part D - Sufficiency handling: runs clean on empty/missing evidence; verdict
           stays DEFERRED regardless of sufficiency; nothing fabricated.
  Part E - Durability/IO: atomic report write, re-readable JSON, deterministic
           consolidation across repeated runs.
  Part F - CLI smoke test (analysis-only main writes report to temp dir).
  Part G - Regression gate: Phase 9.4 and Phase 9.6 (which chains 9.5)
           verifications still PASS unchanged.
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
from phase_9_7_gonogo import Phase9FinalGoNoGo  # noqa: E402
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
# Deterministic synthetic evidence (Phase 9.3 schema, same as Phase 9.6)
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
_TOTALS = {
    "trades": len(_TRADES),
    "wins": sum(1 for t in _TRADES if t["win"]),
    "losses": sum(1 for t in _TRADES if not t["win"]),
    "gross": round(sum(t["gross_pnl"] for t in _TRADES), 4),
    "fees": round(sum(t["fees"] for t in _TRADES), 4),
    "net": round(sum(t["net_pnl"] for t in _TRADES), 4),
}


def _build_evidence_rows():
    rows = []
    for tr in _TRADES:
        rows.append({"type": "trade", "ts": "2026-08-01T08:00:00Z",
                     "data": tr})
    rows.append({"type": "context", "ts": "2026-08-01T09:05:00Z",
                 "data": {"symbol": "BTC", "current_price": 60000.0,
                          "rsi": 45, "macd": 0.1, "ema20": 59800.0}})
    rows.append({"type": "error", "ts": "2026-08-01T12:00:00Z",
                 "data": {"message": "simulated error record",
                          "level": "info"}})
    return rows


def write_evidence(tmp_dir, rows=None, summary_override=None):
    rows = rows if rows is not None else _build_evidence_rows()
    ev = os.path.join(tmp_dir, "beta_evidence.jsonl")
    sm = os.path.join(tmp_dir, "beta_evidence_summary.json")
    with open(ev, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    summary = dict(summary_override or {})
    summary.setdefault("collection_id", "p97_test")
    summary.setdefault("evidence_log", ev)
    summary.setdefault("metrics_summary", "")
    summary.setdefault("restart_count", 0)
    summary.setdefault("cycles", 0)
    summary.setdefault("trades", _TOTALS["trades"])
    summary.setdefault("wins", _TOTALS["wins"])
    summary.setdefault("losses", _TOTALS["losses"])
    summary.setdefault("gross_pnl", _TOTALS["gross"])
    summary.setdefault("net_pnl", _TOTALS["net"])
    summary.setdefault("fees_total", _TOTALS["fees"])
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
    check("phase_9_7 module imports (analysis only)",
          Phase9FinalGoNoGo is not None)
    check("consolidation layer declares analysis_only safety posture",
          Phase9FinalGoNoGo(None, None).safety()["analysis_only"] is True)
    bot_main.PAPER_BALANCE = bot_main.INITIAL_PAPER_BALANCE
    before = bot_main.PAPER_BALANCE
    with tempfile.TemporaryDirectory() as tmp:
        ev, sm = write_evidence(tmp)
        Phase9FinalGoNoGo.from_files(ev, sm).consolidate()
    after = bot_main.PAPER_BALANCE
    check("main.py paper state untouched (balance before==after)",
          before == after, "(%.2f -> %.2f)" % (before, after))
    check("paper positions/history untouched",
          bot_main.PAPER_POSITIONS == {} and bot_main.PAPER_TRADE_HISTORY == [])


# ============================================================================
# Part B - Consolidation of 9.4/9.5/9.6 handoffs
# ============================================================================
def test_consolidation():
    print("=" * 70)
    print("PART B - Consolidation of Phase 9.4/9.5/9.6 handoff blocks")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as tmp:
        ev, sm = write_evidence(tmp)
        rep = Phase9FinalGoNoGo.from_files(ev, sm).consolidate()
        check("top-level keys present",
              all(k in rep for k in ("phase", "schema", "consolidated_from",
                                     "evidence_sufficiency", "safety",
                                     "verdict")))
        check("phase is 9.7 / schema 1", rep["phase"] == "9.7"
              and rep["schema"] == Phase9FinalGoNoGo.SCHEMA)
        cf = rep["consolidated_from"]
        check("consolidated_from carries 9.4, 9.5, 9.6 blocks",
              set(cf.keys()) == {"phase_9_4_performance",
                                 "phase_9_5_strategy_market",
                                 "phase_9_6_stability_endurance"})
        check("9.4 block carries observed_metrics + sufficiency",
              "observed_metrics" in cf["phase_9_4_performance"]
              and "sufficiency" in cf["phase_9_4_performance"])
        check("9.5 block carries strategy_robustness + sufficiency",
              "strategy_robustness" in cf["phase_9_5_strategy_market"]
              and "sufficiency" in cf["phase_9_5_strategy_market"])
        check("9.6 block carries stability flags",
              set(cf["phase_9_6_stability_endurance"]["stability"].keys())
              == {"phase_9_4_deterministic", "phase_9_5_deterministic",
                  "state_consistency", "error_handling_stable",
                  "extended_run_stable"})
        om = cf["phase_9_4_performance"]["observed_metrics"]
        check("observed trade count reconciles (3)",
              om.get("total_trades") == _TOTALS["trades"])
        check("observed net P&L reconciles (14.52)",
              abs(_f(om.get("net_pnl")) - _TOTALS["net"]) < 1e-9,
              "(%s vs %s)" % (om.get("net_pnl"), _TOTALS["net"]))
        check("observed gross P&L reconciles (15.0)",
              abs(_f(om.get("gross_pnl")) - _TOTALS["gross"]) < 1e-9)
        check("observed fees reconcile (0.48)",
              abs(_f(om.get("fees_total")) - _TOTALS["fees"]) < 1e-9)
        check("observed wins/losses reconcile (2/1)",
              om.get("wins") == _TOTALS["wins"]
              and om.get("losses") == _TOTALS["losses"])
        safety = rep["safety"]
        check("report records dry_run True / live False",
              safety["dry_run"] is True
              and safety["live_trading_enabled"] is False)
        check("report records zero real-order routes",
              safety["no_real_order_proof"]["place_calls"] == 0
              and safety["no_real_order_proof"]["signed_calls"] == 0)


# ============================================================================
# Part C - Verdict policy (no invented thresholds)
# ============================================================================
def test_verdict():
    print("=" * 70)
    print("PART C - Verdict policy")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as tmp:
        ev, sm = write_evidence(tmp)
        rep = Phase9FinalGoNoGo.from_files(ev, sm).consolidate()
        v = rep["verdict"]
        check("verdict GO_NOGO_DEFERRED (no invented thresholds)",
              v["decision"] == "GO_NOGO_DEFERRED")
        check("verdict reason states thresholds not established",
              "threshold" in v["reason"].lower())
        check("threshold_policy explicitly 'none established'",
              "none established" in v["threshold_policy"].lower())
        check("sufficiency note keeps GO/NO-GO deferred regardless",
              "deferred" in rep["evidence_sufficiency"]["note"].lower())
        check("no definitive GO or NO-GO claim in verdict",
              v["decision"] not in ("GO", "NO-GO"))


# ============================================================================
# Part D - Sufficiency handling / degradation
# ============================================================================
def test_degradation():
    print("=" * 70)
    print("PART D - Sufficiency handling (empty/missing evidence)")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as tmp:
        ev = os.path.join(tmp, "empty.jsonl")
        sm = os.path.join(tmp, "empty_summary.json")
        open(ev, "w").close()
        with open(sm, "w", encoding="utf-8") as fh:
            json.dump({"collection_id": "p97_empty"}, fh)
        rep = Phase9FinalGoNoGo.from_files(ev, sm).consolidate()
        check("consolidate() runs clean on empty evidence", rep is not None)
        check("verdict stays DEFERRED on empty evidence (nothing fabricated)",
              rep["verdict"]["decision"] == "GO_NOGO_DEFERRED")
        om = rep["consolidated_from"]["phase_9_4_performance"][
            "observed_metrics"]
        check("empty evidence reports zero trades (no fabricated history)",
              om.get("total_trades") == 0)
        check("missing evidence file also degrades cleanly",
              Phase9FinalGoNoGo.from_files(
                  os.path.join(tmp, "missing.jsonl"),
                  os.path.join(tmp, "missing_summary.json"))
              .consolidate()["verdict"]["decision"] == "GO_NOGO_DEFERRED")


# ============================================================================
# Part E - Durability / IO / determinism
# ============================================================================
def test_durability():
    print("=" * 70)
    print("PART E - Durability, IO, determinism")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as tmp:
        ev, sm = write_evidence(tmp)
        final = Phase9FinalGoNoGo.from_files(ev, sm)
        rep = final.consolidate()
        out = os.path.join(tmp, "beta_gonogo_9_7.json")
        path = final.write_report(rep, out)
        check("write_report returns the output path", path == out)
        check("report file exists and tmp cleaned up",
              os.path.exists(out) and not os.path.exists(out + ".tmp"))
        with open(out, encoding="utf-8") as fh:
            persisted = json.load(fh)
        check("persisted report re-reads with phase 9.7 + verdict",
              persisted["phase"] == "9.7"
              and persisted["verdict"]["decision"] == "GO_NOGO_DEFERRED")
        check("persisted report carries consolidated blocks",
              set(persisted["consolidated_from"].keys())
              == {"phase_9_4_performance", "phase_9_5_strategy_market",
                  "phase_9_6_stability_endurance"})
        dumps = [json.dumps(Phase9FinalGoNoGo.from_files(ev, sm)
                            .consolidate(), sort_keys=True)
                 for _ in range(2)]
        check("consolidation deterministic across repeated runs",
              dumps[0] == dumps[1])


# ============================================================================
# Part F - CLI smoke test (analysis-only)
# ============================================================================
def test_cli():
    print("=" * 70)
    print("PART F - CLI smoke test")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as tmp:
        ev, sm = write_evidence(tmp)
        out = os.path.join(tmp, "cli_report.json")
        r = subprocess.run(
            [os.path.join(_HERE, "venv", "bin", "python"),
             "phase_9_7_gonogo.py", "--evidence", ev, "--summary", sm,
             "--out", out],
            cwd=_HERE, capture_output=True, text=True, timeout=600)
        out_all = (r.stdout or "") + (r.stderr or "")
        check("CLI exit code 0", r.returncode == 0, "(rc=%s)" % r.returncode)
        check("CLI wrote report file", os.path.exists(out))
        if os.path.exists(out):
            with open(out, encoding="utf-8") as fh:
                persisted = json.load(fh)
            check("CLI report verdict GO_NOGO_DEFERRED",
                  persisted["verdict"]["decision"] == "GO_NOGO_DEFERRED")
        else:
            check("CLI report verdict GO_NOGO_DEFERRED", False, out_all[-200:])
        check("CLI output contains no order placement",
              "place_order" not in out_all)


# ============================================================================
# Part G - Regression gate (Phase 9.4 + Phase 9.6 chain still pass)
# ============================================================================
def test_regression_gate():
    print("=" * 70)
    print("PART G - Regression gate: Phase 9.4 / 9.5 / 9.6 still PASS")
    print("=" * 70)
    venv_py = os.path.join(_HERE, "venv", "bin", "python")
    r = subprocess.run([venv_py, "phase_9_4_verify_analysis.py"],
                       cwd=_HERE, capture_output=True, text=True, timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    check("Phase 9.4 verification exit code 0", r.returncode == 0,
          "(rc=%s)" % r.returncode)
    check("Phase 9.4 reports PASS", "Phase 9.4 verification: PASS" in out)
    r = subprocess.run([venv_py, "phase_9_5_verify_validation.py"],
                       cwd=_HERE, capture_output=True, text=True, timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    check("Phase 9.5 verification exit code 0", r.returncode == 0,
          "(rc=%s)" % r.returncode)
    check("Phase 9.5 reports PASS", "Phase 9.5 verification: PASS" in out)
    r = subprocess.run([venv_py, "phase_9_6_verify_stability.py"],
                       cwd=_HERE, capture_output=True, text=True, timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    check("Phase 9.6 verification exit code 0", r.returncode == 0,
          "(rc=%s)" % r.returncode)
    check("Phase 9.6 reports PASS", "Phase 9.6 verification: PASS" in out)


# ============================================================================
# Runner
# ============================================================================
def run():
    print("Phase 9.7 - Verify Final GO/NO-GO Validation")
    print("DRY_RUN=True | LIVE_TRADING_ENABLED=False | analysis only, "
          "no real orders.")
    print("=" * 70)
    bot_main.PAPER_BALANCE = bot_main.INITIAL_PAPER_BALANCE
    bot_main.PAPER_POSITIONS = {}
    bot_main.PAPER_TRADE_HISTORY = []
    bot_main.ALERTS = {}
    for part in [test_safety, test_consolidation, test_verdict,
                 test_degradation, test_durability, test_cli,
                 test_regression_gate]:
        try:
            part()
        except Exception as e:  # noqa: BLE001
            check(getattr(part, "__name__", "part") + " completed",
                  False, "(raised: %r)" % e)
    print()
    print("=" * 70)
    if FAILURES:
        print("VERIFY GONOGO: %d FAILURE(S) -> %s" % (len(FAILURES), FAILURES))
        print("Phase 9.7 verification: FAIL")
        sys.exit(1)
    print("VERIFY GONOGO: ALL CHECKS PASSED (Phase 9.7)")
    print("Technical verification of Phase9FinalGoNoGo passed.")
    print("Final verdict remains GO_NOGO_DEFERRED (no thresholds invented).")
    print("Phase 9.7 verification: PASS")
    sys.exit(0)


if __name__ == "__main__":
    run()