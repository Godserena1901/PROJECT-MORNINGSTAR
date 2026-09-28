import pytest
from unittest.mock import Mock

from binance_service import BinanceService


def test_place_order_dry_run_preserves_parameters_and_does_not_call_network(monkeypatch):
    service = BinanceService(
        api_key="key",
        api_secret="secret",
        dry_run=True,
        live_trading_enabled=False,
    )
    network_called = False

    def fail_if_called(*args, **kwargs):
        nonlocal network_called
        network_called = True
        raise AssertionError("A real Binance order/network call must not occur.")

    monkeypatch.setattr(service.session, "request", fail_if_called)
    monkeypatch.setattr(service.session, "get", fail_if_called)
    monkeypatch.setattr(service.session, "post", fail_if_called)

    result = service.place_order(
        symbol="BTC",
        side="BUY",
        quantity=0.25,
        price=60000.0,
        stop_loss=59000.0,
        take_profit=62000.0,
    )

    # The DRY_RUN branch returns a superset execution report (simulated order id,
    # status label, execution fields, ...). Assert the requested parameters are
    # preserved rather than requiring exact dict equality with the whole report.
    expected = {
        "status": "SIMULATED",
        "dry_run": True,
        "symbol": "BTC",
        "side": "BUY",
        "quantity": 0.25,
        "price": 60000.0,
        "stop_loss": 59000.0,
        "take_profit": 62000.0,
    }
    assert {key: result[key] for key in expected} == expected
    assert result["success"] is True
    assert result["status_label"] == "simulated"
    assert result["executed_quantity"] == 0.0
    assert result["execution_price"] == 60000.0
    assert str(result["order_id"]).startswith("SIM-")
    assert network_called is False


def test_place_order_non_dry_run_is_blocked_when_live_trading_disabled():
    service = BinanceService(
        dry_run=False,
        live_trading_enabled=False,
    )

    with pytest.raises(RuntimeError, match="LIVE TRADING IS DISABLED"):
        service.place_order(
            symbol="BTC",
            side="BUY",
            quantity=1.0,
            price=100.0,
        )


def test_place_order_non_dry_run_submits_signed_order_when_live_trading_enabled(monkeypatch):
    """Live order placement is implemented (Phase 3.2c), NOT a NotImplementedError.

    When DRY_RUN is off and the live-trading shield is explicitly enabled,
    place_order() submits through the signed /api/v3/order endpoint and returns a
    structured execution report. The exchange session is fully mocked here, so no
    real order (and no real network call) can ever be sent by this test.
    """
    service = BinanceService(
        api_key="key",
        api_secret="secret",
        base_url="https://api.binance.com",
        dry_run=False,
        live_trading_enabled=True,
    )
    monkeypatch.setattr(
        service,
        "get_symbol_rules",
        lambda symbol: {
            "success": True,
            "step_size": 0.01,
            "min_qty": 0.01,
            "max_qty": 1000.0,
            "tick_size": 0.01,
            "min_price": 0.01,
            "max_price": 1_000_000.0,
            "min_notional": 5.0,
        },
    )

    def forbid_network(*args, **kwargs):
        raise AssertionError("A real Binance network call must not occur.")

    monkeypatch.setattr(service.session, "request", forbid_network)
    monkeypatch.setattr(service.session, "get", forbid_network)

    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "symbol": "BTCUSDT",
        "orderId": 12345,
        "clientOrderId": "test-client-order",
        "status": "FILLED",
        "executedQty": "1.0",
        "cummulativeQuoteQty": "100.0",
        "price": "100.0",
    }
    post_mock = Mock(return_value=response)
    monkeypatch.setattr(service.session, "post", post_mock)

    result = service.place_order(
        symbol="BTC",
        side="BUY",
        quantity=1.0,
        price=100.0,
    )

    assert result["success"] is True
    assert result["status"] == "FILLED"
    assert result["status_label"] == "filled"
    assert result["dry_run"] is False
    assert result["symbol"] == "BTCUSDT"
    assert result["side"] == "BUY"
    assert result["order_id"] == 12345
    assert result["executed_quantity"] == 1.0
    assert result["execution_price"] == 100.0

    post_mock.assert_called_once()
    assert post_mock.call_args.args[0] == "https://api.binance.com/api/v3/order"
    assert post_mock.call_args.kwargs["data"]["symbol"] == "BTCUSDT"


def test_validate_order_accepts_valid_quantity_price_and_notional(monkeypatch):
    service = BinanceService(dry_run=True, live_trading_enabled=False)
    monkeypatch.setattr(
        service,
        "get_symbol_rules",
        lambda symbol: {
            "success": True,
            "step_size": 0.01,
            "min_qty": 0.01,
            "max_qty": 1000.0,
            "tick_size": 0.01,
            "min_price": 0.01,
            "max_price": 1_000_000.0,
            "min_notional": 5.0,
        },
    )

    assert service.validate_order("BTC", quantity=1.0, price=10.0) == (True, None)


def test_validate_order_rejects_quantity_below_minimum(monkeypatch):
    service = BinanceService(dry_run=True, live_trading_enabled=False)
    monkeypatch.setattr(
        service,
        "get_symbol_rules",
        lambda symbol: {
            "success": True,
            "step_size": 0.01,
            "min_qty": 0.01,
            "max_qty": 1000.0,
            "tick_size": 0.01,
            "min_price": 0.01,
            "max_price": 1_000_000.0,
            "min_notional": 5.0,
        },
    )

    ok, error = service.validate_order("BTC", quantity=0.001, price=100.0)

    assert ok is False
    assert "below minimum allowed" in error


def test_validate_order_rejects_price_below_minimum(monkeypatch):
    service = BinanceService(dry_run=True, live_trading_enabled=False)
    monkeypatch.setattr(
        service,
        "get_symbol_rules",
        lambda symbol: {
            "success": True,
            "step_size": 0.01,
            "min_qty": 0.01,
            "max_qty": 1000.0,
            "tick_size": 0.01,
            "min_price": 10.0,
            "max_price": 1_000_000.0,
            "min_notional": 5.0,
        },
    )

    ok, error = service.validate_order("BTC", quantity=1.0, price=5.0)

    assert ok is False
    assert "below minimum" in error


def test_validate_order_rejects_notional_below_minimum(monkeypatch):
    service = BinanceService(dry_run=True, live_trading_enabled=False)
    monkeypatch.setattr(
        service,
        "get_symbol_rules",
        lambda symbol: {
            "success": True,
            "step_size": 0.01,
            "min_qty": 0.01,
            "max_qty": 1000.0,
            "tick_size": 0.01,
            "min_price": 0.01,
            "max_price": 1_000_000.0,
            "min_notional": 5.0,
        },
    )

    ok, error = service.validate_order("BTC", quantity=0.1, price=10.0)

    assert ok is False
    assert "less than the Binance minimum" in error
