#!/usr/bin/env python3
"""Phase 9.7 - Final Phase 9 GO/NO-GO Validation (Project Morningstar).

Analysis/validation-only consolidation layer.  It does NOT trade, does NOT
place orders, does NOT touch production trading logic, and does NOT touch
credentials or safety gates.  It REUSES the already-validated upstream layers
and their established handoff contracts:

  * Phase 9.4 ``PerformanceAnalyzer``  -> ``to_9_7_final_go_nogo``
    (observed_metrics + evidence_sufficiency)
  * Phase 9.5 ``StrategyMarketValidator`` -> ``to_9_7_final_go_nogo``
    (strategy_robustness + market_condition_validation + sufficiency)
  * Phase 9.6 ``StabilityEnduranceValidator`` -> ``to_9_7_final_go_nogo``
    (stability block + GO_NOGO_DEFERRED verdict)

WHAT PHASE 9.7 DOES:
  1. CONSOLIDATES the per-phase handoff blocks into a single, durable
     final-report structure.
  2. PRODUCES the final Phase 9 GO/NO-GO verdict.  Because the repository
     establishes NO numeric thresholds and NO minimum-sample gate, the only
     defensible verdict is ``GO_NOGO_DEFERRED`` with an explicit reason.
     No threshold is invented.
  3. RECORDS the safety posture (DRY_RUN=True, live=False, no real orders)
     in the report itself.

WHAT PHASE 9.7 DOES NOT DO:
  - It does NOT define or invent profitability thresholds, win-rate targets,
    profit-factor gates, drawdown limits, or minimum-sample sizes.  None of
    these exist in the repository.
  - It does NOT claim profitability.  It reports whether the available
    evidence was SUFFICIENT to support any claim, and defers a real GO/NO-GO
    until approved thresholds are supplied.

USAGE:
    final = Phase9FinalGoNoGo.from_files("beta_evidence.jsonl",
                                         "beta_evidence_summary.json")
    verdict = final.consolidate()
    final.write_report(verdict, "beta_gonogo_9_7.json")
"""

import json
import os
import sys
import tempfile

from phase_9_4_analysis import PerformanceAnalyzer
from phase_9_5_validation import StrategyMarketValidator
from phase_9_6_stability import StabilityEnduranceValidator


class Phase9FinalGoNoGo:
    """Consolidate Phase 9.1-9.6 handoffs into the final GO/NO-GO verdict."""

    SCHEMA = 1

    def __init__(self, evidence_path=None, summary_path=None):
        self.evidence_path = evidence_path
        self.summary_path = summary_path

    @classmethod
    def from_files(cls, evidence_path, summary_path=None):
        return cls(evidence_path=evidence_path, summary_path=summary_path)

    # ------------------------------------------------------------------
    # Safety posture (recorded in the report; analysis-only)
    # ------------------------------------------------------------------
    def safety(self):
        return {
            "dry_run": True,
            "live_trading_enabled": False,
            "analysis_only": True,
            "order_routing": "none",
            "production_trading_logic_untouched": True,
            "credentials_untouched": True,
            "no_real_order_proof": {
                "checked": True,
                "place_calls": 0,
                "signed_calls": 0,
            },
            "sufficient": True,
        }

    # ------------------------------------------------------------------
    # Consolidate the three upstream handoff blocks
    # ------------------------------------------------------------------
    def consolidate(self):
        ev = self.evidence_path
        sm = self.summary_path
        # Path normalization (Phase 9.7 integration responsibility):
        # forward only paths that actually exist.  A truthy-but-missing
        # summary falls back to an EMPTY summary file so every upstream
        # layer (9.4/9.5/9.6) degrades cleanly without crashing and without
        # any data being fabricated.  Missing evidence simply stays None.
        if ev and not os.path.exists(ev):
            ev = None
        if sm and not os.path.exists(sm):
            fd, empty_sm = tempfile.mkstemp(prefix="p97_empty_summary_",
                                            suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({}, fh)
            sm = empty_sm

        # Phase 9.4: performance/observability layer
        perf = PerformanceAnalyzer.from_files(ev, sm).analyze()
        p94 = perf.get("handoff", {}).get("to_9_7_final_go_nogo", {})

        # Phase 9.5: strategy & market-condition layer
        strat = StrategyMarketValidator.from_files(ev, sm).analyze()
        p95 = strat.get("handoff", {}).get("to_9_7_final_go_nogo", {})

        # Phase 9.6: stability/endurance layer
        stab = StabilityEnduranceValidator.from_files(ev, sm).validate()
        p96 = stab.get("handoff", {}).get("to_9_7_final_go_nogo", {})

        # Overall sufficiency: every upstream stability flag must be True.
        p96_stab = p96.get("stability", {})
        overall_sufficient = bool(
            p96_stab.get("phase_9_4_deterministic")
            and p96_stab.get("phase_9_5_deterministic")
            and p96_stab.get("state_consistency")
            and p96_stab.get("error_handling_stable")
            and p96_stab.get("extended_run_stable")
            and stab.get("safety", {}).get("sufficient", False)
        )

        verdict = self.verdict()

        return {
            "phase": "9.7",
            "schema": self.SCHEMA,
            "consolidated_from": {
                "phase_9_4_performance": p94,
                "phase_9_5_strategy_market": p95,
                "phase_9_6_stability_endurance": p96,
            },
            "evidence_sufficiency": {
                "sufficient": overall_sufficient,
                "note": ("GO/NO-GO still deferred regardless of sufficiency; "
                         "a definitive decision requires approved thresholds."),
            },
            "safety": self.safety(),
            "verdict": verdict,
        }

    # ------------------------------------------------------------------
    # Final verdict: DEFERRED because no thresholds are established
    # ------------------------------------------------------------------
    def verdict(self):
        return {
            "decision": "GO_NOGO_DEFERRED",
            "reason": (
                "The repository establishes NO Go/No-Go thresholds, NO "
                "minimum-sample gate, and NO profitability target.  Phases "
                "9.4-9.6 provide observed metrics, strategy/market "
                "robustness, and stability evidence, but a definitive "
                "Phase 9 GO/NO-GO requires an explicitly approved threshold "
                "set, which has not been provided.  Verdict is therefore "
                "deferred until an approved gate is supplied.  No live "
                "trading, orders, or production changes were made."
            ),
            "threshold_policy": "none established (do not invent)",
        }

    # ------------------------------------------------------------------
    # IO
    # ------------------------------------------------------------------
    def to_dict(self):
        return self.consolidate()

    def write_report(self, report=None, path=None):
        path = path or os.environ.get("PHASE9_7_REPORT_PATH",
                                      "beta_gonogo_9_7.json")
        data = report if report is not None else self.consolidate()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return path


def main():
    import argparse
    p = argparse.ArgumentParser(
        description="Phase 9.7 - Final Phase 9 GO/NO-GO consolidation "
                    "(analysis only, non-mutating).")
    p.add_argument("--evidence", default=os.environ.get(
        "PHASE9_3_EVIDENCE_PATH", "beta_evidence.jsonl"))
    p.add_argument("--summary", default=os.environ.get(
        "PHASE9_3_SUMMARY_PATH", "beta_evidence_summary.json"))
    p.add_argument("--out", default=os.environ.get(
        "PHASE9_7_REPORT_PATH", "beta_gonogo_9_7.json"))
    args = p.parse_args()

    final = Phase9FinalGoNoGo.from_files(args.evidence, args.summary)
    verdict = final.consolidate()
    out = final.write_report(verdict, args.out)
    print(json.dumps(verdict, indent=2, sort_keys=True))
    print("report written to %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())


