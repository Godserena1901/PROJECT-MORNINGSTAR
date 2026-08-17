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
"""

import os
import time
import hmac
import hashlib
import math
from typing import Dict, Any, Optional, List, Tuple
from urllib.parse import urlencode
import requests
from dotenv import load_dotenv

load_dotenv()


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

    def place_order(
        self,
        symbol: Optional[str] = None,
        side: Optional[str] = None,
        quantity: Optional[float] = None,
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        *args,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Execution-layer entry point for order placement.

        DRY_RUN MODE (Phase 3.2b) — checked FIRST, on by default:
        While self.dry_run is True, this method NEVER contacts Binance to place a
        real order. It only simulates and logs the intended order (symbol, side,
        quantity, entry/current price, stop loss, take profit) and returns a
        structured simulation result. This check happens before, and independently
        of, the live-trading safety shield below, so DRY_RUN alone is sufficient to
        guarantee no real order is ever sent.

        SAFETY SHIELD (unchanged from Phase 3.2): Live order placement remains
        strictly blocked whenever live_trading_enabled is False. This guard is
        permanent until a future phase explicitly enables live trading via a
        confirmed, reviewed configuration change. Paper trading remains the only
        permitted execution mode alongside DRY_RUN simulation.
        """
        if self.dry_run:
            simulated_order = {
                "status": "SIMULATED",
                "dry_run": True,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "price": price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            }
            print(
                "[DRY_RUN] Simulated order — NO real order sent to Binance | "
                f"Symbol: {symbol} | Side: {side} | Quantity: {quantity} | "
                f"Entry/Current Price: {price} | Stop Loss: {stop_loss} | "
                f"Take Profit: {take_profit}"
            )
            return simulated_order

        if not self.live_trading_enabled:
            raise RuntimeError(
                "LIVE TRADING IS DISABLED. Real orders cannot be placed. "
                "Allmightsee Prime is operating in paper-trading-only mode."
            )
        raise NotImplementedError("Live order placement has not been implemented.")


# Global default service instance
binance_service = BinanceService()
