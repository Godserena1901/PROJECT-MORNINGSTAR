#!/usr/bin/env python3
"""Phase 9.1 - Beta Metrics Recorder (Project Morningstar).

READ-ONLY performance/measurement layer for the paper environment.  It places
no orders, does not touch the trading engine, and never enables live trading.
It consumes data the existing Phase 7/8 paper infrastructure already produces
(closed-trade history, balances, cycle reports) and turns it into a metrics
timeline:

  * starting / ending / max balance
  * number of trades, wins, losses, win rate, profit factor
  * realized P&L (gross) and NET after modeled trading fees
  * fees (configurable rate, default 0.001)
  * equity curve sampled per cycle + max drawdown
  * exposure (average / max), open-position count
  * strategy / signal (side), entry/exit prices, exit reason
  * failed orders / errors (durable event log)
  * uptime / restarts, daily loss series, emergency-shutdown events

All inputs are passed in by the caller (no network, no credentials).  The
existing `paper_state.json` is never modified - only read by the caller.

SAFETY: credentials are never logged; nothing here calls Binance; DRY_RUN and
LIVE_TRADING_ENABLED are never changed.

Usage:
    rec = phase_9_metrics.MetricsRecorder(fee_rate=0.001)
    rec.start_session()
    rec.record_trade(trade)     # for each closed trade
    rec.record_cycle(report)    # for each autopilot cycle sample
    rec.append_event("error", "detail")
    summary = rec.write_summary()
"""

import json
import os
import time
import datetime as _dt
import threading

from phase_7_autopilot import _env_float, _env_str


def _now_iso():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MetricsRecorder:
    """Collects paper-trading metrics into JSONL rows plus a summary JSON."""

    SCHEMA = 1

    def __init__(self, path=None, fee_rate=None, price_fn=None,
                 initial_balance=1000.0, daily_loss_limit_usd=0.0):
        self.path = path or _env_str("PHASE9_METRICS_PATH", "metrics.jsonl")
        base = self.path.rsplit(".", 1)[0] if "." in self.path else self.path
        self.summary_path = base + "_summary.json"
        self.fee_rate = abs(_env_float(
            "PHASE9_FEE_RATE", fee_rate if fee_rate is not None else 0.001))
        # price_fn(symbol)->price, used for unrealized equity (optional).
        self.price_fn = price_fn
        self.initial_balance = float(initial_balance or 0.0)
        self.daily_loss_limit_usd = float(daily_loss_limit_usd or 0.0)

        self._lock = threading.Lock()
        self._session_epoch = time.time()
        self.session_start = _now_iso()
        self.restart_count = 0
        self.rows = []
        self.trade_count = 0
        self.cycle_count = 0
        self.errors_count = 0
        self.emergency_count = 0
        self.offline_count = 0
        self.daily_loss_halt = False

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------
    def start_session(self):
        """Detect a restart from a prior summary and record session_start."""
        prev = self._load_summary(self.summary_path)
        if prev:
            try:
                self.restart_count = int(prev.get("restart_count", 0)) + 1
            except (TypeError, ValueError):
                self.restart_count = 1
        self.append_event("session_start",
                          {"restart_count": self.restart_count})
        return self

    @staticmethod
    def _load_summary(path):
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None

    def append_event(self, kind, payload):
        """Record a durable, timestamped event row (error / halt / note)."""
        row = {"type": "event", "ts": _now_iso(), "kind": kind,
               "data": payload, "restart_count": self.restart_count}
        with self._lock:
            self.rows.append(row)
        return row

    # ------------------------------------------------------------------
    # Uptime
    # ------------------------------------------------------------------
    @property
    def uptime_seconds(self):
        return max(0.0, time.time() - self._session_epoch)

# ------------------------------------------------------------------
    # Trade recording
    # ------------------------------------------------------------------
    @staticmethod
    def _f(trade, key, default=0.0):
        try:
            v = trade.get(key, default)
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    def record_trade(self, trade):
        """Append one closed-trade row with gross/net/fee math."""
        entry = self._f(trade, "entry")
        exit_p = self._f(trade, "exit")
        qty = self._f(trade, "quantity")
        gross = self._f(trade, "pnl")
        status = trade.get("status", "")
        side = trade.get("side", "")

        entry_notional = qty * entry
        exit_notional = qty * exit_p
        fees = (entry_notional + exit_notional) * self.fee_rate
        net = gross - fees
        # win is defined on a net basis
        close_reason = status if status in ("TAKE_PROFIT", "STOP_LOSS") \
            else str(trade.get("close_reason", status))

        row = {
            "type": "trade",
            "ts": _now_iso(),
            "symbol": trade.get("symbol", ""),
            "side": side,
            "strategy": trade.get("strategy", "ALLMIGHTSEE_PRIME"),
            "signal": trade.get("signal", side),
            "entry": entry,
            "exit": exit_p,
            "quantity": qty,
            "entry_notional": round(entry_notional, 8),
            "exit_notional": round(exit_notional, 8),
            "gross_pnl": round(gross, 8),
            "fees": round(fees, 8),
            "net_pnl": round(net, 8),
            "exit_reason": close_reason,
            "entry_reason": trade.get("reason", ""),
            "confidence": trade.get("confidence"),
            "opened_at": trade.get("opened_at", ""),
            "closed_at": trade.get("closed_at", ""),
        }
        with self._lock:
            self.rows.append(row)
            self.trade_count += 1
        return row

# ------------------------------------------------------------------
    # Cycle (equity / exposure) recording
    # ------------------------------------------------------------------
    def record_cycle(self, cycle):
        """Append one cycle sample row and tally durable counters."""
        report = cycle or {}
        exposure = float(report.get("exposure_used_usd", 0.0) or 0.0)
        equity = float(report.get("equity", exposure) or exposure)
        open_pos = int(report.get("open_positions", 0) or 0)
        status = report.get("status", "")
        offline = bool(report.get("offline", False))
        daily_loss_halt = bool(report.get("daily_loss_halt", False))
        errors = report.get("errors") or []
        halted_reason = report.get("halted_reason") or ""

        if offline:
            self.offline_count += 1
        if status == "EMERGENCY_HALT" or (halted_reason and "emergency" in
                                          str(halted_reason).lower()):
            self.emergency_count += 1
            self.append_event("emergency_halt",
                              {"reason": halted_reason or status})
        if daily_loss_halt:
            self.daily_loss_halt = True
        if errors:
            for err in errors:
                self.errors_count += 1
                self.append_event("error", {"msg": err, "status": status})

        with self._lock:
            self.cycle_count += 1
        row = {
            "type": "cycle",
            "ts": _now_iso(),
            "status": status,
            "cycle": self.cycle_count,
            "equity": round(equity, 8),
            "exposure": round(exposure, 8),
            "open_positions": open_pos,
            "offline": offline,
            "daily_loss_halt": daily_loss_halt,
            "restart_count": self.restart_count,
        }
        with self._lock:
            self.rows.append(row)
        return row

    # ------------------------------------------------------------------
    # Equity curve / drawdown
    # ------------------------------------------------------------------
    def equity_curve(self):
        return [r for r in self.rows if r.get("type") == "cycle"]

    def drawdown(self):
        """Peak-to-trough max drawdown over the sampled equity curve."""
        curve = [r.get("equity", 0.0) for r in self.equity_curve()]
        if not curve:
            return {"max_drawdown_pct": 0.0, "peak": None,
                    "trough": None, "current": None}
        peak = curve[0]
        max_dd = 0.0
        trough = curve[0]
        for eq in curve:
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (peak - eq) / peak
                if dd > max_dd:
                    max_dd = dd
                    trough = eq
        return {"max_drawdown_pct": round(max_dd * 100.0, 4),
                "peak": round(peak, 4), "trough": round(trough, 4),
                "current": round(curve[-1], 4)}

# ------------------------------------------------------------------
    # Aggregations
    # ------------------------------------------------------------------
    def _trades(self):
        return [r for r in self.rows if r.get("type") == "trade"]

    def win_loss(self):
        trades = self._trades()
        wins = losses = 0
        gross = fees = net = 0.0
        buy = sell = buy_w = sell_w = 0
        win_pnls, loss_pnls = [], []
        for t in trades:
            n = t.get("net_pnl", 0.0)
            g = t.get("gross_pnl", 0.0)
            fee = t.get("fees", 0.0)
            gross += g
            fees += fee
            net += n
            if t.get("side") == "BUY":
                buy += 1
            else:
                sell += 1
            if n > 0:
                wins += 1
                win_pnls.append(n)
                if t.get("side") == "BUY":
                    buy_w += 1
            elif n < 0:
                losses += 1
                loss_pnls.append(n)
        total = wins + losses
        pf = (sum(win_pnls) / abs(sum(loss_pnls))) if sum(loss_pnls) else (
            sum(win_pnls) if win_pnls else 0.0)
        return {
            "trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round((wins / total * 100) if total else 0.0, 4),
            "avg_win": round(sum(win_pnls) / len(win_pnls), 8) if win_pnls else 0.0,
            "avg_loss": round(sum(loss_pnls) / len(loss_pnls), 8) if loss_pnls else 0.0,
            "gross_pnl": round(gross, 8),
            "fees": round(fees, 8),
            "net_pnl": round(net, 8),
            "profit_factor": round(pf, 4),
            "buy_trades": buy,
            "sell_trades": sell,
            "buy_wins": buy_w,
            "sell_wins": sell_w,
        }

    def balances(self):
        start = self.initial_balance
        net = sum(t.get("net_pnl", 0.0) for t in self._trades())
        end = start + net
        max_equity = max([start, end] + [r.get("equity", 0.0)
                                         for r in self.equity_curve()] or [start])
        return {"starting": round(start, 4), "ending": round(end, 4),
                "max": round(max_equity, 4)}

    def exposure_stats(self):
        curves = self.equity_curve()
        exps = [r.get("exposure", 0.0) for r in curves]
        return {"avg": round(sum(exps) / len(exps), 4) if exps else 0.0,
                "max": round(max(exps), 4) if exps else 0.0,
                "sample_count": len(exps)}

    def daily_pnl_series(self):
        by_day = {}
        for t in self._trades():
            day = (t.get("closed_at") or t.get("ts") or "")[:10]
            by_day[day] = round(by_day.get(day, 0.0) + t.get("net_pnl", 0.0), 8)
        return {d: by_day[d] for d in sorted(by_day)}

    # ------------------------------------------------------------------
    # Events / counters
    # ------------------------------------------------------------------
    def error_events(self):
        return [r for r in self.rows
                if r.get("type") == "event" and r.get("kind") == "error"]

    def emergency_events(self):
        return [r for r in self.rows
                if r.get("type") == "event"
                and r.get("kind") == "emergency_halt"]

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    def write_summary(self):
        """Write metrics.jsonl (append each row) + metrics_summary.json."""
        summary = {
            "schema": self.SCHEMA,
            "session_started_at": self.session_start,
            "restart_count": self.restart_count,
            "uptime_s": round(self.uptime_seconds, 2),
            "fee_rate": self.fee_rate,
            "balances": self.balances(),
            "win_loss": self.win_loss(),
            "drawdown": self.drawdown(),
            "exposure": self.exposure_stats(),
            "daily_pnl": self.daily_pnl_series(),
            "daily_loss_halt": self.daily_loss_halt,
            "daily_loss_limit_usd": self.daily_loss_limit_usd,
            "cycles": self.cycle_count,
            "errors": self.errors_count,
            "offline_samples": self.offline_count,
            "emergency_halt_events": self.emergency_count,
            "trade_rows": self.trade_count,
        }
        # JSONL
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                for r in self.rows:
                    fh.write(json.dumps(r) + "\n")
            os.replace(tmp, self.path)
        except Exception as e:
            raise RuntimeError("could not write jsonl") from e
        # summary
        tmp = self.summary_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.summary_path)
        return summary


def main():
    """Minimal CLI: build a recorder and print an empty summary (no side
    effects on the engine)."""
    rec = MetricsRecorder()
    rec.start_session()
    summary = rec.write_summary()
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())