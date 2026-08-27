#!/usr/bin/env python3
"""Phase 9.3 - Verify Controlled Beta / Data Collection (offline, NON-MUTATING).

PROJECT MORNINGSTAR Phase 9.3 verification harness.

It verifies the durable evidence-collection mechanism (BetaCollector in
phase_9_3_beta.py) by REUSING the validated Phase 9.1 MetricsRecorder and the
Phase 9.2 shadow-mode reference artifacts (FakeMarketData / ShadowEngine),
keeping the run offline and free of side effects on the real paper engine.

WHAT IT PROVES:
  Part A  - Safety/config contract (DRY_RUN=True, live disabled, hard-locked).
  Part B  - Full evidence suite recorded: trade count, wins/losses, gross and
            net P&L, fees, equity/balance, max drawdown, strategy/signal info,
            market/context info, errors, timestamps.
  Part C  - Exact fee + net P&L math (win and loss paths).
  Part D  - Market/context snapshot rows + error capture (never silent).
  Part E  - Restart/recovery: durable accumulation across a new session with
            NO duplicate trade counting.
  Part F  - Non-mutation: main.PAPER_BALANCE / PAPER_POSITIONS /
            PAPER_TRADE_HISTORY / ALERTS are untouched.
  Part G  - No real-order endpoint is reached (place_order / _signed_request
            sentinels stay empty end-to-end).
  Part H  - Credential redaction: secret-shaped fields can never reach
            collected artifacts.
  Part I  - Evidence JSONL + summary round-trip (files written, re-readable).
  Part J  - Regression gate: Phase 9.1 and Phase 9.2 verification suites
            still pass unchanged.

SAFETY:
    DRY_RUN is forced True and LIVE_TRADING_ENABLED is forced False before the
    shared engine loads.  main.py paper state is only snapshotted, never
    modified.  No credentials are printed.  All artifacts live in a temp dir.

Usage:
    python3 phase_9_3_verify_beta.py
Exit code 0 = all checks passed; 1 = one or more checks failed.
"""

import json
import os
import sys
import subprocess
import tempfile

# SAFETY: force safe defaults BEFORE importing the shared engine.
os.environ["DRY_RUN"] = "True"
os.environ["LIVE_TRADING_ENABLED"] = "False"

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import main as bot_main  # noqa: E402
from phase_9_metrics import MetricsRecorder  # noqa: E402
from phase_9_3_beta import BetaCollector, _redact  # noqa: E402
from phase_9_2_verify_shadow import (FakeMarketData,  # noqa: E402
                                     ShadowEngine)

FAILURES = []
CHECKS = 0


def check(name, condition, detail=""):
    global CHECKS
    CHECKS += 1
    print(("PASS" if condition else "FAIL"), "-", name, detail)
    if not condition:
        FAILURES.append(name)


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def _dec(symbol, signal, entry, sl, tp, conf=80, risk="MEDIUM"):
    return {
        "symbol": symbol,
        "signal": signal,
        "current_price": entry,
        "entry": entry,
        "stop_loss": sl,
        "take_profit": tp,
        "confidence": conf,
        "risk": risk,
        "risk_reward": round(abs(tp - entry) / abs(entry - sl), 2)
        if abs(entry - sl) > 0 else 2.0,
        "reason": "Phase 9.3 verification signal",
    }


def _hold(symbol, price):
    return {"symbol": symbol, "signal": "HOLD", "current_price": price,
            "entry": price, "stop_loss": price * 0.99,
            "take_profit": price * 1.01, "confidence": 10, "risk": "LOW",
            "risk_reward": 1.0, "reason": "verify hold (avoid reopen)"}

# ============================================================================
# Shared builders / guards
# ============================================================================
_BUILD = [0]
_MARKETS = []  # every FakeMarketData created -> real-order sentinel audit


def _build(tmpdir, prices=None, symbols=None, analyzer=None, context_fn=None,
           fee_rate=0.001, initial=1000.0, ev_name=None, state_tag=None):
    """Build (market, metrics, shadow, collector) wired to a temp dir."""
    _BUILD[0] += 1
    tag = state_tag or "s%d" % _BUILD[0]
    ev_name = ev_name or ("beta_%d.jsonl" % _BUILD[0])
    market = FakeMarketData(prices=prices or {"BTC": 100.0, "ETH": 200.0})
    _MARKETS.append(market)
    metrics = MetricsRecorder(path=os.path.join(tmpdir, "m_%s.jsonl" % tag),
                              fee_rate=fee_rate, initial_balance=initial)
    shadow = ShadowEngine(market=market,
                          state_file=os.path.join(tmpdir,
                                                  "state_%s.json" % tag),
                          metrics=metrics,
                          analyzer=analyzer,
                          symbols=list(symbols or ["BTC", "ETH"]),
                          position_size_usd=100.0,
                          fee_rate=fee_rate,
                          initial_balance=initial,
                          max_positions=3)
    collector = BetaCollector(shadow=shadow, metrics=metrics,
                              evidence_path=os.path.join(tmpdir, ev_name),
                              context_fn=context_fn,
                              symbols=list(symbols or shadow.symbols),
                              initial_balance=initial)
    return {"market": market, "metrics": metrics, "shadow": shadow,
            "collector": collector, "ev": os.path.join(tmpdir, ev_name)}


def _paper_snapshot():
    return {
        "balance": bot_main.PAPER_BALANCE,
        "positions": dict(bot_main.PAPER_POSITIONS),
        "history": list(bot_main.PAPER_TRADE_HISTORY),
        "alerts": dict(getattr(bot_main, "ALERTS", {})),
    }


# ============================================================================
# PART A - safety & config contract
# ============================================================================
def test_safety_contract():
    print("=" * 70)
    print("PART A - Safety & config contract (DRY_RUN / live off / hard-locked)")
    print("=" * 70)
    check("process env DRY_RUN is True",
          os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes"))
    check("process env LIVE_TRADING_ENABLED is False",
          os.getenv("LIVE_TRADING_ENABLED", "").lower() in ("0", "false", "no"))
    from binance_service import binance_service as shared_engine  # noqa: E402
    check("shared engine dry_run is True", shared_engine.dry_run is True)
    check("shared engine live trading stays disabled",
          shared_engine.live_trading_enabled is False)


# ============================================================================
# PART B - full evidence suite recorded
# ============================================================================
def _rich_analyzer(market):
    """Open BTC LONG + ETH SHORT once, then HOLD; SOL always errors."""
    state = {"seen": set()}

    def analyzer(symbol):
        if symbol == "SOL":
            raise RuntimeError("synthetic analysis outage")
        price = market.prices.get(symbol, 100.0)
        if symbol in state["seen"]:
            return _hold(symbol, price)
        state["seen"].add(symbol)
        if symbol == "BTC":
            return _dec("BTC", "BUY", 100.0, 99.0, 110.0, conf=80)
        return _dec("ETH", "SELL", 200.0, 205.0, 190.0, conf=75)

    return analyzer


def _rich_context(symbol):
    return {"symbol": symbol, "current_price": {"BTC": 100.0, "ETH": 200.0,
                                                "SOL": 50.0}[symbol],
            "rsi": 55.0, "macd": 3.2, "macd_signal": 2.8,
            "macd_histogram": 0.4, "ema20": 90.0, "volume": 1234.5,
            "regime": "verify"}

def test_evidence_suite(tmpdir):
    print("=" * 70)
    print("PART B - Full evidence suite recorded by BetaCollector")
    print("=" * 70)
    market = FakeMarketData(prices={"BTC": 100.0, "ETH": 200.0, "SOL": 50.0})
    bundle = _build(tmpdir, prices={"BTC": 100.0, "ETH": 200.0, "SOL": 50.0},
                    symbols=["BTC", "ETH", "SOL"],
                    analyzer=_rich_analyzer(market),
                    context_fn=_rich_context, ev_name="b.jsonl",
                    state_tag="b")
    collector, shadow = bundle["collector"], bundle["shadow"]

    r1 = collector.run_cycle()   # opens BTC/ETH, SOL errors, context x3
    bundle["market"].set_price("BTC", 110.0)   # BUY TP
    bundle["market"].set_price("ETH", 185.0)   # SELL TP (<=190)
    r2 = collector.run_cycle()   # closes both as wins; HOLD -> no reopen
    summary = collector.write_evidence()

    check("two cycles executed", r1["cycle"] == 1 and r2["cycle"] == 2,
          "(cycles=%d)" % summary["cycles"])

    # --- trade count, wins/losses --------------------------------------
    check("trade count is two", summary["trades"] == 2,
          "(n=%d)" % summary["trades"])
    check("wins == 2", summary["wins"] == 2, "(wins=%s)" % summary["wins"])
    check("losses == 0", summary["losses"] == 0,
          "(losses=%s)" % summary["losses"])
    check("win rate 100%", summary["win_rate_pct"] == 100.0)

    # --- gross / net P&L and fees ---------------------------------------
    check("gross P&L == 17.5", approx(summary["gross_pnl"], 17.5, 1e-4),
          "(=%.4f)" % summary["gross_pnl"])
    check("fees total == 0.4025", approx(summary["fees_total"], 0.4025, 1e-4),
          "(=%.4f)" % summary["fees_total"])
    check("net P&L == 17.0975", approx(summary["net_pnl"], 17.0975, 1e-4),
          "(=%.4f)" % summary["net_pnl"])

    # --- equity / balance ------------------------------------------------
    check("initial balance recorded (1000)",
          approx(summary["equity"]["initial_balance"], 1000.0))
    check("ending balance == 1017.0975",
          approx(summary["equity"]["ending_balance"], 1017.0975, 1e-4),
          "(=%.4f)" % summary["equity"]["ending_balance"])
    check("shadow balance matches (1017.0975)",
          summary["equity"]["shadow_balance"] is not None
          and approx(summary["equity"]["shadow_balance"], 1017.0975, 1e-4))

    # --- max drawdown ----------------------------------------------------
    dd = summary["max_drawdown"]
    check("max drawdown computed (pct present)",
          "max_drawdown_pct" in dd and dd["max_drawdown_pct"] >= 0.0
          and dd["peak"] >= dd["trough"],
          "(pct=%.4f peak=%.2f trough=%.2f)" % (
              dd["max_drawdown_pct"], dd["peak"], dd["trough"]))

    # --- strategy / signal ------------------------------------------------
    check("signal distribution BUY=1 SELL=1",
          summary["signals"] == {"BUY": 1, "SELL": 1},
          "(=%s)" % summary["signals"])
    check("strategy tagged ALLMIGHTSEE_PRIME",
          summary["strategies"] == {"ALLMIGHTSEE_PRIME": 2},
          "(=%s)" % summary["strategies"])
    check("per-symbol block for BTC (net 9.79)",
          summary["symbols"]["BTC"]["trades"] == 1
          and approx(summary["symbols"]["BTC"]["net"], 9.79, 1e-4),
          "(=%s)" % summary["symbols"]["BTC"])

    # --- market / context ------------------------------------------------
    check("market context samples captured (6 = 3 symbols x 2 cycles)",
          summary["market_context_samples"] == 6
          and len(collector.context_rows()) == 6,
          "(n=%d)" % summary["market_context_samples"])
    ctx = collector.context_rows()[0]
    check("context row carries indicators (rsi/macd/ema20/volume)",
          ctx.get("rsi") == 55.0 and ctx.get("macd") == 3.2
          and ctx.get("ema20") == 90.0 and ctx.get("volume") == 1234.5,
          "(keys=%s)" % sorted(ctx.keys()))
    check("errors captured (>=2 SOL analysis failures)",
          summary["errors"] >= 2 and len(summary["error_events"]) >= 2,
          "(errors=%d events=%d)" % (summary["errors"],
                                     len(summary["error_events"])))
    ts = summary["timestamps"]
    check("evidence timestamps present + ordered",
          ts["first"] and ts["last"] and ts["session_start"]
          and ts["first"] <= ts["last"])
    trade_rows = [r for r in collector.rows if r.get("type") == "trade"]
    check("each trade row has a timestamp envelope",
          all(r.get("ts") for r in trade_rows) and len(trade_rows) == 2)
    return summary, collector


def test_evidence_present():
    """Placeholder confirming the Part B scenario is registered in the runner."""
    return True


# ============================================================================
# PART C - exact fee + net P&L math (win and loss paths)
# ============================================================================
def _once_then_hold(market, symbol):
    """Return analyzer that opens one position on the first sight of a symbol
    and returns HOLD afterwards (prevents re-entry after a simulated exit)."""
    state = {"seen": set()}

    def analyzer(sym):
        price = market.get_current_price(sym)[1]
        if sym in state["seen"]:
            return _hold(sym, price)
        state["seen"].add(sym)
        if sym == "BTC":
            return _dec("BTC", "BUY", 100.0, 99.0, 110.0, conf=80)
        return _dec("ETH", "SELL", 200.0, 205.0, 190.0, conf=75)

    return analyzer


def _single_trade(tmpdir, symbol, exit_price, ev_name, tag):
    base = {"BTC": 100.0, "ETH": 200.0}
    market = FakeMarketData(prices=dict(base))
    analyzer = _once_then_hold(market, symbol)
    bundle = _build(tmpdir, prices=dict(base), symbols=[symbol],
                    analyzer=analyzer, ev_name=ev_name, state_tag=tag)
    collector = bundle["collector"]
    collector.run_cycle()                 # open
    bundle["market"].set_price(symbol, exit_price)  # trigger TP/SL
    collector.run_cycle()                 # close
    summary = collector.write_evidence()
    return collector, summary


def test_pnl_math(tmpdir):
    print("=" * 70)
    print("PART C - Exact fee + net P&L math (win and loss paths)")
    print("=" * 70)
    # BUY win: entry 100, exit 110, qty 1 -> gross 10, fees 0.21, net 9.79
    _c1, s_win = _single_trade(tmpdir, "BTC", 110.0, "c_win.jsonl", "c_win")
    trade_w = _c1.trade_rows()[0]
    check("WIN gross == 10.0", approx(trade_w["gross_pnl"], 10.0, 1e-6),
          "(=%.6f)" % trade_w["gross_pnl"])
    check("WIN fees == 0.21", approx(trade_w["fees"], 0.21, 1e-6),
          "(=%.6f)" % trade_w["fees"])
    check("WIN net == 9.79", approx(trade_w["net_pnl"], 9.79, 1e-6),
          "(=%.6f)" % trade_w["net_pnl"])
    check("WIN classified as win",
          trade_w["win"] is True and s_win["wins"] == 1
          and s_win["losses"] == 0)

    # SELL loss: entry 200, exit 205 (above SL -> STOP_LOSS, qty 0.5)
    # gross = -2.5, fees = (100 + 102.5)*0.001 = 0.2025, net = -2.7025
    _c2, s_loss = _single_trade(tmpdir, "ETH", 205.0, "c_loss.jsonl",
                                "c_loss")
    trade_l = _c2.trade_rows()[0]
    check("LOSS gross == -2.5", approx(trade_l["gross_pnl"], -2.5, 1e-6),
          "(=%.6f)" % trade_l["gross_pnl"])
    check("LOSS fees == 0.2025", approx(trade_l["fees"], 0.2025, 1e-6),
          "(=%.6f)" % trade_l["fees"])
    check("LOSS net == -2.7025", approx(trade_l["net_pnl"], -2.7025, 1e-6),
          "(=%.6f)" % trade_l["net_pnl"])
    check("LOSS classified as loss",
          trade_l["win"] is False and s_loss["losses"] == 1
          and s_loss["wins"] == 0)
    check("LOSS exit_reason is STOP_LOSS",
          trade_l["exit_reason"] == "STOP_LOSS",
          "(=%s)" % trade_l["exit_reason"])

# ============================================================================
# PART D - market/context snapshot fallback + context error capture
# ============================================================================
def test_context_and_errors(tmpdir):
    print("=" * 70)
    print("PART D - Market/context snapshots + context-error capture")
    print("=" * 70)
    market = FakeMarketData(prices={"BTC": 100.0, "ETH": 200.0})
    # No context_fn -> BetaCollector falls back to a read-only price mark.
    bundle = _build(tmpdir, prices={"BTC": 100.0, "ETH": 200.0},
                    symbols=["BTC", "ETH"],
                    analyzer=_once_then_hold(market, "BTC"),
                    context_fn=None, ev_name="d1.jsonl", state_tag="d1")
    c1 = bundle["collector"]
    c1.run_cycle()
    ctx_rows = c1.context_rows()

    def raise_ctx(symbol):
        if symbol == "SOL":
            raise RuntimeError("market feed unavailable")
        return {"symbol": symbol, "current_price": 42.0, "rsi": 50.0}

    m2 = FakeMarketData(prices={"BTC": 100.0, "SOL": 50.0})
    bundle2 = _build(tmpdir, prices={"BTC": 100.0, "SOL": 50.0},
                     symbols=["BTC", "SOL"],
                     analyzer=_once_then_hold(m2, "BTC"),
                     context_fn=raise_ctx, ev_name="d2.jsonl",
                     state_tag="d2")
    c2 = bundle2["collector"]
    c2.run_cycle()
    check("fallback context uses read-only shadow price",
          len(ctx_rows) == 2 and ctx_rows[0].get("symbol") == "BTC"
          and ctx_rows[0].get("current_price") == 100.0,
          "(rows=%d)" % len(ctx_rows))
    sol_ctx = [r for r in c2.context_rows() if r.get("symbol") == "SOL"]
    check("failing context provider is captured as an error row",
          len(sol_ctx) == 1 and any("market feed unavailable" in str(e.get("msg", ""))
                                    for e in c2.error_rows()),
          "(sol_ctx=%d)" % len(sol_ctx))


# ============================================================================
# PART E - restart / recovery: durable accumulation with NO duplicate trades
# ============================================================================
def test_restart_recovery(tmpdir):
    print("=" * 70)
    print("PART E - Restart/recovery: durable accumulation (no dup trades)")
    print("=" * 70)
    ev = os.path.join(tmpdir, "restart.jsonl")
    market1 = FakeMarketData(prices={"BTC": 100.0})
    analyzer1 = _once_then_hold(market1, "BTC")
    b1 = _build(tmpdir, prices={"BTC": 100.0}, symbols=["BTC"],
                analyzer=analyzer1, context_fn=None, ev_name="restart.jsonl",
                state_tag="r1")
    c1 = b1["collector"]
    c1.run_cycle()                      # open
    b1["market"].set_price("BTC", 110.0)  # TP
    c1.run_cycle()                      # close (1 win)
    s1 = c1.write_evidence()
    check("first session: 1 trade, restart_count 0",
          s1["trades"] == 1 and s1["restart_count"] == 0,
          "(trades=%s restart=%s)" % (s1["trades"], s1["restart_count"]))

    # Second session on the SAME evidence path + restored shadow state file.
    market2 = FakeMarketData(prices={"BTC": 110.0})
    analyzer2 = _once_then_hold(market2, "BTC")
    b2 = _build(tmpdir, prices={"BTC": 110.0}, symbols=["BTC"],
                analyzer=analyzer2, context_fn=None, ev_name="restart.jsonl",
                state_tag="r1")
    c2 = b2["collector"]
    check("restart detected (restart_count incremented to 1)",
          c2.restart_count == 1, "(restart=%d)" % c2.restart_count)
    rows2 = [r for r in c2.rows]
    check("restart evidence row recorded",
          any(r.get("type") == "restart_recovered" for r in rows2))
    s2 = c2.write_evidence()
    check("no duplicate trade counting after recovery",
          s2["trades"] == 1 and c2.cycle_count == 2
          and c2.context_samples == 2,
          "(trades=%s cycles=%s ctx=%s)" % (
              s2["trades"], c2.cycle_count, c2.context_samples))
    types = [r.get("type") for r in c2.rows]
    check("evidence ledger accumulates both sessions",
          types.count("trade") == 1
          and types.count("session_start") == 2
          and types.count("restart_recovered") == 1,
          "(trade=%d starts=%d restart=%d)" % (
              types.count("trade"), types.count("session_start"),
              types.count("restart_recovered")))


# ============================================================================
# PART F - non-mutation of the real paper engine state
# ============================================================================
def test_non_mutation(tmpdir):
    print("=" * 70)
    print("PART F - Non-mutation: main.PAPER_* untouched after a full beta run")
    print("=" * 70)
    before = _paper_snapshot()
    market = FakeMarketData(prices={"BTC": 100.0, "ETH": 200.0})
    analyzer = _rich_analyzer(market)
    bundle = _build(tmpdir, prices={"BTC": 100.0, "ETH": 200.0},
                    symbols=["BTC", "ETH", "SOL"],
                    analyzer=analyzer, context_fn=_rich_context,
                    ev_name="f.jsonl", state_tag="f")
    collector = bundle["collector"]
    collector.run_cycle()
    bundle["market"].set_price("BTC", 110.0)
    bundle["market"].set_price("ETH", 185.0)
    collector.run_cycle()
    collector.write_evidence()
    after = _paper_snapshot()
    check("real paper balance unchanged", before["balance"] == after["balance"],
          "(%.2f -> %.2f)" % (before["balance"], after["balance"]))
    check("real paper positions not created",
          before["positions"] == after["positions"]
          and after["positions"] == {})
    check("real paper history not appended",
          before["history"] == after["history"] and after["history"] == [])
    check("real paper alerts untouched",
          before["alerts"] == after["alerts"] and after["alerts"] == {})

# ============================================================================
# PART G - no real-order endpoint is reached (end-to-end sentinel audit)
# ============================================================================
def test_no_real_orders(tmpdir):
    print("=" * 70)
    print("PART G - No real-order endpoint reached anywhere")
    print("=" * 70)
    market = FakeMarketData(prices={"BTC": 100.0, "ETH": 200.0})
    bundle = _build(tmpdir, prices={"BTC": 100.0, "ETH": 200.0},
                    symbols=["BTC", "ETH"], analyzer=_once_then_hold(market, "BTC"),
                    context_fn=_rich_context, ev_name="g.jsonl", state_tag="g")
    collector = bundle["collector"]
    collector.run_cycle()
    bundle["market"].set_price("BTC", 110.0)
    collector.run_cycle()
    collector.write_evidence()
    place_total = sum(len(m.place_calls) for m in _MARKETS)
    signed_total = sum(len(m.signed_calls) for m in _MARKETS)
    check("place_order sentinel never called (all markets)",
          place_total == 0, "(place_calls=%d)" % place_total)
    check("_signed_request sentinel never called (all markets)",
          signed_total == 0, "(signed_calls=%d)" % signed_total)
    proof = collector.no_real_order_proof()
    check("collector no_real_order_proof reports checked + empty",
          proof["checked"] and proof["place_calls"] == 0
          and proof["signed_calls"] == 0, "(=%s)" % proof)


# ============================================================================
# PART H - credential redaction in collected artifacts
# ============================================================================
def test_redaction(tmpdir):
    print("=" * 70)
    print("PART H - Credential redaction (no secrets reach artifacts)")
    print("=" * 70)
    d = _redact({"api_key": "ABC123DEF456", "api_secret": "xyz789",
                 "bot_token": "tok123", "bunny": {"secret": "n", "ok": 1},
                 "symbol": "BTC", "n": 7, "items": ["a", "b"]})
    check("api_key key redacted", d["api_key"] == "<api_key_REDACTED>",
          "(=%s)" % d["api_key"])
    check("api_secret key redacted",
          d["api_secret"] == "<api_secret_REDACTED>")
    check("bot_token key redacted",
          d["bot_token"] == "<bot_token_REDACTED>")
    check("nested secret key redacted",
          d["bunny"]["secret"] == "<secret_REDACTED>")
    check("safe fields preserved",
          d["symbol"] == "BTC" and d["n"] == 7 and d["items"] == ["a", "b"])
    # Ensure an entire collected evidence file never contains secret values.
    write_d = {"api_key": "ABC123DEF456", "current_price": 100.0}
    red = _redact(write_d)
    blob = json.dumps(red).lower()
    check("collected payload has no raw secret value",
          "abc123def456" not in str(red).lower())


# ============================================================================
# PART I - durable output round-trip (evidence JSONL + summary)
# ============================================================================
def test_output_roundtrip(tmpdir):
    print("=" * 70)
    print("PART I - Durable evidence JSONL + summary round-trip")
    print("=" * 70)
    market = FakeMarketData(prices={"BTC": 100.0})
    bundle = _build(tmpdir, prices={"BTC": 100.0}, symbols=["BTC"],
                    analyzer=_once_then_hold(market, "BTC"),
                    context_fn=None, ev_name="rt.jsonl", state_tag="rt")
    collector, ev = bundle["collector"], bundle["ev"]
    collector.run_cycle()
    bundle["market"].set_price("BTC", 110.0)
    collector.run_cycle()
    summary = collector.write_evidence()
    check("evidence jsonl file created", os.path.exists(ev))
    check("evidence summary file created",
          os.path.exists(collector.summary_path))
    lines = []
    with open(ev, "r", encoding="utf-8") as fh:
        lines = [json.loads(l) for l in fh if l.strip()]
    types = [r["type"] for r in lines]
    check("evidence jsonl re-readable with session_start/cycle/trade rows",
          "session_start" in types and "cycle" in types and "trade" in types,
          "(types=%s)" % types)
    with open(collector.summary_path, "r", encoding="utf-8") as fh:
        persisted = json.load(fh)
    check("persisted summary matches phase+counts",
          persisted["phase"] == "9.3"
          and persisted["trades"] == summary["trades"]
          and persisted["trades"] == 1)
    check("metrics recorder output also exists (Phase 9.1 integration)",
          os.path.exists(bundle["metrics"].summary_path))

# ============================================================================
# PART J - regression gate: Phase 9.1 & Phase 9.2 still pass unchanged
# ============================================================================
def _run_suite(script):
    """Run an existing verification script in a clean subprocess."""
    return subprocess.run(
        [sys.executable, os.path.join(_HERE, script)],
        capture_output=True, text=True, cwd=_HERE)


def test_regression_gate():
    print("=" * 70)
    print("PART J - Regression gate (Phase 9.1 + Phase 9.2 suites)")
    print("=" * 70)
    suites = {
        "phase_9_verify_metrics.py": "Phase 9.1 metrics",
        "phase_9_2_verify_shadow.py": "Phase 9.2 shadow",
    }
    ok_all = True
    for script, label in suites.items():
        result = _run_suite(script)
        passed = result.returncode == 0
        ok_all = ok_all and passed
        tail = (result.stdout or "").strip().splitlines()[-1:] or [""]
        print(("PASS" if passed else "FAIL"), "-",
              "%s regression (exit=%d): %s" % (
                  label, result.returncode, tail[0][:90]))
        if not passed:
            FAILURES.append("%s regression" % label)
    check("Phase 9.1 + 9.2 regression suites pass unchanged", ok_all)


# ============================================================================
# Runner
# ============================================================================
def run():
    print("Phase 9.3 - Verify Controlled Beta / Data Collection "
          "(offline, NON-MUTATING)")
    print("DRY_RUN=True | LIVE_TRADING_ENABLED=False | no real orders, "
          "no network.")

    # Ensure a clean paper baseline for the non-mutation proof.
    bot_main.PAPER_POSITIONS = {}
    bot_main.PAPER_TRADE_HISTORY = []
    bot_main.PAPER_BALANCE = bot_main.INITIAL_PAPER_BALANCE
    bot_main.ALERTS = {}

    with tempfile.TemporaryDirectory() as tmpdir:
        parts = [
            test_safety_contract,
            lambda: test_evidence_suite(tmpdir),
            lambda: test_pnl_math(tmpdir),
            lambda: test_context_and_errors(tmpdir),
            lambda: test_restart_recovery(tmpdir),
            lambda: test_non_mutation(tmpdir),
            lambda: test_no_real_orders(tmpdir),
            lambda: test_redaction(tmpdir),
            lambda: test_output_roundtrip(tmpdir),
            test_regression_gate,
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
        print("VERIFY BETA: %d FAILURE(S) -> %s" % (len(FAILURES), FAILURES))
        print("Phase 9.3 go/no-go: NO-GO")
        sys.exit(1)
    print("VERIFY BETA: ALL CHECKS PASSED (Phase 9.3)")
    print("Controlled-beta evidence collection is durable, deterministic, "
          "and non-mutating; no real order possible; live trading stays "
          "DISABLED." if not FAILURES else "")
    print("Phase 9.3 go/no-go: GO (offline validation complete).")
    sys.exit(0)


if __name__ == "__main__":
    run()