import os
import asyncio
import pandas as pd
import requests
from dotenv import load_dotenv
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from binance_service import binance_service


COINS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "XRP": "ripple",
}

OWNER_ID = 5360748328
INITIAL_PAPER_BALANCE = 1000.0
PAPER_BALANCE = 1000.0
PAPER_POSITIONS = {}
PAPER_TRADE_HISTORY = []

# Amount of simulated capital (in USD) allocated to each paper trade.
# This allows realistic position sizing and accurate dollar PnL tracking.
PAPER_POSITION_SIZE_USD = 100.0

ALERTS = {}
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")


def get_klines(symbol, interval="15m", limit=100):
    url = (
        "https://api.binance.com/api/v3/klines"
        f"?symbol={symbol}USDT&interval={interval}&limit={limit}"
    )

    response = requests.get(url, timeout=10)
    response.raise_for_status()
    data = response.json()

    df = pd.DataFrame(
        data,
        columns=[
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_asset_volume",
            "trades",
            "taker_buy_base",
            "taker_buy_quote",
            "ignore",
        ],
    )

    df["close"] = df["close"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["volume"] = df["volume"].astype(float)

    df["ema20"] = EMAIndicator(df["close"], window=20).ema_indicator()
    df["rsi"] = RSIIndicator(df["close"], window=14).rsi()

    macd = MACD(df["close"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_histogram"] = macd.macd_diff()

    atr = AverageTrueRange(df["high"], df["low"], df["close"])
    df["atr"] = atr.average_true_range()

    return df


def score_conditions(conditions):
    return sum(weight for passed, weight in conditions if passed)


def analyze_symbol(symbol):
    df = get_klines(symbol)
    df_1h = get_klines(symbol, interval="1h")
    df_4h = get_klines(symbol, interval="4h")

    current_price = df["close"].iloc[-1]
    ema20 = df["ema20"].iloc[-1]
    rsi = df["rsi"].iloc[-1]
    macd = df["macd"].iloc[-1]
    macd_signal = df["macd_signal"].iloc[-1]
    macd_histogram = df["macd_histogram"].iloc[-1]
    atr = df["atr"].iloc[-1]
    volume = df["volume"].iloc[-1]
    average_volume = df["volume"].tail(20).mean()

    bullish_1h_trend = df_1h["close"].iloc[-1] > df_1h["ema20"].iloc[-1]
    bullish_4h_trend = df_4h["close"].iloc[-1] > df_4h["ema20"].iloc[-1]
    bearish_1h_trend = df_1h["close"].iloc[-1] < df_1h["ema20"].iloc[-1]
    bearish_4h_trend = df_4h["close"].iloc[-1] < df_4h["ema20"].iloc[-1]
    strong_volume = volume > average_volume

    bullish_conditions = [
        (bullish_1h_trend, 20),
        (bullish_4h_trend, 20),
        (strong_volume, 10),
        (current_price > ema20, 15),
        (macd > macd_signal, 15),
        (macd_histogram > 0, 10),
        (45 <= rsi < 70, 10),
        (atr > 0, 5),
    ]
    bearish_conditions = [
        (bearish_1h_trend, 20),
        (bearish_4h_trend, 20),
        (strong_volume, 10),
        (current_price < ema20, 15),
        (macd < macd_signal, 15),
        (macd_histogram < 0, 10),
        (30 < rsi <= 55, 10),
        (atr > 0, 5),
    ]

    bullish_score = score_conditions(bullish_conditions)
    bearish_score = score_conditions(bearish_conditions)
    score_gap = abs(bullish_score - bearish_score)
    score = max(bullish_score, bearish_score)

    entry = current_price
    stop_loss = current_price * 0.99
    take_profit = current_price * 1.01
    risk_reward = round((take_profit - entry) / (entry - stop_loss), 2)

    signal = "HOLD"
    reason = "Mixed market conditions."
    confidence = score
    risk = "HIGH"

    buy_setup = (
        bullish_score >= 70
        and bullish_score >= bearish_score + 15
        and current_price > ema20
        and macd > macd_signal
        and macd_histogram > 0
        and (40 <= rsi <= 68)
    )
    sell_setup = (
        bearish_score >= 70
        and bearish_score >= bullish_score + 15
        and current_price < ema20
        and macd < macd_signal
        and macd_histogram < 0
        and (32 <= rsi <= 60)
    )

    if buy_setup:
        signal = "BUY"
        reason = (
            "Bullish score is strong. Price is above EMA20, MACD momentum is "
            "positive, RSI is in the optimal acceleration band, and higher-timeframe trend support outweighs bearish pressure."
        )
        confidence = bullish_score
        risk = "LOW" if bullish_score >= 80 else "MEDIUM"

        entry = current_price
        stop_loss = current_price - (atr * 1.5)
        take_profit = current_price + (atr * 2.5)
        risk_reward = round((take_profit - entry) / (entry - stop_loss), 2)
    elif sell_setup:
        signal = "SELL"
        reason = (
            "Bearish score is strong. Price is below EMA20, MACD momentum is "
            "negative, RSI confirms downward pressure, and higher-timeframe trend pressure outweighs bullish signals."
        )
        confidence = bearish_score
        risk = "LOW" if bearish_score >= 80 else "MEDIUM"

        entry = current_price
        stop_loss = current_price + (atr * 1.5)
        take_profit = current_price - (atr * 2.5)
        risk_reward = round((entry - take_profit) / (stop_loss - entry), 2)
    elif rsi >= 70:
        reason = "RSI is overbought. Wait for a pullback."
        confidence = max(confidence, 65)
        risk = "MEDIUM"
    elif rsi <= 30:
        reason = "RSI is oversold. Watch for a possible reversal."
        confidence = max(confidence, 65)
        risk = "MEDIUM"
    elif score_gap < 15:
        reason = "Bullish and bearish scores are too close. Market conditions are mixed."
        risk = "MEDIUM"
    elif bullish_score > bearish_score:
        reason = "Bullish conditions are building, but the score is not strong enough for a BUY."
        risk = "MEDIUM" if bullish_score >= 60 else "HIGH"
    elif bearish_score > bullish_score:
        reason = "Bearish conditions are building, but the score is not strong enough for a SELL."
        risk = "MEDIUM" if bearish_score >= 60 else "HIGH"
    else:
        reason = "Mixed market conditions."
        risk = "MEDIUM"

    return {
        "symbol": symbol,
        "current_price": current_price,
        "ema20": ema20,
        "rsi": rsi,
        "macd": macd,
        "macd_signal": macd_signal,
        "macd_histogram": macd_histogram,
        "volume": volume,
        "average_volume": average_volume,
        "signal": signal,
        "confidence": confidence,
        "risk": risk,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_reward": risk_reward,
        "reason": reason,
    }

def open_paper_trade(analysis):
    """
    Open a simulated paper trade from an analysis dict.

    Safety guards:
    - Refuses to open if live_trading_enabled is True (paper and live are mutually exclusive).
    - Will not open a duplicate position for the same symbol.
    - Will not open if signal is not BUY or SELL.

    Position sizing:
    - Allocates PAPER_POSITION_SIZE_USD of simulated capital to each trade.
    - Quantity is rounded to Binance LOT_SIZE stepSize for realistic simulation.
      If symbol rules are unavailable, falls back to raw quantity.
    """
    global PAPER_POSITIONS

    # Safety: paper trading and live trading are mutually exclusive.
    if binance_service.live_trading_enabled:
        print("open_paper_trade: blocked — live trading is enabled. Paper trades are disabled in live mode.")
        return False

    symbol = analysis["symbol"]
    signal = analysis["signal"]

    if signal not in ("BUY", "SELL"):
        return False

    if symbol in PAPER_POSITIONS:
        return False

    entry_price = analysis["entry"]

    # Apply Binance PRICE_FILTER tick-size rounding for simulation realism.
    # Falls back to the raw value if exchange rules cannot be fetched (e.g., offline).
    entry_ok, formatted_entry, entry_err = binance_service.format_price(symbol, entry_price)
    if entry_ok:
        entry_price = formatted_entry
    else:
        print(f"open_paper_trade: could not format entry price for {symbol} ({entry_err}); using raw value.")

    stop_loss = analysis["stop_loss"]
    stop_ok, formatted_stop, stop_err = binance_service.format_price(symbol, stop_loss)
    if stop_ok:
        stop_loss = formatted_stop
    else:
        print(f"open_paper_trade: could not format stop-loss for {symbol} ({stop_err}); using raw value.")

    take_profit = analysis["take_profit"]
    tp_ok, formatted_tp, tp_err = binance_service.format_price(symbol, take_profit)
    if tp_ok:
        take_profit = formatted_tp
    else:
        print(f"open_paper_trade: could not format take-profit for {symbol} ({tp_err}); using raw value.")

    # Calculate simulated position size in base asset units.
    raw_quantity = PAPER_POSITION_SIZE_USD / entry_price if entry_price > 0 else 0.0

    # Apply Binance LOT_SIZE step-size rounding for simulation realism.
    # Falls back to raw quantity if exchange rules cannot be fetched (e.g., offline).
    qty_ok, formatted_qty, qty_err = binance_service.format_quantity(symbol, raw_quantity)
    if qty_ok:
        quantity = formatted_qty
    else:
        quantity = raw_quantity
        print(f"open_paper_trade: could not apply step-size for {symbol} ({qty_err}); using raw quantity.")

    PAPER_POSITIONS[symbol] = {
        "symbol": symbol,
        "side": signal,
        "entry": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "confidence": analysis["confidence"],
        "quantity": quantity,
        "position_size_usd": quantity * entry_price,
        "opened_at": str(pd.Timestamp.now()),
        "status": "OPEN",
    }

    print(
        f"Paper trade opened: {symbol} {signal} | Entry: ${entry_price:,.2f} "
        f"| Qty: {quantity:.6f} | Size: ${quantity * entry_price:,.2f}"
    )
    return True

def check_paper_positions():
    """
    Monitor all open paper positions and close them when TP or SL is reached.

    Market data source:
    - Uses binance_service.get_current_price() for a lightweight ticker call.
    - Falls back to get_klines() last close if the ticker call fails.

    PnL accounting:
    - Scales PnL by position quantity for accurate dollar-value tracking.
    - BUY PnL  = quantity * (exit_price - entry_price)
    - SELL PnL = quantity * (entry_price - exit_price)
    """
    global PAPER_BALANCE, PAPER_TRADE_HISTORY, PAPER_POSITIONS
    for symbol, position in list(PAPER_POSITIONS.items()):
        if position.get("status") != "OPEN":
            continue

        try:
            # Primary: lightweight ticker price fetch via binance_service.
            price_ok, current_price, price_err = binance_service.get_current_price(symbol)

            if not price_ok or current_price <= 0:
                # Fallback: use last close from 1m candles.
                try:
                    data = get_klines(symbol, interval="1m", limit=5)
                    if data is None or data.empty:
                        continue
                    current_price = float(data.iloc[-1]["close"])
                except Exception:
                    print(f"check_paper_positions: could not fetch price for {symbol}, skipping.")
                    continue

            entry = float(position["entry"])
            stop_loss = float(position["stop_loss"])
            take_profit = float(position["take_profit"])
            side = position["side"]
            # Quantity defaults to 1.0 for legacy positions opened before Phase 3.2.
            quantity = float(position.get("quantity", 1.0))

            closed = False
            pnl = 0.0

            if side == "BUY":
                if current_price >= take_profit:
                    position["status"] = "TAKE_PROFIT"
                    position["exit"] = current_price
                    position["closed_at"] = str(pd.Timestamp.now())
                    pnl = quantity * (current_price - entry)
                    closed = True

                elif current_price <= stop_loss:
                    position["status"] = "STOP_LOSS"
                    position["exit"] = current_price
                    position["closed_at"] = str(pd.Timestamp.now())
                    pnl = quantity * (current_price - entry)
                    closed = True

            elif side == "SELL":
                if current_price <= take_profit:
                    position["status"] = "TAKE_PROFIT"
                    position["exit"] = current_price
                    position["closed_at"] = str(pd.Timestamp.now())
                    pnl = quantity * (entry - current_price)
                    closed = True

                elif current_price >= stop_loss:
                    position["status"] = "STOP_LOSS"
                    position["exit"] = current_price
                    position["closed_at"] = str(pd.Timestamp.now())
                    pnl = quantity * (entry - current_price)
                    closed = True

            if closed:
                position["pnl"] = pnl
                PAPER_BALANCE += pnl
                PAPER_TRADE_HISTORY.append(position.copy())
                if symbol in PAPER_POSITIONS:
                    del PAPER_POSITIONS[symbol]
                print(
                    f"Paper trade closed for {symbol} ({side}): {position['status']} "
                    f"at ${current_price:,.2f} | Qty: {quantity:.6f} "
                    f"| PnL: {pnl:+.4f} | New Balance: ${PAPER_BALANCE:,.2f}"
                )

        except Exception as e:
            print(f"Paper position check error for {symbol}: {e}")

def get_paper_statistics():
    total_trades = len(PAPER_TRADE_HISTORY)

    if total_trades == 0:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "average_pnl": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "profit_factor": 0.0,
            "best_trade": 0.0,
            "worst_trade": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "buy_trades": 0,
            "buy_wins": 0,
            "sell_trades": 0,
            "sell_wins": 0,
            "recent_trades": [],
        }

    wins = sum(
        1 for trade in PAPER_TRADE_HISTORY
        if trade.get("status") == "TAKE_PROFIT" or trade.get("pnl", 0) > 0
    )
    losses = sum(
        1 for trade in PAPER_TRADE_HISTORY
        if trade.get("status") == "STOP_LOSS" or trade.get("pnl", 0) < 0
    )

    pnls = []
    winning_pnls = []
    losing_pnls = []

    buy_trades = [t for t in PAPER_TRADE_HISTORY if t.get("side") == "BUY"]
    sell_trades = [t for t in PAPER_TRADE_HISTORY if t.get("side") == "SELL"]

    buy_wins = sum(1 for t in buy_trades if t.get("status") == "TAKE_PROFIT" or t.get("pnl", 0) > 0)
    sell_wins = sum(1 for t in sell_trades if t.get("status") == "TAKE_PROFIT" or t.get("pnl", 0) > 0)

    for trade in PAPER_TRADE_HISTORY:
        if "pnl" in trade:
            pnl = float(trade["pnl"])
        else:
            entry = float(trade["entry"])
            exit_price = float(trade["exit"])
            pnl = (exit_price - entry) if trade["side"] == "BUY" else (entry - exit_price)

        pnls.append(pnl)
        if pnl > 0:
            winning_pnls.append(pnl)
        elif pnl < 0:
            losing_pnls.append(pnl)

    total_pnl = sum(pnls)
    gross_profit = sum(winning_pnls)
    gross_loss = sum(losing_pnls)
    average_pnl = total_pnl / total_trades if total_trades else 0.0
    win_rate = (wins / total_trades) * 100 if total_trades else 0.0

    avg_win = (gross_profit / len(winning_pnls)) if winning_pnls else 0.0
    avg_loss = (gross_loss / len(losing_pnls)) if losing_pnls else 0.0

    if gross_loss != 0:
        profit_factor = gross_profit / abs(gross_loss)
    elif gross_profit > 0:
        profit_factor = gross_profit
    else:
        profit_factor = 0.0

    best_trade = max(pnls) if pnls else 0.0
    worst_trade = min(pnls) if pnls else 0.0

    recent_trades = PAPER_TRADE_HISTORY[-5:]

    return {
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "average_pnl": average_pnl,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "buy_trades": len(buy_trades),
        "buy_wins": buy_wins,
        "sell_trades": len(sell_trades),
        "sell_wins": sell_wins,
        "recent_trades": recent_trades,
    }

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ This bot is currently in private testing.")
        return

    statistics = get_paper_statistics()

    if statistics["total_trades"] == 0:
        await update.message.reply_text(
            "📊 Paper Trading Performance Statistics\n\n"
            "💼 Total Trades: 0\n"
            "ℹ️ No closed trades recorded yet.\n"
            f"💵 Current Balance: ${PAPER_BALANCE:,.2f}\n"
            f"📈 Open Positions: {len(PAPER_POSITIONS)}"
        )
        return

    return_pct = ((PAPER_BALANCE - INITIAL_PAPER_BALANCE) / INITIAL_PAPER_BALANCE) * 100
    pnl_sign = "+" if statistics["total_pnl"] >= 0 else ""
    avg_sign = "+" if statistics["average_pnl"] >= 0 else ""

    message = (
        "📊 Paper Trading Performance Statistics\n\n"
        f"💼 Total Trades: {statistics['total_trades']}\n"
        f"✅ Wins: {statistics['wins']} | ❌ Losses: {statistics['losses']}\n"
        f"🎯 Win Rate: {statistics['win_rate']:.2f}%\n"
        f"💰 Total P&L: {pnl_sign}${statistics['total_pnl']:,.2f}\n"
        f"📈 Average P&L: {avg_sign}${statistics['average_pnl']:,.2f}\n"
        f"💵 Balance: ${PAPER_BALANCE:,.2f} ({return_pct:+.2f}%)\n"
        f"⚖️ Profit Factor: {statistics['profit_factor']:.2f}\n"
        f"🟢 Best Trade: +${statistics['best_trade']:,.2f}\n"
        f"🔴 Worst Trade: ${statistics['worst_trade']:,.2f}\n"
        f"🟢 Avg Win: +${statistics['avg_win']:,.2f}\n"
        f"🔴 Avg Loss: ${statistics['avg_loss']:,.2f}\n\n"
        f"📊 Directional Breakdown:\n"
        f"• BUY (Long): {statistics['buy_trades']} trades ({statistics['buy_wins']} wins)\n"
        f"• SELL (Short): {statistics['sell_trades']} trades ({statistics['sell_wins']} wins)\n"
    )

    if statistics["recent_trades"]:
        message += "\n📜 Recent Closed Trades:\n"
        for t in reversed(statistics["recent_trades"]):
            trade_pnl = t.get("pnl", 0.0)
            trade_sign = "+" if trade_pnl >= 0 else ""
            status_icon = "✅" if t.get("status") == "TAKE_PROFIT" else "🛑"
            message += (
                f"{status_icon} {t.get('symbol')} ({t.get('side')}): "
                f"${t.get('entry', 0):,.2f} ➜ ${t.get('exit', 0):,.2f} "
                f"({trade_sign}${trade_pnl:,.2f})\n"
            )

    await update.message.reply_text(message)

async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ This bot is currently in private beta.")
        return

    # Pre-scan connectivity check: fail fast with a clear message if Binance is unreachable.
    ping_res = binance_service.ping()
    if ping_res.get("status") != "ONLINE":
        await update.message.reply_text(
            "⚠️ Market Scan Failed\n\n"
            "Cannot reach Binance API. Please try again shortly.\n"
            f"Error: {ping_res.get('error', 'Unknown connectivity error')}"
        )
        return

    results = {"BUY": [], "SELL": [], "HOLD": []}
    errors = []

    for symbol in COINS:
        # Symbol validation: confirm symbol is actively TRADING on Binance before analysis.
        rules = binance_service.get_symbol_rules(symbol)
        if rules.get("success") and not rules.get("is_trading", True):
            errors.append(f"{symbol}: not currently in TRADING status on Binance")
            continue

        try:
            analysis = analyze_symbol(symbol)
        except Exception as error:
            errors.append(f"{symbol}: {error}")
            continue

        results[analysis["signal"]].append(analysis)

    results["BUY"].sort(key=lambda item: item["confidence"], reverse=True)
    results["SELL"].sort(key=lambda item: item["confidence"], reverse=True)

    best_opportunity = None
    if results["BUY"]:
        best_opportunity = results["BUY"][0]
    elif results["SELL"]:
        best_opportunity = results["SELL"][0]

    def format_group(group_name):
        if not results[group_name]:
            return "None"
        return "\n".join(
            f"{item['symbol']}: {item['confidence']}%"
            for item in results[group_name]
        )

    message = "Market Scan\n\n"

    if best_opportunity:
        trade_opened = open_paper_trade(best_opportunity)

        message += (
            "⭐ Best Opportunity\n"
            f"{best_opportunity['symbol']} {best_opportunity['signal']} "
            f"({best_opportunity['confidence']}%)\n"
            f"Current Price: ${best_opportunity['current_price']:,.2f}\n"
        )
        if trade_opened:
            pos = PAPER_POSITIONS.get(best_opportunity["symbol"], {})
            qty = pos.get("quantity", 0.0)
            size = pos.get("position_size_usd", 0.0)
            message += (
                f"📄 Paper Trade Opened\n"
                f"Qty: {qty:.6f} | Size: ${size:,.2f}\n"
                f"SL: ${best_opportunity['stop_loss']:,.2f} | "
                f"TP: ${best_opportunity['take_profit']:,.2f}\n"
            )
        else:
            message += "ℹ️ No new position opened (already open or HOLD signal).\n"

        message += f"Reason: {best_opportunity['reason']}\n\n"
    else:
        message += "⭐ Best Opportunity\nNo actionable signals found.\n\n"

    message += (
        f"BUY\n{format_group('BUY')}\n\n"
        f"SELL\n{format_group('SELL')}\n\n"
        f"HOLD\n{format_group('HOLD')}"
    )

    if errors:
        message += "\n\nCould not scan:\n" + "\n".join(errors)

    await update.message.reply_text(message)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hello! I am Allmightsee Prime.\n\nI'm alive and ready to trade!"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("🚫 This bot is currently in private testing.")
        return

    await update.message.reply_text(
        "🤖 Allmightsee Prime\n\n"
        "📖 Available Commands\n\n"
        "/start - Start the bot\n"
        "/scan - Scan the market & open best trade\n"
        "/price <COIN> - Detailed coin analysis & signals\n"
        "/paper - Paper trading account & active positions\n"
        "/stats - Detailed performance statistics & history\n"
        "/exchange - Binance API status & live account balance\n"
        "/alert <COIN> <PRICE> - Set price alert\n"
        "/status - Bot operational status\n"
        "/ping - Latency check\n"
        "/help - Show this guide\n\n"
        "🪙 Supported Coins\n"
        "BTC, ETH, SOL, BNB, XRP"
    )

async def exchange(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("🚫 This bot is currently in private mode.")
        return

    ping_res = binance_service.ping()
    ping_status = ping_res.get("status", "OFFLINE")
    latency = f"{ping_res.get('latency_ms', 'N/A')}ms" if ping_res.get("latency_ms") is not None else "N/A"

    server_time_res = binance_service.get_server_time()
    drift = f"{server_time_res.get('drift_ms', 'N/A')}ms" if server_time_res.get("drift_ms") is not None else "N/A"

    api_key_masked = binance_service.get_masked_api_key()
    live_status = "ENABLED (Live Trading Active)" if binance_service.live_trading_enabled else "DISABLED (Paper Trading Active)"
    dry_run_status = "ON (Orders are simulated/logged only)" if binance_service.dry_run else "OFF (Real orders may be sent)"

    message = (
        "🏦 Binance Exchange Status\n\n"
        f"🌐 API Connectivity: {ping_status} (Latency: {latency})\n"
        f"⏱️ Time Sync Drift: {drift}\n"
        f"🔑 API Key: {api_key_masked}\n"
        f"🛡️ Live Orders: {live_status}\n"
        f"🧪 DRY_RUN Mode: {dry_run_status}\n\n"
    )

    if binance_service.is_configured:
        account_res = binance_service.get_account_balances(tracked_assets=list(COINS.keys()) + ["USDT"])
        if account_res.get("success"):
            balances = account_res.get("balances", {})
            message += "💼 Live Account Balances:\n"
            for asset, b in sorted(balances.items()):
                total = b.get("total", 0.0)
                free = b.get("free", 0.0)
                locked = b.get("locked", 0.0)
                if total > 0 or asset == "USDT":
                    if asset == "USDT":
                        message += f"• {asset}: ${free:,.2f} free (${locked:,.2f} locked | Total: ${total:,.2f})\n"
                    else:
                        message += f"• {asset}: {free:.6f} free ({locked:.6f} locked)\n"
        else:
            message += f"⚠️ Account Details: {account_res.get('error')}\n"
    else:
        message += "ℹ️ Binance API Key & Secret not set in .env (Add BINANCE_API_KEY and BINANCE_API_SECRET for live account info).\n"

    await update.message.reply_text(message)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("🚫 This bot is currently in private mode.")
        return

    await update.message.reply_text(
        "🟢 Bot Status\n\n"
        "Status: Online\n"
        "🤖 Allmightsee Prime is running normally."
    )

async def paper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("🚫 This bot is currently in private mode.")
        return

    return_pct = ((PAPER_BALANCE - INITIAL_PAPER_BALANCE) / INITIAL_PAPER_BALANCE) * 100
    msg = (
        "📊 Paper Trading Account\n\n"
        f"💵 Balance: ${PAPER_BALANCE:,.2f} ({return_pct:+.2f}%)\n"
        f"📈 Open Positions: {len(PAPER_POSITIONS)}"
    )

    if PAPER_POSITIONS:
        msg += "\n\n📍 Active Open Positions:\n"
        for sym, pos in PAPER_POSITIONS.items():
            msg += (
                f"• {sym} ({pos.get('side')}): Entry ${pos.get('entry', 0):,.2f} | "
                f"SL: ${pos.get('stop_loss', 0):,.2f} | TP: ${pos.get('take_profit', 0):,.2f}\n"
            )

    await update.message.reply_text(msg)

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("🚫 This bot is currently in private mode.")
        return

    await update.message.reply_text(
            "🏓 Pong!\n\n"
            "🟢 Status: Online\n"
            "⚡ Response Time: Excellent"
    )

async def alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ This bot is currently in private mode.")
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "Usage:\n"
            "/alert BTC 120000"
        )
        return

    symbol = context.args[0].upper()

    if symbol not in COINS:
        await update.message.reply_text("❌ Coin not supported.")
        return

    try:
        target_price = float(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid price.")
        return

    ALERTS[symbol] = target_price

    await update.message.reply_text(
        f"✅ Alert saved!\n\n"
        f"Coin: {symbol}\n"
        f"Target Price: ${target_price:,.2f}"

    )

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ This bot is currently in private beta.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /price BTC\nAvailable: BTC, ETH, SOL, BNB, XRP"
        )
        return

    symbol = context.args[0].upper()

    if symbol not in COINS:
        await update.message.reply_text("❌ Coin not supported. Available: BTC, ETH, SOL, BNB, XRP")
        return

    try:
        analysis = analyze_symbol(symbol)
    except requests.RequestException as error:
        await update.message.reply_text(f"Could not fetch market data: {error}")
        return
    except Exception as error:
        await update.message.reply_text(f"Analysis error: {error}")
        return

    await update.message.reply_text(
        f"{symbol} Price Analysis\n\n"
        f"Current Price: ${analysis['current_price']:,.2f}\n"
        f"EMA20: ${analysis['ema20']:,.2f}\n"
        f"RSI: {analysis['rsi']:.2f}\n"
        f"MACD: {analysis['macd']:.2f}\n"
        f"MACD Signal: {analysis['macd_signal']:.2f}\n"
        f"MACD Histogram: {analysis['macd_histogram']:.2f}\n"
        f"Volume: {analysis['volume']:,.2f}\n"
        f"Average Volume: {analysis['average_volume']:,.2f}\n\n"
        f"📢 Signal: {analysis['signal']}\n"
        f"🎯 Confidence: {analysis['confidence']}%\n"
        f"⚠️ Risk: {analysis['risk']}\n"
        f"🎯 Entry: ${analysis['entry']:,.2f}\n"
        f"🛑 Stop Loss: ${analysis['stop_loss']:,.2f}\n"
        f"💰 Take Profit: ${analysis['take_profit']:,.2f}\n"
        f"⚖️ Risk/Reward: 1:{analysis['risk_reward']}\n"
        f"📝 Reason: {analysis['reason']}"
    )

async def check_alerts(app):
    while True:
        try:
            for symbol, target_price in list(ALERTS.items()):
                try:
                    df = get_klines(symbol)
                    current_price = df["close"].iloc[-1]

                    if current_price is None:
                        continue
                    if current_price >= target_price:
                        await app.bot.send_message(
                            chat_id=OWNER_ID,
                            text=(
                                f"🚨 PRICE ALERT!\n\n"
                                f"{symbol} has reached your target.\n"
                                f"Current Price: ${current_price:,.2f}\n"
                                f"Target Price: ${target_price:,.2f}"
                            )
                        )
                        del ALERTS[symbol]

                except Exception as e:
                    print(f"Alert error for {symbol}: {e}")

            check_paper_positions()
        except Exception as loop_error:
            print(f"Background loop error: {loop_error}")

        await asyncio.sleep(60)



async def post_init(app):
   asyncio.create_task(check_alerts(app), name="check_alerts")


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Add it to your .env file.")

    app = (
    Application.builder()
    .token(TOKEN)
    .post_init(post_init)
    .build()
)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("paper", paper))
    app.add_handler(CommandHandler("alert", alert))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("scan", scan))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("exchange", exchange))

    print("🤖 Allmightsee Prime is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
