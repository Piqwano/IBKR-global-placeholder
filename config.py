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

# PAPER vs LIVE toggle
PAPER_MODE = os.getenv("TRADING_MODE", "paper").lower() == "paper"

# Market data type: 1 = live (paid), 3 = delayed (free, 15-20 min lag)
MARKET_DATA_LIVE = os.getenv("IB_MARKET_DATA", "delayed").lower() == "live"

# ══════════════════════════════════════════════════════════════════════════
#  RECONNECT / RESILIENCE
# ══════════════════════════════════════════════════════════════════════════

RECONNECT_INITIAL_DELAY = 5
RECONNECT_MAX_DELAY = 300
RECONNECT_BACKOFF_FACTOR = 2.0
RECONNECT_MAX_ATTEMPTS = 0           # 0 = infinite

CONNECTION_TIMEOUT = 15
TRADE_FILL_TIMEOUT = 30

# ══════════════════════════════════════════════════════════════════════════
#  STRATEGY — RSI
# ══════════════════════════════════════════════════════════════════════════

RSI_PERIOD = 14
RSI_OVERSOLD = 40
RSI_OVERBOUGHT = 60

USE_VOLUME_FILTER = False
USE_TREND_FILTER = False
USE_MA20_FILTER = False

# ══════════════════════════════════════════════════════════════════════════
#  EXITS
# ══════════════════════════════════════════════════════════════════════════

DEFAULT_TRAILING_STOP = 0.06
DEFAULT_TAKE_PROFIT = 0.08

USE_ATR_STOPS = False
ATR_PERIOD = 14
ATR_MULTIPLIER = 1.5

USE_BRACKET_ORDERS = True
REPAIR_TP_AFTER_FILL = True
REATTACH_BRACKETS_ON_RECONCILE = True

# ══════════════════════════════════════════════════════════════════════════
#  POSITION SIZING (base)
# ══════════════════════════════════════════════════════════════════════════

POSITION_SIZE_PCT = 0.15             # 15% per trade
MAX_POSITIONS = 5
CASH_RESERVE_PCT = 0.20

REGIME_SIZE_MULTIPLIERS = {
    "BULL":    1.0,
    "CAUTION": 0.35,
    "BEAR":    0.0,
}

# ══════════════════════════════════════════════════════════════════════════
#  VOLATILITY-ADJUSTED POSITION SIZING
# ══════════════════════════════════════════════════════════════════════════
# Scales each new position by the ratio of target annualised volatility to
# the stock's own annualised vol (ATR-based). Keeps portfolio-wide vol
# contribution roughly constant across names of different volatility.
#
# Applied AFTER base size × regime multiplier:
#   final_amount = base_amount × vol_scalar
# where:
#   annualised_vol = (ATR / price) × sqrt(252)
#   vol_scalar     = VOL_TARGET_ANNUAL / annualised_vol   (clamped)
#
# Low-vol names get boosted (up to 1.8×), high-vol names get shrunk (down
# to 0.4×).

USE_VOL_ADJUSTED_SIZING = True
VOL_TARGET_ANNUAL = 0.15            # 15% target annualised per-position vol
ATR_PERIOD_FOR_SIZING = 20          # Separate ATR window for sizing
VOL_SCALAR_MIN = 0.4                # Floor on vol scalar
VOL_SCALAR_MAX = 1.8                # Ceiling on vol scalar

# ══════════════════════════════════════════════════════════════════════════
#  LOSS LIMITS
# ══════════════════════════════════════════════════════════════════════════

DAILY_LOSS_LIMIT_PCT = 0.02          # -2% daily → halt new buys
MAX_DRAWDOWN_PCT = 0.15              # -15% from peak → flatten + halt
FLATTEN_ON_MAX_DD = True

DAILY_RESET_TZ = os.getenv("DAILY_RESET_TZ", "America/New_York")
RESET_MAX_DD_ON_START = os.getenv("RESET_MAX_DD", "").lower() in ("1", "true", "yes")

# ══════════════════════════════════════════════════════════════════════════
#  MARKET REGIME — SPY + VIX
# ══════════════════════════════════════════════════════════════════════════
# Regime determination now uses both SPY trend and VIX level:
#
#   BULL:    SPY > 200MA  AND  VIX < VIX_BULL_MAX              (default <20)
#   CAUTION: SPY > 200MA  AND  VIX_BULL_MAX ≤ VIX ≤ VIX_CAUTION_MAX_ABOVE_200MA
#         OR SPY > 200MA  AND  VIX > VIX_CAUTION_MAX_ABOVE_200MA (defensive)
#         OR SPY ≤ 200MA  AND  VIX < VIX_CAUTION_MAX_BELOW_200MA
#   BEAR:    SPY ≤ 200MA  AND  VIX ≥ VIX_CAUTION_MAX_BELOW_200MA
#
# If VIX fetch fails (no subscription / API error), falls back to the old
# SPY-only logic: above 50MA + 200MA → BULL, above 200MA → CAUTION, else BEAR.

USE_VIX_IN_REGIME = True
VIX_BULL_MAX = 20.0                       # BULL ceiling
VIX_CAUTION_MAX_ABOVE_200MA = 25.0        # SPY up, but CAUTION if VIX ≤ 25
VIX_CAUTION_MAX_BELOW_200MA = 22.0        # SPY down, still CAUTION if VIX < 22

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
#  SLIPPAGE MODEL (backtest)
# ══════════════════════════════════════════════════════════════════════════

SLIPPAGE_BPS = {
    "SMART": 2, "ASX": 5, "LSE": 5,
    "IBIS": 8, "SBF": 8, "AEB": 8,
    "SEHK": 10, "SGX": 10,
}

BACKTEST_FILL_MODE = "next_open"

# ══════════════════════════════════════════════════════════════════════════
#  SEHK BOARD LOT SIZES
# ══════════════════════════════════════════════════════════════════════════

SEHK_LOT_SIZES = {
    "700":  100, "9988": 100, "5": 400,
    "1299": 200, "388":  100, "3690": 100,
}

# ══════════════════════════════════════════════════════════════════════════
#  MARKET HOURS
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
PARTIAL_SELL_RECONCILE_WAIT = 1.5

# ══════════════════════════════════════════════════════════════════════════
#  STATE PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════

STATE_FILE = os.getenv("BOT_STATE_FILE", "bot_state.json")
STATE_SAVE_ON_EVERY_FILL = True

# ══════════════════════════════════════════════════════════════════════════
#  NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

SEND_DAILY_SUMMARY = True           # Discord summary at each day rollover

# ══════════════════════════════════════════════════════════════════════════
#  DASHBOARD (FastAPI)
# ══════════════════════════════════════════════════════════════════════════

DASHBOARD_ENABLED = True
# Railway sets PORT automatically for web services; fall back to 8000.
# (Not a new config burden — if PORT isn't set, we just use 8000.)
DASHBOARD_PORT = int(os.getenv("PORT", "8000"))

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
