#!/usr/bin/env python3
"""
Phase 3.4.2 - Verify Order Lifecycle & Status Transitions
(Project Morningstar / Allmightsee Prime - Paper -> Testnet Validation).

Safe paper/test validation of the EXISTING order-execution engine covering
how an order travels through its lifecycle:

  A. Pending (NEW) -> Filled (FILLED)            - placed, then confirmed filled
  B. Pending -> Partially filled -> Filled       - fill progression
  C. Partially filled -> Cancelled               - cancel after partial fill
  D. Pending -> Cancelled (cancel_order -> confirm)
  E. Expiry & edge states                        - EXPIRED, PENDING_NEW, REJECTED,
                                                   failed status lookup (clean error)
  F. Paper-side lifecycle (DRY_RUN default)      - place -> confirm -> cancel,
                                                   fully simulated, no network

SAFETY (identical to Phase 3.4.1):
  * no real-money orders, no Binance network calls, every network sink stubbed,
  * DRY_RUN=True default and live trading disabled are preserved unchanged,
  * no changes to verified Phase 3.1/3.2/3.3 functionality,
  * explicitly OUT of scope: TP/SL monitoring, position tracking, balance
    updates, reconnection testing, extended simulations.

Usage:
    python3 phase_3_4_2_verify_lifecycle.py
Exit code 0 = all checks passed; 1 = one or more checks failed.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from binance_service import BinanceService, binance_service

FAILURES = []


def check(name, condition, detail=""):
    """Record a single verification result."""
    print(("PASS" if condition else "FAIL"), "-", name, detail)
    if not condition:
        FAILURES.append(name)


class _NoNet:
    """Network sink that fails loudly - proves zero Binance contact."""

    def get(self, *a, **k):
        raise AssertionError("NETWORK GET ATTEMPTED during verification")

    def post(self, *a, **k):
        raise AssertionError("NETWORK POST ATTEMPTED during verification")

    def delete(self, *a, **k):
        raise AssertionError("NETWORK DELETE ATTEMPTED during verification")


class _RespQueue:
    """Canned-response store; a (method, path) key maps to a dict or a FIFO list."""

    def __init__(self, spec):
        self.spec = spec

    def next(self, method, path):
        item = self.spec.get((method, path))
        if item is None:
            return {"success": True, "data": {}, "http_status": 200}
        if isinstance(item, list):
            return item[0] if len(item) == 1 else item.pop(0)
        return item


class LifecycleEngine(BinanceService):
    """Engine subclass that records payloads and returns canned lifecycle responses."""

    def __init__(self, responses):
        # Dummy credentials - never used for real signing because
        # _signed_request below is overridden.
        super().__init__(
            api_key="VERIFY_ONLY_KEY",
            api_secret="VERIFY_ONLY_SECRET",
            live_trading_enabled=True,  # local instantiation ONLY (still stubbed)
            dry_run=False,              # exercises the real construction/status code
        )
        self.responses = _RespQueue(responses)
        self.captured = []  # (method, path, params) copies of outgoing requests
        self.session = _NoNet()  # network hard-blocked

    def _signed_request(self, method, path, params=None):
        self.captured.append((method, path, dict(params or {})))
        return self.responses.next(method, path)

    def format_quantity(self, symbol, quantity):
        return (True, round(float(quantity), 6), None)

    def format_price(self, symbol, price):
        return (True, round(float(price), 2), None)

    def validate_order(self, symbol, quantity, price):
        return (True, None)

    def get_current_price(self, symbol):
        return (True, 61000.0, None)


def _order(status, symbol="BTCUSDT", side="BUY", order_id=1001, otype="LIMIT",
           orig="0.00100000", execed="0.00000000", quote="0.00000000",
           price="61000.00000000", client="lc1"):
    """Build a Binance-style order snapshot dict for the canned responses."""
    return {
        "symbol": symbol, "side": side, "orderId": order_id, "type": otype,
        "status": status, "clientOrderId": client,
        "origQty": orig, "executedQty": execed,
        "cummulativeQuoteQty": quote, "price": price,
    }


def _ok(response_data):
    return {"success": True, "http_status": 200, "data": response_data}


def verify_lifecycle_scenarios():
    print("=" * 70)
    print("PART 1 - Order lifecycle & status transitions (network stubbed)")
    print("=" * 70)

    # -- A. Pending (NEW) -> Filled -------------------------------------
    eng = LifecycleEngine({
        ("POST", "/api/v3/order"): _ok(_order("NEW", otype="LIMIT")),
        ("GET", "/api/v3/order"): [
            _ok(_order("NEW")),
            _ok(_order("FILLED", execed="0.00100000", quote="61.00000000")),
        ],
    })
    placed = eng.place_order(symbol="BTC", side="BUY", quantity=0.001,
                             price=61000.0, order_type="LIMIT", time_in_force="GTC")
    param = eng.captured[0][2]
    check("A: LIMIT order placed as pending (NEW)",
          placed.get("status") == "NEW" and placed.get("status_label") == "pending"
          and placed.get("order_id") == 1001)
    check("A: limit payload has price + GTC", param.get("price") == 61000.0
          and param.get("timeInForce") == "GTC")
    s1 = eng.get_order_status("BTC", 1001)
    check("A: first confirmation still pending",
          s1.get("status") == "NEW" and s1.get("status_label") == "pending"
          and s1.get("executed_quantity") == 0.0)
    s2 = eng.get_order_status("BTC", 1001)
    check("A: second confirmation filled",
          s2.get("status") == "FILLED" and s2.get("status_label") == "filled"
          and s2.get("executed_quantity") == 0.001
          and abs((s2.get("execution_price") or 0) - 61.0 / 0.001) < 1e-9)
    check("A: status polls carried orderId",
          all(c[0] == "GET" and c[2].get("orderId") == 1001 for c in eng.captured[1:]))

    # -- B. Pending -> Partially filled -> Filled (fill progression) ------
    eng2 = LifecycleEngine({
        ("POST", "/api/v3/order"): _ok(_order("NEW", symbol="ETHUSDT", side="SELL",
                                              order_id=2002, otype="LIMIT",
                                              orig="0.10000000", price="3200.00000000")),
        ("GET", "/api/v3/order"): [
            _ok(_order("PARTIALLY_FILLED", symbol="ETHUSDT", side="SELL", order_id=2002,
                       otype="LIMIT", orig="0.10000000", execed="0.04000000",
                       quote="128.00000000", price="3200.00000000")),
            _ok(_order("PARTIALLY_FILLED", symbol="ETHUSDT", side="SELL", order_id=2002,
                       otype="LIMIT", orig="0.10000000", execed="0.07000000",
                       quote="224.50000000", price="3200.00000000")),
            _ok(_order("FILLED", symbol="ETHUSDT", side="SELL", order_id=2002,
                       otype="LIMIT", orig="0.10000000", execed="0.10000000",
                       quote="320.00000000", price="3200.00000000")),
        ],
    })
    eng2.place_order(symbol="ETH", side="SELL", quantity=0.1, price=3200.0,
                     order_type="LIMIT")
    p1 = eng2.get_order_status("ETH", 2002)
    p2 = eng2.get_order_status("ETH", 2002)
    f = eng2.get_order_status("ETH", 2002)
    check("B: partial fill step 1 (40%)",
          p1.get("status_label") == "partially_filled" and p1.get("executed_quantity") == 0.04)
    check("B: partial fill step 2 (70%)",
          p2.get("status_label") == "partially_filled" and p2.get("executed_quantity") == 0.07
          and abs((p2.get("execution_price") or 0) - 224.5 / 0.07) < 1e-9)
    check("B: final fill completes lifecycle",
          f.get("status_label") == "filled" and f.get("executed_quantity") == 0.1
          and abs((f.get("execution_price") or 0) - 3200.0) < 1e-9)

    # -- C. Partially filled -> Cancelled (partial fill preserved) --------
    eng3 = LifecycleEngine({
        ("POST", "/api/v3/order"): _ok(_order("NEW", symbol="ETHUSDT", order_id=3003,
                                              otype="LIMIT", orig="0.10000000",
                                              price="3200.00000000")),
        ("GET", "/api/v3/order"): [
            _ok(_order("PARTIALLY_FILLED", symbol="ETHUSDT", order_id=3003,
                       otype="LIMIT", orig="0.10000000", execed="0.05000000",
                       quote="160.00000000", price="3200.00000000")),
            _ok(_order("CANCELED", symbol="ETHUSDT", order_id=3003,
                       otype="LIMIT", orig="0.10000000", execed="0.05000000",
                       quote="160.00000000", price="3200.00000000")),
        ],
    })
    eng3.place_order(symbol="ETH", side="BUY", quantity=0.1, price=3200.0,
                     order_type="LIMIT")
    partial = eng3.get_order_status("ETH", 3003)
    cancelled = eng3.get_order_status("ETH", 3003)
    check("C: partial fill observed",
          partial.get("status_label") == "partially_filled" and partial.get("executed_quantity") == 0.05)
    check("C: cancelled after partial fill keeps executed qty",
          cancelled.get("status_label") == "cancelled"
          and cancelled.get("status") == "CANCELED"
          and cancelled.get("executed_quantity") == 0.05)

    # -- D. Pending -> cancel_order -> confirmed CANCELED ------------------
    eng4 = LifecycleEngine({
        ("POST", "/api/v3/order"): _ok(_order("NEW", order_id=4004, otype="LIMIT")),
        ("DELETE", "/api/v3/order"): _ok(
            {"symbol": "BTCUSDT", "side": "BUY", "orderId": 4004,
             "price": "61000.00000000", "origQty": "0.00100000",
             "executedQty": "0.00000000", "status": "CANCELED"}),
        ("GET", "/api/v3/order"): [
            _ok(_order("CANCELED", order_id=4004, otype="LIMIT")),
        ],
    })
    eng4.place_order(symbol="BTC", side="BUY", quantity=0.001, price=61000.0,
                     order_type="LIMIT")
    c = eng4.cancel_order("BTC", 4004)
    conf = eng4.get_order_status("BTC", 4004)
    check("D: cancel_order returns CANCELED",
          c.get("status") == "CANCELED" and c.get("status_label") == "cancelled")
    check("D: cancel payload targets signed DELETE endpoint",
          any(cmd[0] == "DELETE" and cmd[1] == "/api/v3/order"
              and cmd[2].get("orderId") == 4004 for cmd in eng4.captured))
    check("D: status confirmation shows cancelled",
          conf.get("status") == "CANCELED" and conf.get("status_label") == "cancelled")

    # -- E. Expiry & edge states + failed lookup ---------------------------
    st = LifecycleEngine({("GET", "/api/v3/order"): _ok(_order("EXPIRED"))}).get_order_status("BTC", 1001)
    check("E: EXPIRED normalized to cancelled",
          st.get("status") == "EXPIRED" and st.get("status_label") == "cancelled")

    st = LifecycleEngine({("GET", "/api/v3/order"): _ok(_order("PENDING_NEW"))}).get_order_status("BTC", 1001)
    check("E: PENDING_NEW treated as pending", st.get("status_label") == "pending")

    st = LifecycleEngine({("GET", "/api/v3/order"): _ok(_order("REJECTED"))}).get_order_status("BTC", 1001)
    check("E: REJECTED status lookup normalized",
          st.get("status") == "REJECTED" and st.get("status_label") == "rejected")

    st = LifecycleEngine({
        ("GET", "/api/v3/order"): {"success": False, "http_status": 400, "code": -2013,
                                   "error": "Binance API Error (400): Order does not exist."},
    }).get_order_status("BTC", 999999)
    check("E: failed status lookup returns clean structured error",
          st.get("success") is False and st.get("status") is None
          and "does not exist" in st.get("error", "").lower()
          and st.get("status_label") == "unknown")


def verify_paper_side_lifecycle():
    print()
    print("=" * 70)
    print("PART 2 - Paper-side lifecycle via engine (default DRY_RUN config)")
    print("=" * 70)

    svc = binance_service  # existing default instance (from .env)
    check("P2: default DRY_RUN=True", svc.dry_run is True, f"(dry_run={svc.dry_run})")
    check("P2: live trading disabled", svc.live_trading_enabled is False)
    svc.session = _NoNet()  # hard-block any network

    placed = svc.place_order(symbol="BTC", side="BUY", quantity=0.001, price=61000.0,
                             stop_loss=59000.0, take_profit=63000.0)
    oid = placed.get("order_id")
    check("P2: paper entry lifecycle starts SIMULATED",
          placed.get("status") == "SIMULATED" and placed.get("dry_run") is True
          and str(oid).startswith("SIM-"),
          f"(status={placed.get('status')}, order_id={oid})")
    confirmed = svc.get_order_status("BTC", oid)
    check("P2: paper order confirmation simulated",
          confirmed.get("status") == "SIMULATED" and confirmed.get("dry_run") is True)
    cancelled = svc.cancel_order("BTC", oid)
    check("P2: paper order cancel simulated + cancelled",
          cancelled.get("status") == "CANCELED"
          and cancelled.get("status_label") == "cancelled"
          and cancelled.get("dry_run") is True)


def main():
    print("Phase 3.4.2 - Verify Order Lifecycle & Status Transitions (paper/test)")
    print("No real orders, no network, no config changes. DRY_RUN default preserved.")
    verify_lifecycle_scenarios()
    verify_paper_side_lifecycle()
    print()
    print("=" * 70)
    if FAILURES:
        print(f"VERIFY LIFECYCLE: {len(FAILURES)} FAILURE(S) -> {FAILURES}")
        sys.exit(1)
    print("VERIFY LIFECYCLE: ALL CHECKS PASSED (Phase 3.4.2)")
    sys.exit(0)


if __name__ == "__main__":
    main()