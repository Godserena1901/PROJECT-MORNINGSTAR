#!/usr/bin/env python3
"""Phase 8 - Autonomous 24/7 Trading (paper, supervised) - Project Morningstar.

Turns the verified Phase 5-7 foundation into an always-on autonomous trading
loop for the paper environment:

    WAKE -> SCAN -> ANALYZE -> DECIDE -> EXECUTE -> MONITOR -> EXIT/MANAGE
         -> RECORD RESULT -> CONTINUE SCANNING

This layer REUSES the Phase 7 Autopilot (risk limits, persistence, telemetry,
DRY_RUN-only paper execution) and the existing main.py decision engine +
paper book read-only.  It adds the resilience and supervision required for
24/7 operation:

  * internet disconnects / exchange API outages -> connectivity gate +
    exponential backoff; no trading while offline, resumes on re-connect
  * machine restarts -> durable supervisor + main.py paper state is persisted,
    existing open positions are restored and monitored
  * duplicate orders / runaway exposure -> phase-7 caps + per-symbol
    idempotency/cooldown guard re-applied at the autonomous entry point
  * stale market data -> pre-open freshness/price sanity guard
  * unexpected balances / insufficient funds -> available-balance gate
  * failed orders -> captured into telemetry (recent_errors + report); the
    supervisory loop keeps running and never silently drops failures
  * daily loss limit -> realized PnL for the current UTC day is tracked; once
    the configured limit is breached, NEW entries halt while existing
    positions keep being managed to epsilon (exit handling)
  * emergency shutdown -> a kill-switch file (or PHASE8_KILL_SWITCH env) stops
    the loop and persists state

SAFETY: DRY_RUN stays True, LIVE_TRADING_ENABLED stays False, and the
autonomous engine can NEVER self-enable live trading.  All execution stays on
the existing paper engine; no request is ever sent and no real order is placed.

Usage:
    python3 phase_8_autopilot.py
Exit code 0 = clean stop; 1 = fatal configuration error or emergency halt.
"""

import os
import sys
import time
import threading
import datetime as _dt

from phase_7_autopilot import (
    Autopilot,
    _env_float,
    _env_int,
    _env_str,
)

# SAFETY: re-assert safe defaults before anything else in this process.
os.environ.setdefault("DRY_RUN", "True")
os.environ.setdefault("LIVE_TRADING_ENABLED", "False")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main as bot_main  # noqa: E402
from binance_service import binance_service  # noqa: E402

class Autonomous(Autopilot):
    """24/7 supervised paper-trading loop with resilience + safety halts."""

    def __init__(self, analyze=None, state_file=None, interval_seconds=None,
                 max_positions=None, max_exposure_usd=None,
                 position_size_usd=None, min_open_gap_seconds=None,
                 symbols=None, notifier=None,
                 daily_loss_limit_usd=None, emergency_file=None,
                 backoff_initial_s=None, backoff_max_s=None,
                 max_balance_fraction=None, min_balance_usd=None,
                 sleep_fn=None):
        super().__init__(
            analyze=analyze,
            state_file=state_file,
            interval_seconds=interval_seconds,
            max_positions=max_positions,
            max_exposure_usd=max_exposure_usd,
            position_size_usd=position_size_usd,
            min_open_gap_seconds=min_open_gap_seconds,
            symbols=symbols,
            notifier=notifier,
        )
        # --- resilience configuration (env-configurable, safe defaults) ----
        self.daily_loss_limit_usd = abs(_env_float(
            "PHASE8_DAILY_LOSS_LIMIT_USD", daily_loss_limit_usd or 0.0))
        self.emergency_file = emergency_file or _env_str(
            "PHASE8_EMERGENCY_FILE", "EMERGENCY_STOP")
        self.backoff_initial_s = _env_float(
            "PHASE8_BACKOFF_INITIAL_S", backoff_initial_s or 2.0)
        self.backoff_max_s = _env_float(
            "PHASE8_BACKOFF_MAX_S", backoff_max_s or 60.0)
        self.max_balance_fraction = _env_float(
            "PHASE8_MAX_BALANCE_FRACTION",
            max_balance_fraction if max_balance_fraction is not None else 0.5)
        self.min_balance_usd = _env_float(
            "PHASE8_MIN_BALANCE_USD",
            min_balance_usd if min_balance_usd is not None else 0.0)
        self._sleep = sleep_fn or time.sleep

        # --- supervisor state ----------------------------------------------
        self.operative = True          # False after emergency/normal halt
        self.halted_reason = None
        self.daily_loss_halt = False   # sticky for the day once breached
        self.daily_pnl_today = 0.0
        self.offline = False
        self.consecutive_failures = 0
        self.backoff_seconds = self.backoff_initial_s
        self.last_cycle_status = "STOPPED"

    # ------------------------------------------------------------------
    # UTC helpers / daily-loss ledger
    # ------------------------------------------------------------------
    @staticmethod
    def _today_iso():
        return _dt.datetime.now(_dt.timezone.utc).date().isoformat()

    def available_balance(self):
        """Simulated (paper) or real free balance.  In DRY_RUN / live=False we
        use the paper balance; live account balance is only reachable when the
        separate live switch is enabled (never by this module)."""
        if getattr(self, "live", False):
            ok, free, err = self.service.get_available_usdt()
            if not ok:
                self.recent_errors.append(f"available_balance: {err}")
                return 0.0
            return free
        return float(getattr(bot_main, "PAPER_BALANCE", 0.0))

    def update_daily_pnl(self):
        """Recompute today's realized paper PnL from closed-trade history."""
        today = self._today_iso()
        total = 0.0
        for tr in bot_main.PAPER_TRADE_HISTORY:
            closed = str(tr.get("closed_at", ""))[:10]
            if closed == today:
                total += self._read_float(tr, "pnl", 0.0)
        self.daily_pnl_today = total
        if self.daily_loss_limit_usd > 0 and total <= -self.daily_loss_limit_usd:
            if not self.daily_loss_halt:
                self.daily_loss_halt = True
                self.halted_reason = (
                    f"daily loss limit reached ({total:,.2f} <= "
                    f"-{self.daily_loss_limit_usd:,.2f})")
                self._log("DAILY LOSS HALT: " + self.halted_reason, "ERROR")
        return total

# ------------------------------------------------------------------
    # Connectivity / backoff
    # ------------------------------------------------------------------
    def _ping_ok(self):
        try:
            res = self.service.ping()
            ok = bool(res and res.get("status") == "ONLINE")
        except Exception as e:  # noqa: BLE001
            self.recent_errors.append(f"connectivity ping raised: {e}")
            ok = False
        if not ok:
            self.offline = True
            self.consecutive_failures += 1
            self.backoff_seconds = min(
                self.backoff_max_s,
                self.backoff_initial_s * (2 ** (self.consecutive_failures - 1)))
            self._log(
                f"EXCHANGE OFFLINE (attempt #{self.consecutive_failures}); "
                f"backoff={self.backoff_seconds:.0f}s", "WARN")
        else:
            was_offline = self.offline
            self.offline = False
            self.consecutive_failures = 0
            self.backoff_seconds = self.backoff_initial_s
            if was_offline:
                self._log("Connectivity restored; resuming.", "INFO")
        return ok

    # ------------------------------------------------------------------
    # Emergency shutdown (kill switch)
    # ------------------------------------------------------------------
    def check_emergency_stop(self):
        """Return a reason string if an emergency stop is commanded, else None."""
        path = getattr(self, "emergency_file", "") or ""
        if path and os.path.exists(path):
            return f"Emergency stop file present at {path}"
        env = os.getenv("PHASE8_KILL_SWITCH", "").strip().lower()
        if env in ("1", "true", "yes", "stop"):
            return "PHASE8_KILL_SWITCH environment variable set"
        return None

    def command_emergency_halt(self, reason):
        """Hard stop: halt new work, log + notify, persist state."""
        self.operative = False
        self.halted_reason = reason or "emergency halt"
        self._log("EMERGENCY HALT: " + str(self.halted_reason), "ERROR")
        self.save_state()
        return self.halted_reason

    # ------------------------------------------------------------------
    # Position reconciliation on restart / unexpected balances
    # ------------------------------------------------------------------
    def reconcile_positions(self):
        """Sanity-check every open paper position loaded from state."""
        anomalies = []
        for symbol, pos in self.open_positions().items():
            qty = self._read_float(pos, "quantity", 0.0)
            entry = self._read_float(pos, "entry", 0.0)
            sl = self._read_float(pos, "stop_loss", 0.0)
            tp = self._read_float(pos, "take_profit", 0.0)
            if (qty <= 0 or entry <= 0 or sl <= 0 or tp <= 0
                    or pos.get("status") != "OPEN"):
                anomalies.append(f"{symbol}: malformed open position {pos}")
        if anomalies:
            self.recent_errors.extend(anomalies)
            self._log("POSITION RECONCILIATION anomalies: " + str(anomalies),
                      "WARN")
        return anomalies

# ------------------------------------------------------------------
    # Autonomous entry guards (on top of Phase 7 evaluate caps)
    # ------------------------------------------------------------------
    def _proposed_notional(self, decision):
        qty = self._read_float(decision, "quantity", 0.0)
        entry = self._read_float(decision, "entry", 0.0)
        if qty > 0 and entry > 0:
            return qty * entry
        return float(self.position_size_usd)

    def evaluate(self, decision):
        """Phase 7 caps + Phase 8 resilience guards (stale/balance/daily)."""
        ok, reason = super().evaluate(decision)
        if not ok:
            return ok, reason
        # stale market data / invalid decision plumbing
        if decision.get("stale"):
            return False, "stale market data (flag set)"
        current = self._read_float(decision, "current_price", 0.0)
        entry = self._read_float(decision, "entry", 0.0)
        sl = self._read_float(decision, "stop_loss", 0.0)
        tp = self._read_float(decision, "take_profit", 0.0)
        if current <= 0 or entry <= 0 or sl <= 0 or tp <= 0:
            return False, \
                "invalid/stale market data (current/entry/SL/TP missing or <=0)"
        # unexpected / insufficient available balance
        avail = self.available_balance()
        if avail < 0:
            return False, "unexpected balance (negative available)"
        notional = self._proposed_notional(decision)
        if notional > avail * self.max_balance_fraction:
            return False, \
                f"insufficient available balance (need ${notional:,.0f}, " \
                f"free ${avail:,.0f} <= {self.max_balance_fraction:.0%} cap)"
        # daily loss halt blocks NEW entries (existing exits still monitored)
        if self.daily_loss_halt:
            return False, "daily loss halt active (no new entries)"
        return True, None

    # ------------------------------------------------------------------
    # Report + one autonomous cycle
    # ------------------------------------------------------------------
    def _build_report(self, status, scan=None, exit_closed=None, saved=None,
                      halted_reason=None):
        scan = scan or {"scanned": [], "opened": [], "rejected": {}}
        return {
            "status": status,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "scanned": scan.get("scanned", []),
            "opened": scan.get("opened", []),
            "rejected": scan.get("rejected", {}),
            "exits_closed": exit_closed or [],
            "offline": bool(self.offline),
            "consecutive_failures": self.consecutive_failures,
            "backoff_seconds": self.backoff_seconds,
            "daily_pnl_today": round(self.daily_pnl_today, 2),
            "daily_loss_halt": bool(self.daily_loss_halt),
            "halted_reason": halted_reason or self.halted_reason,
            "exposure_used_usd": round(self.current_exposure(), 2),
            "open_positions": len(self.open_positions()),
            "errors": list(self.recent_errors[-20:]),
            "state_saved": bool(saved),
        }

# ------------------------------------------------------------------
    # Supervised persistence (extends Phase 7 snapshot)
    # ------------------------------------------------------------------
    def _snapshot(self):
        snap = super()._snapshot()
        snap["autonomous"] = {
            "operative": self.operative,
            "halted_reason": self.halted_reason,
            "daily_loss_halt": self.daily_loss_halt,
            "daily_pnl_today": round(self.daily_pnl_today, 2),
            "consecutive_failures": self.consecutive_failures,
            "backoff_seconds": self.backoff_seconds,
            "last_cycle_status": self.last_cycle_status,
        }
        return snap

    def load_state(self):
        loaded = super().load_state() or False
        try:
            import json as _json
            with open(self.state_file, "r", encoding="utf-8") as fh:
                data = _json.load(fh)
            auto = data.get("autonomous") or {}
            self.operative = bool(auto.get("operative", self.operative))
            self.halted_reason = auto.get("halted_reason") or self.halted_reason
            self.daily_loss_halt = bool(auto.get(
                "daily_loss_halt", self.daily_loss_halt))
            self.daily_pnl_today = self._read_float(
                auto, "daily_pnl_today", self.daily_pnl_today)
            self.consecutive_failures = int(auto.get(
                "consecutive_failures", self.consecutive_failures) or 0)
            self.backoff_seconds = self._read_float(
                auto, "backoff_seconds", self.backoff_seconds)
        except Exception:
            pass
        return loaded

    # ------------------------------------------------------------------
    # One autonomous cycle
    # ------------------------------------------------------------------
    def run_cycle(self):
        """WAKE -> guards -> exits -> scan/open -> record -> report."""
        self.recent_errors = []
        scan = None
        exit_closed = []
        saved = False

        # 1. EMERGENCY / kill switch
        em = self.check_emergency_stop()
        if em:
            self.command_emergency_halt(em)
            self.last_cycle_status = "EMERGENCY_HALT"
            saved = self.save_state()
            return self._build_report("EMERGENCY_HALT", saved=saved, halted_reason=em)

        # 2. CONNECTIVITY (internet / exchange outages)
        if not self._ping_ok():
            self.last_cycle_status = "OFFLINE_BACKOFF"
            saved = self.save_state()
            if self._sleep is not None:
                self._sleep(self.backoff_seconds)
            return self._build_report("OFFLINE_BACKOFF", saved=saved)

        # 3. POSITIONS after restart / unexpected balances
        self.reconcile_positions()
        avail = self.available_balance()
        if avail < 0:
            reason = "unexpected negative available balance during cycle"
            self.command_emergency_halt(reason)
            self.last_cycle_status = "EMERGENCY_HALT"
            return self._build_report("EMERGENCY_HALT", saved=True, halted_reason=reason)
        if not self.operative:
            self.last_cycle_status = "STOPPED"
            return self._build_report("STOPPED", saved=self.save_state())

        # 4. DAILY LOSS ledger + gate (exits still run under a loss halt)
        self.update_daily_pnl()
        exit_closed = self.run_exit_cycle()
        if self.daily_loss_halt:
            self.last_cycle_status = "DAILY_LOSS_HALT"
            scan = {"scanned": [], "opened": [], "rejected": {
                "_all": ["daily loss halt active (no new entries)"]}}
        else:
            scan = self.run_scan_cycle()

        # 5. RECORD RESULT
        saved = self.save_state()
        self.last_cycle_status = "RUNNING" if not self.daily_loss_halt \
            else "DAILY_LOSS_HALT"
        report = self._build_report(self.last_cycle_status, scan=scan,
                                    exit_closed=exit_closed, saved=saved)
        self._log(
            f"autonomous cycle | {report['status']} | scanned={len(report['scanned'])} "
            f"opened={report['opened']} closed={report['exits_closed']} "
            f"daily_pnl=${report['daily_pnl_today']:,.2f} errors={len(report['errors'])}",
            "ERROR" if report["errors"] else "INFO")
        return report

    def serve(self, stop_event=None, iterations=None):
        """Supervised 24/7 loop; breaks on emergency halt or stop_event."""
        if stop_event is None:
            stop_event = threading.Event()
        self.load_state()
        self._log(
            f"Autonomous serving | interval={self.interval_seconds}s "
            f"symbols={self.symbols} max_positions={self.max_positions} "
            f"max_exposure=${self.max_exposure_usd:,.2f} "
            f"daily_loss_limit=${self.daily_loss_limit_usd:,.2f} "
            f"state_file={self.state_file}", "INFO")
        count = 0
        while self.operative and not stop_event.is_set():
            count += 1
            self._log(f"WAKE cycle #{count}", "INFO")
            try:
                report = self.run_cycle()
            except Exception as e:  # noqa: BLE001
                self.last_cycle_status = "CYCLE_ERROR"
                self.recent_errors.append(f"serve cycle error: {e}")
                self._log(f"serve cycle error: {e}", "ERROR")
                self.save_state()
                report = self._build_report("CYCLE_ERROR", saved=True)
            if report.get("status") == "EMERGENCY_HALT":
                break
            if iterations is not None and count >= iterations:
                break
            if stop_event.wait(self.interval_seconds):
                break
        self.save_state()
        self._log("Autonomous stopped cleanly.", "INFO")


def main():
    """CLI entry point for the 24/7 autonomous paper autopilot."""
    if os.getenv("DRY_RUN", "True").lower() not in ("1", "true", "yes"):
        print("FATAL: DRY_RUN must stay True for the Phase 8 autopilot.")
        sys.exit(1)
    if os.getenv("LIVE_TRADING_ENABLED", "False").lower() in ("1", "true", "yes"):
        print("FATAL: LIVE_TRADING_ENABLED must stay False for the autopilot.")
        sys.exit(1)

    autonomous = Autonomous()
    stop_event = threading.Event()
    try:
        autonomous.serve(stop_event=stop_event)
    except KeyboardInterrupt:
        stop_event.set()
        autonomous._log("KeyboardInterrupt received.", "WARN")
    except Exception as e:  # noqa: BLE001
        autonomous._log(f"Fatal error: {e}", "ERROR")
        sys.exit(1)
    if not autonomous.operative:
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()