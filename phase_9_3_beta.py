#!/usr/bin/env python3
"""Phase 9.3 - Controlled Beta / Data Collection (Project Morningstar).

Durable evidence-collection mechanism for the controlled paper/shadow beta.

It REUSES (never re-implements or replaces) the validated layers:

  * Phase 9.1 ``MetricsRecorder`` (``phase_9_metrics.py``) for trade / cycle /
    event aggregation and summary output.
  * Phase 9.2 shadow-mode contract (``phase_9_2_verify_shadow.py``) - a
    NON-MUTATING ``ShadowEngine`` that observes the read-only decision
    pipeline, records intended order payloads, and applies simulated
    "shadow" fills only. No real order is ever routed.

The Phase 9.3 contribution is a single, self-contained evidence ledger that
dry-collects the full evidence suite needed for the Phase 9.4-9.7 analysis:

    * trade count
    * wins / losses
    * gross P&L and net P&L
    * fees
    * equity / balance
    * maximum drawdown
    * strategy / signal information
    * market / context information (per-symbol indicator snapshot)
    * errors
    * timestamps (session boundaries + every row)
    * restart / recovery information (prior-session detection)

Each evidence row is written to a durable JSONL file, and a consolidated
summary (JSON) is produced through the same atomic rename pattern used by the
Phase 9.1 recorder.  Everything the collector writes pass through a
credential-redaction routine so no API key / secret / token can reach an
artifact.

SAFETY:
    * No networking and no order routing - the shadow engine only simulates.
    * DRY_RUN / LIVE_TRADING_ENABLED are never changed by this module.
    * main.py's real paper book (PAPER_BALANCE / PAPER_POSITIONS /
      PAPER_TRADE_HISTORY) is never read-through nor written.
    * No live-trading path is enabled.

Usage:
    collector = BetaCollector(shadow=engine, metrics=recorder)
    collector.run_cycle()
    summary = collector.write_evidence()

    # CLI: runs one controlled collection cycle with no network access.
    python3 phase_9_3_beta.py
"""

import json
import os
import sys
import time
import threading
import datetime as _dt

from phase_7_autopilot import _env_float, _env_str

# Credentials that must never appear in any collected artifact.  We read the
# values from the environment purely so we can redact them if they somehow
# bubble up into payload text.  They are never logged or written.
_SECRET_ENV_NAMES = (
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BINANCE_TESTNET_API_KEY",
    "BINANCE_TESTNET_API_SECRET",
    "BOT_TOKEN",
)


def _now_iso():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _redact(value):
    """Recursively strip credential-shaped values from any collected data.

    Keys that look like secrets are replaced wholesale; string values are
    scanned for any env-provided secret value and masked.
    """
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            low = str(key).lower()
            if any(part in low for part in ("api_key", "api_secret",
                                            "secret", "bot_token",
                                            "password", "token")):
                out[key] = "<%s_REDACTED>" % key
            else:
                out[key] = _redact(val)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        s = value
        for name in _SECRET_ENV_NAMES:
            val = os.environ.get(name, "") or ""
            if val and len(val) > 6:
                s = s.replace(val, "<%s_REDACTED>" % name)
        return s
    return value

class BetaCollector:
    """Controlled-Beta evidence collector.

    Wraps a Phase 9.2 shadow engine and a Phase 9.1 MetricsRecorder and
    produces a durable, self-contained evidence ledger plus a consolidated
    summary ready for Phase 9.4-9.7 analysis.
    """

    SCHEMA = 1
    MODE = "CONTROLLED_BETA"

    def __init__(self, shadow, metrics, evidence_path=None, context_fn=None,
                 symbols=None, initial_balance=None, collection_id=None):
        self.shadow = shadow
        self.metrics = metrics
        if evidence_path is None:
            evidence_path = _env_str("PHASE9_3_EVIDENCE_PATH",
                                     "beta_evidence.jsonl")
        self.evidence_path = evidence_path
        base = evidence_path.rsplit(".", 1)[0] if "." in evidence_path \
            else evidence_path
        self.summary_path = base + "_summary.json"

        self.context_fn = context_fn
        self.symbols = list(symbols or getattr(shadow, "symbols", None)
                            or ["BTC"])
        self.initial_balance = float(
            initial_balance
            if initial_balance is not None
            else getattr(shadow, "initial_balance", 1000.0))
        self.collection_id = collection_id or (
            "%s-%d-%d" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
                          os.getpid(), int(time.time() * 1000) % 1000000))

        self._lock = threading.Lock()
        self._session_epoch = time.time()
        self.session_start = _now_iso()
        self.restart_count = 0
        self.rows = self._load_prior_rows()   # preserve durable history
        self._trade_seen = set()              # dedupe closed trades across restarts
        self._err_seen = 0                    # high-water mark of shadow errors
        self._seed_from_prior()               # cumulative counters/dedupe
        self._detect_restart()
        self._ev("session_start", {
            "mode": self.MODE,
            "collection_id": self.collection_id,
            "symbols": self.symbols,
            "initial_balance": self.initial_balance,
            "recovered_prior_session": self.restart_count > 0,
        })

    # ------------------------------------------------------------------
    # Restart / recovery detection (prior durable summary)
    # ------------------------------------------------------------------
    def _detect_restart(self):
        prev = self._load_summary(self.summary_path)
        if not prev:
            return
        try:
            self.restart_count = int(prev.get("restart_count", 0)) + 1
        except (TypeError, ValueError):
            self.restart_count = 1
        self._ev("restart_recovered", {
            "restart_count": self.restart_count,
            "prior_trades": prev.get("trades", 0) or 0,
            "prior_cycles": prev.get("cycles", 0) or 0,
            "recovered_from": self.summary_path,
        })

    @staticmethod
    def _load_summary(path):
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:  # noqa: BLE001
            return None

    def _load_prior_rows(self):
        """Reload the existing evidence ledger so restarts ACCUMULATE.

        A new collector on the same evidence path continues the durable
        history instead of overwriting it (recovery/continuity for 9.4-9.7).
        """
        if not os.path.exists(self.evidence_path):
            return []
        prior = []
        try:
            with open(self.evidence_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    prior.append(json.loads(line))
        except Exception:  # noqa: BLE001
            return []
        return prior

    def _seed_from_prior(self):
        """Seed cumulative counters and trade-dedupe keys from prior rows."""
        self.cycle_count = sum(1 for r in self.rows
                               if r.get("type") == "cycle")
        self.context_samples = sum(1 for r in self.rows
                                   if r.get("type") == "context")
        self.errors_count = sum(1 for r in self.rows
                                if r.get("type") == "error")
        for row in self.rows:
            if row.get("type") != "trade":
                continue
            data = row.get("data") or {}
            key = (str(data.get("symbol", "")),
                   str(data.get("side", "")),
                   self._round(data.get("entry")),
                   self._round(data.get("exit")),
                   str(data.get("closed_at", "") or ""))
            self._trade_seen.add(key)

    # ------------------------------------------------------------------
    # Evidence row primitives
    # ------------------------------------------------------------------
    def _ev(self, kind, data):
        """Append one durable evidence row under the session envelope."""
        row = {
            "phase": self.SCHEMA,
            "type": kind,
            "ts": _now_iso(),
            "restart_count": self.restart_count,
            "data": _redact(data),
        }
        with self._lock:
            self.rows.append(row)
        return row

    @property
    def uptime_seconds(self):
        return max(0.0, time.time() - self._session_epoch)

    @staticmethod
    def _round(value):
        if value is None:
            return None
        try:
            return round(float(value), 8)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Market / context snapshots
    # ------------------------------------------------------------------
    def _default_context(self, symbol):
        """Fallback context snapshot: a read-only price mark from the shadow
        market source (no credentials, no network)."""
        price = None
        market = getattr(self.shadow, "market", None)
        if market is not None and hasattr(market, "get_current_price"):
            try:
                _ok, price, _err = market.get_current_price(symbol)
            except Exception:  # noqa: BLE001
                price = None
        return {"symbol": symbol, "current_price": price,
                "source": "shadow_market_read_only"}

    def record_context(self, symbols=None, context_fn=None):
        """Capture one per-symbol market/context snapshot.

        ``context_fn`` mirrors the rich output of main.analyze_symbol (rsi,
        macd, ema20, volume, etc.) so later analysis phases can correlate each
        trade with the market state that produced it.  Falls back to a
        read-only price snapshot if no provider is wired.
        """
        provider = context_fn or self.context_fn
        captured = []
        for symbol in (symbols or self.symbols):
            try:
                if provider is not None:
                    ctx = provider(symbol)
                else:
                    ctx = self._default_context(symbol)
                data = ctx if isinstance(ctx, dict) else \
                    {"symbol": symbol, "value": ctx}
                data.setdefault("symbol", symbol)
            except Exception as e:  # noqa: BLE001
                data = {"symbol": symbol, "error": str(e)}
                self.errors_count += 1
                self._ev("error", {"src": "context_snapshot",
                                   "msg": str(e)})
            self._ev("context", data)
            self.context_samples += 1
            captured.append(data)
        return captured

# ------------------------------------------------------------------
    # Cycle orchestration
    # ------------------------------------------------------------------
    def run_cycle(self, status="RUNNING", offline=False,
                  daily_loss_halt=False):
        """Run one controlled collection cycle.

        Order mirrors the Phase 8/9 supervised loop: drive shadow exits first
        (simulated TP/SL fills -> MetricsRecorder trades), then a shadow scan
        (read-only pipeline -> intended payloads -> shadow opens), then sample
        equity/exposure and persist an evidence cycle row.
        """
        # 1. market/context snapshot for each watched symbol
        self.record_context()

        # 2. shadow-driven exits then scan (Phase 9.2 validated engine)
        exit_closed = self.shadow.run_exit_cycle()
        scan = self.shadow.run_scan_cycle()

        # 3. portfolio sample for the cycle
        equity = self._round(self.shadow.mark_equity())
        exposure = self._round(self.shadow.exposure_used())
        open_pos = int(len(getattr(self.shadow, "positions", {}) or {}))
        with self._lock:
            self.cycle_count += 1

        self._ev("cycle", {
            "cycle": self.cycle_count,
            "status": status,
            "offline": bool(offline),
            "daily_loss_halt": bool(daily_loss_halt),
            "equity": equity,
            "exposure_used_usd": exposure,
            "open_positions": open_pos,
            "scan": {"scanned": len(scan.get("scanned", [])),
                     "opened": len(scan.get("opened", [])),
                     "rejected": scan.get("rejected", {})},
            "exits_closed": list(exit_closed or []),
        })
        if self.metrics is not None:
            try:
                self.metrics.record_cycle({
                    "equity": equity,
                    "exposure_used_usd": exposure,
                    "open_positions": open_pos,
                    "status": status,
                    "offline": offline,
                    "daily_loss_halt": daily_loss_halt,
                })
            except Exception as e:  # noqa: BLE001
                self._ev("error", {"src": "metrics_pipeline", "msg": str(e)})

        # 4. capture newly closed shadow trades into the evidence log
        self.capture_new_trades()

        # 5. drain any new shadow errors
        self.capture_new_errors()

        return {"cycle": self.cycle_count, "scanned": scan.get("scanned", []),
                "opened": scan.get("opened", []),
                "rejected": scan.get("rejected", {}),
                "exits_closed": list(exit_closed or []),
                "equity": equity, "exposure_used_usd": exposure}

# ------------------------------------------------------------------
    # Trade / error capture from the shadow engine
    # ------------------------------------------------------------------
    def capture_new_trades(self):
        """Copy newly closed shadow trades into the evidence ledger once."""
        history = list(getattr(self.shadow, "history", []) or [])
        recorded = 0
        for trade in history:
            key = (str(trade.get("symbol", "")),
                   str(trade.get("side", "")),
                   self._round(trade.get("entry")),
                   self._round(trade.get("exit")),
                   str(trade.get("closed_at", "") or ""))
            if key in self._trade_seen:
                continue
            self._trade_seen.add(key)
            gross = self._round(trade.get("pnl"))
            net = self._round(trade.get("net_pnl"))
            fees = self._round(trade.get("fees"))
            # The shadow engine persists gross + net but not always a separate
            # fees field; derive it so fees are always evidenced (net=gross-fees).
            if fees is None and gross is not None and net is not None:
                fees = round(gross - net, 8)
            self._ev("trade", {
                "symbol": trade.get("symbol", ""),
                "side": trade.get("side", ""),
                "strategy": trade.get("strategy", "ALLMIGHTSEE_PRIME"),
                "signal": trade.get("signal", trade.get("side", "")),
                "entry": self._round(trade.get("entry")),
                "exit": self._round(trade.get("exit")),
                "quantity": self._round(trade.get("quantity")),
                "gross_pnl": gross,
                "fees": fees,
                "net_pnl": net,
                "exit_reason": trade.get("status", ""),
                "entry_reason": trade.get("reason", ""),
                "closed_at": trade.get("closed_at", ""),
                "opened_at": trade.get("opened_at", ""),
                "win": (net or 0.0) > 0.0,
            })
            recorded += 1
        return recorded

    def capture_new_errors(self):
        """Persist shadow errors since the last high-water mark."""
        errs = list(getattr(self.shadow, "recent_errors", []) or [])
        added = 0
        for msg in errs[self._err_seen:]:
            self.errors_count += 1
            self._ev("error", {"src": "shadow_engine", "msg": str(msg)})
            added += 1
        self._err_seen = max(self._err_seen, len(errs))
        return added

# ------------------------------------------------------------------
    # Evidence aggregation (self-contained for Phase 9.4-9.7 analysis)
    # ------------------------------------------------------------------
    def _rows_of(self, kind):
        return [r.get("data") or {} for r in self.rows
                if r.get("type") == kind]

    def trade_rows(self):
        return self._rows_of("trade")

    def cycle_rows(self):
        return self._rows_of("cycle")

    def context_rows(self):
        return self._rows_of("context")

    def error_rows(self):
        return self._rows_of("error")

    def drawdown(self):
        """Max drawdown from cycle equity samples + initial balance."""
        series = [self.initial_balance]
        series += [
            float(r["equity"]) for r in self.cycle_rows()
            if r.get("equity") is not None
        ]
        peak = trough = series[0]
        max_dd = 0.0
        for value in series:
            peak = max(peak, value)
            trough = min(trough, value) if value < trough else trough
            if peak > 0:
                max_dd = max(max_dd, (peak - value) / peak)
        return {"max_drawdown_pct": round(max_dd * 100.0, 4),
                "peak": round(peak, 4), "trough": round(trough, 4),
                "samples": len(series)}

    def build_evidence_summary(self):
        trades = self.trade_rows()
        gross = sum(float(t.get("gross_pnl") or 0.0) for t in trades)
        fees = sum(float(t.get("fees") or 0.0) for t in trades)
        net = sum(float(t.get("net_pnl") or 0.0) for t in trades)
        wins = sum(1 for t in trades if (t.get("net_pnl") or 0.0) > 0.0)
        losses = len(trades) - wins
        signals = {}
        strategies = {}
        by_symbol = {}
        for trade in trades:
            signal = str(trade.get("signal") or trade.get("side") or "?")
            strategy = str(trade.get("strategy") or "?")
            symbol = str(trade.get("symbol") or "?")
            signals[signal] = signals.get(signal, 0) + 1
            strategies[strategy] = strategies.get(strategy, 0) + 1
            block = by_symbol.setdefault(symbol, {"trades": 0, "wins": 0,
                                                  "losses": 0, "net": 0.0})
            block["trades"] += 1
            block["wins"] += 1 if (trade.get("net_pnl") or 0.0) > 0.0 else 0
            block["losses"] += 1 if (trade.get("net_pnl") or 0.0) <= 0.0 else 0
            block["net"] = round(block["net"] + (trade.get("net_pnl") or 0.0),
                                 8)
        for block in by_symbol.values():
            block["net"] = round(block["net"], 4)
        errors = self.error_rows()
        cycles = self.cycle_rows()
        ts_all = [r.get("ts") for r in self.rows if r.get("ts")]
        return {
            "phase": "9.3",
            "schema": self.SCHEMA,
            "mode": self.MODE,
            "collection_id": self.collection_id,
            "started_at": self.session_start,
            "ended_at": _now_iso(),
            "restart_count": self.restart_count,
            "cycles": self.cycle_count,
            "cycle_samples": len(cycles),
            "trades": len(trades),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(wins / len(trades) * 100.0, 2)
            if trades else 0.0,
            "gross_pnl": round(gross, 4),
            "net_pnl": round(net, 4),
            "fees_total": round(fees, 4),
            "equity": {
                "initial_balance": round(self.initial_balance, 4),
                "ending_balance": round(self.initial_balance + net, 4),
                "shadow_balance": self._round(
                    getattr(self.shadow, "balance", None)),
                "last_sample": cycles[-1].get("equity") if cycles else None,
            },
            "max_drawdown": self.drawdown(),
            "exposure_max_usd": round(max(
                [float(r.get("exposure_used_usd") or 0.0)
                 for r in cycles] + [0.0]), 4),
            "signals": signals,
            "strategies": strategies,
            "symbols": by_symbol,
            "market_context_samples": self.context_samples,
            "errors": self.errors_count,
            "error_events": errors,
            "timestamps": {
                "first": ts_all[0] if ts_all else None,
                "last": ts_all[-1] if ts_all else None,
                "session_start": self.session_start,
            },
            "evidence_log": self.evidence_path,
            "metrics_summary": getattr(self.metrics, "summary_path", None),
        }

# ------------------------------------------------------------------
    # Durable output (atomic rename, phase-9.1 pattern)
    # ------------------------------------------------------------------
    def write_evidence(self):
        """Atomically write evidence JSONL + consolidated summary JSON.

        Returns the consolidated evidence summary used by Phase 9.4-9.7.
        """
        summary = self.build_evidence_summary()

        tmp = self.evidence_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                for row in self.rows:
                    fh.write(json.dumps(row) + "\n")
            os.replace(tmp, self.evidence_path)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("could not write evidence jsonl") from e

        tmp = self.summary_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.summary_path)

        # Also flush the Phase 9.1 MetricsRecorder so both durable artifacts
        # (evidence ledger + metrics ledger/summary) land on the same write.
        if self.metrics is not None:
            self.metrics.write_summary()
        return summary

    # ------------------------------------------------------------------
    # Safety introspection
    # ------------------------------------------------------------------
    def no_real_order_proof(self):
        """Report whether any real-order route was touched (must be empty)."""
        market = getattr(self.shadow, "market", None)
        if market is None:
            return {"checked": False, "place_calls": 0, "signed_calls": 0}
        place = list(getattr(market, "place_calls", None) or [])
        signed = list(getattr(market, "signed_calls", None) or [])
        return {"checked": True, "place_calls": len(place),
                "signed_calls": len(signed)}


def main():
    """Minimal CLI: one controlled collection cycle (no network, no orders).

    Builds an isolated shadow engine using the Phase 9.2 reference market
    (read-only pricing) and writes the durable evidence artifacts.  DRY_RUN
    and LIVE_TRADING_ENABLED are never changed.
    """
    print("Phase 9.3 - Controlled Beta / Data Collection (CLI)")
    print("No orders, no network; DRY_RUN / live trading untouched.")

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from phase_9_metrics import MetricsRecorder  # noqa: E402
    from phase_9_2_verify_shadow import (FakeMarketData,  # noqa: E402
                                         ShadowEngine)

    market = FakeMarketData(prices={"BTC": 60000.0, "ETH": 2000.0})
    metrics = MetricsRecorder(path=_env_str("PHASE9_3_METRICS_PATH",
                                            "beta_metrics.jsonl"))
    shadow = ShadowEngine(market=market,
                          state_file=_env_str("PHASE9_3_STATE_FILE",
                                              "beta_shadow_state.json"),
                          metrics=metrics,
                          symbols=["BTC", "ETH"])

    def _context(symbol):
        ok, price, _err = market.get_current_price(symbol)
        return {"symbol": symbol, "current_price": price,
                "indicator": "cli_sample"}

    collector = BetaCollector(shadow=shadow, metrics=metrics,
                              context_fn=_context)
    collector.run_cycle()
    summary = collector.write_evidence()
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())