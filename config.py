"""
Global RSI Bot — Configuration
================================
Stock universe across US, ASX, UK, EU, HK, Canada, Singapore markets.
All magic numbers / timings / thresholds live here. Do NOT hardcode in helpers.
"""

import os
from datetime import time

# ══════════════════════════════════════════════════════════════════════════
#  CONNECTION & MODE
# ══════════════════════════════════════════════════════════════════════════

IB_HOST = os.getenv("IB_GATEWAY_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_GATEWAY_PORT", "7497"))
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "10"))

# PAPER vs LIVE toggle — controlled by env var, checked everywhere
PAPER_MODE = os.getenv("TRADING_MODE", "paper").lower() == "paper"

# Market data type: 1 = live (paid), 3 = delayed (free, 15-20 min lag)
MARKET_DATA_LIVE = os.getenv("IB_MARKET_DATA", "delayed").lower() == "live"

# ══════════════════════════════════════════════════════════════════════════
#  RECONNECT / RESILIENCE
# ══════════════════════════════════════════════════════════════════════════

RECONNECT_INITIAL_DELAY = 5          # Seconds before first reconnect attempt
RECONNECT_MAX_DELAY = 300            # Max backoff (5 min)
RECONNECT_BACKOFF_FACTOR = 2.0       # Exponential backoff multiplier
RECONNECT_MAX_ATTEMPTS = 0           # 0 = infinite retries

CONNECTION_TIMEOUT = 15              # Seconds to wait for connect()
TRADE_FILL_TIMEOUT = 30              # Seconds to wait for a market order to fill

# ══════════════════════════════════════════════════════════════════════════
#  STRATEGY — RSI (backtest-optimal values)
# ══════════════════════════════════════════════════════════════════════════

RSI_PERIOD = 14
RSI_OVERSOLD = 40                    # Buy trigger
RSI_OVERBOUGHT = 60                  # RSI-based exit (default, overridable per asset)

# Entry filters — kept OFF by default because user's backtests showed OFF is optimal
# with RSI 40/60. Flip to True here if you re-run backtests and prefer filtered entries.
USE_VOLUME_FILTER = False            # Volume > 20-day avg
USE_TREND_FILTER = False             # Price > 200-day MA
USE_MA20_FILTER = False              # Price > 20-day MA (short-term trend confirmation)

# ══════════════════════════════════════════════════════════════════════════
#  EXITS
# ══════════════════════════════════════════════════════════════════════════

DEFAULT_TRAILING_STOP = 0.06         # 6% — backtest optimal
DEFAULT_TAKE_PROFIT = 0.08           # 8% — backtest optimal

# ATR-based dynamic stops — experimental alternative to fixed pct stops
USE_ATR_STOPS = False
ATR_PERIOD = 14
ATR_MULTIPLIER = 1.5

# Bracket orders — server-side TP + trailing stop attached on entry
USE_BRACKET_ORDERS = True            # CRITICAL — survives restarts, protects if bot dies

# After parent fill, re-price the take-profit limit to the ACTUAL fill price
# (otherwise TP is based on pre-fill estimate and slippage distorts the target).
REPAIR_TP_AFTER_FILL = True

# When reconciling existing positions at startup, attach protective brackets
# if none exist (prevents naked positions after a crash restart).
REATTACH_BRACKETS_ON_RECONCILE = True

# ══════════════════════════════════════════════════════════════════════════
#  POSITION SIZING
# ══════════════════════════════════════════════════════════════════════════

POSITION_SIZE_PCT = 0.15             # 15% per trade
MAX_POSITIONS = 5                    # Across all markets
CASH_RESERVE_PCT = 0.20              # Always keep 20% cash

REGIME_SIZE_MULTIPLIERS = {
    "BULL":    1.0,
    "CAUTION": 0.35,
    "BEAR":    0.0,
}

# ══════════════════════════════════════════════════════════════════════════
#  LOSS LIMITS (CRITICAL — trading halts)
# ══════════════════════════════════════════════════════════════════════════

DAILY_LOSS_LIMIT_PCT = 0.02          # -2% → halt new buys for the rest of the day
MAX_DRAWDOWN_PCT = 0.15              # -15% from peak NLV → FLATTEN ALL + halt
FLATTEN_ON_MAX_DD = True

# Timezone for "what is today" when tracking daily P&L reset.
# Default: NYSE session boundary — changes at midnight NY time.
DAILY_RESET_TZ = os.getenv("DAILY_RESET_TZ", "America/New_York")

# Set env var RESET_MAX_DD=1 on restart to clear a previously-hit max-DD flag
# (forces explicit opt-in to resuming trading after a max-DD halt).
RESET_MAX_DD_ON_START = os.getenv("RESET_MAX_DD", "").lower() in ("1", "true", "yes")

# ══════════════════════════════════════════════════════════════════════════
#  COMMISSION FILTER
# ══════════════════════════════════════════════════════════════════════════

MAX_COMMISSION_PCT = 0.03

EXCHANGE_COMMISSIONS = {
    "SMART": 1.55,
    "SGX":   3.00,
    "IBIS":  5.10,
    "SBF":   5.10,
    "AEB":   5.10,
    "SEHK":  5.50,
    "ASX":   6.00,
    "LSE":   6.00,
}

# ══════════════════════════════════════════════════════════════════════════
#  SLIPPAGE MODEL (used by backtest)
# ══════════════════════════════════════════════════════════════════════════

SLIPPAGE_BPS = {
    "SMART": 2,
    "ASX":   5,
    "LSE":   5,
    "IBIS":  8,
    "SBF":   8,
    "AEB":   8,
    "SEHK":  10,
    "SGX":   10,
}

# ══════════════════════════════════════════════════════════════════════════
#  BACKTEST FILL MODEL
# ══════════════════════════════════════════════════════════════════════════
# "next_open"  — signals on day T fill at day T+1's open (rigorous, no look-ahead)
# "same_close" — signals on day T fill at day T's close (simpler, mildly optimistic)
BACKTEST_FILL_MODE = "next_open"

# ══════════════════════════════════════════════════════════════════════════
#  SEHK BOARD LOT SIZES
# ══════════════════════════════════════════════════════════════════════════

SEHK_LOT_SIZES = {
    "700":  100, "9988": 100, "5": 400,
    "1299": 200, "388":  100, "3690": 100,
}

# ══════════════════════════════════════════════════════════════════════════
#  MARKET HOURS (with lunch-break support)
# ══════════════════════════════════════════════════════════════════════════

EXCHANGE_SESSIONS = {
    "SMART": {"tz": "America/New_York",   "open": time(9, 30),  "close": time(16, 0),  "days": [0,1,2,3,4], "lunch": None},
    "ASX":   {"tz": "Australia/Sydney",   "open": time(10, 0),  "close": time(16, 0),  "days": [0,1,2,3,4], "lunch": None},
    "LSE":   {"tz": "Europe/London",      "open": time(8, 0),   "close": time(16, 30), "days": [0,1,2,3,4], "lunch": None},
    "IBIS":  {"tz": "Europe/Berlin",      "open": time(9, 0),   "close": time(17, 30), "days": [0,1,2,3,4], "lunch": None},
    "SBF":   {"tz": "Europe/Paris",       "open": time(9, 0),   "close": time(17, 30), "days": [0,1,2,3,4], "lunch": None},
    "AEB":   {"tz": "Europe/Amsterdam",   "open": time(9, 0),   "close": time(17, 30), "days": [0,1,2,3,4], "lunch": None},
    "SEHK":  {"tz": "Asia/Hong_Kong",     "open": time(9, 30),  "close": time(16, 0),  "days": [0,1,2,3,4], "lunch": (time(12, 0), time(13, 0))},
    "SGX":   {"tz": "Asia/Singapore",     "open": time(9, 0),   "close": time(17, 0),  "days": [0,1,2,3,4], "lunch": None},
}

MARKET_HOURS_GRACE_MINS = 5

# ══════════════════════════════════════════════════════════════════════════
#  TIMING
# ══════════════════════════════════════════════════════════════════════════

SCAN_INTERVAL_SECS = 60 * 15
BARS_FOR_RSI = 250
REGIME_CACHE_TTL = 1800
MARKET_DATA_SNAPSHOT_WAIT = 3
MARKET_DATA_BATCH_WAIT = 4
RATE_LIMIT_PER_SYMBOL = 0.3
ERROR_RETRY_DELAY = 60
PARTIAL_SELL_RECONCILE_WAIT = 1.5   # Seconds to wait before reconciling after a sell

# ══════════════════════════════════════════════════════════════════════════
#  STATE PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════

STATE_FILE = os.getenv("BOT_STATE_FILE", "bot_state.json")
STATE_SAVE_ON_EVERY_FILL = True

# ══════════════════════════════════════════════════════════════════════════
#  NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

# ══════════════════════════════════════════════════════════════════════════
#  CORRELATION GROUPS
# ══════════════════════════════════════════════════════════════════════════

MAX_PER_CORR_GROUP = 2

CORR_GROUPS = {
    "us_big_tech":   {"AAPL", "GOOGL", "META", "AMZN", "NVDA", "MSFT"},
    "us_semis":      {"NVDA", "AMD", "AVGO", "ASML"},
    "us_banks":      {"JPM", "BAC", "GS", "C", "WFC"},
    "us_defence":    {"GLD", "TLT"},
    "us_sp500":      {"SPY", "SPYM"},
    "us_value":      {"T", "PFE", "INTC", "VZ", "KO"},
    "us_growth":     {"SOFI", "HOOD"},
    "us_fintech":    {"PYPL", "XYZ", "SOFI"},
    "us_auto":       {"GM", "RIVN", "TSLA"},
    "us_consumer":   {"KO", "WMT", "NKE", "DIS"},
    "us_healthcare": {"PFE", "MRK", "ABBV", "JNJ"},
    "us_telecom":    {"T", "VZ"},
    "au_banks":      {"CBA", "WBC", "NAB", "ANZ"},
    "au_miners":     {"BHP", "RIO", "FMG", "S32", "MIN"},
    "au_lithium":    {"PLS", "MIN"},
    "au_other":      {"QAN", "CSL", "JHX"},
    "uk_banks":      {"HSBA", "BARC", "LLOY", "NWG"},
    "uk_oil":        {"SHEL", "BP."},
    "uk_mining":     {"GLEN", "AAL", "RIO"},
    "uk_defence":    {"RR.", "BA."},
    "eu_luxury":     {"RMS"},
    "eu_auto":       {"BMW", "MBG", "VOW3"},
    "hk_tech":       {"9988", "700"},
    "hk_finance":    {"5", "1299", "388"},
    "ca_banks":      {"RY", "TD", "BNS", "BMO"},
    "ca_resources":  {"ENB", "CNQ", "GOLD"},
    "sg_banks":      {"D05", "U11", "O39"},
    "precious_metals": {"GLD", "GDX", "GOLD", "SLV", "FRES", "NEM"},
    "copper_exposure": {"COPX", "BHP", "ANTO", "SFR"},
    "oil_exposure":    {"USO", "XLE", "SHEL", "BP.", "WDS", "ENB", "TTE"},
    "metals_mining":   {"XME", "S32", "VALE"},
}

# ══════════════════════════════════════════════════════════════════════════
#  PER-ASSET EXIT CONFIG
# ══════════════════════════════════════════════════════════════════════════

ASSET_CONFIG = {
    "NVDA":  {"trailing_stop": 0.07, "take_profit": 0.10, "rsi_exit": 68},
    "TSLA":  {"trailing_stop": 0.08, "take_profit": 0.12, "rsi_exit": 65},
    "AMD":   {"trailing_stop": 0.07, "take_profit": 0.10, "rsi_exit": 68},
    "SOFI":  {"trailing_stop": 0.08, "take_profit": 0.12, "rsi_exit": 65},
    "HOOD":  {"trailing_stop": 0.08, "take_profit": 0.12, "rsi_exit": 65},
    "RIVN":  {"trailing_stop": 0.10, "take_profit": 0.15, "rsi_exit": 65},
    "XYZ":   {"trailing_stop": 0.08, "take_profit": 0.12, "rsi_exit": 65},
    "NKE":   {"trailing_stop": 0.07, "take_profit": 0.10, "rsi_exit": 65},
    "FMG":   {"trailing_stop": 0.07, "take_profit": 0.12, "rsi_exit": 65},
    "PLS":   {"trailing_stop": 0.10, "take_profit": 0.15, "rsi_exit": 65},
    "MIN":   {"trailing_stop": 0.08, "take_profit": 0.12, "rsi_exit": 65},
    "S32":   {"trailing_stop": 0.07, "take_profit": 0.10, "rsi_exit": 65},
    "SFR":   {"trailing_stop": 0.08, "take_profit": 0.12, "rsi_exit": 65},
    "SLV":   {"trailing_stop": 0.08, "take_profit": 0.12, "rsi_exit": 65},
    "COPX":  {"trailing_stop": 0.08, "take_profit": 0.12, "rsi_exit": 65},
    "GDX":   {"trailing_stop": 0.07, "take_profit": 0.10, "rsi_exit": 65},
    "XME":   {"trailing_stop": 0.07, "take_profit": 0.10, "rsi_exit": 65},
    "USO":   {"trailing_stop": 0.08, "take_profit": 0.12, "rsi_exit": 65},
    "GLEN":  {"trailing_stop": 0.07, "take_profit": 0.10, "rsi_exit": 65},
    "700":   {"trailing_stop": 0.07, "take_profit": 0.12, "rsi_exit": 65},
    "9988":  {"trailing_stop": 0.08, "take_profit": 0.12, "rsi_exit": 65},
    "3690":  {"trailing_stop": 0.07, "take_profit": 0.12, "rsi_exit": 65},
    "SHOP":  {"trailing_stop": 0.08, "take_profit": 0.12, "rsi_exit": 65},
}

# ══════════════════════════════════════════════════════════════════════════
#  STOCK UNIVERSE
# ══════════════════════════════════════════════════════════════════════════

STOCK_UNIVERSE = [
    # US
    ("AAPL", "SMART", "USD", "Apple"),
    ("NVDA", "SMART", "USD", "Nvidia"),
    ("AMZN", "SMART", "USD", "Amazon"),
    ("META", "SMART", "USD", "Meta"),
    ("GOOGL", "SMART", "USD", "Alphabet"),
    ("TSLA", "SMART", "USD", "Tesla"),
    ("JPM", "SMART", "USD", "JPMorgan"),
    ("V", "SMART", "USD", "Visa"),
    ("AMD", "SMART", "USD", "AMD"),
    ("NFLX", "SMART", "USD", "Netflix"),
    ("BAC", "SMART", "USD", "Bank of America"),
    ("GS", "SMART", "USD", "Goldman Sachs"),
    ("DIS", "SMART", "USD", "Disney"),
    ("COST", "SMART", "USD", "Costco"),
    ("HD", "SMART", "USD", "Home Depot"),
    ("AVGO", "SMART", "USD", "Broadcom"),
    ("CRM", "SMART", "USD", "Salesforce"),
    ("T", "SMART", "USD", "AT&T"),
    ("PFE", "SMART", "USD", "Pfizer"),
    ("SOFI", "SMART", "USD", "SoFi Technologies"),
    ("C", "SMART", "USD", "Citigroup"),
    ("WFC", "SMART", "USD", "Wells Fargo"),
    ("INTC", "SMART", "USD", "Intel"),
    ("HOOD", "SMART", "USD", "Robinhood"),
    ("UBER", "SMART", "USD", "Uber"),
    ("KO", "SMART", "USD", "Coca-Cola"),
    ("WMT", "SMART", "USD", "Walmart"),
    ("MRK", "SMART", "USD", "Merck"),
    ("VZ", "SMART", "USD", "Verizon"),
    ("CSCO", "SMART", "USD", "Cisco"),
    ("PYPL", "SMART", "USD", "PayPal"),
    ("XYZ", "SMART", "USD", "Block"),
    ("GM", "SMART", "USD", "General Motors"),
    ("RIVN", "SMART", "USD", "Rivian"),
    ("NKE", "SMART", "USD", "Nike"),
    ("MSFT", "SMART", "USD", "Microsoft"),
    ("XOM", "SMART", "USD", "Exxon Mobil"),
    ("ABBV", "SMART", "USD", "AbbVie"),
    ("JNJ", "SMART", "USD", "Johnson & Johnson"),
    ("LMT", "SMART", "USD", "Lockheed Martin"),
    ("SPYM", "SMART", "USD", "S&P 500 Mini ETF"),
    ("SPY", "SMART", "USD", "S&P 500 ETF"),
    ("QQQ", "SMART", "USD", "Nasdaq 100 ETF"),
    ("TLT", "SMART", "USD", "20yr Treasury ETF"),
    ("SLV", "SMART", "USD", "Silver ETF"),
    ("XLE", "SMART", "USD", "Energy Sector ETF"),
    ("COPX", "SMART", "USD", "Copper Miners ETF"),
    ("GDX", "SMART", "USD", "Gold Miners ETF"),
    ("XME", "SMART", "USD", "Metals & Mining ETF"),
    ("GLD", "SMART", "USD", "Gold ETF"),
    ("USO", "SMART", "USD", "Crude Oil ETF"),
    ("URA", "SMART", "USD", "Uranium ETF"),
    ("VALE", "SMART", "USD", "Vale SA"),

    # ASX
    ("BHP", "ASX", "AUD", "BHP Group"),
    ("CBA", "ASX", "AUD", "Commonwealth Bank"),
    ("NAB", "ASX", "AUD", "National Aust Bank"),
    ("WBC", "ASX", "AUD", "Westpac"),
    ("ANZ", "ASX", "AUD", "ANZ Bank"),
    ("FMG", "ASX", "AUD", "Fortescue Metals"),
    ("RIO", "ASX", "AUD", "Rio Tinto"),
    ("WDS", "ASX", "AUD", "Woodside Energy"),
    ("MQG", "ASX", "AUD", "Macquarie Group"),
    ("WES", "ASX", "AUD", "Wesfarmers"),
    ("TLS", "ASX", "AUD", "Telstra"),
    ("WOW", "ASX", "AUD", "Woolworths"),
    ("ALL", "ASX", "AUD", "Aristocrat Leisure"),
    ("REA", "ASX", "AUD", "REA Group"),
    ("XRO", "ASX", "AUD", "Xero"),
    ("S32", "ASX", "AUD", "South32"),
    ("PLS", "ASX", "AUD", "Pilbara Minerals"),
    ("MIN", "ASX", "AUD", "Mineral Resources"),
    ("NEM", "ASX", "AUD", "Newmont Mining"),
    ("SFR", "ASX", "AUD", "Sandfire Resources"),
    ("QAN", "ASX", "AUD", "Qantas"),
    ("CSL", "ASX", "AUD", "CSL Limited"),
    ("JHX", "ASX", "AUD", "James Hardie"),
    ("COL", "ASX", "AUD", "Coles Group"),
    ("STO", "ASX", "AUD", "Santos"),
    ("ORG", "ASX", "AUD", "Origin Energy"),
    ("IAG", "ASX", "AUD", "Insurance Australia"),

    # UK
    ("SHEL", "LSE", "GBP", "Shell"),
    ("AZN", "LSE", "GBP", "AstraZeneca"),
    ("HSBA", "LSE", "GBP", "HSBC"),
    ("ULVR", "LSE", "GBP", "Unilever"),
    ("BP.", "LSE", "GBP", "BP"),
    ("BARC", "LSE", "GBP", "Barclays"),
    ("LLOY", "LSE", "GBP", "Lloyds"),
    ("GSK", "LSE", "GBP", "GSK"),
    ("RKT", "LSE", "GBP", "Reckitt"),
    ("VOD", "LSE", "GBP", "Vodafone"),
    ("GLEN", "LSE", "GBP", "Glencore"),
    ("AAL", "LSE", "GBP", "Anglo American"),
    ("ANTO", "LSE", "GBP", "Antofagasta"),
    ("FRES", "LSE", "GBP", "Fresnillo"),
    ("RR.", "LSE", "GBP", "Rolls-Royce"),
    ("BA.", "LSE", "GBP", "BAE Systems"),
    ("NWG", "LSE", "GBP", "NatWest Group"),
    ("TSCO", "LSE", "GBP", "Tesco"),
    ("BT.A", "LSE", "GBP", "BT Group"),
    ("LSEG", "LSE", "GBP", "London Stock Exchange"),

    # Germany
    ("SIE", "IBIS", "EUR", "Siemens"),
    ("ALV", "IBIS", "EUR", "Allianz"),
    ("DTE", "IBIS", "EUR", "Deutsche Telekom"),
    ("BAS", "IBIS", "EUR", "BASF"),
    ("BMW", "IBIS", "EUR", "BMW"),
    ("MBG", "IBIS", "EUR", "Mercedes-Benz"),
    ("ADS", "IBIS", "EUR", "Adidas"),
    ("SDF", "IBIS", "EUR", "K+S"),
    ("VOW3", "IBIS", "EUR", "Volkswagen"),
    ("DB1", "IBIS", "EUR", "Deutsche Boerse"),

    # Netherlands / France
    ("ASML", "AEB", "EUR", "ASML"),
    ("OR", "SBF", "EUR", "L'Oreal"),
    ("AI", "SBF", "EUR", "Air Liquide"),
    ("RMS", "SBF", "EUR", "Hermes"),
    ("TTE", "SBF", "EUR", "TotalEnergies"),

    # Hong Kong
    ("700", "SEHK", "HKD", "Tencent"),
    ("9988", "SEHK", "HKD", "Alibaba"),
    ("5", "SEHK", "HKD", "HSBC HK"),
    ("1299", "SEHK", "HKD", "AIA Group"),
    ("388", "SEHK", "HKD", "HKEX"),
    ("3690", "SEHK", "HKD", "Meituan"),

    # Canada (dual-listed via SMART)
    ("RY", "SMART", "USD", "Royal Bank of Canada"),
    ("TD", "SMART", "USD", "Toronto-Dominion Bank"),
    ("BNS", "SMART", "USD", "Bank of Nova Scotia"),
    ("BMO", "SMART", "USD", "Bank of Montreal"),
    ("SHOP", "SMART", "USD", "Shopify"),
    ("ENB", "SMART", "USD", "Enbridge"),
    ("CNQ", "SMART", "USD", "Canadian Natural Resources"),
    ("GOLD", "SMART", "USD", "Barrick Gold"),
    ("CP", "SMART", "USD", "Canadian Pacific Kansas"),
    ("MFC", "SMART", "USD", "Manulife Financial"),

    # Singapore
    ("D05", "SGX", "SGD", "DBS Group"),
    ("U11", "SGX", "SGD", "UOB"),
    ("O39", "SGX", "SGD", "OCBC Bank"),
    ("Z74", "SGX", "SGD", "Singtel"),
    ("BN4", "SGX", "SGD", "Keppel Corp"),
    ("C6L", "SGX", "SGD", "Singapore Airlines"),
]
