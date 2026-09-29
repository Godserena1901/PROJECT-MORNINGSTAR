# Allmightsee Prime

**Project Morningstar** — a Python **paper-trading / market-analysis bot** with a Telegram control interface and a read-only Binance market-data integration. The system scans crypto markets, produces rule-based technical-analysis signals, simulates trades against an in-memory paper book, tracks simulated performance, and exposes the results through Telegram commands and a set of standalone verification/evidence scripts.

> **Live trading is not enabled.** Allmightsee Prime is a research and simulation project. It does **not** place real orders and does **not** trade real funds. No profitability is claimed or implied. See [Safety and live-trading limitations](#safety-and-live-trading-limitations).

---

## Project status

- **State:** Active development — **paper/simulation only**.
- **Execution mode:** `DRY_RUN=True` by default; order intents are simulated and logged, never sent.
- **Live trading:** **Not enabled.** `LIVE_TRADING_ENABLED` defaults to `False`, and the paper book refuses to operate if it is ever enabled.
- **Tests:** 17 automated tests passing locally (`pytest`), network fully mocked.
- **Maturity:** The decision, market-data, paper-book, and evidence-collection layers are implemented and covered by verification scripts. Live/capital deployment is intentionally out of scope.

---

## Key features

- **Telegram bot interface** with owner-restricted commands for scanning, analysis, paper positions, statistics, alerts, and exchange status.
- **Rule-based technical-analysis engine** producing `BUY` / `SELL` / `HOLD` signals with confidence, risk rating, entry, stop-loss, take-profit, and risk/reward.
- **Binance market-data integration** (public endpoints) for prices, klines/OHLCV, symbol trading rules, server time, and connectivity.
- **Paper trading / simulation engine** with a simulated balance, fixed per-trade allocation, position sizing rounded to Binance lot step sizes, and take-profit / stop-loss exits.
- **Performance analytics** for the paper book: win rate, total/average P&L, profit factor, best/worst trade, average win/loss, and a directional (BUY/SELL) breakdown.
- **Execution service with safety gates**: order intents flow through a single `place_order()` entry point that checks `DRY_RUN` first and the live-trading shield second.
- **Order-rule validation** using Binance `LOT_SIZE` / `PRICE_FILTER` / `NOTIONAL` rules (step size, tick size, min quantity, min notional).
- **Price alerts** checked by a background loop.
- **Phase verification and evidence tooling**: standalone scripts that audit the decision → execution → paper → monitoring path and collect/analyse simulated performance evidence.
- **Automated tests** for the execution engine's safety behavior and the paper-trading / analysis logic.

---

## Technical stack

| Area | Technology |
|------|------------|
| Language | Python (developed and tested on Python 3.8.9) |
| Data / analysis | `pandas` |
| Technical indicators | `ta` (`EMAIndicator`, `RSIIndicator`, `MACD`, `AverageTrueRange`) |
| Exchange HTTP client | `requests` (with `requests.Session`) |
| Telegram interface | `python-telegram-bot` (`Application`, `CommandHandler`, `run_polling`) |
| Configuration | `python-dotenv` (`.env`); environment variables |
| Testing | `pytest` (with `unittest.mock`) |
| Dependencies | Declared in `requirements.txt` |

`requirements.txt`:

```text
pandas
requests
python-dotenv
ta
python-telegram-bot
```

---

## High-level architecture / components

The project is organised in layers, from user-facing interface down to simulated execution:

```text
                 Telegram user
                      |
                      v
+---------------------------------------------------------------+
| main.py  (interface + strategy + paper book + monitor loop)    |
|  - Command handlers (/start, /scan, /price, /paper, /stats,    |
|    /exchange, /alert, /status, /ping, /help)                   |
|  - analyze_symbol() / score_conditions()  -> rule-based signal |
|  - get_klines()  -> delegates fetch, attaches indicators       |
|  - open_paper_trade() / check_paper_positions() / stats        |
|  - background loop: price alerts + open-position monitoring    |
+---------------------------------------------------------------+
                      |
                      v
+---------------------------------------------------------------+
| binance_service.py  (BinanceService, shared singleton)         |
|  - Public market data: ping, server time, ticker price,        |
|    klines, exchangeInfo (symbol rules)                         |
|  - Order-rule helpers: format_quantity, format_price,          |
|    validate_order                                              |
|  - Execution entry point: place_order() with DRY_RUN + shield  |
|  - Account (read-only): account info, balances, permissions    |
|  - DRY_RUN simulation for order/status/cancel                  |
+---------------------------------------------------------------+
                      |
                      v
+---------------------------------------------------------------+
| Binance REST API  (public + optionally signed/read-only)       |
+---------------------------------------------------------------+

Standalone orchestration / verification layers (not part of the bot
process): phase_*_verify_*.py audits, phase_7/8 autopilot paper loops,
phase_9_metrics / phase_9_3..9.7 evidence + analysis scripts.
```

**Component summary**

- **`main.py`** — the bot entry point. Owns the Telegram handlers, the analysis engine, the in-memory paper book, and a 60-second background loop that checks price alerts and open paper positions.
- **`binance_service.py`** — the exchange service. Centralises every Binance call so safety checks (`DRY_RUN`, live-trading shield, order-rule validation) live in one auditable place.
- **Phase scripts** — standalone audit / analysis / evidence tooling (see [Project structure](#project-structure)). They reuse `main.py` and `binance_service.py`; they do not replace them.

---

## Paper-trading / simulation functionality

The bot runs an **in-memory paper trading book** (`main.py`). No real orders are involved.

**Account model**

- Starting simulated balance: `INITIAL_PAPER_BALANCE = 1000.0` (USD).
- Simulated capital allocated per trade: `PAPER_POSITION_SIZE_USD = 100.0`.
- Open positions: `PAPER_POSITIONS`; closed trades: `PAPER_TRADE_HISTORY`; current balance: `PAPER_BALANCE`.
- State lives in process memory (see [Current limitations](#current-limitations)).

**Opening a simulated trade — `open_paper_trade(analysis)`**

1. Refuses to run if `binance_service.live_trading_enabled` is `True` (paper and live are mutually exclusive).
2. Refuses to run if `binance_service.dry_run` is `False` (paper flow requires DRY_RUN simulation).
3. Only accepts `BUY` or `SELL` signals and rejects duplicate open positions for the same symbol.
4. Rounds entry, stop-loss, and take-profit to Binance tick size, and rounds quantity to Binance lot step size (falling back to raw values if rules cannot be fetched, e.g. offline).
5. Routes the intended entry order through `binance_service.place_order()`. Only a confirmed `DRY_RUN` **simulated** execution is accepted into the paper book — a non-simulated result is rejected defensively.
6. Records the position (side, entry, SL, TP, confidence, quantity, simulated size, simulated order ID, execution status/price).

**Closing a simulated trade — `check_paper_positions()`**

- Uses `binance_service.get_current_price()` for the latest price, falling back to the last close of a short 1-minute klines fetch.
- For **BUY** positions: closes at take-profit when price ≥ TP, at stop-loss when price ≤ SL.
- For **SELL** positions: closes at take-profit when price ≤ TP, at stop-loss when price ≥ SL.
- Simulated P&L is scaled by position quantity:
  - BUY: `quantity * (exit_price - entry_price)`
  - SELL: `quantity * (entry_price - exit_price)`
- The simulated balance is updated and the closed trade is appended to `PAPER_TRADE_HISTORY`.
- The exit intent is also routed through `place_order()` for consistent DRY_RUN logging.

**Statistics — `get_paper_statistics()`**

Returns total trades, wins/losses, win rate, total and average P&L, gross profit/loss, profit factor, best/worst trade, average win/loss, BUY/SELL trade and win counts, and the most recent closed trades.

**Monitoring loop**

While the bot is running, a background task wakes every 60 seconds, evaluates pending price alerts, and calls `check_paper_positions()`.

---

## Telegram bot functionality and existing commands

The bot is built with `python-telegram-bot` and started with long polling (`app.run_polling()`). Commands are registered in `main.py`:

| Command | Purpose |
|---------|---------|
| `/start` | Greeting / liveness message. |
| `/help` | Lists available commands and supported coins (owner-restricted). |
| `/scan` | Pings Binance, verifies each symbol is in `TRADING` status, analyses all supported coins, ranks signals by confidence, and attempts to open a paper trade on the best opportunity. |
| `/price <COIN>` | Detailed analysis and signal for one coin: price, EMA20, RSI, MACD, MACD signal, histogram, volume, average volume, signal, confidence, risk, entry, stop-loss, take-profit, risk/reward, and reason. |
| `/paper` | Paper-trading account summary: simulated balance, return %, open positions with entry/SL/TP and simulated order IDs. |
| `/stats` | Detailed paper performance statistics and recent closed trades. |
| `/exchange` | Binance API connectivity, latency, server-time drift, masked API key, live/DRY_RUN status, read-only API-permission check, and (when credentials are configured) read-only account balances. |
| `/alert <COIN> <PRICE>` | Registers a price alert; the background loop notifies when the coin's price reaches or exceeds the target. |
| `/status` | Bot operational status. |
| `/ping` | Latency/response check. |

**Access control:** every command except `/start` is restricted to the owner Telegram user ID configured as `OWNER_ID` in `main.py`. Other users receive a "private testing/mode" message.

**Supported coins:** `BTC`, `ETH`, `SOL`, `BNB`, `XRP`.

**Background behaviour:** a task started at application init checks registered price alerts and open paper positions every 60 seconds.

---

## Binance market-data integration

All Binance access is centralised in `BinanceService` (`binance_service.py`) and shares a single `requests.Session`. Only the public endpoints are needed for the bot's core functionality; the signed endpoints are used only for optional read-only account information and for the (disabled) execution path.

**Public endpoints used**

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `ping()` | `GET /api/v3/ping` | Connectivity check with measured latency. |
| `get_server_time()` | `GET /api/v3/time` | Server time and local clock-drift measurement. |
| `get_current_price(symbol)` | `GET /api/v3/ticker/price` | Latest price for a symbol. |
| `get_klines(symbol, interval, limit)` | `GET /api/v3/klines` | OHLCV candles. Validates the interval, normalises/validates the symbol, handles empty/malformed/HTTP-failure responses, and rejects stale candles. |
| `get_symbol_rules(symbol)` | `GET /api/v3/exchangeInfo` | Trading rules: `LOT_SIZE` (min/max quantity, step size), `PRICE_FILTER` (min/max price, tick size), and `NOTIONAL`/`MIN_NOTIONAL`. Cached in-process. |

Supported kline intervals: `1m`, `5m`, `15m`, `30m`, `1h`, `4h`, `1d`. The analysis engine uses `15m`, `1h`, and `4h`.

**Order-rule helpers (no order placed)**

- `format_quantity()` — rounds a quantity down to the symbol's lot step size and enforces min/max quantity.
- `format_price()` — rounds a price to the symbol's tick size and enforces min/max price.
- `validate_order()` — combines the above and checks the order notional against the symbol's minimum notional.

**Read-only account endpoints (optional, require credentials)**

- `get_account_info()` → `GET /api/v3/account`
- `get_account_balances()` / `get_available_usdt()` — free, locked, and total balances.
- `assert_api_permissions()` — read-only check confirming the key can trade and that withdrawal is disabled (the read-only mandate). It never places an order and never changes `DRY_RUN` or `LIVE_TRADING_ENABLED`.
- `get_masked_api_key()` — returns a masked key for safe display.

**Execution engine (DRY_RUN-first; live path disabled)**

- `place_order()` — the single entry point for order intents. Checks `DRY_RUN` first (simulate + log, never contact Binance) and the live-trading shield second.
- `get_order_status()` / `cancel_order()` — both short-circuit to simulated results while `DRY_RUN` is `True`.
- `classify_order_status()` — normalises raw Binance statuses to `pending` / `filled` / `partially_filled` / `cancelled` / `rejected` / `unknown`.

---

## Technical-analysis indicators actually used

Indicators are computed with the `ta` library in `main.get_klines()` and read on the **latest candle** in `analyze_symbol()`.

| Indicator | Library call | Parameters | Used for |
|-----------|--------------|------------|----------|
| EMA20 | `ta.trend.EMAIndicator` | window = 20 | Trend filter (price above/below EMA20) on the 15m frame, and on the 1h / 4h frames. |
| RSI | `ta.momentum.RSIIndicator` | window = 14 | Momentum band scoring and setup gating. |
| MACD | `ta.trend.MACD` | default (12, 26, 9) | `macd`, `macd_signal`, and `macd_histogram` (`macd_diff()`). |
| ATR | `ta.volatility.AverageTrueRange` | default (14) | Volatility filter and stop-loss / take-profit sizing. |
| Volume | raw kline volume | — | Compares the latest volume against the trailing 20-candle mean (`strong_volume`). |

**Signal construction (`analyze_symbol` / `score_conditions`)**

Bullish and bearish conditions are weighted and summed into a `bullish_score` and `bearish_score` (each up to 105):

- 1h trend above/below EMA20 — 20
- 4h trend above/below EMA20 — 20
- `strong_volume` (latest volume > 20-candle mean) — 10
- Price above/below EMA20 — 15
- MACD above/below its signal line — 15
- MACD histogram above/below zero — 10
- RSI band (`45–69` bullish, `31–55` bearish) — 10
- ATR above zero — 5

A `BUY` setup requires `bullish_score >= 70`, `bullish_score >= bearish_score + 15`, price above EMA20, MACD above signal, a positive histogram, and RSI in `40–68`. A `SELL` setup is the mirrored condition. Otherwise the result is `HOLD` with an explanatory reason. Confidence is the winning score; risk is `LOW`/`MEDIUM`/`HIGH`. For actionable setups, stop-loss and take-profit are derived from ATR (1.5x and 2.5x respectively), giving a risk/reward ratio.

These are deterministic, rule-based heuristics — **not** a prediction of future returns and **not** investment advice.

---

## Safety and live-trading limitations

**Live trading is not enabled.** The project is a paper-trading and simulation system.

- **`DRY_RUN` defaults to `True`.** While true, `place_order()` simulates and logs the intended order and never contacts Binance to place an order.
- **`LIVE_TRADING_ENABLED` defaults to `False`.** When live trading is disabled, `place_order()` raises a `RuntimeError` shield instead of submitting anything.
- **Paper trading is blocked whenever live trading is enabled**, and also blocked whenever `DRY_RUN` is off — the paper flow only ever accepts a confirmed `SIMULATED` result from the execution engine.
- **No real orders are placed by this project's default configuration.** Although the execution service contains a guarded, signed order-submission code path (`POST`/`GET`/`DELETE /api/v3/order`), it is switched off by default, is not exercised by the bot's paper workflow, and is **not enabled in this repository**.
- **No real-money capital is used.** The bot never funds, moves, or trades real funds, and the default configuration cannot place orders. Credentials are optional; the core bot works with public market data only, and any account access via those credentials is read-only. (The separate Phase 5 script targets a Binance **testnet** account, not a funded production account.)
- **Secret hygiene.** Credentials live only in a local `.env` file, which is git-ignored (`.gitignore` contains `.env`). No keys are hard-coded or committed.
- **Read-only API mandate.** If credentials are supplied, the permission check verifies `canTrade` and requires withdrawal to be disabled.
- **Tests never hit the network.** The test suite mocks the exchange session, so it cannot place an order.

**Not financial advice.** Signals are heuristic and unvalidated for profitability; the simulated evidence collected so far is explicitly treated as insufficient to support a profitability claim.

---

## Project structure

```text
PROJECT-MORNINGSTAR/
├── main.py                          # Bot entry point: Telegram handlers, analysis engine,
│                                    #   in-memory paper book, background monitor loop
├── main 2.py                        # Early minimal scaffold bot (legacy, single /start handler)
├── binance_service.py               # BinanceService: market data, symbol rules, validation,
│                                    #   DRY_RUN-first execution engine, read-only account access
├── requirements.txt                 # Python dependencies
├── .env.example                     # Template for environment configuration (no real secrets)
├── .gitignore                       # Ignores venv/ and .env
│
├── tests/
│   ├── test_binance_service.py      # Execution-engine safety + order-rule validation tests (7)
│   └── test_main.py                 # Paper book, statistics, klines, analysis tests (10)
│
├── phase_3_1_verify_permissions.py  # API-permission verification (read-only)
├── phase_3_4_1_verify_orders.py     # Order placement verification
├── phase_3_4_2_verify_lifecycle.py  # Order lifecycle verification
├── phase_3_4_3_verify_positions.py  # Position handling verification
├── phase_3_4_4_verify_balance.py    # Balance retrieval verification
├── phase_3_4_5_verify_reconnect.py  # Reconnect / resilience verification
├── phase_3_4_6_verify_extended.py   # Leftover placeholder stub (not a runnable verification)
├── phase_4_verify_market_data.py    # Market-data (klines/ticker) verification
├── phase_5_verify_testnet.py        # Testnet execution verification (needs testnet keys)
├── phase_6_verify_readiness.py      # Offline live-trading readiness gate (stubbed transport)
├── phase_7_autopilot.py             # Scheduled paper-trading autopilot (risk limits)
├── phase_7_verify_autopilot.py      # Autopilot verification
├── phase_8_autopilot.py             # Supervised 24/7 paper autopilot loop
├── phase_8_verify_autopilot.py      # Autopilot verification
├── phase_9_metrics.py               # Phase 9.1 metrics recorder (read-only)
├── phase_9_verify_metrics.py        # Metrics verification
├── phase_9_2_verify_shadow.py       # Shadow-mode (non-mutating) engine verification
├── phase_9_3_beta.py                # Controlled beta / evidence collection
├── phase_9_3_verify_beta.py         # Beta collector verification
├── phase_9_4_analysis.py            # Performance analysis over collected evidence
├── phase_9_4_verify_analysis.py     # Analysis verification
├── phase_9_5_validation.py          # Strategy / market-condition validation
├── phase_9_5_verify_validation.py   # Validation verification
├── phase_9_6_stability.py           # Stability / endurance validation
├── phase_9_6_verify_stability.py    # Stability verification
├── phase_9_7_gonogo.py              # Final Phase 9 GO/NO-GO consolidation (analysis only)
├── phase_9_7_verify_gonogo.py       # GO/NO-GO verification
├── stage0_evidence_run.py           # Controlled evidence-collection run (public data only)
├── stage0_evidence/                 # Generated evidence ledger + summaries + run log
└── beta_*.json                      # Generated Phase 9 analysis/validation/stability/GO-NO-GO reports
```

The Phase 3–9 scripts and `stage0_evidence_run.py` are **standalone** tools. They reuse the validated `main.py` / `binance_service.py` layers for auditing, paper orchestration, and evidence collection; they are not required to run the Telegram bot and they do not enable live trading.

---

## Installation / setup

Prerequisites: Python 3.8+ (developed and tested on 3.8.9) and `pip`. A `venv/` folder is used in this workspace and is git-ignored.

```bash
# 1. Clone the repository
git clone https://github.com/Godserena1901/PROJECT-MORNINGSTAR.git
cd PROJECT-MORNINGSTAR

# 2. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
```

Dependencies are exactly those declared in `requirements.txt`: `pandas`, `requests`, `python-dotenv`, `ta`, and `python-telegram-bot`.

---

## Environment / configuration

Configuration is read from environment variables, loaded from a local `.env` file via `python-dotenv` (`load_dotenv()` is called in both `main.py` and `binance_service.py`). **Never commit `.env`** — it is git-ignored, and `.env.example` is the template.

```bash
cp .env.example .env
```

**Variables used by the code**

| Variable | Required? | Default in code | Purpose |
|----------|-----------|-----------------|---------|
| `BOT_TOKEN` | Yes (to run the bot) | none | Telegram bot token. `main()` raises if it is missing. |
| `BINANCE_API_KEY` | No | `""` | Read-only account access / optional signed endpoints. The bot runs on public data without it. |
| `BINANCE_API_SECRET` | No | `""` | Pairs with `BINANCE_API_KEY`. |
| `BINANCE_BASE_URL` | No | `https://api.binance.com` | Base URL for Binance REST calls (can point at a sandbox). |
| `BINANCE_TESTNET_API_KEY` | No | none read in `binance_service.py` | Used by the Phase 5 testnet verification script. |
| `BINANCE_TESTNET_API_SECRET` | No | none read in `binance_service.py` | Pairs with the testnet key. |
| `LIVE_TRADING_ENABLED` | No | `False` | Live-trading shield. **Leave `False`.** |
| `DRY_RUN` | No | `True` | Simulation mode. **Leave `True`.** |

Notes:

- Values are parsed case-insensitively; `true` / `1` / `yes` are treated as enabled.
- `DRY_RUN` and `LIVE_TRADING_ENABLED` are independent layers: `DRY_RUN` is checked first, then the live-trading shield.
- Telegram command access is further restricted to the owner `OWNER_ID` constant in `main.py`.
- Keep secrets only in `.env` (or your environment); do not paste them into source, issues, or commits. The `/exchange` command displays only a masked key.

---

## How to run the project

All commands below assume the virtual environment is activated and the working directory is the repository root.

**1. Run the Telegram bot (primary entry point)**

```bash
python3 main.py
```

- `main()` validates that `BOT_TOKEN` is set and raises `RuntimeError("BOT_TOKEN is missing. Add it to your .env file.")` otherwise.
- It registers the handlers (`/start`, `/help`, `/status`, `/ping`, `/paper`, `/alert`, `/price`, `/scan`, `/stats`, `/exchange`), starts the background monitor, prints a startup message, and begins long polling.
- With the default configuration (`DRY_RUN=True`, `LIVE_TRADING_ENABLED=False`), any order intent is simulated and logged only.

**2. Standalone verification / orchestration scripts (optional)**

These are runnable modules with their own documented usage; they are not needed for the bot. Representative examples:

```bash
# Offline live-trading readiness gate (stubbed transport; no requests/orders sent)
python3 phase_6_verify_readiness.py

# Market-data / execution / shadow-mode verification
python3 phase_4_verify_market_data.py
python3 phase_9_2_verify_shadow.py

# Paper-trading autopilot (scheduled / supervised paper loops)
python3 phase_7_autopilot.py
python3 phase_8_autopilot.py

# Testnet execution verification (requires BINANCE_TESTNET_API_KEY / _SECRET)
BINANCE_TESTNET_API_KEY=... BINANCE_TESTNET_API_SECRET=... python3 phase_5_verify_testnet.py

# Controlled evidence collection (public market data only; no real orders)
python3 stage0_evidence_run.py --interval 300 --max-cycles 200 \
    --symbols BTC,ETH,SOL --evidence-dir stage0_evidence

# Phase 9 analysis / validation / GO-NO-GO over collected evidence
python3 phase_9_7_gonogo.py --evidence stage0_evidence/beta_evidence.jsonl \
    --summary stage0_evidence/beta_evidence_summary.json --out beta_gonogo_9_7.json
```

Most verification scripts exit `0` when all checks pass and `1` when one or more checks fail; the autopilot scripts exit `0` on a clean stop and `1` on a fatal error. Some scripts intentionally force their own safe values (`DRY_RUN=True`, `LIVE_TRADING_ENABLED=False`) for the duration of the process.

---

## How to run the test suite

The tests require `pytest` (not listed in `requirements.txt`) and use `unittest.mock`; **no network access is required** — the exchange session is mocked.

```bash
# From the repository root, with the virtual environment active
python3 -m pytest tests -v
```

---

## Current testing status

**17 tests passing locally.**

```text
tests/test_binance_service.py   7 passed   # place_order guards / DRY_RUN behavior (session
                                           #   mocked) + validate_order quantity & price rules
tests/test_main.py             10 passed   # paper trades, TP/SL exits, statistics,
                                           #   get_klines indicators, analyze_symbol signals

----------------------------- 17 passed, 1 warning -----------------------------
```

The single warning is an environment-level `urllib3`/`NotOpenSSLWarning` caused by the local Python build using LibreSSL; it is unrelated to the application logic.

---

## Current limitations

- **No live trading.** Live trading is not enabled and has not been validated for real capital. The default configuration cannot place real orders.
- **No profitability claim.** The strategies are rule-based heuristics. The Phase 9 evidence layer explicitly reports that the collected simulated evidence is insufficient to support a profitability verdict (`GO_NOGO_DEFERRED`) because no approved thresholds or minimum-sample gates exist.
- **In-memory paper state.** In `main.py`, `PAPER_BALANCE`, `PAPER_POSITIONS`, `PAPER_TRADE_HISTORY`, and `ALERTS` are process-local; they are not persisted by the bot and reset when it restarts. (The Phase 7–9 scripts implement their own durable ledgers.)
- **Single-process bot.** State is not shared across processes or instances; there is no database, queue, or multi-user account model.
- **Owner-only interface.** Commands other than `/start` are gated to a single `OWNER_ID`; there is no multi-user or role model.
- **Limited symbol universe.** `BTC`, `ETH`, `SOL`, `BNB`, `XRP`, on `15m` / `1h` / `4h` timeframes.
- **Alerts are upside-only.** The alert loop notifies when price reaches or exceeds the stored target; there is no "below target" alert.
- **Polling-based monitoring.** The background loop checks every 60 seconds, so exits (TP/SL) and alerts are not tick-accurate.
- **No backtesting framework.** Performance evaluation relies on forward, simulated evidence collected by the Phase 9 / Stage 0 scripts.
- **No CI/CD configuration** is present in the repository.
- **Public market data requires connectivity.** Analysis and paper monitoring depend on the Binance public API; failures surface as user-facing error messages and are handled per call.

---

## Future development (planned — not implemented)

> Everything in this section is **planned only**. None of it exists in the current code, and nothing here should be read as a current capability or a promise of profitability.

- **Persistent paper state** (database or file-backed) so balances, positions, and trade history survive restarts.
- **Additional symbols and timeframes**, driven by configuration rather than constants.
- **Backtesting and walk-forward evaluation** for the rule-based signal engine.
- **Richer alerts** (below-target and percentage-change alerts) and more frequent monitoring.
- **Packaging and deployment** (containerisation, process supervision, health checks).
- **CI pipeline** running the test suite automatically.
- **Expanded test coverage** for market-data validation and phase tooling.
- **Multi-user / role-based Telegram access.**
- Any move toward **real-capital execution** would first require approved, versioned performance thresholds and a formal safety review; it is **out of scope** and **not enabled** today.

---

## Disclaimer

This project is provided for **educational and research purposes only**. It is a simulation/paper-trading system with **live trading not enabled**. It does not provide financial, investment, or trading advice. Trading crypto assets involves substantial risk; past or simulated performance does not indicate future results. Use at your own risk.
