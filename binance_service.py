"""
Binance Exchange Integration Service for Project Morningstar / All-Might C Prime.

Provides secure, authenticated communication with the Binance REST API.
Handles:
- Secure environment configuration (API Key & Secret)
- HMAC-SHA256 signature generation for private endpoints
- Account balance retrieval (free, locked, total USDT & crypto balances)
- Exchange trading rules, filters, step sizes, and price/qty precision formatting
- Strict safety controls: Live trading remains disabled by default.
 - DRY_RUN execution mode (Phase 3.2b): When enabled (the default), the order
   execution layer simulates/logs intended orders instead of contacting the
   Binance order-placement endpoint. This is a safety layer in addition to,
   not a replacement for, the LIVE_TRADING_ENABLED shield below.
 - Order execution engine (Phase 3.2c): place_order() submits exchange orders
  to the signed /api/v3/order endpoint only when explicitly enabled, retrieves
  and confirms order status via get_order_status(), and supports cancellation
  via cancel_order(). Raw Binance order statuses are normalized to filled,
  partially_filled, pending, cancelled, or rejected labels.
 - Strategy-flow integration (Phase 3.3): main.py routes paper BUY/SELL entries
   and exits through place_order(), so every intended order flows through this
   engine while DRY_RUN remains enabled by default.
 - API-permission verification (Phase 3.1): assert_api_permissions() verifies
   the configured key may trade and that the read-only mandate holds, using
   get_account_info() (canTrade / withdrawal-disabled) - a read-only check
   that never places orders or alters the safety guards.
"""

import os
import time
import hmac
import hashlib
import math
import re
from typing import Dict, Any, Optional, List, Tuple
from urllib.parse import urlencode
import requests
from dotenv import load_dotenv
import pandas as pd

load_dotenv()


# ============================================================================
# ORDER STATUS NORMALIZATION (Phase 3.2c)
# Maps raw Binance order status strings to stable, human-readable labels so the
# execution engine can distinguish filled, partially filled, pending, cancelled
# and rejected orders regardless of API-version wording.
# ============================================================================
ORDER_STATUS_LABELS = {
    "NEW": "pending",
    "PENDING_NEW": "pending",
    "PARTIALLY_FILLED": "partially_filled",
    "FILLED": "filled",
    "CANCELED": "cancelled",
    "PENDING_CANCEL": "cancelled",
    "EXPIRED": "cancelled",
        "REJECTED": "rejected",
}


# ============================================================================
# Phase 4 - Market-data helpers (klines/candles) for BTC & ETH (+ others).
# Whitelists and interval metadata consumed by BinanceService.get_klines().
# ============================================================================
SUPPORTED_KLINE_INTERVALS = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")
_INTERVAL_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}
# A candle whose open_time is older than 2x its interval is treated as stale:
# generous enough to absorb clock skew, small enough to flag a paused/halted pair.
_STALE_FACTOR = 2.0

_KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_asset_volume", "trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,10}$")


class BinanceService:
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        live_trading_enabled: Optional[bool] = None,
        dry_run: Optional[bool] = None,
    ):
        self.api_key = api_key if api_key is not None else os.getenv("BINANCE_API_KEY", "")
        self.api_secret = api_secret if api_secret is not None else os.getenv("BINANCE_API_SECRET", "")
        self.base_url = (base_url or os.getenv("BINANCE_BASE_URL", "https://api.binance.com")).rstrip("/")
        
        if live_trading_enabled is not None:
            self.live_trading_enabled = live_trading_enabled
        else:
            self.live_trading_enabled = os.getenv("LIVE_TRADING_ENABLED", "False").lower() in ("true", "1", "yes")

        # ==========================================================================
        # DRY_RUN MODE (Phase 3.2b) — CONFIGURE HERE / VIA .env
        # --------------------------------------------------------------------------
        # DRY_RUN defaults to True (safe). While True, place_order() NEVER sends a
        # real order to Binance — it only simulates and logs the intended order.
        # This is independent from, and in addition to, LIVE_TRADING_ENABLED above.
        # ==========================================================================
        if dry_run is not None:
            self.dry_run = dry_run
        else:
            self.dry_run = os.getenv("DRY_RUN", "True").lower() in ("true", "1", "yes")

        self.session = requests.Session()
        self.timeout = 10
        self._symbol_info_cache: Dict[str, Dict[str, Any]] = {}

    @property
    def is_configured(self) -> bool:
        """Check if both API Key and Secret are configured."""
        return bool(self.api_key and self.api_secret)

    def get_masked_api_key(self) -> str:
        """Return a safely masked version of the API Key for logging/UI display."""
        if not self.api_key:
            return "Not Configured"
        if len(self.api_key) <= 8:
            return "******"
        return f"{self.api_key[:4]}...{self.api_key[-4:]}"

    def _generate_signature(self, query_string: str) -> str:
        """Generate HMAC-SHA256 signature for authenticated Binance requests."""
        if not self.api_secret:
            raise ValueError("Binance API Secret is not configured.")
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _get_headers(self) -> Dict[str, str]:
        """Generate request headers with API key."""
        headers = {"User-Agent": "ProjectMorningstar-TradingBot/1.0"}
        if self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key
        return headers

    def ping(self) -> Dict[str, Any]:
        """Test public connectivity to Binance API."""
        url = f"{self.base_url}/api/v3/ping"
        start_time = time.time()
        try:
            res = self.session.get(url, headers=self._get_headers(), timeout=self.timeout)
            latency_ms = round((time.time() - start_time) * 1000, 2)
            if res.status_code == 200:
                return {"status": "ONLINE", "latency_ms": latency_ms, "error": None}
            return {
                "status": "ERROR",
                "latency_ms": latency_ms,
                "error": f"HTTP {res.status_code}: {res.text}",
            }
        except requests.RequestException as e:
            return {"status": "OFFLINE", "latency_ms": None, "error": str(e)}

    def get_server_time(self) -> Dict[str, Any]:
        """Check Binance server time and local timestamp drift."""
        url = f"{self.base_url}/api/v3/time"
        try:
            res = self.session.get(url, headers=self._get_headers(), timeout=self.timeout)
            res.raise_for_status()
            server_time = res.json().get("serverTime", 0)
            local_time = int(time.time() * 1000)
            drift_ms = abs(local_time - server_time)
            return {"server_time": server_time, "local_time": local_time, "drift_ms": drift_ms, "error": None}
        except Exception as e:
            return {"server_time": None, "local_time": int(time.time() * 1000), "drift_ms": None, "error": str(e)}

    def get_account_info(self) -> Dict[str, Any]:
        """
        Fetch authenticated account details from Binance /api/v3/account.
        Returns account permissions, balances, and commission rates.
        """
        if not self.is_configured:
            return {
                "success": False,
                "error": "Binance API Key and Secret are not configured in environment variables.",
                "data": None,
            }

        url = f"{self.base_url}/api/v3/account"
        params: Dict[str, Any] = {
            "timestamp": int(time.time() * 1000),
            "recvWindow": 5000,
        }
        query_string = urlencode(params)
        params["signature"] = self._generate_signature(query_string)

        try:
            res = self.session.get(url, headers=self._get_headers(), params=params, timeout=self.timeout)
            if res.status_code == 200:
                return {"success": True, "error": None, "data": res.json()}
            
            err_data = {}
            try:
                err_data = res.json()
            except Exception:
                pass
            msg = err_data.get("msg", res.text)
            return {"success": False, "error": f"Binance API Error ({res.status_code}): {msg}", "data": None}
        except requests.RequestException as e:
            return {"success": False, "error": f"Network Error: {str(e)}", "data": None}

    def assert_api_permissions(self) -> Dict[str, Any]:
        """
        Verify the Binance API key permissions required for trading (Phase 3.1,
        Requirement 5).

        Uses get_account_info() to confirm the account can trade and that the
        read-only mandate is respected (withdrawal/transfer remains disabled).
        This is a strictly read-only check - it never places an order and never
        changes LIVE_TRADING_ENABLED or DRY_RUN.

        Returns a structured result:
            on success: {"success": True, "error": None, ...permissions}
            on failure: {"success": False, "error": <msg>, ...permissions}
        """
        account_res = self.get_account_info()
        if not account_res["success"]:
            return {
                "success": False,
                "error": account_res.get("error"),
                "can_trade": None,
                "withdraw_allowed": None,
                "account_type": None,
            }

        data = account_res["data"]
        can_trade = bool(data.get("canTrade", False))
        withdraw_allowed = bool(data.get("withdrawAllEnabled", False))
        account_type = data.get("accountType", "SPOT")

        if not can_trade:
            return {
                "success": False,
                "error": "Binance API key is not permitted to trade (canTrade is False).",
                "can_trade": can_trade,
                "withdraw_allowed": withdraw_allowed,
                "account_type": account_type,
            }

        if withdraw_allowed:
            return {
                "success": False,
                "error": "Binance API key has withdrawal enabled; violates the read-only mandate.",
                "can_trade": can_trade,
                "withdraw_allowed": withdraw_allowed,
                "account_type": account_type,
            }

        return {
            "success": True,
            "error": None,
            "can_trade": can_trade,
            "withdraw_allowed": withdraw_allowed,
            "account_type": account_type,
        }

    def get_account_balances(self, tracked_assets: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Retrieve structured asset balances (free, locked, total).
        If tracked_assets is provided, filters for those assets; otherwise returns all non-zero balances.
        """
        account_res = self.get_account_info()
        if not account_res["success"]:
            return account_res

        data = account_res["data"]
        raw_balances = data.get("balances", [])
        
        parsed_balances: Dict[str, Dict[str, float]] = {}
        for b in raw_balances:
            asset = b.get("asset", "")
            free = float(b.get("free", 0.0))
            locked = float(b.get("locked", 0.0))
            total = free + locked
            
            if tracked_assets is not None:
                if asset in tracked_assets:
                    parsed_balances[asset] = {"free": free, "locked": locked, "total": total}
            else:
                if total > 0:
                    parsed_balances[asset] = {"free": free, "locked": locked, "total": total}

        # Ensure USDT is always present in output
        if "USDT" not in parsed_balances and (tracked_assets is None or "USDT" in tracked_assets):
            parsed_balances["USDT"] = {"free": 0.0, "locked": 0.0, "total": 0.0}

        return {
            "success": True,
            "can_trade": data.get("canTrade", False),
            "account_type": data.get("accountType", "SPOT"),
            "balances": parsed_balances,
            "error": None,
        }

    def get_available_usdt(self) -> Tuple[bool, float, Optional[str]]:
        """Helper to quickly check free available USDT balance."""
        res = self.get_account_balances(tracked_assets=["USDT"])
        if not res["success"]:
            return False, 0.0, res["error"]
        usdt_info = res["balances"].get("USDT", {})
        return True, usdt_info.get("free", 0.0), None

    def get_symbol_rules(self, symbol: str) -> Dict[str, Any]:
        """
        Fetch and cache trading rules and filters for a specific symbol (e.g. BTCUSDT).
        Returns minQty, stepSize, tickSize, minNotional, and precision.
        """
        symbol_pair = f"{symbol.upper()}USDT" if not symbol.upper().endswith("USDT") else symbol.upper()
        
        if symbol_pair in self._symbol_info_cache:
            return self._symbol_info_cache[symbol_pair]

        url = f"{self.base_url}/api/v3/exchangeInfo"
        params = {"symbol": symbol_pair}
        try:
            res = self.session.get(url, headers=self._get_headers(), params=params, timeout=self.timeout)
            res.raise_for_status()
            data = res.json()
            symbols = data.get("symbols", [])
            if not symbols:
                return {"success": False, "error": f"Symbol {symbol_pair} not found on Binance."}
            
            s_info = symbols[0]
            status = s_info.get("status", "")
            base_asset = s_info.get("baseAsset", "")
            quote_asset = s_info.get("quoteAsset", "")
            base_precision = s_info.get("baseAssetPrecision", 8)
            quote_precision = s_info.get("quoteAssetPrecision", 8)
            
            min_qty = 0.0
            max_qty = float("inf")
            step_size = 0.0
            min_price = 0.0
            max_price = float("inf")
            tick_size = 0.0
            min_notional = 5.0  # Default spot min notional on Binance is usually $5 or $10
            
            for f in s_info.get("filters", []):
                f_type = f.get("filterType")
                if f_type == "LOT_SIZE":
                    min_qty = float(f.get("minQty", 0.0))
                    max_qty = float(f.get("maxQty", float("inf")))
                    step_size = float(f.get("stepSize", 0.0))
                elif f_type == "PRICE_FILTER":
                    min_price = float(f.get("minPrice", 0.0))
                    max_price = float(f.get("maxPrice", float("inf")))
                    tick_size = float(f.get("tickSize", 0.0))
                elif f_type in ("NOTIONAL", "MIN_NOTIONAL"):
                    min_notional = float(f.get("minNotional", f.get("notional", 5.0)))

            rules = {
                "success": True,
                "symbol": symbol_pair,
                "base_asset": base_asset,
                "quote_asset": quote_asset,
                "status": status,
                "is_trading": status == "TRADING",
                "base_precision": base_precision,
                "quote_precision": quote_precision,
                "min_qty": min_qty,
                "max_qty": max_qty,
                "step_size": step_size,
                "min_price": min_price,
                "max_price": max_price,
                "tick_size": tick_size,
                "min_notional": min_notional,
                "error": None,
            }
            self._symbol_info_cache[symbol_pair] = rules
            return rules
        except Exception as e:
            return {"success": False, "symbol": symbol_pair, "error": f"Failed to fetch rules for {symbol_pair}: {str(e)}"}

    def format_quantity(self, symbol: str, quantity: float) -> Tuple[bool, float, Optional[str]]:
        """
        Format quantity according to Binance LOT_SIZE stepSize and minimum quantity rules.
        """
        rules = self.get_symbol_rules(symbol)
        if not rules.get("success"):
            return False, quantity, rules.get("error")

        step_size = rules["step_size"]
        min_qty = rules["min_qty"]
        max_qty = rules["max_qty"]

        if step_size > 0:
            precision = int(round(-math.log10(step_size))) if step_size < 1 else 0
            rounded_qty = math.floor(quantity / step_size) * step_size
            rounded_qty = round(rounded_qty, precision)
        else:
            rounded_qty = quantity

        if rounded_qty < min_qty:
            return False, rounded_qty, f"Quantity {rounded_qty} is below minimum allowed {min_qty} for {symbol}."
        if rounded_qty > max_qty:
            return False, rounded_qty, f"Quantity {rounded_qty} exceeds maximum allowed {max_qty} for {symbol}."

        return True, rounded_qty, None

    def format_price(self, symbol: str, price: float) -> Tuple[bool, float, Optional[str]]:
        """
        Format price according to Binance PRICE_FILTER tickSize.
        """
        rules = self.get_symbol_rules(symbol)
        if not rules.get("success"):
            return False, price, rules.get("error")

        tick_size = rules["tick_size"]
        min_price = rules["min_price"]
        max_price = rules["max_price"]

        if tick_size > 0:
            precision = int(round(-math.log10(tick_size))) if tick_size < 1 else 0
            rounded_price = round(round(price / tick_size) * tick_size, precision)
        else:
            rounded_price = price

        if rounded_price < min_price:
            return False, rounded_price, f"Price {rounded_price} is below minimum {min_price} for {symbol}."
        if rounded_price > max_price:
            return False, rounded_price, f"Price {rounded_price} exceeds maximum {max_price} for {symbol}."

        return True, rounded_price, None

    def validate_order(self, symbol: str, quantity: float, price: float) -> Tuple[bool, Optional[str]]:
        """
        Validate that quantity, price, and notional (quantity * price) meet Binance trading rules.
        """
        qty_ok, formatted_qty, qty_err = self.format_quantity(symbol, quantity)
        if not qty_ok:
            return False, qty_err

        price_ok, formatted_price, price_err = self.format_price(symbol, price)
        if not price_ok:
            return False, price_err

        rules = self.get_symbol_rules(symbol)
        min_notional = rules.get("min_notional", 5.0)
        notional = formatted_qty * formatted_price

        if notional < min_notional:
            return False, f"Order notional (${notional:,.2f}) is less than the Binance minimum of ${min_notional:,.2f}."

        return True, None

    def get_current_price(self, symbol: str) -> Tuple[bool, float, Optional[str]]:
        """
        Fetch the latest spot price for a symbol from /api/v3/ticker/price.
        Lightweight alternative to pulling full candles just for a current price check.
        Returns (success, price, error_message).
        """
        symbol_pair = f"{symbol.upper()}USDT" if not symbol.upper().endswith("USDT") else symbol.upper()
        url = f"{self.base_url}/api/v3/ticker/price"
        params = {"symbol": symbol_pair}
        try:
            res = self.session.get(url, headers=self._get_headers(), params=params, timeout=self.timeout)
            res.raise_for_status()
            price = float(res.json().get("price", 0.0))
            if price <= 0:
                return False, 0.0, f"Received invalid price {price} for {symbol_pair}."
            return True, price, None
        except Exception as e:
            return False, 0.0, f"Failed to fetch price for {symbol_pair}: {str(e)}"

    def get_klines(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 100,
    ) -> Tuple[bool, Optional["pd.DataFrame"], Optional[str]]:
        """
        Fetch OHLCV candlesticks (klines) from /api/v3/klines (Phase 4).

        Safe retrieval for BTC & ETH (and any other quoted symbol):
          * ``interval`` is validated against ``SUPPORTED_KLINE_INTERVALS``;
          * ``symbol`` is normalized to ``<SYMBOL>USDT`` and format-validated;
          * empty / non-list / HTTP-failure responses return a clean
            ``(False, None, error)`` tuple instead of crashing callers;
          * the newest candle's ``open_time`` is recency-checked so a paused or
            halted pair is surfaced as stale data rather than being trusted.

        Returns
        -------
        Tuple[bool, Optional[pandas.DataFrame], Optional[str]]
            ``(success, dataframe_or_None, error_message)``
        """
        # --- input validation (no network) -----------------------------------
        if interval not in SUPPORTED_KLINE_INTERVALS:
            return (
                False,
                None,
                f"Unsupported interval '{interval}'. "
                f"Allowed: {', '.join(SUPPORTED_KLINE_INTERVALS)}.",
            )

        raw = (symbol or "").upper()
        base = raw[:-4] if raw.endswith("USDT") else raw
        symbol_pair = f"{base}USDT"
        if not base or not _SYMBOL_RE.match(base):
            return False, None, f"Invalid symbol '{symbol}'. Expected e.g. 'BTC' or 'BTCUSDT'."

        # --- fetch -----------------------------------------------------------
        url = f"{self.base_url}/api/v3/klines"
        params = {"symbol": symbol_pair, "interval": interval, "limit": limit}
        try:
            res = self.session.get(
                url, headers=self._get_headers(), params=params, timeout=self.timeout
            )
            res.raise_for_status()
            data = res.json()
        except Exception as e:
            return False, None, f"Failed to fetch klines for {symbol_pair}: {str(e)}"

        # --- empty / malformed response -------------------------------------
        if not isinstance(data, list) or not data:
            return False, None, f"No klines data returned for {symbol_pair}."

        # --- build DataFrame -------------------------------------------------
        try:
            df = pd.DataFrame(data, columns=_KLINE_COLUMNS)
            df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce")
            df["close_time"] = pd.to_numeric(df["close_time"], errors="coerce")
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            df["high"] = pd.to_numeric(df["high"], errors="coerce")
            df["low"] = pd.to_numeric(df["low"], errors="coerce")
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
        except Exception as e:
            return False, None, f"Failed to parse klines for {symbol_pair}: {str(e)}"

        # --- staleness check on the newest candle ---------------------------
        try:
            newest_open_time = int(df["open_time"].iloc[-1])
            age_ms = int(time.time() * 1000) - newest_open_time
            threshold_ms = _STALE_FACTOR * _INTERVAL_SECONDS[interval] * 1000
            if age_ms > threshold_ms:
                return (
                    False,
                    df,
                    f"Stale data for {symbol_pair}: newest candle is "
                    f"{age_ms / 1000.0:.0f}s old (>{_STALE_FACTOR:g}x interval).",
                )
        except Exception:
            return False, df, f"Could not evaluate candle recency for {symbol_pair}."

        return True, df, None

    def classify_order_status(self, raw_status: Optional[str]) -> str:
        """
        Map a raw Binance order status string to a normalized, human-readable label.

        Phase 3.2c execution engine. Distinguishes order states where supported
        by the Binance API:
          - pending:           NEW, PENDING_NEW
          - filled:            FILLED
          - partially_filled:  PARTIALLY_FILLED
          - cancelled:         CANCELED, PENDING_CANCEL, EXPIRED
          - rejected:          REJECTED
          - unknown:           anything else / missing
        """
        if not raw_status:
            return "unknown"
        return ORDER_STATUS_LABELS.get(str(raw_status).strip().upper(), "unknown")

    def _signed_request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Send an authenticated request to a Binance private endpoint (Phase 3.2c).

        Automatically stamps timestamp + recvWindow, signs the query string with
        the configured API secret, and sets the X-MBX-APIKEY header. Returns a
        structured, normalized result:
            {"success": True, "data": <parsed json>, "http_status": 200}
            {"success": False, "error": <message>, "code": <int|None>,
             "http_status": <int|None>}
        """
        if not self.is_configured:
            return {
                "success": False,
                "error": "Binance API Key and Secret are not configured in environment variables.",
                "code": None,
                "http_status": None,
            }

        body = dict(params or {})
        body["timestamp"] = int(time.time() * 1000)
        body["recvWindow"] = 5000
        body["signature"] = self._generate_signature(urlencode(body))

        url = f"{self.base_url}{path}"
        method_upper = method.upper()
        try:
            if method_upper == "POST":
                res = self.session.post(url, headers=self._get_headers(), data=body, timeout=self.timeout)
            elif method_upper == "DELETE":
                res = self.session.delete(url, headers=self._get_headers(), params=body, timeout=self.timeout)
            else:
                res = self.session.get(url, headers=self._get_headers(), params=body, timeout=self.timeout)
        except requests.RequestException as e:
            return {
                "success": False,
                "error": f"Network Error: {str(e)}",
                "code": None,
                "http_status": None,
            }

        try:
            data = res.json()
        except Exception:
            data = {}

        if res.status_code == 200:
            return {"success": True, "data": data, "http_status": res.status_code}

        return {
            "success": False,
            "error": f"Binance API Error ({res.status_code}): {data.get('msg', res.text)}",
            "code": data.get("code"),
            "http_status": res.status_code,
        }

    def _execution_report(
        self,
        symbol: str,
        side: str,
        quantity: Optional[float],
        order_type: str,
        price: Optional[float] = None,
        order_id: Optional[str] = None,
        status: Optional[str] = None,
        executed_quantity: float = 0.0,
        execution_price: Optional[float] = None,
        client_order_id: Optional[str] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        success: bool = True,
        raw_order: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Build a single consistent execution result dict and log it clearly.

        Logs symbol, side, quantity, order ID, order type, price, status and the
        execution (average fill) price for every real order (Phase 3.2c engine).
        """
        report = {
            "success": success,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "order_id": order_id,
            "order_type": order_type,
            "status": status,
            "status_label": self.classify_order_status(status),
            "executed_quantity": executed_quantity,
            "execution_price": execution_price,
            "client_order_id": client_order_id,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "dry_run": False,
            "error": error,
            "raw_order": raw_order,
        }
        error_suffix = f" | Error: {error}" if error else ""
        print(
            f"[ORDER EXECUTION] Symbol: {symbol} | Side: {side} | Quantity: {quantity} | "
            f"Type: {order_type} | Order ID: {order_id} | Price: {price} | "
            f"Status: {status} (normalized: {report['status_label']}) | "
            f"Executed Qty: {executed_quantity} | "
            f"Execution Price: {execution_price if execution_price is not None else 'N/A'}"
            f"{error_suffix}"
        )
        return report

    def place_order(
        self,
        symbol: Optional[str] = None,
        side: Optional[str] = None,
        quantity: Optional[float] = None,
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        order_type: str = "MARKET",
        time_in_force: str = "GTC",
        *args,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Execution-layer entry point for order placement (Phase 3.2c execution engine).

        Supports the existing BUY and SELL execution flow:
          - side: "BUY" or "SELL"
          - order_type: "MARKET" (default) or "LIMIT"
          - For LIMIT orders, `price` and `time_in_force` (GTC/IOC/FOK) are sent.

        DRY_RUN MODE (Phase 3.2b) — checked FIRST, on by default:
        While self.dry_run is True, this method NEVER contacts Binance to place a
        real order. It only simulates and logs the intended order (symbol, side,
        quantity, entry/current price, stop loss, take profit) and returns a
        structured simulation result that includes a simulated order ID. This
        check happens before, and independently of, the live-trading safety shield
        below, so DRY_RUN alone is sufficient to guarantee no real order is ever
        sent.

        SAFETY SHIELD (unchanged from Phase 3.2): Live order placement remains
        strictly blocked whenever live_trading_enabled is False. This guard is
        permanent until a future phase explicitly enables live trading via a
        confirmed, reviewed configuration change. Paper trading remains the only
        permitted execution mode alongside DRY_RUN simulation.

        When both guards pass, the order is submitted to the signed
        POST /api/v3/order endpoint and a structured execution report (symbol,
        side, quantity, order ID, order type, status, execution price) is
        returned/logged. Order status can then be confirmed later with
        get_order_status() or modified with cancel_order().
        """
        if self.dry_run:
            simulated_order = {
                "success": True,
                "status": "SIMULATED",
                "status_label": "simulated",
                "dry_run": True,
                "order_id": f"SIM-{int(time.time() * 1000)}",
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "price": price,
                "order_type": order_type,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "executed_quantity": 0.0,
                "execution_price": price if price is not None else None,
                "client_order_id": None,
                "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "error": None,
                "raw_order": None,
            }
            print(
                "[DRY_RUN] Simulated order — NO real order sent to Binance | "
                f"Symbol: {symbol} | Side: {side} | Quantity: {quantity} | "
                f"Order ID: {simulated_order['order_id']} | Type: {order_type} | "
                f"Entry/Current Price: {price} | Stop Loss: {stop_loss} | "
                f"Take Profit: {take_profit}"
            )
            return simulated_order

        if not self.live_trading_enabled:
            raise RuntimeError(
                "LIVE TRADING IS DISABLED. Real orders cannot be placed. "
                "Allmightsee Prime is operating in paper-trading-only mode."
            )

        # ====================================================================
        # REAL ORDER SUBMISSION (Phase 3.2c execution engine)
        # --------------------------------------------------------------------
        # Only reachable when BOTH safety guards above have passed:
        #   1) DRY_RUN is OFF  (explicit, reviewed configuration change)
        #   2) live_trading_enabled is ON
        # Binance trading-rule checks (format_quantity, format_price,
        # validate_order) are enforced BEFORE any request reaches the API.
        # ====================================================================
        symbol_pair = f"{symbol.upper()}USDT" if not symbol.upper().endswith("USDT") else symbol.upper()
        side_upper = (side or "").upper()
        order_type_upper = (order_type or "MARKET").upper()

        if side_upper not in ("BUY", "SELL"):
            return self._execution_report(
                symbol=symbol_pair,
                side=side_upper,
                quantity=quantity,
                order_type=order_type_upper,
                price=price,
                status="REJECTED",
                success=False,
                error=f"Invalid side '{side}' — must be BUY or SELL.",
            )

        if not quantity or quantity <= 0:
            return self._execution_report(
                symbol=symbol_pair,
                side=side_upper,
                quantity=quantity,
                order_type=order_type_upper,
                price=price,
                status="REJECTED",
                success=False,
                error="Quantity must be a positive number.",
            )

        # Apply Binance LOT_SIZE step-size rounding (risk control, unchanged).
        qty_ok, formatted_qty, qty_err = self.format_quantity(symbol_pair, quantity)
        if not qty_ok:
            return self._execution_report(
                symbol=symbol_pair,
                side=side_upper,
                quantity=quantity,
                order_type=order_type_upper,
                price=price,
                status="REJECTED",
                success=False,
                error=qty_err,
            )

        params: Dict[str, Any] = {
            "symbol": symbol_pair,
            "side": side_upper,
            "type": order_type_upper,
            "quantity": formatted_qty,
        }

        reference_price = price
        formatted_price = price

        if order_type_upper == "LIMIT":
            if not price or price <= 0:
                return self._execution_report(
                    symbol=symbol_pair,
                    side=side_upper,
                    quantity=formatted_qty,
                    order_type=order_type_upper,
                    price=price,
                    status="REJECTED",
                    success=False,
                    error="A positive price is required for LIMIT orders.",
                )
            price_ok, formatted_price, price_err = self.format_price(symbol_pair, price)
            if not price_ok:
                return self._execution_report(
                    symbol=symbol_pair,
                    side=side_upper,
                    quantity=formatted_qty,
                    order_type=order_type_upper,
                    price=price,
                    status="REJECTED",
                    success=False,
                    error=price_err,
                )
            valid, validation_error = self.validate_order(symbol_pair, formatted_qty, formatted_price)
            if not valid:
                return self._execution_report(
                    symbol=symbol_pair,
                    side=side_upper,
                    quantity=formatted_qty,
                    order_type=order_type_upper,
                    price=formatted_price,
                    status="REJECTED",
                    success=False,
                    error=validation_error,
                )
            params["price"] = formatted_price
            params["timeInForce"] = (time_in_force or "GTC").upper()

        elif order_type_upper == "MARKET":
            # Notional check uses the provided price or the live market price.
            if not reference_price:
                price_ok_live, reference_price, _ = self.get_current_price(symbol_pair)
                if not price_ok_live:
                    reference_price = None
            if reference_price and reference_price > 0:
                valid, validation_error = self.validate_order(symbol_pair, formatted_qty, reference_price)
                if not valid:
                    return self._execution_report(
                        symbol=symbol_pair,
                        side=side_upper,
                        quantity=formatted_qty,
                        order_type=order_type_upper,
                        price=reference_price,
                        status="REJECTED",
                        success=False,
                        error=validation_error,
                    )

        else:
            return self._execution_report(
                symbol=symbol_pair,
                side=side_upper,
                quantity=formatted_qty,
                order_type=order_type_upper,
                price=price,
                status="REJECTED",
                success=False,
                error=f"Unsupported order type '{order_type}' — use MARKET or LIMIT.",
            )

        # Submit the order to the signed Binance endpoint and build the report.
        submitted = self._signed_request("POST", "/api/v3/order", params)
        if not submitted["success"]:
            return self._execution_report(
                symbol=symbol_pair,
                side=side_upper,
                quantity=formatted_qty,
                order_type=order_type_upper,
                price=price,
                status="REJECTED",
                stop_loss=stop_loss,
                take_profit=take_profit,
                success=False,
                error=submitted["error"],
            )

        raw = submitted["data"]
        raw_status = raw.get("status", "UNKNOWN")
        executed_qty = float(raw.get("executedQty", 0.0) or 0.0)
        quote_qty = float(raw.get("cummulativeQuoteQty", 0.0) or 0.0)
        execution_price = (quote_qty / executed_qty) if executed_qty else (price if price else reference_price)

        return self._execution_report(
            symbol=symbol_pair,
            side=side_upper,
            quantity=formatted_qty,
            order_type=order_type_upper,
            price=formatted_price if order_type_upper == "LIMIT" else (price if price else reference_price),
            order_id=raw.get("orderId"),
            status=raw_status,
            executed_quantity=executed_qty,
            execution_price=execution_price,
            client_order_id=raw.get("clientOrderId"),
            stop_loss=stop_loss,
            take_profit=take_profit,
            raw_order=raw,
        )


    def get_order_status(
        self,
        symbol: str,
        order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Retrieve and confirm the current status of an order (GET /api/v3/order).

        Returns the raw Binance status plus a normalized label so callers can
        distinguish filled, partially filled, pending, cancelled and rejected
        orders (Phase 3.2c execution engine).

        DRY_RUN is checked first — in simulation mode no request is ever sent
        and a simulated confirmation (order status "SIMULATED") is returned.
        """
        symbol_pair = f"{symbol.upper()}USDT" if not symbol.upper().endswith("USDT") else symbol.upper()

        if self.dry_run:
            print(
                "[DRY_RUN] Simulated order status confirmation — NO request sent to Binance | "
                f"Symbol: {symbol_pair} | Order ID: {order_id}"
            )
            return {
                "success": True,
                "dry_run": True,
                "symbol": symbol_pair,
                "order_id": order_id,
                "status": "SIMULATED",
                "status_label": "simulated",
                "executed_quantity": 0.0,
                "execution_price": None,
                "error": None,
                "raw_order": None,
            }

        if not order_id:
            return {
                "success": False,
                "dry_run": False,
                "symbol": symbol_pair,
                "order_id": None,
                "status": None,
                "status_label": self.classify_order_status(None),
                "error": "order_id is required to query the order status.",
                "raw_order": None,
            }

        res = self._signed_request("GET", "/api/v3/order", {"symbol": symbol_pair, "orderId": order_id})
        if not res["success"]:
            return {
                "success": False,
                "dry_run": False,
                "symbol": symbol_pair,
                "order_id": order_id,
                "status": None,
                "status_label": self.classify_order_status(None),
                "error": res["error"],
                "raw_order": None,
            }

        raw = res["data"]
        raw_status = raw.get("status", "UNKNOWN")
        executed_qty = float(raw.get("executedQty", 0.0) or 0.0)
        quote_qty = float(raw.get("cummulativeQuoteQty", 0.0) or 0.0)
        execution_price = (quote_qty / executed_qty) if executed_qty else None

        return {
            "success": True,
            "dry_run": False,
            "symbol": raw.get("symbol", symbol_pair),
            "side": raw.get("side"),
            "quantity": float(raw.get("origQty", 0.0) or 0.0),
            "price": float(raw.get("price", 0.0) or 0.0),
            "order_id": raw.get("orderId", order_id),
            "order_type": raw.get("type"),
            "status": raw_status,
            "status_label": self.classify_order_status(raw_status),
            "executed_quantity": executed_qty,
            "execution_price": execution_price,
            "client_order_id": raw.get("clientOrderId"),
            "error": None,
            "raw_order": raw,
        }


    def cancel_order(
        self,
        symbol: str,
        order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Cancel an existing order (DELETE /api/v3/order) and return its final status.

        A successfully cancelled order is normalized to the "cancelled" label.

        DRY_RUN is checked first — in simulation mode no request is ever sent
        and a simulated cancellation is returned instead.
        """
        symbol_pair = f"{symbol.upper()}USDT" if not symbol.upper().endswith("USDT") else symbol.upper()

        if self.dry_run:
            print(
                "[DRY_RUN] Simulated cancel — NO real cancel sent to Binance | "
                f"Symbol: {symbol_pair} | Order ID: {order_id}"
            )
            return {
                "success": True,
                "dry_run": True,
                "symbol": symbol_pair,
                "order_id": order_id,
                "status": "CANCELED",
                "status_label": self.classify_order_status("CANCELED"),
                "executed_quantity": 0.0,
                "error": None,
                "raw_order": None,
            }

        if not order_id:
            return {
                "success": False,
                "dry_run": False,
                "symbol": symbol_pair,
                "order_id": None,
                "status": None,
                "status_label": self.classify_order_status(None),
                "error": "order_id is required to cancel an order.",
                "raw_order": None,
            }

        res = self._signed_request("DELETE", "/api/v3/order", {"symbol": symbol_pair, "orderId": order_id})
        if not res["success"]:
            return {
                "success": False,
                "dry_run": False,
                "symbol": symbol_pair,
                "order_id": order_id,
                "status": None,
                "status_label": self.classify_order_status(None),
                "error": res["error"],
                "raw_order": None,
            }

        raw = res["data"]
        raw_status = raw.get("status", "CANCELED")
        return {
            "success": True,
            "dry_run": False,
            "symbol": raw.get("symbol", symbol_pair),
            "side": raw.get("side"),
            "quantity": float(raw.get("origQty", 0.0) or 0.0),
            "price": float(raw.get("price", 0.0) or 0.0),
            "order_id": raw.get("orderId", order_id),
            "order_type": raw.get("type"),
            "status": raw_status,
            "status_label": self.classify_order_status(raw_status),
            "executed_quantity": float(raw.get("executedQty", 0.0) or 0.0),
            "error": None,
            "raw_order": raw,
        }


# Global default service instance
binance_service = BinanceService()
