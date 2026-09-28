import pandas as pd
import pytest
from unittest.mock import Mock

import main


@pytest.fixture(autouse=True)
def reset_paper_state(monkeypatch):
    monkeypatch.setattr(main, "PAPER_BALANCE", main.INITIAL_PAPER_BALANCE)
    monkeypatch.setattr(main, "PAPER_POSITIONS", {})
    monkeypatch.setattr(main, "PAPER_TRADE_HISTORY", [])


def make_analysis(symbol="BTC", signal="BUY", entry=100.0, stop_loss=99.0, take_profit=101.0):
    return {
        "symbol": symbol,
        "signal": signal,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "confidence": 80,
    }


def make_indicator_frame(close=100.0, ema20=95.0, rsi=50.0, macd=2.0,
                         macd_signal=1.0, macd_histogram=1.0, atr=2.0,
                         volume=200.0, average_volume=100.0, rows=20):
    """Build an indicator frame whose newest candle carries the given values.

    ``analyze_symbol()`` compares the latest ``volume`` against the trailing
    20-candle mean (``df["volume"].tail(20).mean()``), so the frame needs several
    candles: the older ``rows - 1`` candles use ``average_volume`` and the newest
    candle uses ``volume``. A single-row frame can never satisfy
    ``volume > average_volume`` because the mean would equal ``volume`` itself.
    """
    base = {
        "close": close,
        "ema20": ema20,
        "rsi": rsi,
        "macd": macd,
        "macd_signal": macd_signal,
        "macd_histogram": macd_histogram,
        "atr": atr,
        "volume": average_volume,
    }
    frame = [dict(base) for _ in range(rows - 1)]
    frame.append({**base, "volume": volume})
    return pd.DataFrame(frame)


def test_open_paper_trade_creates_buy_position(monkeypatch):
    monkeypatch.setattr(
        main.binance_service,
        "format_price",
        lambda symbol, value: (True, value, None),
    )
    monkeypatch.setattr(
        main.binance_service,
        "format_quantity",
        lambda symbol, value: (True, value, None),
    )
    monkeypatch.setattr(main.binance_service, "live_trading_enabled", False)

    analysis = make_analysis()

    assert main.open_paper_trade(analysis) is True
    assert "BTC" in main.PAPER_POSITIONS
    position = main.PAPER_POSITIONS["BTC"]
    assert position["side"] == "BUY"
    assert position["entry"] == 100.0
    assert position["stop_loss"] == 99.0
    assert position["take_profit"] == 101.0
    assert position["quantity"] == 1.0
    assert position["position_size_usd"] == 100.0
    assert position["status"] == "OPEN"


def test_open_paper_trade_rejects_hold_and_duplicate(monkeypatch):
    monkeypatch.setattr(
        main.binance_service,
        "format_price",
        lambda symbol, value: (True, value, None),
    )
    monkeypatch.setattr(
        main.binance_service,
        "format_quantity",
        lambda symbol, value: (True, value, None),
    )

    assert main.open_paper_trade(make_analysis(signal="HOLD")) is False
    assert main.open_paper_trade(make_analysis()) is True
    assert main.open_paper_trade(make_analysis()) is False


def test_open_paper_trade_is_blocked_when_live_trading_enabled(monkeypatch):
    monkeypatch.setattr(main.binance_service, "live_trading_enabled", True)
    assert main.open_paper_trade(make_analysis()) is False
    assert main.PAPER_POSITIONS == {}


def test_check_paper_positions_closes_buy_at_take_profit_and_updates_balance(monkeypatch):
    main.PAPER_POSITIONS["BTC"] = {
        "symbol": "BTC",
        "side": "BUY",
        "entry": 100.0,
        "stop_loss": 95.0,
        "take_profit": 105.0,
        "quantity": 2.0,
        "confidence": 80,
        "status": "OPEN",
    }
    monkeypatch.setattr(
        main.binance_service,
        "get_current_price",
        lambda symbol: (True, 105.0, None),
    )

    main.check_paper_positions()

    assert main.PAPER_POSITIONS == {}
    assert main.PAPER_BALANCE == 1010.0
    assert len(main.PAPER_TRADE_HISTORY) == 1
    trade = main.PAPER_TRADE_HISTORY[0]
    assert trade["status"] == "TAKE_PROFIT"
    assert trade["exit"] == 105.0
    assert trade["pnl"] == 10.0


def test_check_paper_positions_closes_sell_at_stop_loss(monkeypatch):
    main.PAPER_POSITIONS["ETH"] = {
        "symbol": "ETH",
        "side": "SELL",
        "entry": 100.0,
        "stop_loss": 105.0,
        "take_profit": 95.0,
        "quantity": 2.0,
        "confidence": 70,
        "status": "OPEN",
    }
    monkeypatch.setattr(
        main.binance_service,
        "get_current_price",
        lambda symbol: (True, 105.0, None),
    )

    main.check_paper_positions()

    assert main.PAPER_POSITIONS == {}
    assert main.PAPER_BALANCE == 990.0
    assert main.PAPER_TRADE_HISTORY[0]["status"] == "STOP_LOSS"
    assert main.PAPER_TRADE_HISTORY[0]["pnl"] == -10.0


def test_get_paper_statistics_returns_zero_state():
    stats = main.get_paper_statistics()

    assert stats["total_trades"] == 0
    assert stats["wins"] == 0
    assert stats["losses"] == 0
    assert stats["win_rate"] == 0.0
    assert stats["total_pnl"] == 0.0
    assert stats["profit_factor"] == 0.0
    assert stats["recent_trades"] == []


def test_get_paper_statistics_calculates_pnl_and_directional_metrics():
    main.PAPER_TRADE_HISTORY.extend([
        {"symbol": "BTC", "side": "BUY", "status": "TAKE_PROFIT", "pnl": 20.0},
        {"symbol": "ETH", "side": "BUY", "status": "STOP_LOSS", "pnl": -10.0},
        {"symbol": "SOL", "side": "SELL", "status": "TAKE_PROFIT", "pnl": 5.0},
        {"symbol": "BNB", "side": "SELL", "status": "STOP_LOSS", "pnl": -5.0},
    ])

    stats = main.get_paper_statistics()

    assert stats["total_trades"] == 4
    assert stats["wins"] == 2
    assert stats["losses"] == 2
    assert stats["win_rate"] == 50.0
    assert stats["total_pnl"] == 10.0
    assert stats["average_pnl"] == 2.5
    assert stats["gross_profit"] == 25.0
    assert stats["gross_loss"] == -15.0
    assert stats["profit_factor"] == pytest.approx(25 / 15)
    assert stats["best_trade"] == 20.0
    assert stats["worst_trade"] == -10.0
    assert stats["avg_win"] == 12.5
    assert stats["avg_loss"] == -7.5
    assert stats["buy_trades"] == 2
    assert stats["buy_wins"] == 1
    assert stats["sell_trades"] == 2
    assert stats["sell_wins"] == 1
    assert len(stats["recent_trades"]) == 4


def test_get_klines_builds_dataframe_and_indicators(monkeypatch):
    # Raw HTTP retrieval + OHLCV parsing is delegated to binance_service.get_klines()
    # (Phase 4); main.get_klines() consumes that parsed frame and attaches
    # indicators, so the delegation boundary is what gets mocked here.
    raw_klines = pd.DataFrame({
        "open_time": [1_700_000_000_000 + i * 60_000 for i in range(30)],
        "open": [float(99 + i) for i in range(30)],
        "high": [float(101 + i) for i in range(30)],
        "low": [float(98 + i) for i in range(30)],
        "close": [float(100 + i) for i in range(30)],
        "volume": [10.0 + i for i in range(30)],
    })

    delegated = Mock(return_value=(True, raw_klines, None))
    monkeypatch.setattr(main.binance_service, "get_klines", delegated)

    df = main.get_klines("BTC", interval="15m", limit=30)

    assert len(df) == 30
    for column in [
        "close", "high", "low", "volume",
        "ema20", "rsi", "macd", "macd_signal",
        "macd_histogram", "atr",
    ]:
        assert column in df.columns
    assert df["close"].dtype.kind == "f"
    assert df["volume"].dtype.kind == "f"
    delegated.assert_called_once_with("BTC", interval="15m", limit=30)


def test_analyze_symbol_returns_buy_setup_from_existing_signal_rules(monkeypatch):
    frame_15m = make_indicator_frame(
        close=100.0, ema20=95.0, rsi=50.0, macd=2.0,
        macd_signal=1.0, macd_histogram=1.0, atr=2.0,
        volume=200.0, average_volume=100.0,
    )
    frame_1h = make_indicator_frame(close=110.0, ema20=100.0)
    frame_4h = make_indicator_frame(close=120.0, ema20=100.0)

    monkeypatch.setattr(
        main,
        "get_klines",
        Mock(side_effect=[frame_15m, frame_1h, frame_4h]),
    )

    result = main.analyze_symbol("BTC")

    assert result["signal"] == "BUY"
    assert result["confidence"] == 105
    assert result["risk"] == "LOW"
    assert result["entry"] == 100.0
    assert result["stop_loss"] == 97.0
    assert result["take_profit"] == 105.0
    assert result["risk_reward"] == pytest.approx(1.67)
    assert "Bullish score is strong" in result["reason"]


def test_analyze_symbol_returns_hold_when_scores_are_too_close(monkeypatch):
    frame_15m = make_indicator_frame(
        close=100.0, ema20=100.0, rsi=50.0, macd=1.0,
        macd_signal=1.0, macd_histogram=0.0, atr=2.0,
        volume=100.0, average_volume=100.0,
    )
    frame_1h = make_indicator_frame(close=100.0, ema20=100.0)
    frame_4h = make_indicator_frame(close=100.0, ema20=100.0)

    monkeypatch.setattr(
        main,
        "get_klines",
        Mock(side_effect=[frame_15m, frame_1h, frame_4h]),
    )

    result = main.analyze_symbol("BTC")

    assert result["signal"] == "HOLD"
    assert result["reason"] == "Bullish and bearish scores are too close. Market conditions are mixed."
    assert result["risk"] == "MEDIUM"
