#!/usr/bin/env python3
"""
Phase 3.4.1 — Verify Orders (Project Morningstar / Allmightsee Prime).

Safe paper/test validation of the EXISTING order-execution path ONLY.

This harness verifies, against the existing BinanceService engine and the
existing paper/test configuration:

  Part 1 - DRY_RUN pathway (default config, network blocked):
           * the engine receives expected BUY / SELL order information
             (symbol, side, quantity, price, order type),
           * orders are simulated/logged with a simulated order ID (status
             SIMULATED) and never reach the Binance network,
           * simulated status confirmation (get_order_status) and simulated
             cancellation (cancel_order) behave safely.

  Part 2 - Request construction & response/status handling:
           * the exact POST /api/v3/order payload built by place_order() for
             MARKET and LIMIT orders (symbol, side, type, quantity, + price,
             timeInForce for LIMIT),
           * normalized handling of successful (FILLED), partially filled
             (PARTIALLY_FILLED), rejected (REJECTED), cancelled (CANCELED)
             and pending (NEW) order statuses,
           * risk-control enforcement: invalid side, missing LIMIT price and
             below-minimum quantity are rejected BEFORE any request is built.

SAFETY:
  * no real-money orders, no Binance network calls, no live trading enabled,
  * no changes to configuration or API permissions,
  * does NOT touch TP/SL monitoring, position tracking, balance updates,
    reconnect tests or extended simulations (out of Phase 3.4.1 scope).

Usage:
    python3 phase_3_4_1_verify_orders.py
Exit code 0 = all checks passed; 1 = one or more checks failed.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from binance_service import BinanceService, binance_service, ORDER_STATUS_LABELS

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


# ============================================================================
# PART 1 - DRY_RUN pathway using the existing default (paper) configuration
# ============================================================================
def verify_dry_run_pathway():
    print("=" * 70)
    print("PART 1 - DRY_RUN pathway (existing default config, network blocked)")
    print("=" * 70)

    svc = binance_service  # module-level default instance (from .env)

    check("default instance has DRY_RUN=True", svc.dry_run is True, f"(dry_run={svc.dry_run})")
    check("default instance has live trading disabled", svc.live_trading_enabled is False)

    # Hard block all network so a network call fails the harness outright.
    svc.session = _NoNet()

    buy = svc.place_order(
        symbol="BTC", side="BUY", quantity=0.001, price=61000.0,
        stop_loss=59000.0, take_profit=63000.0,
    )
    check("DRY_RUN BUY status SIMULATED", buy.get("status") == "SIMULATED", f"(status={buy.get('status')})")
    check("DRY_RUN BUY flagged dry_run", buy.get("dry_run") is True)
    check("DRY_RUN BUY has simulated order ID", str(buy.get("order_id", "")).startswith("SIM-"))
    check("DRY_RUN BUY carries engine inputs",
          buy.get("symbol") == "BTC" and buy.get("side") == "BUY"
          and buy.get("quantity") == 0.001 and buy.get("price") == 61000.0,
          f"(side={buy.get('side')}, qty={buy.get('quantity')}, price={buy.get('price')})")
    check("DRY_RUN BUY carries SL/TP", buy.get("stop_loss") == 59000.0 and buy.get("take_profit") == 63000.0)

    sell = svc.place_order(
        symbol="ETH", side="SELL", quantity=0.1, price=3200.0,
        order_type="LIMIT", time_in_force="IOC",
    )
    check("DRY_RUN SELL LIMIT SIMULATED",
          sell.get("status") == "SIMULATED" and sell.get("order_type") == "LIMIT",
          f"(type={sell.get('order_type')})")

    confirmed = svc.get_order_status("BTC", buy["order_id"])
    check("DRY_RUN status confirmation simulated",
          confirmed.get("status") == "SIMULATED" and confirmed.get("dry_run") is True)

    cancelled = svc.cancel_order("BTC", buy["order_id"])
    check("DRY_RUN cancel simulated + classified cancelled",
          cancelled.get("status") == "CANCELED"
          and cancelled.get("status_label") == "cancelled")

    expected_labels = {
        "NEW": "pending", "PENDING_NEW": "pending",
        "PARTIALLY_FILLED": "partially_filled",
        "FILLED": "filled",
        "CANCELED": "cancelled", "PENDING_CANCEL": "cancelled",
        "EXPIRED": "cancelled", "REJECTED": "rejected",
    }
    mapping_ok = all(svc.classify_order_status(k) == v for k, v in expected_labels.items())
    check("classify_order_status maps 8 Binance states", mapping_ok)
    check("unknown status falls back to 'unknown'",
          svc.classify_order_status("BOGUS") == "unknown"
          and svc.classify_order_status(None) == "unknown")


# ============================================================================
# PART 2 - Request construction & response/status handling (network stubbed)
# ============================================================================
# Engages the engine's real code paths while every network sink is stubbed:
# outgoing requests are captured, responses are canned. No real order, no real
# credentials, no live-trading configuration is enabled.
# ============================================================================
class VerifyEngine(BinanceService):
    """Engine subclass that records payloads and returns canned responses."""

    def __init__(self, responses):
        # Dummy credentials - never used for real signing because
        # _signed_request below is overridden.
        super().__init__(
            api_key="VERIFY_ONLY_KEY",
            api_secret="VERIFY_ONLY_SECRET",
            live_trading_enabled=True,  # local instantiation ONLY (still stubbed)
            dry_run=False,              # exercises the real construction/status code
        )
        self.responses = responses
        self.captured = []  # (method, path, params) copies of outgoing requests
        self.session = _NoNet()  # network hard-blocked

    def _signed_request(self, method, path, params=None):
        self.captured.append((method, path, dict(params or {})))
        return self.responses.get(
            (method, path),
            {"success": True, "data": {}, "http_status": 200},
        )

    def format_quantity(self, symbol, quantity):
        return (True, round(float(quantity), 6), None)

    def format_price(self, symbol, price):
        return (True, round(float(price), 2), None)

    def validate_order(self, symbol, quantity, price):
        return (True, None)

    def get_current_price(self, symbol):
        return (True, 61000.0, None)


def _filled_market():
    return {
        ("POST", "/api/v3/order"): {
            "success": True,
            "http_status": 200,
            "data": {
                "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET",
                "orderId": 123456789, "clientOrderId": "abc123",
                "status": "FILLED", "origQty": "0.00100000",
                "price": "0.00000000", "executedQty": "0.00100000",
                "cummulativeQuoteQty": "61.00000000",
            },
        }
    }


def verify_request_construction_and_statuses():
    print()
    print("=" * 70)
    print("PART 2 - Request construction & response/status handling (stubbed)")
    print("=" * 70)

    # -- 2a. MARKET BUY: exact payload + FILLED handling + avg fill price ----
    eng = VerifyEngine(_filled_market())
    res = eng.place_order(symbol="BTC", side="BUY", quantity=0.001, price=61000.0)
    expected = {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": 0.001}
    check("MARKET BUY request constructed exactly",
          len(eng.captured) == 1 and eng.captured[0][0] == "POST"
          and eng.captured[0][1] == "/api/v3/order"
          and eng.captured[0][2] == expected,
          f"(payload={eng.captured[0][2] if eng.captured else None})")
    check("engine received BUY order info", res.get("side") == "BUY"
          and res.get("symbol") == "BTCUSDT" and res.get("quantity") == 0.001)
    check("FILLED response handled", res.get("status") == "FILLED"
          and res.get("status_label") == "filled" and res.get("order_id") == 123456789)
    check("avg fill price computed from fills",
          abs((res.get("execution_price") or 0) - 61.0 / 0.001) < 1e-9,
          f"(price={res.get('execution_price')})")
    check("filled report flags not dry_run", res.get("dry_run") is False)

    # -- 2b. LIMIT SELL: price rounding + timeInForce in payload ------------
    eng2 = VerifyEngine({
        ("POST", "/api/v3/order"): {
            "success": True, "http_status": 200,
            "data": {"symbol": "ETHUSDT", "side": "SELL", "type": "LIMIT",
                     "orderId": 987, "status": "FILLED",
                     "price": "3200.12000000", "executedQty": "0.10000000",
                     "cummulativeQuoteQty": "320.01200000",
                     "origQty": "0.10000000"},
        },
    })
    res = eng2.place_order(symbol="ETH", side="SELL", quantity=0.1,
                           price=3200.12345, order_type="LIMIT", time_in_force="IOC")
    param = eng2.captured[0][2]
    check("LIMIT SELL payload has price + timeInForce",
          param.get("symbol") == "ETHUSDT" and param.get("side") == "SELL"
          and param.get("type") == "LIMIT" and param.get("price") == 3200.12
          and param.get("timeInForce") == "IOC", f"(payload={param})")
    check("LIMIT order filled report", res.get("status") == "FILLED"
          and res.get("order_id") == 987)

    # -- 2c. PARTIALLY_FILLED response -> classified correctly --------------
    eng3 = VerifyEngine({
        ("POST", "/api/v3/order"): {
            "success": True, "http_status": 200,
            "data": {"symbol": "ETHUSDT", "side": "SELL", "type": "LIMIT",
                     "orderId": 555, "status": "PARTIALLY_FILLED",
                     "price": "3200.00000000", "executedQty": "0.05000000",
                     "cummulativeQuoteQty": "160.00000000",
                     "origQty": "0.10000000"},
        },
    })
    res = eng3.place_order(symbol="ETH", side="SELL", quantity=0.1,
                           price=3200.0, order_type="LIMIT")
    check("PARTIALLY_FILLED handled",
          res.get("status") == "PARTIALLY_FILLED"
          and res.get("status_label") == "partially_filled"
          and res.get("executed_quantity") == 0.05
          and abs((res.get("execution_price") or 0) - 160.0 / 0.05) < 1e-9)

    # -- 2d. REJECTED (API-level) -> rejected execution report ---------------
    eng4 = VerifyEngine({
        ("POST", "/api/v3/order"): {
            "success": False, "http_status": 400, "code": -2010,
            "error": "Binance API Error (400): "
                     "Account has insufficient balance for requested action.",
        },
    })
    res = eng4.place_order(symbol="BTC", side="BUY", quantity=0.001, price=61000.0)
    check("API rejection mapped to REJECTED report",
          res.get("status") == "REJECTED" and res.get("status_label") == "rejected"
          and res.get("success") is False
          and "insufficient balance" in res.get("error", "").lower())
    check("rejected request WAS constructed (server rejected it)",
          len(eng4.captured) == 1 and eng4.captured[0][1] == "/api/v3/order")

    # -- 2e. CANCELED via cancel_order -> cancelled report -------------------
    eng5 = VerifyEngine({
        ("DELETE", "/api/v3/order"): {
            "success": True, "http_status": 200,
            "data": {"symbol": "BTCUSDT", "side": "BUY", "orderId": 555,
                     "price": "60000.00000000", "origQty": "0.00100000",
                     "executedQty": "0.00000000", "status": "CANCELED"},
        },
    })
    res = eng5.cancel_order("BTC", 555)
    check("CANCELED order classified cancelled",
          res.get("status") == "CANCELED" and res.get("status_label") == "cancelled"
          and res.get("success") is True and res.get("dry_run") is False)
    check("cancel request targets signed DELETE endpoint",
          eng5.captured[0][0] == "DELETE" and eng5.captured[0][1] == "/api/v3/order"
          and eng5.captured[0][2].get("symbol") == "BTCUSDT"
          and eng5.captured[0][2].get("orderId") == 555)

    # -- 2f. Pre-submission risk-control rejections (no request leaves) ------
    eng6 = VerifyEngine(_filled_market())
    res = eng6.place_order(symbol="BTC", side="HOLD", quantity=0.001)
    check("invalid side rejected BEFORE submission",
          res.get("status") == "REJECTED" and res.get("success") is False
          and eng6.captured == [], f"(captured={len(eng6.captured)})")

    eng7 = VerifyEngine(_filled_market())
    res = eng7.place_order(symbol="BTC", side="BUY", quantity=0.001, order_type="LIMIT")
    check("missing LIMIT price rejected BEFORE submission",
          res.get("status") == "REJECTED" and res.get("success") is False
          and eng7.captured == [])

    class UnderMinQtyEngine(VerifyEngine):
        def format_quantity(self, symbol, quantity):
            return (False, None, f"Quantity below minimum allowed for {symbol}.")

    eng8 = UnderMinQtyEngine(_filled_market())
    res = eng8.place_order(symbol="BTC", side="BUY", quantity=0.000000001)
    check("below-minimum quantity rejected BEFORE submission",
          res.get("status") == "REJECTED" and res.get("success") is False
          and "below minimum" in res.get("error", "").lower()
          and eng8.captured == [])

    # -- 2g. get_order_status normalization across known states --------------
    def status_engine(raw_status, extra=None):
        data = {"symbol": "ETHUSDT", "side": "BUY", "orderId": 777,
                "price": "3200.00000000", "origQty": "0.10000000",
                "executedQty": "0.00000000", "status": raw_status}
        if extra:
            data.update(extra)
        return VerifyEngine({("GET", "/api/v3/order"): {"success": True, "http_status": 200, "data": data}})

    st = status_engine("NEW").get_order_status("ETH", 777)
    check("pending order status normalized", st.get("status") == "NEW"
          and st.get("status_label") == "pending")
    st = status_engine("PARTIALLY_FILLED", {"executedQty": "0.05000000", "cummulativeQuoteQty": "160.00000000"}).get_order_status("ETH", 777)
    check("partial order status normalized", st.get("status_label") == "partially_filled")
    st = status_engine("CANCELED").get_order_status("ETH", 777)
    check("cancelled order status normalized", st.get("status_label") == "cancelled")
    st = status_engine("REJECTED").get_order_status("ETH", 777)
    check("rejected order status normalized", st.get("status_label") == "rejected")
    st = status_engine("EXPIRED").get_order_status("ETH", 777)
    check("expired order status normalized to cancelled", st.get("status_label") == "cancelled")


def main():
    print("Phase 3.4.1 - Verify Orders (paper/test validation)")
    print("No real orders, no network, no config changes. DRY_RUN default preserved.")
    verify_dry_run_pathway()
    verify_request_construction_and_statuses()
    print()
    print("=" * 70)
    if FAILURES:
        print(f"VERIFY ORDERS: {len(FAILURES)} FAILURE(S) -> {FAILURES}")
        sys.exit(1)
    print("VERIFY ORDERS: ALL CHECKS PASSED (Phase 3.4.1)")
    sys.exit(0)


if __name__ == "__main__":
    main()