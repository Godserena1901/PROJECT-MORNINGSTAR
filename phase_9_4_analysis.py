#!/usr/bin/env python3
"""Phase 9.4 - Performance Analysis (Project Morningstar).

Analysis-only layer over the Phase 9.3 controlled-beta evidence ledger.

It consumes ONLY the evidence already produced and validated by Phases 9.1-9.3
(no new collection, no network, no orders):

  * evidence JSONL rows from BetaCollector (``phase_9_3_beta.py``):
      - trade  rows: symbol, side, strategy, signal, entry, exit, quantity,
                     gross_pnl, fees, net_pnl, exit_reason, opened_at,
                     closed_at, win
      - cycle  rows: equity, exposure_used_usd, open_positions, status,
                     offline, daily_loss_halt, scan{scanned,opened,rejected}
      - context rows: per-symbol market/context snapshot
      - error  rows: captured errors
  * the Phase 9.3 consolidated summary (build_evidence_summary) which carries
    the recorded aggregate figures, incl. equity {initial_balance,
    ending_balance, shadow_balance} and max_drawdown.

The analyzer computes ONLY "observed metrics" that are directly derivable from
those fields.  It does NOT fabricate missing history, does NOT manufacture a
dataset, does NOT introduce trading rules, and does NOT claim profitability on
a small sample.  When a metric is unsupported by the recorded evidence, or the
sample is too small for a statistically meaningful conclusion, the analyzer
reports sufficiency = "insufficient"/"unavailable" with the contributing sample
size and a reason - never an estimate.

Handoff to Phases 9.5/9.6/9.7 is exposed through the structured report returned
by analyze()/report().

SAFETY:
    Analysis only.  No live trading, no network, no orders, no change to
    DRY_RUN / LIVE_TRADING_ENABLED, no change to production logic, and no
    credential handling.

Usage:
    analyzer = PerformanceAnalyzer.from_files("beta_evidence.jsonl",
                                              "beta_evidence_summary.json")
    report = analyzer.analyze()
    analyzer.write_report(report, "beta_analysis_9_4.json")
"""

import json
import os
import sys


def _f(value, default=0.0):
    """Safe float coercion (None-tolerant); returns default on failure."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _num(value):
    """Return a float or None for a numeric field (no defaulting)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(part, total):
    return round(part / total * 100.0, 4) if total else None


class PerformanceAnalyzer:
    """Compute observed performance metrics from Phase 9.3 evidence rows."""

    SCHEMA = 1

    def __init__(self, trades=None, cycles=None, contexts=None, errors=None,
                 summary=None):
        self.trades = list(trades or [])
        self.cycles = list(cycles or [])
        self.contexts = list(contexts or [])
        self.errors = list(errors or [])
        self.summary = dict(summary or {})

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------
    @classmethod
    def from_files(cls, evidence_path, summary_path=None):
        trades, cycles, contexts, errors = [], [], [], []
        if evidence_path and os.path.exists(evidence_path):
            with open(evidence_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    rtype = row.get("type")
                    data = row.get("data") or row
                    if rtype == "trade":
                        trades.append(data)
                    elif rtype == "cycle":
                        cycles.append(data)
                    elif rtype == "context":
                        contexts.append(data)
                    elif rtype == "error":
                        errors.append(data)
        summary = {}
        if summary_path and os.path.exists(summary_path):
            try:
                with open(summary_path, "r", encoding="utf-8") as fh:
                    summary = json.load(fh)
            except Exception:  # noqa: BLE001
                summary = {}
        return cls(trades, cycles, contexts, errors, summary)

# ------------------------------------------------------------------
    # Observed metrics (directly computed from evidence)
    # ------------------------------------------------------------------
    def metric_counts(self):
        t = self.trades
        wins = sum(1 for tr in t if (_num(tr.get("net_pnl")) or 0.0) > 0.0)
        losses = len(t) - wins
        return {"total_trades": len(t), "wins": wins, "losses": losses}

    def pnl(self):
        gross = sum(_f(x.get("gross_pnl"), 0.0) for x in self.trades)
        fees = sum(_f(x.get("fees"), 0.0) for x in self.trades)
        net = sum(_f(x.get("net_pnl"), 0.0) for x in self.trades)
        return {"gross_pnl": round(gross, 8), "fees_total": round(fees, 8),
                "net_pnl": round(net, 8)}

    def averages(self):
        t = self.trades
        wins = [x for x in t if (_num(x.get("net_pnl")) or 0.0) > 0.0]
        losses = [x for x in t if (_num(x.get("net_pnl")) or 0.0) < 0.0]
        avg_per = (sum(_num(x.get("net_pnl")) or 0.0 for x in t) / len(t)
                   if t else None)
        avg_win = (sum(_num(x.get("net_pnl")) or 0.0 for x in wins)
                   / len(wins)) if wins else None
        avg_loss = (sum(_num(x.get("net_pnl")) or 0.0 for x in losses)
                    / len(losses)) if losses else None
        return {"avg_pnl_per_trade": round(avg_per, 8) if avg_per is not None
                else None,
                "avg_win": round(avg_win, 8) if avg_win is not None else None,
                "avg_loss": round(avg_loss, 8) if avg_loss is not None
                else None}

    def win_rate(self):
        t = self.trades
        wins = sum(1 for x in t if (_num(x.get("net_pnl")) or 0.0) > 0.0)
        return {"win_rate_pct": _pct(wins, len(t)), "sample": len(t)}

    def profit_factor(self):
        """Profit factor = sum(win gross) / abs(sum(loss gross)).

        Returned available ONLY when the evidence supports it: a winning gross
        total and a nonzero absolute losing gross total.  Otherwise reported
        unavailable with a reason - never estimated.
        """
        win_gross = sum(_num(x.get("gross_pnl")) or 0.0 for x in self.trades
                        if (_num(x.get("net_pnl")) or 0.0) > 0.0)
        loss_gross = sum(_num(x.get("gross_pnl")) or 0.0 for x in self.trades
                         if (_num(x.get("net_pnl")) or 0.0) < 0.0)
        reason = []
        if not self.trades:
            return {"profit_factor": None, "available": False,
                    "reason": "no trades recorded"}
        if win_gross == 0.0 and loss_gross == 0.0:
            reason = "no winning and no losing trade with gross P&L"
        elif win_gross == 0.0:
            reason = "no winning trade with nonzero gross P&L"
        elif loss_gross == 0.0:
            reason = "no losing trade with nonzero gross P&L (divisor is zero)"
        if reason:
            return {"profit_factor": None, "available": False,
                    "reason": reason}
        return {"profit_factor": round(win_gross / abs(loss_gross), 6),
                "available": True, "reason": []}

    def equity(self):
        """Equity from recorded cycle samples + summary figures."""
        eq = self.summary.get("equity", {}) or {}
        initial = _num(eq.get("initial_balance"))
        shadow = _num(eq.get("shadow_balance"))
        ending = _num(eq.get("ending_balance"))
        last_sample = _num(eq.get("last_sample"))
        curve = [initial] if initial is not None else []
        curve += [_num(c.get("equity")) for c in self.cycles
                  if c.get("equity") is not None]
        peak = max(curve) if curve else None
        trough = min(curve) if curve else None
        movement = (ending - initial) if (ending is not None
                                          and initial is not None) else None
        return {"initial_balance": initial, "ending_balance": ending,
                "shadow_balance": shadow, "last_sample": last_sample,
                "peak": round(peak, 4) if peak is not None else None,
                "trough": round(trough, 4) if trough is not None else None,
                "movement": round(movement, 4) if movement is not None
                else None,
                "samples": len(curve)}

    def drawdown(self):
        """Max drawdown directly from recorded evidence.

        Preferred source is the Phase 9.3 recorded summary.drawdown; otherwise
        recompute from cycle equity + initial balance using the same formula.
        """
        recorded = self.summary.get("max_drawdown")
        if isinstance(recorded, dict) and "max_drawdown_pct" in recorded:
            return {"max_drawdown_pct": recorded.get("max_drawdown_pct"),
                    "peak": recorded.get("peak"),
                    "trough": recorded.get("trough"),
                    "source": "recorded", "samples": recorded.get("samples")}
        initial = _num(self.summary.get("equity", {}).get("initial_balance"))
        series = [initial] if initial is not None else []
        series += [_num(c.get("equity")) for c in self.cycles
                   if c.get("equity") is not None]
        if not series:
            return {"max_drawdown_pct": None, "peak": None, "trough": None,
                    "source": "unavailable", "samples": 0}
        peak = series[0]
        max_dd = 0.0
        for value in series:
            peak = max(peak, value)
            if peak > 0:
                max_dd = max(max_dd, (peak - value) / peak)
        return {"max_drawdown_pct": round(max_dd * 100.0, 4),
                "peak": round(peak, 4),
                "trough": round(min(series), 4),
                "source": "recomputed", "samples": len(series)}

# ------------------------------------------------------------------
    # Exposure / reliability / attribution
    # ------------------------------------------------------------------
    def exposure(self):
        exps = [_num(c.get("exposure_used_usd")) for c in self.cycles
                if c.get("exposure_used_usd") is not None]
        avg = sum(exps) / len(exps) if exps else None
        return {"exposure_max_usd": round(max(exps), 4) if exps else None,
                "exposure_avg_usd": round(avg, 4) if avg is not None else None,
                "samples": len(exps)}

    def reliability(self):
        offline = sum(1 for c in self.cycles if c.get("offline"))
        emergency = sum(1 for c in self.cycles if c.get("status")
                        == "EMERGENCY_HALT")
        summary = self.summary
        return {
            "errors": len(self.errors),
            "error_events": len(self.errors),
            "offline_samples": offline,
            "emergency_halts": emergency,
            "restart_count": summary.get("restart_count", 0),
            "cycles": summary.get("cycles", len(self.cycles)),
            "cycle_samples": len(self.cycles),
        }

    def attribution(self):
        def counts(by_field):
            out = {}
            for tr in self.trades:
                key = str(tr.get(by_field) or "?")
                out.setdefault(key, {"trades": 0, "wins": 0, "losses": 0,
                                     "net": 0.0})
                out[key]["trades"] += 1
                net = _num(tr.get("net_pnl")) or 0.0
                out[key]["net"] = round(out[key]["net"] + net, 8)
                if net > 0:
                    out[key]["wins"] += 1
                elif net < 0:
                    out[key]["losses"] += 1
            for v in out.values():
                v["net"] = round(v["net"], 4)
            return out

        return {
            "by_signal": counts("signal"),
            "by_side": counts("side"),
            "by_symbol": counts("symbol"),
            "by_exit_reason": counts("exit_reason"),
        }

    # ------------------------------------------------------------------
    # Evidence sufficiency
    # ------------------------------------------------------------------
    def sufficiency(self):
        """Report, per input source, whether evidence is sufficient.

        NO numeric threshold is invented here: a fund-wide statistic is
        reported "sufficient" only when at least one contributing data point
        exists; statistical confidence is never asserted and profitability is
        never claimed on a small sample.  For each metric the contributing
        sample size is reported so a caller-supplied minimum can be applied.
        """
        t = len(self.trades)
        cyc = len(self.cycles)
        ctx = len(self.contexts)
        info = {
            "total_trades": {"n": t,
                             "status": "sufficient" if t else "insufficient"},
            "win_loss": {"n": t,
                         "status": "sufficient" if t >= 1 else "insufficient"},
            "pnl": {"n": t, "status": "sufficient" if t else "insufficient"},
            "avg_per_trade": {"n": t,
                              "status": "sufficient" if t else "insufficient"},
            "profit_factor": {"n": t,
                             "status": "sufficient"
                             if self.profit_factor()["available"]
                             else "insufficient"},
            "equity": {"n": cyc + (1 if self.summary.get("equity", {}).get(
                "initial_balance") is not None else 0),
                "status": "sufficient" if cyc else "insufficient"},
            "drawdown": {"n": cyc,
                         "status": "sufficient" if cyc else "insufficient"},
            "exposure": {"n": cyc,
                         "status": "sufficient" if cyc else "insufficient"},
            "attribution": {"n": t,
                            "status": "sufficient" if t else "insufficient"},
            "market_context": {"n": ctx,
                               "status": "sufficient" if ctx
                               else "insufficient"},
            "errors": {"n": len(self.errors),
                       "status": "sufficient"},
        }
        statement = []
        if not self.trades:
            statement.append(
                "no trades recorded - trade-based performance conclusions "
                "are not possible")
        if not self.cycles:
            statement.append(
                "no cycle samples recorded - equity/drawdown/exposure "
                "conclusions are not possible")
        statement.append(
            "no profitability claim is made over any small sample; sample "
            "sizes are reported for each metric so a caller may apply a "
            "minimum-sample gate")
        return {"per_metric": info, "statement": statement}

# ------------------------------------------------------------------
    # Report / output
    # ------------------------------------------------------------------
    def analyze(self):
        """Compute the full Phase 9.4 structured report."""
        counts = self.metric_counts()
        pnl = self.pnl()
        averages = self.averages()
        win_rate = self.win_rate()
        pf = self.profit_factor()
        equity = self.equity()
        dd = self.drawdown()
        return {
            "phase": "9.4",
            "schema": self.SCHEMA,
            "collection_id": self.summary.get("collection_id"),
            "source_evidence": {
                "evidence_log": self.summary.get("evidence_log",
                                                 "unknown.jsonl"),
                "metrics_summary": self.summary.get("metrics_summary"),
            },
            "observed_metrics": {
                "total_trades": counts["total_trades"],
                "wins": counts["wins"],
                "losses": counts["losses"],
                "win_rate_pct": win_rate["win_rate_pct"],
                "gross_pnl": pnl["gross_pnl"],
                "net_pnl": pnl["net_pnl"],
                "fees_total": pnl["fees_total"],
                "avg_pnl_per_trade": averages["avg_pnl_per_trade"],
                "avg_win": averages["avg_win"],
                "avg_loss": averages["avg_loss"],
                "profit_factor": pf["profit_factor"],
                "profit_factor_available": pf["available"],
                "equity": equity,
                "max_drawdown": dd,
                "exposure": self.exposure(),
                "reliability": self.reliability(),
            },
            "attribution": self.attribution(),
            "market_context": {
                "snapshot_count": len(self.contexts),
                "note": "per-symbol market/context snapshots are recorded "
                        "and available to correlate with trade outcomes",
            },
            "evidence_sufficiency": self.sufficiency(),
            "handoff": self.handoff(),
            "verdict": self.verdict(),
        }

    def _observed_block(self):
        counts = self.metric_counts()
        pnl = self.pnl()
        win_rate = self.win_rate()
        return {"total_trades": counts["total_trades"],
                "wins": counts["wins"], "losses": counts["losses"],
                "win_rate_pct": win_rate["win_rate_pct"],
                "gross_pnl": pnl["gross_pnl"], "net_pnl": pnl["net_pnl"],
                "fees_total": pnl["fees_total"]}

    def handoff(self):
        """Structured handoff to Phases 9.5, 9.6 and 9.7 (no thresholds)."""
        return {
            "to_9_5_strategy_market": {
                "attribution": self.attribution(),
                "market_context_snapshot_count": len(self.contexts),
                "note": "strategy & market-condition validation input"},
            "to_9_6_stability_endurance": {
                "reliability": self.reliability(),
                "equity": self.equity(),
                "max_drawdown": self.drawdown(),
                "note": "stability/endurance validation input"},
            "to_9_7_final_go_nogo": {
                "observed_metrics": self._observed_block(),
                "sufficiency": self.sufficiency()["per_metric"],
                "note": "final Phase 9 GO/NO-GO input"},
        }

# ------------------------------------------------------------------
    # Verdict: NEVER derive GO/NO-GO without an approved threshold.
    # ------------------------------------------------------------------
    def verdict(self):
        """Phase 9.4 makes NO GO/NO-GO trading decision.

        Because no numeric thresholds are established by the repository, this
        layer reports observed metrics + sufficiency only.  Any GO/NO-GO is a
        Phase 9.7 decision requiring an approved minimum-sample gate and
        threshold values.
        """
        return {
            "decision": "GO_NOGO_DEFERRED",
            "reason": "Phase 9.4 performs performance analysis only; a "
                      "GO/NO-GO requires approved thresholds (none "
                      "established). Refer sufficiency + observed metrics to "
                      "Phase 9.7.",
        }

    # ------------------------------------------------------------------
    # IO
    # ------------------------------------------------------------------
    def to_dict(self):
        return self.analyze()

    def write_report(self, report=None, path=None):
        path = path or os.environ.get("PHASE9_4_REPORT_PATH",
                                      "beta_analysis_9_4.json")
        data = report if report is not None else self.analyze()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return path


def main():
    """CLI: analyze an existing evidence set and print the report."""
    import argparse
    p = argparse.ArgumentParser(
        description="Phase 9.4 - Performance Analysis over Phase 9.3 "
                    "evidence (analysis only).")
    p.add_argument("--evidence", default=os.environ.get(
        "PHASE9_3_EVIDENCE_PATH", "beta_evidence.jsonl"))
    p.add_argument("--summary", default=os.environ.get(
        "PHASE9_3_SUMMARY_PATH", "beta_evidence_summary.json"))
    p.add_argument("--out", default=os.environ.get(
        "PHASE9_4_REPORT_PATH", "beta_analysis_9_4.json"))
    args = p.parse_args()

    analyzer = PerformanceAnalyzer.from_files(args.evidence, args.summary)
    report = analyzer.analyze()
    out = analyzer.write_report(report, args.out)
    print(json.dumps(report, indent=2, sort_keys=True))
    print("report written to %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())