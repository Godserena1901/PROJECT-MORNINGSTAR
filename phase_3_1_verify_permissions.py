#!/usr/bin/env python3
"""
Phase 3.1 - Verify API Permissions (Requirement 5)
(Project Morningstar / Allmightsee Prime - Exchange Integration).

Minimal offline verification of BinanceService.assert_api_permissions().

The method must:
  * use get_account_info() to verify canTrade (Requirement 5),
  * enforce the read-only mandate (withdrawal/transfer disabled),
  * report structured success / blocked results,
  * never place an order and never alter LIVE_TRADING_ENABLED or DRY_RUN.

SAFETY:
  * no real-money orders, no Binance network calls (get_account_info stubbed),
  * DRY_RUN=True default and live trading disabled remain intact.

Usage:
    python3 phase_3_1_verify_permissions.py
Exit code 0 = all checks passed; 1 = one or more checks failed.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from binance_service import BinanceService  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    """Record a single verification result."""
    print(("PASS" if condition else "FAIL"), "-", name, detail)
    if not condition:
        FAILURES.append(name)


class _FakeInfo(BinanceService):
    """Inject canned get_account_info() responses without any network."""

    def __init__(self, configured, payload=None):
        # Dummy credentials; is_configured is forced below. Live/DRY flags
        # intentionally mirror the safe defaults so we can assert no change.
        super().__init__(api_key="k", api_secret="s", dry_run=True,
                         live_trading_enabled=False)
        self._configured = configured
        self._payload = payload

    @property
    def is_configured(self):
        return self._configured

    def get_account_info(self):
        if not self._configured:
            return {
                "success": False,
                "error": "Binance API Key and Secret are not configured in environment variables.",
                "data": None,
            }
        return {"success": True, "error": None, "data": self._payload}


def verify_permissions():
    print("=" * 70)
    print("Phase 3.1 - Verify API Permissions (Requirement 5)")
    print("=" * 70)

    # -- 1. Not configured: blocked cleanly, no crash ------------------------
    svc = _FakeInfo(configured=False)
    r = svc.assert_api_permissions()
    check("1: not configured -> blocked", r.get("success") is False
          and "not configured" in r.get("error", "").lower()
          and r.get("can_trade") is None and r.get("withdraw_allowed") is None,
          f"(error={r.get('error')})")

    # -- 2. canTrade=False -> blocked ----------------------------------------
    svc = _FakeInfo(configured=True, payload={"canTrade": False,
                                              "withdrawAllEnabled": False,
                                              "accountType": "SPOT"})
    r = svc.assert_api_permissions()
    check("2: canTrade=False -> blocked", r.get("success") is False
          and r.get("can_trade") is False and "canTrade" in r.get("error", ""),
          f"(error={r.get('error')})")

    # -- 3. canTrade=True but withdrawals enabled -> read-only blocked -------
    svc = _FakeInfo(configured=True, payload={"canTrade": True,
                                              "withdrawAllEnabled": True,
                                              "accountType": "SPOT"})
    r = svc.assert_api_permissions()
    check("3: withdrawal enabled -> blocked (read-only mandate)",
          r.get("success") is False and r.get("can_trade") is True
          and r.get("withdraw_allowed") is True
          and "read-only" in r.get("error", "").lower(),
          f"(error={r.get('error')})")

    # -- 4. canTrade=True and withdrawal disabled -> OK ----------------------
    svc = _FakeInfo(configured=True, payload={"canTrade": True,
                                              "withdrawAllEnabled": False,
                                              "accountType": "SPOT"})
    r = svc.assert_api_permissions()
    check("4: canTrade=True + withdrawal disabled -> OK",
          r.get("success") is True and r.get("error") is None
          and r.get("can_trade") is True
          and r.get("withdraw_allowed") is False
          and r.get("account_type") == "SPOT")

    # -- 5. Safety: method is read-only; no order, no flag change ------------
    svc = _FakeInfo(configured=True, payload={"canTrade": True,
                                              "withdrawAllEnabled": False,
                                              "accountType": "SPOT"})
    # Force flags to safe defaults and confirm they are untouched afterward.
    svc.dry_run = True
    svc.live_trading_enabled = False
    calls = {"place": 0}
    orig_place = svc.place_order
    def counting_place(*a, **k):
        calls["place"] += 1
        return orig_place(*a, **k)
    svc.place_order = counting_place
    r = svc.assert_api_permissions()
    check("5: dry_run stays True", svc.dry_run is True)
    check("5: live_trading stays disabled", svc.live_trading_enabled is False)
    check("5: no order was placed", calls["place"] == 0)
    svc.place_order = orig_place


def run():
    print("Phase 3.1 - Verify API Permissions (offline/test)")
    print("No real orders, no network, no config changes. Safety flags intact.")
    verify_permissions()
    print()
    print("=" * 70)
    if FAILURES:
        print(f"VERIFY PERMISSIONS: {len(FAILURES)} FAILURE(S) -> {FAILURES}")
        sys.exit(1)
    print("VERIFY PERMISSIONS: ALL CHECKS PASSED (Phase 3.1, Requirement 5)")
    sys.exit(0)


if __name__ == "__main__":
    run()