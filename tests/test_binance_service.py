import pytest

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
    monkeypatch.setattr(service.session, "post", fail_if_called)

    result = service.place_order(
        symbol="BTC",
        side="BUY",
        quantity=0.25,
        price=60000.0,
        stop_loss=59000.0,
        take_profit=62000.0,
    )

    assert result == {
        "status": "SIMULATED",
        "dry_run": True,
        "symbol": "BTC",
        "side": "BUY",
        "quantity": 0.25,
        "price": 60000.0,
        "stop_loss": 59000.0,
        "take_profit": 62000.0,
    }
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


def test_place_order_non_dry_run_preserves_existing_not_implemented_behavior():
    service = BinanceService(
        dry_run=False,
        live_trading_enabled=True,
    )

    with pytest.raises(NotImplementedError, match="Live order placement has not been implemented"):
        service.place_order(
            symbol="BTC",
            side="BUY",
            quantity=1.0,
            price=100.0,
        )


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
