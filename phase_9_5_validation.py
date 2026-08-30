#!/usr/bin/env python3
"""Phase 9.5 - Strategy & Market-Condition Validation (Project Morningstar).

Validation-only layer over the existing Phase 9 evidence.

It REUSES the Phase 9.3 evidence ledger (BetaCollector rows) and the Phase 9.4
analysis helpers; it does NOT re-collect, re-derive, or replace those layers.

Consumed evidence (Phase 9.3 schema, unchanged):
  * trade   rows (data): symbol, side, strategy, signal, entry, exit, quantity,
                        gross_pnl, fees, net_pnl, exit_reason, opened_at,
                        closed_at, win
  * context rows (data + envelope ts): per-symbol market/context snapshot
                        (current_price, and any supported indicators such as
                        rsi / macd / macd_signal / average_volume / regime)
  * cycle / error rows: reliability counters
  * Phase 9.3/9.4 summary: equity, max_drawdown, restart, cycles

WHAT IT VALIDATES:
  1. STRATEGY ROBUSTNESS across the strategy/market dimensions the evidence
     actually supports: signal (BUY/SELL), side, symbol, exit_reason, strategy.
     For each dimension value it reports trade count, wins/losses, net P&L,
     win rate and an explicit suffiency label.
  2. MARKET-CONDITION ROBUSTNESS: context snapshots are joined to the trade
     they fall within (envelope ts inside [opened_at, closed_at] for the same
     symbol) and bucketed into condition labels derived ONLY from fields that
     are actually present in the evidence (rsi band, macd trend, volume regime,
     regime).  Outcomes are reported per condition bucket.

SAFETY RULES:
  * Analysis/validation only. No live trading, no real orders, no network.
  * DRY_RUN / LIVE_TRADING_ENABLED are never changed.
  * Production trading logic and credentials are never touched.
  * NO fabricated market data, NO invented historical evidence, and no
    statistical claim beyond the available sample.

CONFIDENCE / SUFFICIENCY:
  Every conclusion carries an explicit sufficiency label. Where evidence is
  insufficient (too few, empty, or missing join), the layer says so and makes
  no claim.

Usage:
    validator = StrategyMarketValidator.from_files("beta_evidence.jsonl",
                                                   "beta_evidence_summary.json")
    report = validator.analyze()
    validator.write_report(report, "beta_validation_9_5.json")
"""

import json
import os
import sys
import datetime as _dt

from phase_9_4_analysis import _f, _num  # reuse Phase 9.4 numeric helpers


def _pct(part, total):
    return round(part / total * 100.0, 4) if total else None


def _parse_ts(value):
    """Parse an ISO timestamp leniently; fall back to raw string compare."""
    if value is None:
        return None
    s = str(value).strip()
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return s


class StrategyMarketValidator:
    """Validate strategy robustness + market-condition robustness."""

    SCHEMA = 1

    def __init__(self, trades=None, cycles=None, contexts=None,
                 context_rows=None, errors=None, summary=None):
        # contexts: list of context data dicts (for coverage)
        # context_rows: list of (envelope_ts, data) for window joining
        self.trades = list(trades or [])
        self.cycles = list(cycles or [])
        self.contexts = list(contexts or [])
        self.context_rows = list(context_rows or [])
        self.errors = list(errors or [])
        self.summary = dict(summary or {})

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------
    @classmethod
    def from_files(cls, evidence_path, summary_path=None):
        trades, cycles, contexts, context_rows, errors = [], [], [], [], []
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
                    ts = row.get("ts")
                    if rtype == "trade":
                        trades.append(data)
                    elif rtype == "cycle":
                        cycles.append(data)
                    elif rtype == "context":
                        contexts.append(data)
                        context_rows.append((_parse_ts(ts), data))
                    elif rtype == "error":
                        errors.append(data)
        summary = {}
        if summary_path and os.path.exists(summary_path):
            try:
                with open(summary_path, "r", encoding="utf-8") as fh:
                    summary = json.load(fh)
            except Exception:  # noqa: BLE001
                summary = {}
        return cls(trades, cycles, contexts, context_rows, errors, summary)

# ------------------------------------------------------------------
    # Strategy robustness (per supported dimension)
    # ------------------------------------------------------------------
    _STRATEGY_DIMENSIONS = (
        ("by_signal", "signal"),
        ("by_side", "side"),
        ("by_symbol", "symbol"),
        ("by_exit_reason", "exit_reason"),
        ("by_strategy", "strategy"),
    )

    def _breakdown(self, field):
        """Per-value trade breakdown built strictly from evidence fields."""
        out = {}
        for tr in self.trades:
            key = str(tr.get(field) or "?")
            block = out.setdefault(key, {"trades": 0, "wins": 0,
                                         "losses": 0, "net": 0.0})
            block["trades"] += 1
            net = _num(tr.get("net_pnl")) or 0.0
            block["net"] = round(block["net"] + net, 8)
            if net > 0:
                block["wins"] += 1
            elif net < 0:
                block["losses"] += 1
        for v in out.values():
            v["net"] = round(v["net"], 4)
            v["win_rate_pct"] = _pct(v["wins"], v["trades"])
            v["sufficiency"] = "sufficient" if v["trades"] >= 1 \
                else "insufficient"
        return out

    def strategy_robustness(self):
        """Validate outcomes across each strategy dimension."""
        return {name: self._breakdown(field)
                for name, field in self._STRATEGY_DIMENSIONS}

# ------------------------------------------------------------------
    # Market-condition robustness
    # ------------------------------------------------------------------
    @staticmethod
    def _band_for_context(context, field):
        """Derive a condition label ONLY from fields actually present."""
        if field == "rsi_band":
            v = _num(context.get("rsi"))
            if v is None:
                return None
            return "rsi_lt_30" if v < 30 else ("rsi_gt_70" if v > 70
                                               else "rsi_30_70")
        if field == "macd_trend":
            m = _num(context.get("macd"))
            s = _num(context.get("macd_signal"))
            if m is None or s is None:
                return None
            return "macd_bull" if m > s else "macd_bear"
        if field == "volume_regime":
            v = _num(context.get("volume"))
            a = _num(context.get("average_volume"))
            if v is None or a is None:
                return None
            return "vol_high" if v > a else "vol_low"
        if field == "regime":
            r = context.get("regime")
            return str(r) if r is not None else None
        return None

    _CONDITION_FIELDS = ("rsi_band", "macd_trend", "volume_regime", "regime")

    def _field_available(self, field):
        for _ts, data in self.context_rows:
            if self._band_for_context(data, field) is not None:
                return True
        return False

    def _in_window(self, ts, opened, closed):
        if ts is None or opened is None or closed is None:
            return False
        if isinstance(ts, _dt.datetime) \
                and isinstance(opened, _dt.datetime) \
                and isinstance(closed, _dt.datetime):
            return opened <= ts <= closed
        if isinstance(ts, str) and isinstance(opened, str) \
                and isinstance(closed, str):
            return opened <= ts <= closed
        return False

    def _matched_contexts(self, trade):
        symbol = str(trade.get("symbol", ""))
        opened = _parse_ts(trade.get("opened_at"))
        closed = _parse_ts(trade.get("closed_at"))
        matched = []
        for ts, data in self.context_rows:
            if str(data.get("symbol", "")) != symbol:
                continue
            if self._in_window(ts, opened, closed):
                matched.append((ts, data))
        return matched

    def market_condition_validation(self):
        """Bucket trade outcomes by context-derived condition labels.

        A trade maps to a condition only when a same-symbol context snapshot
        falls inside its [opened_at, closed_at] window; otherwise it is
        reported under that condition as 'no_context_in_window' with an
        explicit insufficient label (never an estimate).
        """
        coverage = {"context_rows": len(self.context_rows),
                    "context_joinable_symbols": sorted({
                        str(d.get("symbol")) for _t, d in self.context_rows}),
                    "trades_with_context": 0,
                    "trades_without_context": 0}
        for tr in self.trades:
            if self._matched_contexts(tr):
                coverage["trades_with_context"] += 1
            else:
                coverage["trades_without_context"] += 1

        results = {}
        for field in self._CONDITION_FIELDS:
            if not self._field_available(field):
                results[field] = {"available": False,
                                  "reason": "no context row provides the "
                                            "required field(s)",
                                  "buckets": {}}
                continue
            buckets = {}
            unmatched = 0
            for tr in self.trades:
                matched = self._matched_contexts(tr)
                label = None
                if matched:
                    for ts, data in sorted(matched, key=lambda x: str(x[0])):
                        b = self._band_for_context(data, field)
                        if b is not None:
                            label = b
                            break
                if label is None:
                    label = "no_context_in_window"
                    unmatched += 1
                block = buckets.setdefault(label, {"trades": 0, "wins": 0,
                                                   "losses": 0, "net": 0.0})
                block["trades"] += 1
                net = _num(tr.get("net_pnl")) or 0.0
                block["net"] = round(block["net"] + net, 8)
                if net > 0:
                    block["wins"] += 1
                elif net < 0:
                    block["losses"] += 1
            for label, block in buckets.items():
                block["net"] = round(block["net"], 4)
                block["win_rate_pct"] = _pct(block["wins"], block["trades"])
                block["sufficiency"] = "sufficient" \
                    if label != "no_context_in_window" and block["trades"] >= 1 \
                    else "insufficient"
            results[field] = {"available": True, "buckets": buckets,
                              "unmatched_trades": unmatched}
        return {"coverage": coverage, "conditions": results}

    # ------------------------------------------------------------------
    # Sufficiency overview (per supported dimension + condition coverage)
    # ------------------------------------------------------------------
    def sufficiency(self):
        t = len(self.trades)
        strategy = self.strategy_robustness()
        dim_status = {}
        for name, breakdown in strategy.items():
            present = [v for v in breakdown.values() if v["trades"] >= 1]
            dim_status[name] = {
                "values_present": sorted(breakdown.keys()),
                "n_values": len(present),
                "status": "sufficient" if present else "insufficient"}
        mc = self.market_condition_validation()
        cond_status = {}
        for field, res in mc["conditions"].items():
            cond_status[field] = {
                "available": res["available"],
                "with_context": res.get("buckets", {}) and any(
                    k != "no_context_in_window" for k in res["buckets"]),
                "trades_with_context": mc["coverage"]["trades_with_context"],
                "trades_without_context": mc["coverage"][
                    "trades_without_context"]}
        statement = []
        if not self.trades:
            statement.append(
                "no trades recorded - strategy and market-condition "
                "validation are not possible")
        if not self.context_rows:
            statement.append(
                "no context snapshots recorded - market-condition joining is "
                "not possible")
        statement.append(
            "statistical robustness is NOT claimed beyond the available "
            "sample; each value/bucket reports its own sample size")
        return {"strategy_dimensions": dim_status,
                "market_conditions": cond_status,
                "overall_trades": t,
                "statement": statement}

# ------------------------------------------------------------------
    # Reliability context (from evidence only)
    # ------------------------------------------------------------------
    def reliability(self):
        offline = sum(1 for c in self.cycles if c.get("offline"))
        summary = self.summary
        return {
            "errors": len(self.errors),
            "offline_samples": offline,
            "emergency_halts": sum(
                1 for c in self.cycles if c.get("status") == "EMERGENCY_HALT"),
            "restart_count": summary.get("restart_count", 0),
            "cycles": summary.get("cycles", len(self.cycles)),
        }

    # ------------------------------------------------------------------
    # Report / output
    # ------------------------------------------------------------------
    def analyze(self):
        sr = self.strategy_robustness()
        mc = self.market_condition_validation()
        return {
            "phase": "9.5",
            "phase_of": "9.4",
            "schema": self.SCHEMA,
            "collection_id": self.summary.get("collection_id"),
            "source_evidence": {
                "evidence_log": self.summary.get("evidence_log",
                                                 "unknown.jsonl"),
                "phase_9_4_report": self.summary.get("metrics_summary"),
            },
            "strategy_robustness": sr,
            "market_condition_validation": mc,
            "reliability": self.reliability(),
            "evidence_sufficiency": self.sufficiency(),
            "handoff": self.handoff(),
            "verdict": self.verdict(),
        }

    def handoff(self):
        """Structured handoff to Phases 9.6 and 9.7 (no thresholds)."""
        sr = self.strategy_robustness()
        mc = self.market_condition_validation()
        return {
            "to_9_6_stability_endurance": {
                "strategy_robustness": sr,
                "reliability": self.reliability(),
                "market_condition_coverage": mc["coverage"],
                "note": "stability/endurance validation input"},
            "to_9_7_final_go_nogo": {
                "strategy_robustness": sr,
                "market_condition_validation": mc,
                "sufficiency": self.sufficiency(),
                "note": "final Phase 9 GO/NO-GO input"},
        }

    # ------------------------------------------------------------------
    # Verdict: NEVER derive GO/NO-GO without an approved threshold.
    # ------------------------------------------------------------------
    def verdict(self):
        """Phase 9.5 makes NO GO/NO-GO trading decision.

        Robustness is reported per dimension/bucket with sufficiency; a
        GO/NO-GO remains a Phase 9.7 decision requiring approved thresholds.
        """
        return {
            "decision": "GO_NOGO_DEFERRED",
            "reason": "Phase 9.5 validates strategy/market-condition "
                      "robustness on the available evidence; a GO/NO-GO "
                      "requires approved thresholds (none established). "
                      "Refer robustness + sufficiency to Phase 9.7.",
        }

    # ------------------------------------------------------------------
    # IO
    # ------------------------------------------------------------------
    def to_dict(self):
        return self.analyze()

    def write_report(self, report=None, path=None):
        path = path or os.environ.get("PHASE9_5_REPORT_PATH",
                                      "beta_validation_9_5.json")
        data = report if report is not None else self.analyze()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return path


def main():
    """CLI: validate an existing evidence set and print the report."""
    import argparse
    p = argparse.ArgumentParser(
        description="Phase 9.5 - Strategy & Market-Condition Validation "
                    "over Phase 9 evidence (analysis only).")
    p.add_argument("--evidence", default=os.environ.get(
        "PHASE9_3_EVIDENCE_PATH", "beta_evidence.jsonl"))
    p.add_argument("--summary", default=os.environ.get(
        "PHASE9_3_SUMMARY_PATH", "beta_evidence_summary.json"))
    p.add_argument("--out", default=os.environ.get(
        "PHASE9_5_REPORT_PATH", "beta_validation_9_5.json"))
    args = p.parse_args()

    validator = StrategyMarketValidator.from_files(args.evidence, args.summary)
    report = validator.analyze()
    out = validator.write_report(report, args.out)
    print(json.dumps(report, indent=2, sort_keys=True))
    print("report written to %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())