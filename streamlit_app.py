import streamlit as st
import pandas as pd
from kiteconnect import KiteConnect
from streamlit_gsheets import GSheetsConnection
import streamlit.components.v1 as components
import time
import os
import json
import pytz
import requests
from datetime import datetime, timedelta, time as dtime
from concurrent.futures import ThreadPoolExecutor
from streamlit_autorefresh import st_autorefresh

# --- LIVE CHART (TradingView lightweight-charts) ---
# V5: the live chart now loads TradingView's lightweight-charts library straight from a CDN
# inside the page, so `streamlit-lightweight-charts` is NO LONGER NEEDED. This old import is
# kept (harmless) and is simply unused.
try:
    from streamlit_lightweight_charts import renderLightweightCharts
    LWC_AVAILABLE = True
except Exception:
    LWC_AVAILABLE = False

# --- 1. CONFIGURATION & BLUE TOGGLE + TOOLTIP STYLING ---

st.set_page_config(page_title="Master Omni-Scanner Pro", layout="wide")
IST = pytz.timezone('Asia/Kolkata')

# --- MARKET HOURS AUTO-REFRESH CONTROL ---

now_ist = datetime.now(IST)
current_time = now_ist.time()
market_start = dtime(9, 7)
market_end = dtime(15, 30)
is_weekday = now_ist.weekday() < 5  # Monday to Friday

if is_weekday and (market_start <= current_time <= market_end):
    st_autorefresh(interval=60000, key="omni_scanner_autorefresh")

st.markdown("""
<style>
[data-testid="stDataFrame"] td { text-align: center !important; }
[data-testid="stHeader"] th { text-align: center !important; }
[data-testid="stDataFrame"] a { justify-content: center !important; }
.stDataFrame { margin: 0 auto; }

/* Blue Toggle Switch Styling */
span[aria-checked="true"] {
    background-color: #1E88E5 !important;
}
div[data-testid="stCheckbox"] input:checked + div {
    background-color: #1E88E5 !important;
}
div[class*="st-"] [aria-checked="true"] {
    background-color: #1E88E5 !important;
}

/* Score Tooltip Hover Styling */
.score-tooltip {
    position: relative;
    display: inline-block;
    cursor: pointer;
    font-weight: bold;
    color: #1E88E5;
}
.score-tooltip .tooltip-text {
    visibility: hidden;
    width: 250px;
    background-color: #1E1E1E;
    color: #FFFFFF;
    text-align: left;
    border-radius: 6px;
    padding: 8px;
    position: absolute;
    z-index: 99999;
    bottom: 125%;
    left: 50%;
    margin-left: -125px;
    box-shadow: 0px 4px 10px rgba(0, 0, 0, 0.5);
    font-size: 12px;
    line-height: 1.5;
    border: 1px solid #333;
}
.score-tooltip:hover .tooltip-text {
    visibility: visible;
}

/* Standardized HTML Table Alignment */
.custom-table {
    width: 100%;
    border-collapse: collapse;
    text-align: center;
    font-size: 14px;
}
.custom-table th, .custom-table td {
    padding: 8px 12px;
    border-bottom: 1px solid #333;
    text-align: center;
}
.custom-table a {
    color: #1E88E5;
    text-decoration: none;
    font-weight: bold;
}
</style>
""", unsafe_allow_html=True)

try:
    API_KEY = st.secrets["API_KEY"]
    API_SECRET = st.secrets["API_SECRET"]
    conn = st.connection("gsheets", type=GSheetsConnection)
except Exception as e:
    st.error(f"Setup Error: {e}")
    st.stop()

ACTIVE_TRADES_FILE = "active_trades.json"

# ================================================================
# Telegram/PC alert tier configuration
# ================================================================
# Happy Breakout is the priority tier — it's checked first every cycle —
# and still uses its original 8 conditions (price move, volume multiple +
# minimum volume, 15m Donchian breakout, 15m RSI, Daily VO%, RSI-vs-its-
# own-EMA34). Early Watchlist is checked only if Happy Breakout didn't
# already fire this cycle for that symbol, and its LOGIC mirrors the
# Google Sheet's "PDH Break" page instead of its old Donchian/RSI/VO%-based
# conditions: it fires when today's High trades at or above the previous
# trading day's High, Volume Multiple exceeds PDH_VOL_MULTIPLE_THRESHOLD,
# AND Change % vs PrevClose is >= PRICE_MOVE_PCT_THRESHOLD (1%) — the one
# condition it now shares with Happy Breakout. Nothing else gates it (no
# RSI, no Donchian, no Daily VO%). See is_early_watchlist below. The
# "Early Watchlist" name is unchanged everywhere (toggle, Telegram alerts,
# active-trade dedup, dashboard tab).
#
# NOTE: SL conditions were intentionally removed entirely — exits are now
# handled purely by the two-stage EMA exit engine below, not by price-based
# stop levels.
#
# ----------------------------------------------------------------
# V2 — AVG VOLUME UNIFIED + HAPPY BREAKOUT TAB UNIFIED WITH THE ALERT
# ----------------------------------------------------------------
# 1) Avg Volume is now sourced from ONE place everywhere in this app: the
#    live 20-day daily average (last 20 daily candles, SMA basis, including
#    today's still-forming volume) computed by get_daily_avg_vol_20(). This
#    matches TradingView's own Volume indicator settings (MA Length 20,
#    Volume MA: SMA) and is the same basis the dashboard's "Avg Volume" /
#    "Vol Multiple" columns already displayed correctly. The old, separate
#    22-completed-day average (get_daily_avg_vol(), ttl=86400) has been
#    removed — Volume Breakout, Happy Breakout, and Early Watchlist all now
#    read from the exact same avg_v_20 number, so they can never disagree.
# 2) The "🚀 Happy Breakout" dashboard tab previously used its OWN, looser
#    filter (Vol Multiple >=1.1x, RSI>65, VO>=100%) — independent of the
#    stricter 8-condition Happy Breakout Telegram/PC alert (Vol Multiple
#    >2.5x, Volume >500k, Change %>=1%, 15m Donchian breakout, RSI>70, RSI
#    above its own EMA34, Daily VO%>=100%). That tab now filters on the
#    EXACT SAME boolean used for the alert (is_happy_breakout), so the tab
#    and the alert always show the same set of stocks.
#
# V3 — BB MEDIAN REMOVED, RSI COLUMNS ADDED
# The BB Median (20 SMA shifted +6) logic has been removed from this script
# entirely (calculate_bb_median, get_bb_status and the four "BB Med" table
# columns). The dashboard now shows RSI(14) for 15m / 1H / Day / Week
# instead ("RSI 15m", "RSI 1H", "RSI Day", "RSI Wk").
#
# V4 — GAP-UP OPENING RANGE FILTER (sidebar toggle, ON by default)
# Stocks that OPEN >= GAP_UP_PCT_THRESHOLD (1.5%) above the previous day's
# close are held back from ALL alert tiers until they trade above the
# opening-range high (high of the 9:15 and 9:30 candles = first 30 minutes,
# complete at 9:45). Before 9:45 a gap-up stock never alerts; from 9:45 on it
# alerts only while LTP is above that opening-range high, and then the normal
# trigger conditions decide as usual. Non-gap stocks are NOT affected and can
# alert at any time (9:25, 9:30, ...). Alert dedup flags are only written when
# an alert actually fires, so a blocked stock can still alert later. Nothing
# on the dashboard changes — the gate sits only in front of the alert chain.
#
# V5 — "VIEW LIVE CHART" REPLACED WITH THE SAME CHART AS THE GOOGLE SHEET V51.5
# The old streamlit-lightweight-charts chart (candles + volume + EMA9/21 only)
# is replaced by a full in-page TradingView lightweight-charts build with the
# 13 indicator modules of the Pine script "Happy Trading 1.2" + a lower
# RSI / Volume Oscillator pane, settings panel (show/hide, colour, opacity,
# thickness, style), 💾 Save, Light/Dark, Full-screen, timeframes
# 3m 5m 15m 1H 4H D W M, scanner-list symbol picker (+ any typed NSE symbol),
# and a Volume / Avg Vol / Vol M stats row. Nothing else in the scanner changed.
#
# V6 — HAPPY BREAKOUT 2.0 (new default alert) + OLD THREE ALERTS DEFAULT OFF
# A single new alert tier, "Happy Breakout 2.0" (💎), is now the default,
# priority alert for the dashboard tab AND for PC/Telegram notifications.
# It fires only when ALL 8 conditions are true:
#   (1) Vol Multiple >= 1.1x (live volume / 20-day avg volume)
#   (2) Volume >= 150,000
#   (3) Daily Volume Oscillator (1, 20) >= +100%
#   (4) 15m Donchian (28, offset 6): close >= upper channel
#   (5) Change % vs previous close >= 1%
#   (6) 15m RSI(14) >= 60
#   (7) 15m RSI above its own 34-EMA
# (The 8th item is simply the same-day list below, not a trigger condition.)
# SAME-DAY TRIGGERED STOCKS LIST: a new section at the very bottom of the
# page lists every stock that fired a PC/Telegram entry alert TODAY (IST),
# with trigger time, trigger LTP, current LTP and the % move since trigger.
# It is persisted in todays_triggers.json so an app/PC restart does not lose
# it, and it resets automatically on the next calendar day.
# The three older alerts — Happy Breakout, Early Watchlist and HAPPY SF
# (the "Individual Donchian Alerts" toggle) — are KEPT in the code, with
# their conditions and sidebar toggles UNCHANGED, but their toggles now
# default to OFF so you can switch them on whenever you need them.
# Happy Breakout 2.0 is checked FIRST in the alert chain. The Gap-Up
# Opening Range Filter (V4) still gates every alert tier, including this one.
# Dedup for 2.0 is persistent (active_trades.json flag "alerted_happy2"),
# exactly like the other two tiers, and it feeds the same 9/21-EMA exit engine.
# ----------------------------------------------------------------

# LEGACY (no longer used by is_early_watchlist — kept only for reference/
# history; the Early Watchlist condition now uses PDH_VOL_MULTIPLE_THRESHOLD
# instead). See the "PDH BREAK MIRROR" comment further down.
EARLY_WATCHLIST_VOL_MULTIPLE = 1.1
EARLY_WATCHLIST_MIN_VOLUME = 100_000
EARLY_WATCHLIST_RSI_THRESHOLD = 65

HAPPY_BREAKOUT_VOL_MULTIPLE = 2.5
HAPPY_BREAKOUT_MIN_VOLUME = 500_000
HAPPY_BREAKOUT_RSI_THRESHOLD = 70

DAILY_VO_THRESHOLD = 100.0
PRICE_MOVE_PCT_THRESHOLD = 1.0  # vs PrevClose, matches existing "pct" field; used by Happy Breakout, Early Watchlist and Happy Breakout 2.0

# PDH BREAK MIRROR — same threshold and meaning as the Google Sheet's
# PDH_VOL_MULTIPLE_THRESHOLD (Master Key script, PDH Break section): Volume
# Multiple (today's volume ÷ 20-day avg volume incl. today's live volume)
# must exceed this for the PDH-break condition to be able to fire.
PDH_VOL_MULTIPLE_THRESHOLD = 1.5
# HAPPY SF: Donchian 15m Upper Breakout + Volume Multiple threshold
HAPPY_SF_VOL_MULTIPLE = 1.1

# HAPPY BREAKOUT 2.0 (V6) — the new default alert tier
HB2_VOL_MULTIPLE = 1.1          # Vol Multiple >= this
HB2_MIN_VOLUME = 150_000        # Volume >= this
HB2_RSI_THRESHOLD = 60          # 15m RSI(14) >= this
HB2_VO_THRESHOLD = 100.0        # Daily Volume Oscillator (1,20) >= this (%)

# GAP-UP OPENING RANGE FILTER
GAP_UP_PCT_THRESHOLD = 1.5            # open vs previous close, in %
OPENING_RANGE_START = dtime(9, 15)    # first 15m candle of the session
OPENING_RANGE_END = dtime(9, 45)      # range = candles starting 9:15 and 9:30

EARLY_WATCHLIST_EMOJI = "🚩"
HAPPY_BREAKOUT_EMOJI = "🟢"
HAPPY_BREAKOUT2_EMOJI = "💎"

# --- BOOK PROFIT (optional, toggle OFF by default) ---
BOOK_PROFIT_RSI_THRESHOLD = 90.0   # 15m RSI >= this -> "BOOK PROFIT" alert
BOOK_PROFIT_EMOJI = "💰"

# --- PERSISTENT ACTIVE TRADES STORAGE UTILS ---
# active_trades.json is the single source of truth for both (a) exit
# monitoring and (b) alert dedup. Each entry carries "alerted_early",
# "alerted_happy" and (V6) "alerted_happy2" flags: a given tier only fires
# once per lifecycle (from first trigger until Final Exit removes the entry
# entirely). Because this lives on disk, a PC/app restart does NOT cause
# duplicate alerts for a still-ongoing signal — only a genuine Final Exit,
# followed by conditions becoming true again, allows a fresh alert.

def load_active_trades():
    if "active_trades" not in st.session_state:
        if os.path.exists(ACTIVE_TRADES_FILE):
            try:
                with open(ACTIVE_TRADES_FILE, "r") as f:
                    st.session_state["active_trades"] = json.load(f)
            except Exception:
                st.session_state["active_trades"] = {}
        else:
            st.session_state["active_trades"] = {}
    return st.session_state["active_trades"]

def save_active_trades(trades):
    st.session_state["active_trades"] = trades
    try:
        with open(ACTIVE_TRADES_FILE, "w") as f:
            json.dump(trades, f, indent=4)
    except Exception:
        pass

# --- V6: SAME-DAY TRIGGERED STOCKS LOG ---
# Every PC/Telegram entry alert (any tier) is also appended here, once per
# symbol + alert type per calendar day (IST). Persisted to disk so a restart
# doesn't lose today's list; automatically starts empty on a new day.
TODAY_TRIGGERS_FILE = "todays_triggers.json"

def _write_todays_triggers(data):
    try:
        with open(TODAY_TRIGGERS_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception:
        pass

def load_todays_triggers():
    today_key = datetime.now(IST).strftime("%Y-%m-%d")
    data = st.session_state.get("todays_triggers")
    if data is None:
        data = {}
        if os.path.exists(TODAY_TRIGGERS_FILE):
            try:
                with open(TODAY_TRIGGERS_FILE, "r") as f:
                    data = json.load(f)
            except Exception:
                data = {}
    if not isinstance(data, dict) or data.get("date") != today_key:
        data = {"date": today_key, "entries": []}
        _write_todays_triggers(data)
    st.session_state["todays_triggers"] = data
    return data

def log_todays_trigger(symbol, alert_type, score, ltp, chart_url=""):
    data = load_todays_triggers()
    for e in data["entries"]:
        if e.get("Symbol") == symbol and e.get("Type") == alert_type:
            return  # already logged today
    data["entries"].append({
        "Symbol": symbol,
        "Type": alert_type,
        "Score": score,
        "Time": datetime.now(IST).strftime("%H:%M:%S"),
        "Trigger LTP": float(ltp),
        "Chart": chart_url
    })
    st.session_state["todays_triggers"] = data
    _write_todays_triggers(data)

# --- TECHNICAL HELPERS: RSI, EMA & VOLUME OSCILLATOR ---

def calculate_rsi_and_ema(series, period=14, ema_period=34):
    if len(series) < period + 1:
        return 50.0, 50.0

    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    # Wilder's Exponential Smoothing (RMA) - Matches Kite / TradingView exactly
    alpha = 1.0 / period
    avg_gain = gain.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=alpha, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, float('nan'))
    rsi_series = 100.0 - (100.0 / (1.0 + rs))
    rsi_series = rsi_series.fillna(50.0)

    if len(rsi_series) < ema_period:
        last_rsi = float(rsi_series.iloc[-1])
        return last_rsi, last_rsi

    rsi_ema_series = rsi_series.ewm(span=ema_period, adjust=False).mean()
    return float(rsi_series.iloc[-1]), float(rsi_ema_series.iloc[-1])

# Latest RSI(14) for the dashboard's "RSI 15m / 1H / Day / Wk" columns.
# Returns NaN (shown as "N/A" in the rich table, blank in the native table)
# when the timeframe's DataFrame is missing or too short to compute RSI.
def get_latest_rsi(df, period=14):
    if df is None or df.empty or 'close' not in df.columns or len(df) < period + 1:
        return float('nan')
    try:
        rsi_val, _ = calculate_rsi_and_ema(df['close'], period=period)
        return round(float(rsi_val), 2)
    except Exception:
        return float('nan')

# --- TRADINGVIEW VOLUME OSCILLATOR (Shortlen = 1, Longlen = 20) ---

def calculate_volume_oscillator(df, short_len=1, long_len=20):
    if df is None or len(df) < long_len:
        return 0.0

    vol_series = df['volume'].astype(float)
    short_ema = vol_series.ewm(span=short_len, adjust=False).mean()
    long_ema = vol_series.ewm(span=long_len, adjust=False).mean()

    last_short = short_ema.iloc[-1]
    last_long = long_ema.iloc[-1]

    if last_long == 0:
        return 0.0

    vo = ((last_short - last_long) / last_long) * 100.0
    return round(float(vo), 2)

# --- VOLUME DISPLAY FORMATTING ---

def format_volume_short(value):
    try:
        value = float(value)

        # Hide invalid/missing average-volume sentinel
        if value >= 999_999_999:
            return "N/A"

        if value >= 1_000_000:
            return f"{value / 1_000_000:.1f} M"
        elif value >= 1_000:
            return f"{value / 1_000:.1f} K"
        return f"{value:,.0f}"
    except (ValueError, TypeError):
        return "N/A"

def format_volume_multiple(value):
    try:
        value = float(value)
        if value > 0:
            if value >= 2.5:
                return f"{value:.1f}x 🟢"
            return f"{value:.1f}x"
        return "N/A"
    except (ValueError, TypeError):
        return "N/A"

@st.cache_data(ttl=300, show_spinner=False)
def fetch_multi_timeframe_candles(access_token, api_key, instrument_token):
    try:
        kite_inst = KiteConnect(api_key=api_key)
        kite_inst.set_access_token(access_token)
        now = datetime.now(IST)

        def fetch_tf(interval, days):
            try:
                return kite_inst.historical_data(instrument_token, now - timedelta(days=days), now, interval)
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=4) as executor:
            f_15m = executor.submit(fetch_tf, "15minute", 30)
            f_1h = executor.submit(fetch_tf, "60minute", 90)
            f_day = executor.submit(fetch_tf, "day", 1000)
            f_week = executor.submit(fetch_tf, "week", 750)

            hist_15m = f_15m.result()
            hist_1h = f_1h.result()
            hist_day = f_day.result()
            hist_week = f_week.result()

        return (
            pd.DataFrame(hist_15m) if hist_15m else None,
            pd.DataFrame(hist_1h) if hist_1h else None,
            pd.DataFrame(hist_day) if hist_day else None,
            pd.DataFrame(hist_week) if hist_week else None
        )
    except Exception:
        return None, None, None, None

def calculate_5star_score(df_15m, df_1h, df_day, df_week, vol_osc_pct=None, vol_multiple=None):
    # This still only feeds the "Full Market Overview" dashboard table's
    # Score column/tooltip. It is NOT used by the Telegram/PC alert logic
    # below, which computes its own conditions independently.
    c1, c2, c3, c4, c5 = False, False, False, False, False
    rsi_15m_val, rsi_1h_val, rsi_day_val, rsi_wk_val = 0.0, 0.0, 0.0, 0.0

    # Star 1: 15m RVOL >= 2.5x SMA(20)
    if df_15m is not None and len(df_15m) >= 20:
        curr_vol = df_15m['volume'].iloc[-1]
        vol_sma20 = df_15m['volume'].rolling(window=20).mean().iloc[-1]
        if vol_sma20 > 0 and (curr_vol >= 2.5 * vol_sma20):
            c1 = True

    # Star 2: 15m RSI between 70 and 80 OR RSI > EMA(34)
    if df_15m is not None and len(df_15m) >= 34:
        rsi_15m_val, ema_15m = calculate_rsi_and_ema(df_15m['close'])
        if (70 < rsi_15m_val < 80) or rsi_15m_val > ema_15m:
            c2 = True

    # Star 3: 1h RSI >= 70 OR RSI > EMA(34)
    if df_1h is not None and len(df_1h) >= 34:
        rsi_1h_val, ema_1h = calculate_rsi_and_ema(df_1h['close'])
        if rsi_1h_val >= 70 or rsi_1h_val > ema_1h:
            c3 = True

    # Star 4: Daily RSI >= 50 OR RSI > EMA(34)
    if df_day is not None and len(df_day) >= 34:
        rsi_day_val, ema_day = calculate_rsi_and_ema(df_day['close'])
        if rsi_day_val >= 50 or rsi_day_val > ema_day:
            c4 = True

    # Star 5: Weekly RSI >= 50 OR RSI > EMA(34)
    if df_week is not None and len(df_week) >= 34:
        rsi_wk_val, ema_wk = calculate_rsi_and_ema(df_week['close'])
        if rsi_wk_val >= 50 or rsi_wk_val > ema_wk:
            c5 = True

    score_num = sum([c1, c2, c3, c4, c5])
    score_plain = f"{score_num}/5"

    # Daily Volume Oscillator with Green Dot threshold (>= 100%)
    if vol_osc_pct is not None and pd.notna(vol_osc_pct):
        vo_str = f"+{vol_osc_pct:.2f}%" if vol_osc_pct >= 0 else f"{vol_osc_pct:.2f}%"
        if vol_osc_pct >= 100.0:
            vo_str += " 🟢"
    else:
        vo_str = "N/A"

    vol_multiple_str = format_volume_multiple(vol_multiple)

    mark_1 = "✅" if c1 else "❌"
    mark_2 = f" ({rsi_15m_val:.2f}) ✅" if c2 else f" ({rsi_15m_val:.2f}) ❌"
    mark_3 = f" ({rsi_1h_val:.2f}) ✅" if c3 else f" ({rsi_1h_val:.2f}) ❌"
    mark_4 = f" ({rsi_day_val:.2f}) ✅" if c4 else f" ({rsi_day_val:.2f}) ❌"
    mark_5 = f" ({rsi_wk_val:.2f}) ✅" if c5 else f" ({rsi_wk_val:.2f}) ❌"

    html_tooltip = (
        f'<div class="score-tooltip">{score_num}/5'
        f'<span class="tooltip-text"><b>5-Star Checklist Breakdown</b><hr style="margin:4px 0;">'
        f'Vol Spike (2.5x): {mark_1}<br>'
        f'RSI 15m: {mark_2}<br>'
        f'RSI 1h: {mark_3}<br>'
        f'RSI Daily: {mark_4}<br>'
        f'RSI Weekly: {mark_5}<br>'
        f'Vol Multiple: {vol_multiple_str}<br>'
        f'Daily VO: {vo_str}'
        f'</span></div>'
    )

    return score_plain, html_tooltip, rsi_15m_val

# --- 2. PC & TELEGRAM NOTIFICATION ENGINE ---

def send_telegram_raw(message):
    try:
        bot_token = st.secrets["TELEGRAM_BOT_TOKEN"]
        chat_id = st.secrets["TELEGRAM_CHAT_ID"]
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        requests.post(url, data=payload, timeout=5)
    except Exception:
        pass

def send_telegram_alert(symbol, alert_type, ltp, score_emoji, chart_url=""):
    """
    Message format: Score line is the tier emoji (💎 Happy Breakout 2.0 /
    🟢 Happy Breakout / 🚩 Early Watchlist) or the legacy numeric score for
    the untouched Volume/Donchian toggles. No SL lines — SL conditions were
    removed entirely; exits are handled purely by the EMA exit engine.
    """
    chart_link = f'<a href="{chart_url}">Open TV \u2197</a>' if chart_url else ""
    message = (
        f"<b>{alert_type.upper()}: {symbol}</b>\n"
        f"Score: {score_emoji}\n"
        f"Entry: \u20b9{ltp}\n"
        f"Chart: {chart_link}"
    )
    send_telegram_raw(message)

def send_telegram_exit(symbol, exit_type, ltp, chart_url=""):
    chart_link = f'<a href="{chart_url}">Open TV \u2197</a>' if chart_url else ""
    message = (
        f"<b>{exit_type}: {symbol}</b>\n"
        f"LTP: \u20b9{ltp}\n"
        f"Chart: {chart_link}"
    )
    send_telegram_raw(message)

def trigger_alert(symbol, alert_type, ltp, score_emoji, chart_url=""):
    notification_js = f"""
    <script>
    if (Notification.permission === "granted") {{
        const n = new Notification("{alert_type}: {symbol} ({score_emoji})", {{
            body: "Price: {ltp}",
            icon: "https://kite.zerodha.com/static/images/kite-logo.svg"
        }});
        new Audio('https://media.geeksforgeeks.org/wp-content/uploads/20190531135120/beep.mp3').play();
        setTimeout(() => n.close(), 5000);
    }}
    </script>
    """
    components.html(notification_js, height=0)
    st.toast(f"{alert_type}: {symbol} ({score_emoji})", icon="🚀")

    send_telegram_alert(symbol, alert_type, ltp, score_emoji, chart_url=chart_url)

# --- BOOK PROFIT NOTIFICATIONS (PC + Telegram) ---

def send_telegram_book_profit(symbol, ltp, chart_url=""):
    chart_link = f'<a href="{chart_url}">Open TV \u2197\ufe0f</a>' if chart_url else ""
    message = (
        f"<b>{BOOK_PROFIT_EMOJI} BOOK PROFIT: {symbol}</b>\n"
        f"Exit: \u20b9{ltp}\n"
        f"Chart: {chart_link}"
    )
    send_telegram_raw(message)

def trigger_book_profit(symbol, ltp, chart_url=""):
    notification_js = f"""
    <script>
    if (Notification.permission === "granted") {{
        const n = new Notification("{BOOK_PROFIT_EMOJI} BOOK PROFIT: {symbol}", {{
            body: "Exit: {ltp}",
            icon: "https://kite.zerodha.com/static/images/kite-logo.svg"
        }});
        new Audio('https://media.geeksforgeeks.org/wp-content/uploads/20190531135120/beep.mp3').play();
        setTimeout(() => n.close(), 5000);
    }}
    </script>
    """
    components.html(notification_js, height=0)
    st.toast(f"BOOK PROFIT: {symbol} (₹{ltp})", icon="💰")
    send_telegram_book_profit(symbol, ltp, chart_url=chart_url)

# --- 3. SESSION STATE ---

if 'kite' not in st.session_state:
    st.session_state.kite = KiteConnect(api_key=API_KEY)
if 'alerts_history' not in st.session_state:
    st.session_state.alerts_history = []

TOKEN_FILE = "access_token.txt"
if 'access_token' not in st.session_state and os.path.exists(TOKEN_FILE):
    try:
        with open(TOKEN_FILE, "r") as f:
            saved_token = f.read().strip()
        st.session_state.kite.set_access_token(saved_token)
        st.session_state.access_token = saved_token
    except Exception:
        pass

# --- 4. DONCHIAN CHANNEL & CACHED HISTORICAL DATA ---

def get_donchian_status(df, length=28, offset=6):
    if df is None or len(df) < (length + offset):
        return "N/A", False

    upper_channel = df['high'].rolling(window=length).max().shift(offset)
    curr_close = df['close'].iloc[-1]
    curr_upper = upper_channel.iloc[-1]

    if pd.isna(curr_upper):
        return "N/A", False

    is_breakout = curr_close >= curr_upper
    status_str = "🚀 UPPER BREAKOUT" if is_breakout else "Below"
    return status_str, is_breakout

# ================================================================
# PREVIOUS DAY HIGH — mirrors the Google Sheet's PDH Break scan
# (fetchPreviousDayHigh_ / getCachedPreviousDayHigh_ in the Apps Script).
# ================================================================
# Returns the High of the most recent COMPLETED trading day (i.e.
# "yesterday's" High), reusing the same daily candles (df_day) already
# downloaded for Daily VO % — no extra API call needed. Skips today's
# still-forming candle the same way the Apps Script does (filters out any
# candle whose date matches today_key before taking the last remaining
# row's High). Returns 0.0 if no completed day is available yet, exactly
# like the Sheet leaves a symbol un-triggered until its PDH is known.
def get_previous_day_high(df_day, today_key):
    if df_day is None or df_day.empty:
        return 0.0
    try:
        dates = df_day['date'].apply(
            lambda d: (d if isinstance(d, str) else d.strftime('%Y-%m-%d'))[:10]
        )
        completed = df_day[dates != today_key]
        if completed.empty:
            return 0.0
        return float(completed['high'].iloc[-1])
    except Exception:
        return 0.0

# ================================================================
# OPENING RANGE HIGH (9:15-9:45) — for the Gap-Up Opening Range Filter.
# ================================================================
# High of today's 15m candles that START between 9:15 and 9:45 (i.e. the
# 9:15 and 9:30 candles). Returns 0.0 ("unknown") unless today's data already
# contains a candle starting at/after 9:45 — that proves the 9:30 candle is
# complete, so a cached (up to 5 min old) DataFrame fetched mid-way through
# the 9:30 candle can't produce an understated range high. Unknown = the gap
# stock simply stays blocked until fresh data arrives.
def get_opening_range_high(df_15m, today_key):
    if df_15m is None or df_15m.empty or 'date' not in df_15m.columns:
        return 0.0
    try:
        range_highs = []
        range_closed = False
        for d, h in zip(df_15m['date'], df_15m['high']):
            if isinstance(d, str):
                d = pd.to_datetime(d)
            if d.tzinfo is not None:
                d = d.astimezone(IST)
            if d.strftime('%Y-%m-%d') != today_key:
                continue
            t = d.time()
            if OPENING_RANGE_START <= t < OPENING_RANGE_END:
                range_highs.append(float(h))
            elif t >= OPENING_RANGE_END:
                range_closed = True
        if range_highs and range_closed:
            return max(range_highs)
        return 0.0
    except Exception:
        return 0.0

@st.cache_data(ttl=300, show_spinner=False)
def fetch_15m_candles(access_token, api_key, instrument_token):
    try:
        kite_inst = KiteConnect(api_key=api_key)
        kite_inst.set_access_token(access_token)
        now = datetime.now(IST)
        hist = kite_inst.historical_data(instrument_token, now - timedelta(days=10), now, "15minute")
        return pd.DataFrame(hist)
    except Exception:
        return None

# ================================================================
# Resolve the last CLOSED 15-min candle
# ================================================================
# Kite's historical_data(..., to=now) includes the currently-forming
# candle when fetched mid-bar. Reading iloc[-1] directly for an exit
# condition therefore treats an intrabar dip/touch as if the candle had
# already closed below the EMA. This resolves the index of the most recent
# candle whose close time has actually passed.
def get_last_closed_candle_index(df, interval_minutes=15):
    if df is None or df.empty:
        return None

    now = datetime.now(IST)
    last_idx = len(df) - 1
    last_candle_time = df['date'].iloc[last_idx]

    if isinstance(last_candle_time, str):
        last_candle_time = pd.to_datetime(last_candle_time)
    if last_candle_time.tzinfo is None:
        last_candle_time = IST.localize(last_candle_time)

    candle_close_time = last_candle_time + timedelta(minutes=interval_minutes)

    if candle_close_time <= now:
        return last_idx
    # Last row is still forming — fall back to the previous (closed) row.
    return last_idx - 1 if last_idx - 1 >= 0 else None

# ================================================================
# V2: LIVE 20-PERIOD DAILY AVERAGE VOLUME — the ONLY average-volume source
# used anywhere in this app (19 completed days + today's live volume).
# This is a simple 20-period SMA of daily volume, matching TradingView's
# own Volume indicator settings (MA Length: 20, Volume MA: SMA). Every
# volume-based condition in this app — Vol Multiple, Volume Breakout,
# Happy Breakout, Early Watchlist, Happy Breakout 2.0 — reads from this
# single function.
# ================================================================

@st.cache_data(ttl=60, show_spinner=False)
def get_daily_avg_vol_20(access_token, api_key, symbols):
    kite_inst = KiteConnect(api_key=api_key)
    kite_inst.set_access_token(access_token)
    avg_vol_map = {}

    to_date = datetime.now(IST).date()
    from_date = to_date - timedelta(days=45)

    def process_symbol(s, q):
        try:
            if q and 'instrument_token' in q:
                hist = kite_inst.historical_data(
                    q['instrument_token'],
                    from_date,
                    to_date,
                    "day"
                )

                if hist and len(hist) > 0:
                    recent_hist = hist[-20:]
                    vols = [
                        day['volume']
                        for day in recent_hist
                        if 'volume' in day and day['volume'] is not None
                    ]

                    if len(vols) >= 20:
                        return s, sum(vols) / 20

                    if vols:
                        return s, sum(vols) / len(vols)

            if q and q.get('average_quantity', 0) > 0:
                return s, float(q['average_quantity'])

            return s, 999999999

        except Exception:
            if q and q.get('average_quantity', 0) > 0:
                return s, float(q['average_quantity'])
            return s, 999999999

    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i+100]

        try:
            quotes = kite_inst.quote(chunk)

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [
                    executor.submit(process_symbol, s, quotes.get(s))
                    for s in chunk
                ]

                for f in futures:
                    sym, vol_val = f.result()
                    avg_vol_map[sym] = vol_val

        except Exception:
            pass

    return avg_vol_map

# --- 5. MARKET HOURS UTILITY ---

def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    market_start = dtime(9, 7)
    market_end = dtime(15, 30)
    return market_start <= now.time() <= market_end

# ================================================================
# Two-stage EMA-only exit engine (no SL, no forced end-of-day close)
# ================================================================
# Exit 1: 15m close crosses BELOW a buffered 9-EMA line (on the last CLOSED
#         candle) — see ATR_EXIT1_MULTIPLIER below. Fires once, sends a
#         Telegram alert, but the symbol STAYS in active_trades — still
#         being watched for Final Exit.
# Final Exit: 15m close is below its plain 21-EMA (on the last CLOSED
#         candle) — UNCHANGED, no buffer. Fires once, sends a Telegram
#         alert, and REMOVES the symbol from active_trades entirely — this
#         is what frees it to fire a brand new Happy Breakout 2.0 / Happy
#         Breakout / Early Watchlist alert later if conditions become true
#         again.
# There is no forced 15:15 end-of-day close: since the 15-min candle
# series is a continuous multi-day rolling window, these two conditions
# simply keep watching the same position into the next trading session.
#
# ATR CONFIRMATION BUFFER ON EXIT 1 (new): a bare EMA9 touch/cross on a
# 15-min candle is noisy — normal EMA-hugging chop can trip it constantly,
# especially now that Early Watchlist (PDH Break mirror) adds more symbols
# to active_trades than before. Instead of firing the instant the close
# dips under EMA9, Exit 1 now requires the close to clear EMA9 by at least
# ATR_EXIT1_MULTIPLIER × the 15m ATR(14) (Wilder-smoothed, same smoothing
# convention as calculate_rsi_and_ema's avg_gain/avg_loss elsewhere in this
# file) before it counts as a real break, not just noise. ATR scales the
# buffer to each symbol's own recent volatility (a calm large-cap and a
# choppy small-cap get proportionally different buffers) rather than using
# one flat % for every symbol. The crossover framing is preserved — Exit 1
# still fires on the transition from "at/above the buffered line" to
# "below the buffered line" between the previous and current closed
# candle, it just uses the buffered line instead of the raw EMA9. Final
# Exit is intentionally left untouched (plain 21-EMA, no buffer) — it's
# already the slower/smoother of the two and is meant to mean "the
# position is done".
#
# Both alert stages can now be toggled independently from the sidebar
# (notify_exit1 / notify_final_exit): turning a toggle off pauses that
# stage's evaluation (no alert, no state change) without touching the
# other stage or any other logic in this file.
ATR_EXIT1_MULTIPLIER = 0.4  # k in "close must clear EMA9 by k × ATR14" — tune here if needed


def calculate_atr_series(df, period=14):
    """
    Wilder-smoothed Average True Range (alpha = 1/period), matching the same
    smoothing convention already used by calculate_rsi_and_ema's avg_gain /
    avg_loss elsewhere in this file. Returns the FULL series (not just the
    latest value) so callers can index it at the same "last CLOSED candle"
    position used for EMA9/EMA21 (see get_last_closed_candle_index) — never
    the still-forming candle. Returns None if there isn't enough data yet.
    """
    if df is None or len(df) < period + 1:
        return None

    high = df['high']
    low = df['low']
    prev_close = df['close'].shift(1)

    true_range = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    return true_range.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def process_active_trade_exits(kite_inst, access_token, api_key, notify_exit1=True, notify_final_exit=True):
    # Both exit alert stages off: skip entirely, no point fetching candles
    # for a check that won't alert either way.
    if not notify_exit1 and not notify_final_exit:
        return

    active_trades = load_active_trades()
    if not active_trades:
        return

    updated = False
    for sym, data in list(active_trades.items()):
        inst_token = data.get("instrument_token")
        if not inst_token:
            continue

        df_15m = fetch_15m_candles(access_token, api_key, inst_token)
        if df_15m is None or len(df_15m) < 25:
            continue

        df_15m['ema9'] = df_15m['close'].ewm(span=9, adjust=False).mean()
        df_15m['ema21'] = df_15m['close'].ewm(span=21, adjust=False).mean()
        atr_series = calculate_atr_series(df_15m, period=14)  # reuses df_15m — zero extra API calls

        closed_idx = get_last_closed_candle_index(df_15m, interval_minutes=15)
        if closed_idx is None or closed_idx < 1:
            continue

        last_close = df_15m['close'].iloc[closed_idx]
        last_ema9 = df_15m['ema9'].iloc[closed_idx]
        last_ema21 = df_15m['ema21'].iloc[closed_idx]

        prev_close = df_15m['close'].iloc[closed_idx - 1]
        prev_ema9 = df_15m['ema9'].iloc[closed_idx - 1]

        # ATR at the same closed-candle positions as the EMA reads above —
        # 0.0 (no buffer) if ATR isn't available yet (insufficient history).
        atr_now = 0.0
        atr_prev = 0.0
        if atr_series is not None:
            if closed_idx < len(atr_series) and pd.notna(atr_series.iloc[closed_idx]):
                atr_now = float(atr_series.iloc[closed_idx])
            if (closed_idx - 1) < len(atr_series) and pd.notna(atr_series.iloc[closed_idx - 1]):
                atr_prev = float(atr_series.iloc[closed_idx - 1])

        exit1_line_now = last_ema9 - (ATR_EXIT1_MULTIPLIER * atr_now)
        exit1_line_prev = prev_ema9 - (ATR_EXIT1_MULTIPLIER * atr_prev)

        tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{sym}"

        # EXIT 1: crossover below the ATR-buffered 9-EMA line — warning
        # stage, stays active. Gated by the sidebar toggle.
        if notify_exit1 and not data.get("exit1_triggered", False):
            if (prev_close >= exit1_line_prev) and (last_close < exit1_line_now):
                send_telegram_exit(sym, "EXIT 1", round(last_close, 2), chart_url=tv_url)
                data["exit1_triggered"] = True
                updated = True

        # FINAL EXIT: close below the plain 21-EMA (unchanged, no ATR
        # buffer) — removes the position entirely. Gated by its own toggle.
        if notify_final_exit and not data.get("final_exit_triggered", False):
            if last_close < last_ema21:
                send_telegram_exit(sym, "FINAL EXIT", round(last_close, 2), chart_url=tv_url)
                del active_trades[sym]
                updated = True

    if updated:
        save_active_trades(active_trades)


# ================================================================
# BOOK PROFIT engine (optional, sidebar toggle — OFF by default)
# ================================================================
# For every symbol in Active Managed Trades, if the 15m RSI is >=
# BOOK_PROFIT_RSI_THRESHOLD, send a one-time "BOOK PROFIT" PC + Telegram
# alert. Fires once per trade lifecycle (flag stored in active_trades.json).
# Does NOT remove the trade and does NOT touch Exit 1 / Final Exit logic.
def process_book_profit_alerts(access_token, api_key, full_quotes):
    active_trades = load_active_trades()
    if not active_trades:
        return

    updated = False
    for sym, data in list(active_trades.items()):
        if data.get("book_profit_triggered", False):
            continue

        inst_token = data.get("instrument_token")
        if not inst_token:
            continue

        df_15m = fetch_15m_candles(access_token, api_key, inst_token)
        if df_15m is None or len(df_15m) < 15:
            continue

        rsi_val, _ = calculate_rsi_and_ema(df_15m['close'])
        if rsi_val >= BOOK_PROFIT_RSI_THRESHOLD:
            q = full_quotes.get(f"NSE:{sym}")
            ltp = q['last_price'] if q else float(df_15m['close'].iloc[-1])
            tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{sym}"

            trigger_book_profit(sym, ltp, chart_url=tv_url)
            st.session_state.alerts_history.append({
                "Symbol": sym,
                "Type": "Book Profit",
                "Score": BOOK_PROFIT_EMOJI,
                "Time": datetime.now(IST).strftime("%H:%M:%S"),
                "LTP": ltp,
                "Chart": tv_url
            })

            data["book_profit_triggered"] = True
            updated = True

    if updated:
        save_active_trades(active_trades)

# ================================================================
# LIVE INTERACTIVE CHART — "View Live Chart"  (V5 — same chart as the Google Sheet V51.5)
# ================================================================

LIVE_CHART_TIMEFRAMES = {
    "3m": ("3minute", 20),
    "5m": ("5minute", 30),
    "15m": ("15minute", 60),
    "1H": ("60minute", 150),
    "4H": ("60minute", 300),   # built from 60-minute candles
    "D": ("day", 800),
    "W": ("day", 1500),        # built from daily candles
    "M": ("day", 1900),        # built from daily candles
}
LIVE_CHART_HTF_DAYS = 800          # daily history used for previous day / week / month levels
LIVE_CHART_REFRESH_OPTIONS = [0, 10, 15, 30, 60]   # seconds, 0 = off
IST_OFFSET_SECONDS = 19800         # lightweight-charts draws UTC; shift so the axis shows IST


def _live_chart_time(d, daily):
    if isinstance(d, str):
        d = pd.to_datetime(d)
    if daily:
        return d.strftime("%Y-%m-%d")
    if d.tzinfo is None:
        d = IST.localize(d)
    return int(d.timestamp()) + IST_OFFSET_SECONDS


def _live_chart_rows(hist, daily):
    rows = []
    for r in hist:
        rows.append([
            _live_chart_time(r["date"], daily),
            float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
            float(r.get("volume", 0) or 0),
        ])
    return rows


def fetch_live_chart_candles(kite_inst, instrument_token, interval, days):
    # Intentionally NOT cached so every call returns the live, still-forming candle.
    now = datetime.now(IST)
    try:
        hist = kite_inst.historical_data(instrument_token, now - timedelta(days=days), now, interval)
    except Exception as e:
        st.error(f"Chart data error: {e}")
        return None
    return hist if hist else None


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_live_chart_htf(access_token, api_key, instrument_token):
    """Daily candles used for previous day / week / month levels (PDH, PWH, pivots ...)."""
    try:
        k = KiteConnect(api_key=api_key)
        k.set_access_token(access_token)
        now = datetime.now(IST)
        hist = k.historical_data(instrument_token, now - timedelta(days=LIVE_CHART_HTF_DAYS), now, "day")
        return _live_chart_rows(hist, True) if hist else []
    except Exception:
        return []


@st.cache_data(ttl=600, show_spinner=False)
def fetch_live_chart_symbol_info(access_token, api_key, symbol):
    """Instrument token + previous close for a symbol typed by hand (not in the scanner)."""
    try:
        k = KiteConnect(api_key=api_key)
        k.set_access_token(access_token)
        q = k.quote([f"NSE:{symbol}"]).get(f"NSE:{symbol}")
        if q and q.get("instrument_token"):
            return {"token": q["instrument_token"], "prevClose": float((q.get("ohlc") or {}).get("close", 0) or 0)}
    except Exception:
        pass
    return None


# NOTE: this is a raw string; the page must never contain three double quotes in a row.
LIVE_CHART_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  :root { --bg:#131722; --bar:#1e222d; --txt:#d1d4dc; --mut:#9598a1; --bd:#2b2b43; --inp:#2a2e39; --inpbd:#434651; --pnl:#1a1e2b; --sum:#20242f; --foot:#787b86; }
  body.light { --bg:#ffffff; --bar:#f0f3fa; --txt:#131722; --mut:#5d606b; --bd:#d0d3de; --inp:#ffffff; --inpbd:#b2b5be; --pnl:#f8f9fd; --sum:#eceff6; --foot:#5d606b; }
  html, body { height: 100%; margin: 0; background: var(--bg); color: var(--txt); font: 13px Arial, sans-serif; }
  body { display: flex; flex-direction: column; }
  #bar { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; padding: 6px 10px; background: var(--bar); border-bottom: 1px solid var(--bd); }
  select, input, button { background: var(--inp); color: var(--txt); border: 1px solid var(--inpbd); border-radius: 4px; padding: 3px 7px; font-size: 12px; }
  input.hex { width: 64px; text-transform: uppercase; padding: 2px 4px; }
  input[type=number] { width: 62px; padding: 2px 4px; }
  input[type=checkbox] { padding: 0; }
  input[type=color] { width: 34px; height: 22px; padding: 0; border: 1px solid var(--inpbd); background: none; }
  input[type=range] { width: 80px; padding: 0; }
  button { cursor: pointer; }
  button.on { background: #2962ff; border-color: #2962ff; color: #fff; }
  button.dirty { background: #ff9800; border-color: #ff9800; color: #000; }
  #stats { padding: 4px 12px; background: var(--bar); border-bottom: 1px solid var(--bd); font-size: 12px; color: var(--mut); }
  #stats b { color: var(--txt); }
  #hdr { display: flex; flex-wrap: wrap; gap: 14px; align-items: baseline; padding: 4px 12px; }
  #sym { font-size: 17px; font-weight: bold; }
  #ltp { font-size: 17px; font-weight: bold; }
  #chg { font-size: 13px; }
  .up { color: #26a69a; }
  .down { color: #ef5350; }
  #legend { color: var(--mut); font-size: 12px; white-space: pre; }
  #main { flex: 1; min-height: 0; display: flex; }
  #wrap { flex: 1; min-width: 0; display: flex; flex-direction: column; }
  #cwrap { flex: 3; min-height: 0; position: relative; }
  #chart { position: absolute; left: 0; top: 0; right: 0; bottom: 0; }
  #ov { position: absolute; left: 0; top: 0; width: 100%; height: 100%; pointer-events: none; z-index: 5; }
  #tbl { position: absolute; right: 78px; bottom: 40px; display: none; border-collapse: collapse; font-size: 12px; pointer-events: none; z-index: 6; }
  #tbl td { padding: 2px 8px; color: #fff; border: 1px solid var(--bg); }
  #chart2 { flex: 1; min-height: 0; display: none; border-top: 1px solid var(--bd); }
  #panel { display: none; width: 380px; overflow-y: auto; background: var(--pnl); border-left: 1px solid var(--bd); }
  #ptop { position: sticky; top: 0; z-index: 8; display: flex; flex-wrap: wrap; gap: 5px; padding: 6px; background: var(--bar); border-bottom: 1px solid var(--bd); }
  details { border-bottom: 1px solid var(--bd); }
  summary { cursor: pointer; padding: 6px 8px; font-weight: bold; background: var(--sum); display: flex; align-items: center; gap: 6px; }
  .fld { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 3px 10px; }
  .fld label { color: var(--mut); font-size: 12px; }
  .fld .ctl { display: flex; align-items: center; gap: 5px; }
  .opv { width: 28px; text-align: right; color: var(--mut); font-size: 11px; }
  .mbtn { margin: 6px 10px; }
  #foot { display: flex; gap: 8px; align-items: center; padding: 4px 12px; font-size: 11px; color: var(--foot); background: var(--bar); }
  #dot { width: 9px; height: 9px; border-radius: 50%; background: #787b86; display: inline-block; }
  #dot.live { background: #26a69a; }
  #dot.busy { background: #ffb300; }
  #dot.err { background: #ef5350; }
  body.focus #hdr { display: none; }
</style>
</head>
<body>
<div id="bar">
  <button id="indbtn">Indicators</button>
  <button id="savebtn" title="Save indicator + chart settings in this browser">💾 Save</button>
  <button id="themebtn">☀ Light</button>
  <button id="fsbtn" title="Full screen">⛶ Full screen</button>
  <span id="info" style="color:var(--mut)"></span>
</div>
<div id="stats">Scanner data: -</div>
<div id="hdr">
  <span id="sym"></span><span id="ltp"></span><span id="chg"></span><span id="legend"></span>
</div>
<div id="main">
  <div id="wrap">
    <div id="cwrap">
      <div id="chart"></div>
      <canvas id="ov"></canvas>
      <table id="tbl"></table>
    </div>
    <div id="chart2"></div>
  </div>
  <div id="panel"></div>
</div>
<div id="foot"><span id="dot"></span><span id="status">Starting...</span></div>

<script>
(function () {
  var DATA = __DATA__;
  var LIB_URLS = [
    'https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js',
    'https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js'
  ];
  var STYLE_ID = { Solid: 0, Dotted: 1, Dashed: 2 };
  var DASH = { Solid: [], Dotted: [2, 3], Dashed: [6, 4] };
  var STYLES = ['Solid', 'Dashed', 'Dotted'];
  var SRC = ['close', 'open', 'high', 'low', 'hl2', 'hlc3', 'ohlc4'];
  var SHAPE_NAMES = ['Arrow Up', 'Arrow Down', 'Circle', 'Square'];
  var SHAPES = { 'Arrow Up': 'arrowUp', 'Arrow Down': 'arrowDown', 'Circle': 'circle', 'Square': 'square' };

  var state = { symbol: DATA.symbol, tf: DATA.tf, prevClose: DATA.prevClose || 0, base: [], candles: [], htf: [], spacing: 0, gap: null };
  var chart = null, candleS = null, volS = null, volMaS = null, chart2 = null;
  var SER = {}, touched = {}, MK = [], PLS = [], DR = { fills: [], boxes: [], shade: null };
  var CT = null, pane2 = { kind: null, series: [], lines: [] }, syncing = false, applyTimer = null;
  var modErrs = [], noTrack = true, ovErr = '', dirty = false, snapSaved = '', viewTimer = null, openIds = {};
  var memStore = {};

  function $(id) { return document.getElementById(id); }
  function cmp(a, b) { return a < b ? -1 : (a > b ? 1 : 0); }
  function fmt(n) { return (Math.round(n * 100) / 100).toFixed(2); }
  function fmtVol(v) {
    if (v >= 1000000) return (v / 1000000).toFixed(1) + 'M';
    if (v >= 1000) return (v / 1000).toFixed(1) + 'K';
    return String(v);
  }
  function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  function setStatus(msg, kind) { $('status').textContent = msg; $('dot').className = kind || ''; }
  function isNa(v) { return v === null || v === undefined || v !== v; }
  function normHex(v) {
    v = String(v || '').trim();
    var m = /^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.exec(v);
    if (!m) return '';
    var h = m[1];
    if (h.length === 3) h = h.charAt(0) + h.charAt(0) + h.charAt(1) + h.charAt(1) + h.charAt(2) + h.charAt(2);
    return '#' + h.toLowerCase();
  }
  function rgba(hex, op) {
    var h = normHex(hex) || '#ffffff';
    h = h.replace('#', '');
    var r = parseInt(h.substr(0, 2), 16), g = parseInt(h.substr(2, 2), 16), b = parseInt(h.substr(4, 2), 16);
    var a = Math.max(0, Math.min(100, op === undefined ? 100 : op)) / 100;
    return 'rgba(' + r + ',' + g + ',' + b + ',' + a + ')';
  }

  // ---- browser storage (session = auto-kept while you work, local = 💾 Save) ----
  function stGet(kind, key) {
    try { return (kind === 's' ? window.sessionStorage : window.localStorage).getItem(key); }
    catch (e) { return memStore[kind + key] || null; }
  }
  function stSet(kind, key, val) {
    try { (kind === 's' ? window.sessionStorage : window.localStorage).setItem(key, val); }
    catch (e) { memStore[kind + key] = val; }
  }

  // ================================================================
  // SETTINGS SCHEMA  (field = [key, label, type, default, a, b, c])
  //   types: b bool | i int | f float | c colour | o opacity % | w width | s line style | sel select
  // ================================================================
  var emaF = [];
  [[true, 9, '#E040FB'], [true, 200, '#FDD835'], [true, 20, '#00BCD4'], [false, 50, '#FFFFFF'], [false, 100, '#FF9800']]
    .forEach(function (e, i) {
      var k = i + 1;
      emaF.push(['show' + k, 'Show EMA ' + k, 'b', e[0]], ['len' + k, 'EMA ' + k + ' Length', 'i', e[1], 1, 2000, 1], ['col' + k, 'EMA ' + k + ' Color', 'c', e[2]]);
    });
  emaF.push(['op', 'Opacity %', 'o', 100], ['width', 'Line Thickness', 'w', 2], ['style', 'Line Style', 's', 'Solid']);

  var DEFS = [
    { id: 'chart', name: '0. Chart (theme / candles / volume)', noOn: true, f: [
      ['theme', 'Theme', 'sel', 'Dark', ['Dark', 'Light']],
      ['upCol', 'Up Candle Color', 'c', '#26A69A'], ['dnCol', 'Down Candle Color', 'c', '#EF5350'],
      ['showVol', 'Show Volume Bars', 'b', true], ['volOp', 'Volume Bar Opacity %', 'o', 50],
      ['showVolMA', 'Show Volume SMA line (inside volume bars)', 'b', true],
      ['volMaLen', 'Volume SMA Length', 'i', 20, 1, 500, 1], ['volMaCol', 'Volume SMA Color', 'c', '#2962FF'], ['volMaW', 'Volume SMA Thickness', 'w', 1],
      ['rightOff', 'Right Margin (bars)', 'i', 12, 0, 80, 1],
      ['farScale', 'Include far levels (pivots / zones) in auto-scale', 'b', false]
    ] },
    { id: 'ema', name: '1. Exponential Moving Averages (EMA)', f: emaF },
    { id: 'bb', name: '2. Bollinger Bands (BB Median / Upper / Lower)', f: [
      ['len', 'Length', 'i', 20, 2, 1000, 1], ['src', 'Source', 'sel', 'close', SRC], ['mult', 'Multiplier', 'f', 2.0, 0.1, 20, 0.1],
      ['offset', 'Offset', 'i', 0, -500, 500, 1],
      ['showBasis', 'Show Median (Basis) Line', 'b', true], ['basisCol', 'Median (Basis) Color', 'c', '#2962FF'],
      ['showUp', 'Show Upper Band', 'b', true], ['upCol', 'Upper Band Color', 'c', '#F23645'],
      ['showLo', 'Show Lower Band', 'b', true], ['loCol', 'Lower Band Color', 'c', '#089981'],
      ['fillOn', 'Show Background Fill (needs Upper + Lower)', 'b', true], ['fillCol', 'Background Fill Color', 'c', '#2196F3'], ['fillOp', 'Background Fill Opacity %', 'o', 7],
      ['op', 'Line Opacity %', 'o', 100], ['width', 'Line Thickness', 'w', 1], ['style', 'Line Style', 's', 'Solid']
    ] },
    { id: 'sr', name: '3. Dynamic Support & Resistance', f: [
      ['lookback', 'S/R Lookback Bars', 'i', 24, 1, 2000, 1], ['labels', 'Show Price Labels (R1 / S1)', 'b', true],
      ['showRes', 'Show Resistance Line', 'b', true], ['resCol', 'Resistance Color', 'c', '#F23645'],
      ['showSup', 'Show Support Line', 'b', true], ['supCol', 'Support Color', 'c', '#089981'],
      ['op', 'Opacity %', 'o', 100], ['width', 'Line Thickness', 'w', 1], ['style', 'Line Style', 's', 'Dashed']
    ] },
    { id: 'vol', name: '4. Effective Volume Analyzer (see-through candles)', f: [
      ['maLen', 'Volume MA Length', 'i', 20, 1, 500, 1], ['mult', 'High Volume Spurt Multiplier', 'f', 1.5, 0.1, 20, 0.1],
      ['upCol', 'Bullish High-Volume Candle Color', 'c', '#4CAF50'], ['dnCol', 'Bearish High-Volume Candle Color', 'c', '#F23645'],
      ['op', 'Candle Body Opacity % (low = see-through)', 'o', 20], ['edgeOp', 'Border / Wick Opacity %', 'o', 100]
    ] },
    { id: 'lux', name: '5. Lux Trendlines', f: [
      ['len', 'Pivot Length', 'i', 14, 1, 200, 1], ['mult', 'ATR Multiplier', 'f', 1.0, 0.1, 20, 0.1],
      ['showSig', 'Show B / S Breakout Markers', 'b', true],
      ['showUp', 'Show Up (Resistance) Trendline', 'b', true], ['upCol', 'Up Trendline Color', 'c', '#12A79D'],
      ['showDn', 'Show Down (Support) Trendline', 'b', true], ['dnCol', 'Down Trendline Color', 'c', '#F23645'],
      ['op', 'Opacity %', 'o', 100], ['width', 'Line Thickness', 'w', 1], ['style', 'Line Style', 's', 'Dashed']
    ] },
    { id: 'pivots', name: '6. Traditional Pivot Points', f: [
      ['tf', 'Pivot Timeframe', 'sel', 'D', ['D', 'W', 'M']],
      ['onlyCur', 'Only current day / period (hide older steps)', 'b', false],
      ['showP', 'Show Pivot (P)', 'b', true], ['pCol', 'Pivot Point (P) Color', 'c', '#2962FF'],
      ['showR1', 'Show R1', 'b', true], ['showR2', 'Show R2', 'b', true], ['showR3', 'Show R3', 'b', true], ['rCol', 'Resistance Color', 'c', '#F23645'],
      ['showS1', 'Show S1', 'b', true], ['showS2', 'Show S2', 'b', true], ['showS3', 'Show S3', 'b', true], ['sCol', 'Support Color', 'c', '#089981'],
      ['op', 'Opacity %', 'o', 100], ['width', 'Line Thickness', 'w', 1], ['style', 'Line Style', 's', 'Dotted']
    ] },
    { id: 'dc', name: '7. Donchian Channels', f: [
      ['len', 'Length', 'i', 20, 1, 1000, 1], ['offset', 'Offset', 'i', 0, -500, 500, 1],
      ['showUp', 'Show Upper Channel', 'b', true], ['upCol', 'Upper Channel Color', 'c', '#2962FF'],
      ['showLo', 'Show Lower Channel', 'b', true], ['loCol', 'Lower Channel Color', 'c', '#2962FF'],
      ['showMid', 'Show Middle (Basis)', 'b', true], ['midCol', 'Middle (Basis) Color', 'c', '#FF9800'],
      ['fillOn', 'Show Background Fill (needs Upper + Lower)', 'b', true], ['fillCol', 'Background Fill Color', 'c', '#2196F3'], ['fillOp', 'Background Fill Opacity %', 'o', 10],
      ['op', 'Line Opacity %', 'o', 100], ['width', 'Line Thickness', 'w', 1], ['style', 'Line Style', 's', 'Solid']
    ] },
    { id: 'fractals', name: '8. Williams Fractals', f: [
      ['bars', 'Fractal Left/Right Bars', 'i', 2, 1, 50, 1],
      ['showUp', 'Show Top (Up) Fractals', 'b', true], ['upShape', 'Top Fractal Shape', 'sel', 'Arrow Up', SHAPE_NAMES], ['upCol', 'Top Fractal Color', 'c', '#089981'],
      ['showDn', 'Show Bottom (Down) Fractals', 'b', true], ['dnShape', 'Bottom Fractal Shape', 'sel', 'Arrow Down', SHAPE_NAMES], ['dnCol', 'Bottom Fractal Color', 'c', '#F23645'],
      ['size', 'Marker Size', 'f', 0.5, 0.2, 3, 0.1]
    ] },
    { id: 'dash', name: '9. Dashboard & Trend Meter', f: [
      ['lookback', 'Lookback Period', 'i', 10, 1, 200, 1], ['thr', 'Uptrend Threshold', 'i', 8, 1, 200, 1],
      ['showTbl', 'Show Table (Live Buy % / Trend)', 'b', true],
      ['bgCol', 'Uptrend Background Color', 'c', '#089981'], ['bgOp', 'Uptrend Background Opacity %', 'o', 8]
    ] },
    { id: 'yash', name: '10. Yash Edge (dual-line zones + CPR)', f: [
      ['onlyCur', 'Only current day (D / W / M: current period) - no older days', 'b', true],
      ['showCpr', 'Show Central Pivot Line (CPR)', 'b', true],
      ['zw', 'Zone Thickness (% of Range, e.g. 0.15)', 'f', 0.15, 0.05, 0.5, 0.05],
      ['limit', 'Hide on Higher Timeframes (D, W, M)', 'b', false],
      ['r1', 'Show R1', 'b', true], ['r2', 'Show R2', 'b', true], ['r3', 'Show R3', 'b', true],
      ['s1', 'Show S1', 'b', true], ['s2', 'Show S2', 'b', true], ['s3', 'Show S3', 'b', true],
      ['cprCol', 'CPR Line Color', 'c', '#2962FF'], ['resCol', 'Resistance Lines Color', 'c', '#E67814'], ['supCol', 'Support Lines Color', 'c', '#00B450'],
      ['op', 'Opacity %', 'o', 100], ['width', 'Line Thickness', 'w', 1], ['style', 'Line Style', 's', 'Dashed']
    ] },
    { id: 'signals', name: '11. Smart Confluence Buy / Sell Signals', f: [
      ['mode', 'Signal Strategy', 'sel', 'Confluence (EMA + SAR + Vol)', ['Confluence (EMA + SAR + Vol)', 'EMA Crossover Only', 'Lux Breakout + Vol']],
      ['showBuy', 'Show Buy Signals', 'b', true], ['buyCol', 'Buy Signal Color', 'c', '#089981'],
      ['showSell', 'Show Sell Signals', 'b', true], ['sellCol', 'Sell Signal Color', 'c', '#F23645'],
      ['sarStart', 'SAR Start', 'f', 0.02, 0.001, 1, 0.001], ['sarInc', 'SAR Increment', 'f', 0.02, 0.001, 1, 0.001], ['sarMax', 'SAR Maximum', 'f', 0.2, 0.01, 2, 0.01]
    ] },
    { id: 'pro', name: '12. Pro Filtering & Risk Management', f: [
      ['showVWAP', 'Show Daily VWAP', 'b', true], ['vwapCol', 'VWAP Color', 'c', '#FF9800'], ['vwapOp', 'VWAP Opacity %', 'o', 100],
      ['vwapW', 'VWAP Line Thickness', 'w', 2], ['vwapStyle', 'VWAP Line Style', 's', 'Solid'],
      ['useRVOL', 'Require RVOL Spike', 'b', true], ['rvolMin', 'RVOL Minimum', 'f', 1.5, 0.1, 50, 0.1], ['rvolLen', 'RVOL Volume MA Length', 'i', 20, 1, 500, 1],
      ['useADX', 'Filter Chop with ADX', 'b', true], ['adxMin', 'ADX Minimum', 'f', 20, 1, 100, 1], ['adxLen', 'ADX / DI Length', 'i', 14, 1, 200, 1],
      ['showSB', 'Show High-Confluence BUY (SB) markers', 'b', true], ['sbCol', 'High-Confluence BUY (SB) Color', 'c', '#089981'],
      ['showSLTP', 'Show ATR SL / TP Lines', 'b', true], ['atrLen', 'ATR Length', 'i', 14, 1, 200, 1],
      ['slMult', 'SL ATR Multiplier', 'f', 1.5, 0.1, 20, 0.1], ['tpMult', 'TP ATR Multiplier', 'f', 3.0, 0.1, 40, 0.1],
      ['slCol', 'Stop Loss Color', 'c', '#F23645'], ['tpCol', 'Take Profit Color', 'c', '#089981'],
      ['sltpOp', 'SL / TP Opacity %', 'o', 100], ['sltpW', 'SL / TP Line Thickness', 'w', 2], ['sltpStyle', 'SL / TP Line Style', 's', 'Solid']
    ] },
    { id: 'smc', name: '13. LuxAlgo Smart Money Concepts + HTF Highs/Lows', f: [
      ['showStruct', 'Show Structure (BOS / CHoCH)', 'b', true], ['len', 'Swing Structure Length', 'i', 5, 1, 100, 1],
      ['showOB', 'Show Order Blocks', 'b', true], ['obOp', 'Order Block Opacity %', 'o', 20],
      ['showFVG', 'Show Fair Value Gaps (FVG)', 'b', true], ['fvgThr', 'FVG Min Threshold (%)', 'f', 0, 0, 20, 0.05],
      ['fvgCol', 'Fair Value Gap Color', 'c', '#12A79D'], ['fvgOp', 'Fair Value Gap Opacity %', 'o', 30],
      ['bullCol', 'Bullish Structure / OB Color', 'c', '#2962FF'], ['bearCol', 'Bearish Structure / OB Color', 'c', '#F23645'],
      ['maxBoxes', 'Max Boxes Kept (latest)', 'i', 30, 1, 500, 1],
      ['pdh', 'Show PDH (Prev Day High)', 'b', true], ['pdhCol', 'PDH Color', 'c', '#2962FF'],
      ['pdl', 'Show PDL (Prev Day Low)', 'b', true], ['pdlCol', 'PDL Color', 'c', '#2962FF'],
      ['pwh', 'Show PWH (Prev Week High)', 'b', true], ['pwhCol', 'PWH Color', 'c', '#9C27B0'],
      ['pwl', 'Show PWL (Prev Week Low)', 'b', true], ['pwlCol', 'PWL Color', 'c', '#9C27B0'],
      ['pmh', 'Show PMH (Prev Month High)', 'b', true], ['pmhCol', 'PMH Color', 'c', '#12A79D'],
      ['pml', 'Show PML (Prev Month Low)', 'b', true], ['pmlCol', 'PML Color', 'c', '#12A79D'],
      ['hlOp', 'High/Low Opacity %', 'o', 100], ['hlW', 'High/Low Line Thickness', 'w', 1], ['hlStyle', 'High/Low Line Style', 's', 'Dashed']
    ] },
    { id: 'pane', name: '14. Lower Pane (RSI / Volume Oscillator)', noOn: true, f: [
      ['kind', 'Indicator', 'sel', 'None', ['None', 'RSI + its EMA', 'Volume Oscillator']],
      ['rsiLen', 'RSI Length', 'i', 14, 2, 200, 1], ['rsiEma', 'RSI EMA Length', 'i', 34, 2, 200, 1],
      ['rsiCol', 'RSI Color', 'c', '#7E57C2'], ['rsiEmaCol', 'RSI EMA Color', 'c', '#FF7043'],
      ['rsiOp', 'RSI Opacity %', 'o', 100], ['rsiW', 'RSI Line Thickness', 'w', 1], ['rsiStyle', 'RSI Line Style', 's', 'Solid'],
      ['voS', 'VO Short Length', 'i', 1, 1, 200, 1], ['voL', 'VO Long Length', 'i', 20, 2, 500, 1],
      ['voUp', 'VO Color (>= 100)', 'c', '#26A69A'], ['voMid', 'VO Color (0..100)', 'c', '#26A69A'], ['voDn', 'VO Color (< 0)', 'c', '#EF5350']
    ] }
  ];

  var S = {};
  function defaultsFor(m) {
    var o = {};
    if (!m.noOn) o.on = true;
    m.f.forEach(function (fd) { o[fd[0]] = fd[3]; });
    return o;
  }
  DEFS.forEach(function (m) { S[m.id] = defaultsFor(m); });

  function applySaved(js) {
    if (!js) return;
    try {
      var sv = JSON.parse(js);
      DEFS.forEach(function (m) {
        if (!sv[m.id]) return;
        Object.keys(S[m.id]).forEach(function (k) { if (sv[m.id][k] !== undefined) S[m.id][k] = sv[m.id][k]; });
      });
    } catch (e) { /* bad data - defaults are fine */ }
  }

  // Saved settings (permanent) first, then the working copy of this browser tab on top of it.
  function loadSettings() {
    applySaved(stGet('l', 'lc5_saved'));
    snapSaved = JSON.stringify(S);
    applySaved(stGet('s', 'lc5_work'));
    try {
      var v = JSON.parse(stGet('l', 'lc5_view') || '{}');
      if (v.spacing > 0) state.spacing = Number(v.spacing);
      if (typeof v.gap === 'number') state.gap = v.gap;
    } catch (e) {}
    try { openIds = JSON.parse(stGet('s', 'lc5_open') || '{}'); } catch (e2) { openIds = {}; }
    dirty = JSON.stringify(S) !== snapSaved;
  }

  function refreshDirty() {
    dirty = JSON.stringify(S) !== snapSaved;
    $('savebtn').className = dirty ? 'dirty' : '';
    $('savebtn').textContent = dirty ? '💾 Save*' : '💾 Save';
  }
  // every change: keep a working copy (survives the chart's auto-refresh) and update the Save button
  function touch() {
    stSet('s', 'lc5_work', JSON.stringify(S));
    refreshDirty();
  }
  function saveSettings() {
    snapSaved = JSON.stringify(S);
    stSet('l', 'lc5_saved', snapSaved);
    stSet('s', 'lc5_work', snapSaved);
    refreshDirty();
    setStatus('Settings saved in this browser. They load automatically every time.', 'live');
  }

  function scheduleApply() { clearTimeout(applyTimer); applyTimer = setTimeout(function () { applyAll(null); }, 120); }

  // ================================================================
  // THEME
  // ================================================================
  function themeCols() {
    var L = S.chart.theme === 'Light';
    return { bg: L ? '#ffffff' : '#131722', txt: L ? '#131722' : '#d1d4dc', grid: L ? '#eceef3' : '#1e222d', bd: L ? '#d0d3de' : '#2b2b43' };
  }
  function applyTheme() {
    var L = S.chart.theme === 'Light', c = themeCols();
    document.body.className = (L ? 'light ' : '') + (document.body.className.indexOf('focus') >= 0 ? 'focus' : '');
    var o = {
      layout: { background: { type: 'solid', color: c.bg }, textColor: c.txt },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      rightPriceScale: { borderColor: c.bd }, timeScale: { borderColor: c.bd }
    };
    try { if (chart) chart.applyOptions(o); } catch (e) {}
    try { if (chart2) chart2.applyOptions(o); } catch (e2) {}
    $('themebtn').textContent = L ? '🌙 Dark' : '☀ Light';
  }

  // ================================================================
  // SETTINGS PANEL
  // ================================================================
  function mkCtl(m, fd) {
    var key = fd[0], t = fd[2], el, box = document.createElement('span');
    box.className = 'ctl';
    function put(v) { S[m.id][key] = v; touch(); if (m.id === 'chart' && key === 'theme') applyTheme(); scheduleApply(); }
    if (t === 'b') {
      el = document.createElement('input'); el.type = 'checkbox'; el.checked = !!S[m.id][key];
      el.onchange = function () { put(el.checked); };
      box.appendChild(el);
    } else if (t === 'i' || t === 'f') {
      el = document.createElement('input'); el.type = 'number';
      el.min = fd[4]; el.max = fd[5]; el.step = fd[6] || (t === 'i' ? 1 : 0.1); el.value = S[m.id][key];
      el.onchange = function () {
        var v = parseFloat(el.value);
        if (!isFinite(v)) v = fd[3];
        v = Math.min(fd[5], Math.max(fd[4], v));
        if (t === 'i') v = Math.round(v);
        el.value = v; put(v);
      };
      box.appendChild(el);
    } else if (t === 'c') {
      var ci = document.createElement('input'); ci.type = 'color'; ci.value = normHex(S[m.id][key]) || '#ffffff';
      var ti = document.createElement('input'); ti.type = 'text'; ti.className = 'hex'; ti.maxLength = 7; ti.value = ci.value.toUpperCase();
      ti.title = 'Type any hex colour code, e.g. #FF5722';
      ci.oninput = function () { ti.value = ci.value.toUpperCase(); put(ci.value); };
      ti.onchange = function () {
        var v = normHex(ti.value);
        if (v) { ci.value = v; ti.value = v.toUpperCase(); put(v); }
        else ti.value = ci.value.toUpperCase();
      };
      box.appendChild(ci); box.appendChild(ti);
    } else if (t === 'o') {
      el = document.createElement('input'); el.type = 'range'; el.min = 0; el.max = 100; el.step = 1; el.value = S[m.id][key];
      var lab = document.createElement('span'); lab.className = 'opv'; lab.textContent = S[m.id][key];
      el.oninput = function () { lab.textContent = el.value; };
      el.onchange = function () { put(parseInt(el.value, 10)); };
      box.appendChild(el); box.appendChild(lab);
    } else {   // w (width), s (style), sel
      el = document.createElement('select');
      var opts = t === 'w' ? ['1', '2', '3', '4'] : (t === 's' ? STYLES : fd[4]);
      opts.forEach(function (o) {
        var op = document.createElement('option'); op.value = o; op.textContent = o; el.appendChild(op);
      });
      el.value = String(S[m.id][key]);
      el.onchange = function () { put(t === 'w' ? parseInt(el.value, 10) : el.value); };
      box.appendChild(el);
    }
    return box;
  }

  function buildPanel() {
    var host = $('panel');
    var keepScroll = host.scrollTop;
    host.innerHTML = '';
    var top = document.createElement('div'); top.id = 'ptop';
    function tb(text, fn) { var b = document.createElement('button'); b.textContent = text; b.onclick = fn; top.appendChild(b); }
    tb('💾 Save settings', saveSettings);
    tb('Enable all', function () { DEFS.forEach(function (m) { if (!m.noOn) S[m.id].on = true; }); touch(); buildPanel(); applyAll(null); });
    tb('Disable all', function () { DEFS.forEach(function (m) { if (!m.noOn) S[m.id].on = false; }); touch(); buildPanel(); applyAll(null); });
    tb('Reset ALL', function () { DEFS.forEach(function (m) { S[m.id] = defaultsFor(m); }); touch(); buildPanel(); applyTheme(); applyAll(null); });
    host.appendChild(top);

    DEFS.forEach(function (m) {
      var d = document.createElement('details');
      d.open = !!openIds[m.id];
      d.addEventListener('toggle', function () { openIds[m.id] = d.open; stSet('s', 'lc5_open', JSON.stringify(openIds)); });
      var sm = document.createElement('summary');
      if (!m.noOn) {
        var cb = document.createElement('input'); cb.type = 'checkbox'; cb.checked = !!S[m.id].on;
        cb.onclick = function (e) { e.stopPropagation(); };
        cb.onchange = function () { S[m.id].on = cb.checked; touch(); scheduleApply(); };
        sm.appendChild(cb);
      }
      var nm = document.createElement('span'); nm.textContent = m.name; sm.appendChild(nm);
      d.appendChild(sm);
      m.f.forEach(function (fd) {
        var row = document.createElement('div'); row.className = 'fld';
        var lb = document.createElement('label'); lb.textContent = fd[1];
        row.appendChild(lb); row.appendChild(mkCtl(m, fd));
        d.appendChild(row);
      });
      var rb = document.createElement('button'); rb.className = 'mbtn'; rb.textContent = 'Reset this module';
      rb.onclick = function () { S[m.id] = defaultsFor(m); touch(); buildPanel(); applyTheme(); applyAll(null); };
      d.appendChild(rb);
      host.appendChild(d);
    });
    host.scrollTop = keepScroll;
  }

  // ================================================================
  // MATHS (all arrays are aligned with the candle array; null = not available)
  // ================================================================
  function firstValid(a) { for (var i = 0; i < a.length; i++) if (!isNa(a[i])) return i; return -1; }
  function sma(a, len) {
    var o = [], s = 0, i;
    for (i = 0; i < a.length; i++) {
      s += a[i]; if (i >= len) s -= a[i - len];
      o.push(i >= len - 1 ? s / len : null);
    }
    return o;
  }
  function ema(a, len) {     // Pine ta.ema: SMA-seeded
    var n = a.length, o = [], al = 2 / (len + 1), i, s = 0;
    for (i = 0; i < n; i++) o.push(null);
    if (n < len) return o;
    for (i = 0; i < len; i++) s += a[i];
    o[len - 1] = s / len;
    for (i = len; i < n; i++) o[i] = a[i] * al + o[i - 1] * (1 - al);
    return o;
  }
  function rma(a, len) {     // Pine ta.rma: SMA-seeded, skips leading nulls
    var n = a.length, o = [], f = firstValid(a), i, s = 0;
    for (i = 0; i < n; i++) o.push(null);
    if (f < 0 || f + len > n) return o;
    for (i = f; i < f + len; i++) s += a[i];
    o[f + len - 1] = s / len;
    for (i = f + len; i < n; i++) o[i] = (o[i - 1] * (len - 1) + a[i]) / len;
    return o;
  }
  function stdev(a, len) {   // population stdev (ta.stdev)
    var o = [], i, k, m, s;
    for (i = 0; i < a.length; i++) {
      if (i < len - 1) { o.push(null); continue; }
      m = 0; for (k = i - len + 1; k <= i; k++) m += a[k]; m /= len;
      s = 0; for (k = i - len + 1; k <= i; k++) s += (a[k] - m) * (a[k] - m);
      o.push(Math.sqrt(s / len));
    }
    return o;
  }
  function rollMax(a, len, isMax) {
    var o = [], i, k, r;
    for (i = 0; i < a.length; i++) {
      if (i < len - 1) { o.push(null); continue; }
      r = a[i];
      for (k = i - len + 1; k < i; k++) r = isMax ? Math.max(r, a[k]) : Math.min(r, a[k]);
      o.push(r);
    }
    return o;
  }
  function shiftBy(a, off) {  // Pine plot offset: positive = shifted to the right
    var o = [], i, j;
    for (i = 0; i < a.length; i++) { j = i - off; o.push(j >= 0 && j < a.length ? a[j] : null); }
    return o;
  }
  function pivotsOf(a, l, r, isHigh) {   // value of a confirmed pivot, placed on the CONFIRMATION bar
    var n = a.length, o = [], i, j, k, v, ok;
    for (i = 0; i < n; i++) {
      o.push(null);
      j = i - r;
      if (j - l < 0) continue;
      v = a[j]; ok = true;
      for (k = j - l; k <= j + r; k++) {
        if (k === j) continue;
        if (isHigh ? a[k] > v : a[k] < v) { ok = false; break; }
      }
      if (ok) o[i] = v;
    }
    return o;
  }
  function trueRange(H, L, C) {
    var o = [], i;
    for (i = 0; i < H.length; i++) {
      if (i === 0) o.push(H[i] - L[i]);
      else o.push(Math.max(H[i] - L[i], Math.abs(H[i] - C[i - 1]), Math.abs(L[i] - C[i - 1])));
    }
    return o;
  }
  function atrOf(X, len) { return rma(trueRange(X.H, X.L, X.C), len); }
  function adxOf(X, len) {   // Pine ta.dmi(len, len) -> ADX
    var n = X.n, pd = [0], md = [0], i, up, dn;
    for (i = 1; i < n; i++) {
      up = X.H[i] - X.H[i - 1]; dn = X.L[i - 1] - X.L[i];
      pd.push(up > dn && up > 0 ? up : 0);
      md.push(dn > up && dn > 0 ? dn : 0);
    }
    var tr = rma(trueRange(X.H, X.L, X.C), len), p = rma(pd, len), m = rma(md, len), dx = [], pl, mi, sum;
    for (i = 0; i < n; i++) {
      if (isNa(tr[i]) || tr[i] === 0) { dx.push(null); continue; }
      pl = 100 * p[i] / tr[i]; mi = 100 * m[i] / tr[i]; sum = pl + mi;
      dx.push(Math.abs(pl - mi) / (sum === 0 ? 1 : sum));
    }
    var a = rma(dx, len);
    return a.map(function (v) { return isNa(v) ? null : 100 * v; });
  }
  function sarSeries(H, L, C, st, inc, mx) {   // Pine ta.sar
    var n = H.length, out = [], res = null, mm = null, acc = null, below = false, i, first;
    for (i = 0; i < n; i++) out.push(null);
    for (i = 1; i < n; i++) {
      first = false;
      if (i === 1) {
        if (C[1] > C[0]) { below = true; mm = H[1]; res = L[0]; } else { below = false; mm = L[1]; res = H[0]; }
        first = true; acc = st;
      }
      if (i >= 2) {
        res = res + acc * (mm - res);
        if (below) {
          if (res > L[i]) { first = true; below = false; res = Math.max(H[i], mm); mm = L[i]; acc = st; }
        } else {
          if (res < H[i]) { first = true; below = true; res = Math.min(L[i], mm); mm = H[i]; acc = st; }
        }
        if (!first) {
          if (below) { if (H[i] > mm) { mm = H[i]; acc = Math.min(acc + inc, mx); } }
          else { if (L[i] < mm) { mm = L[i]; acc = Math.min(acc + inc, mx); } }
        }
      }
      if (below) { res = Math.min(res, L[i - 1]); if (i > 1) res = Math.min(res, L[i - 2]); }
      else { res = Math.max(res, H[i - 1]); if (i > 1) res = Math.max(res, H[i - 2]); }
      out[i] = res;
    }
    return out;
  }
  function crossUp(a, b, i) { return i > 0 && !isNa(a[i]) && !isNa(b[i]) && !isNa(a[i - 1]) && !isNa(b[i - 1]) && a[i] > b[i] && a[i - 1] <= b[i - 1]; }
  function crossDn(a, b, i) { return i > 0 && !isNa(a[i]) && !isNa(b[i]) && !isNa(a[i - 1]) && !isNa(b[i - 1]) && a[i] < b[i] && a[i - 1] >= b[i - 1]; }
  function srcOf(X, name) {
    if (name === 'open') return X.O; if (name === 'high') return X.H; if (name === 'low') return X.L;
    var o = [], i;
    for (i = 0; i < X.n; i++) {
      if (name === 'hl2') o.push((X.H[i] + X.L[i]) / 2);
      else if (name === 'hlc3') o.push((X.H[i] + X.L[i] + X.C[i]) / 3);
      else if (name === 'ohlc4') o.push((X.O[i] + X.H[i] + X.L[i] + X.C[i]) / 4);
      else o.push(X.C[i]);
    }
    return o;
  }

  // ---- higher time frame helpers (previous day / week / month) ----
  function mondayOf(d) {
    var p = d.split('-'), t = Date.UTC(+p[0], +p[1] - 1, +p[2]), dow = new Date(t).getUTCDay();
    t -= ((dow + 6) % 7) * 86400000;
    return new Date(t).toISOString().substr(0, 10);
  }
  function pkey(d, g) { return g === 'D' ? d : (g === 'W' ? mondayOf(d) : d.substr(0, 7)); }
  function dailyList(X) {
    if (state.htf.length) return state.htf;
    var out = [], cur = null, i;           // fallback: build days from the loaded candles
    for (i = 0; i < X.n; i++) {
      if (!cur || cur.d !== X.D[i]) { cur = { d: X.D[i], h: X.H[i], l: X.L[i], c: X.C[i] }; out.push(cur); }
      else { cur.h = Math.max(cur.h, X.H[i]); cur.l = Math.min(cur.l, X.L[i]); cur.c = X.C[i]; }
    }
    return out;
  }
  function buildPeriods(list, g) {
    var out = [], cur = null, i, r, k;
    for (i = 0; i < list.length; i++) {
      r = list[i]; k = pkey(r.d, g);
      if (!cur || cur.k !== k) { cur = { k: k, h: r.h, l: r.l, c: r.c }; out.push(cur); }
      else { cur.h = Math.max(cur.h, r.h); cur.l = Math.min(cur.l, r.l); cur.c = r.c; }
    }
    return out;
  }
  function prevSeries(X, g) {   // previous-period High/Low/Close for every bar (like request.security high[1])
    var per = buildPeriods(dailyList(X), g), h = [], l = [], c = [], brk = [], p = -1, lastK = null, i, k;
    for (i = 0; i < X.n; i++) {
      k = pkey(X.D[i], g);
      while (p + 1 < per.length && per[p + 1].k < k) p++;
      if (p >= 0) { h.push(per[p].h); l.push(per[p].l); c.push(per[p].c); } else { h.push(null); l.push(null); c.push(null); }
      brk.push(k !== lastK); lastK = k;
    }
    return { h: h, l: l, c: c, brk: brk };
  }
  function prevOfLast(X, g) {
    var per = buildPeriods(dailyList(X), g), k = pkey(X.D[X.n - 1], g), p = -1;
    while (p + 1 < per.length && per[p + 1].k < k) p++;
    return p >= 0 ? per[p] : null;
  }
  // keeps only the values that belong to the CURRENT day / week / month (everything older becomes null)
  function onlyCurrent(arr, X, g) {
    var k = pkey(X.D[X.n - 1], g);
    return arr.map(function (v, i) { return pkey(X.D[i], g) === k ? v : null; });
  }

  // ---- timeframe aggregation (4H / W / M) ----
  function aggHours(b, k) {
    var out = [], cur = null, cnt = 0, day = null, i, c, d;
    for (i = 0; i < b.length; i++) {
      c = b[i]; d = Math.floor(c[0] / 86400);
      if (d !== day || cnt >= k) { cur = [c[0], c[1], c[2], c[3], c[4], c[5]]; out.push(cur); day = d; cnt = 1; }
      else { cur[2] = Math.max(cur[2], c[2]); cur[3] = Math.min(cur[3], c[3]); cur[4] = c[4]; cur[5] += c[5]; cnt++; }
    }
    return out;
  }
  function aggDaily(b, g) {
    var out = [], cur = null, key = null, i, c, k;
    for (i = 0; i < b.length; i++) {
      c = b[i]; k = pkey(c[0], g);
      if (!cur || k !== key) { cur = [c[0], c[1], c[2], c[3], c[4], c[5]]; out.push(cur); key = k; }
      else { cur[2] = Math.max(cur[2], c[2]); cur[3] = Math.min(cur[3], c[3]); cur[4] = c[4]; cur[5] += c[5]; }
    }
    return out;
  }
  function aggregate(b, tf) {
    if (tf === '4H') return aggHours(b, 4);
    if (tf === 'W') return aggDaily(b, 'W');
    if (tf === 'M') return aggDaily(b, 'M');
    return b;
  }

  // ================================================================
  // SERIES HELPERS
  // ================================================================
  function lineSer(key, vals, color, op, width, style, o) {
    o = o || {};
    touched[key] = 1;
    var opts = {
      color: rgba(color, op), lineWidth: width, lineStyle: STYLE_ID[style] || 0,
      lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false, title: '',
      autoscaleInfoProvider: (o.far && !S.chart.farScale) ? function () { return null; } : undefined
    };
    var s = SER[key];
    if (!s) { s = chart.addLineSeries(opts); SER[key] = s; } else s.applyOptions(opts);
    var data = [], i, v, p;
    for (i = 0; i < CT.n; i++) {
      v = vals[i];
      if (isNa(v)) data.push({ time: CT.T[i] });
      else {
        p = { time: CT.T[i], value: v };
        if (o.brk && o.brk[i]) p.color = 'rgba(0,0,0,0)';
        data.push(p);
      }
    }
    s.setData(data);
  }
  function rawSer(key, pts, color, op, width, style) {   // sparse series: [{i, v}, ...]
    touched[key] = 1;
    var opts = {
      color: rgba(color, op), lineWidth: width, lineStyle: STYLE_ID[style] || 0,
      lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false, title: '',
      autoscaleInfoProvider: S.chart.farScale ? undefined : function () { return null; }
    };
    var s = SER[key];
    if (!s) { s = chart.addLineSeries(opts); SER[key] = s; } else s.applyOptions(opts);
    s.setData(pts.map(function (p) { return { time: CT.T[p.i], value: p.v }; }));
  }
  function mark(i, pos, shape, color, text, size) {
    MK.push({ i: i, position: pos, shape: shape, color: color, text: text || '', size: size || 1 });
  }
  // full-width horizontal price line with an axis label (used by S/R and PDH / PDL / PWH ...)
  function priceLine(price, title, col, op, w, style) {
    if (isNa(price)) return;
    PLS.push(candleS.createPriceLine({
      price: price, color: rgba(col, op), lineWidth: w, lineStyle: STYLE_ID[style] || 0,
      axisLabelVisible: true, title: title || ''
    }));
  }
  function clearPriceLines() {
    PLS.forEach(function (p) { try { candleS.removePriceLine(p); } catch (e) {} });
    PLS = [];
  }

  // ================================================================
  // CONTEXT + SHARED CALCULATIONS
  // ================================================================
  function makeCtx() {
    var C = state.candles, n = C.length, X = { n: n, T: [], O: [], H: [], L: [], C: [], V: [], D: [], hv: null };
    var i, c;
    X.dailyTf = (state.tf === 'D' || state.tf === 'W' || state.tf === 'M');
    for (i = 0; i < n; i++) {
      c = C[i];
      X.T.push(c[0]); X.O.push(c[1]); X.H.push(c[2]); X.L.push(c[3]); X.C.push(c[4]); X.V.push(c[5]);
      X.D.push(typeof c[0] === 'string' ? c[0] : new Date(c[0] * 1000).toISOString().substr(0, 10));
    }
    return X;
  }

  function luxCalc(X) {   // Lux trendlines - needed by the Lux module AND by the signal / pro modules
    var P = S.lux, n = X.n, u = [], l = [], buy = [], sell = [], i, up = null, lo = null;
    for (i = 0; i < n; i++) { u.push(null); l.push(null); buy.push(false); sell.push(false); }
    if (!P.on || n < 2 * P.len + 2) return { u: u, l: l, buy: buy, sell: sell, up: null, lo: null };
    var ph = pivotsOf(X.H, P.len, P.len, true), pl = pivotsOf(X.L, P.len, P.len, false), atr = atrOf(X, P.len);
    for (i = 0; i < n; i++) {
      if (ph[i] !== null && !isNa(atr[i])) {
        up = { x1: i - P.len, y1: ph[i], pb: -(atr[i] / P.len * P.mult) };
      }
      if (pl[i] !== null && !isNa(atr[i])) {
        lo = { x1: i - P.len, y1: pl[i], pb: (atr[i] / P.len * P.mult) };
      }
      if (up) u[i] = up.y1 + up.pb * (i - up.x1);
      if (lo) l[i] = lo.y1 + lo.pb * (i - lo.x1);
      buy[i] = crossUp(X.C, u, i);
      sell[i] = crossDn(X.C, l, i);
    }
    return { u: u, l: l, buy: buy, sell: sell, up: up, lo: lo };
  }

  // ================================================================
  // MODULES
  // ================================================================
  var MODS = {};

  MODS.vol = function (X, P) {            // 4. Effective Volume Analyzer: see-through candles on high volume
    var ma = sma(X.V, P.maLen), hv = [], i, base;
    for (i = 0; i < X.n; i++) {
      if (!isNa(ma[i]) && X.V[i] > ma[i] * P.mult) {
        base = X.C[i] >= X.O[i] ? P.upCol : P.dnCol;
        hv.push({ body: rgba(base, P.op), edge: rgba(base, P.edgeOp) });
      } else hv.push(null);
    }
    X.hv = hv;
  };

  MODS.ema = function (X, P) {            // 1. EMAs
    for (var k = 1; k <= 5; k++) {
      if (P['show' + k]) lineSer('ema' + k, ema(X.C, P['len' + k]), P['col' + k], P.op, P.width, P.style);
    }
  };

  MODS.bb = function (X, P) {             // 2. Bollinger Bands
    var src = srcOf(X, P.src), basis = sma(src, P.len), sd = stdev(src, P.len), up = [], lo = [], i;
    for (i = 0; i < X.n; i++) {
      if (isNa(basis[i])) { up.push(null); lo.push(null); }
      else { up.push(basis[i] + P.mult * sd[i]); lo.push(basis[i] - P.mult * sd[i]); }
    }
    basis = shiftBy(basis, P.offset); up = shiftBy(up, P.offset); lo = shiftBy(lo, P.offset);
    if (P.showBasis) lineSer('bbB', basis, P.basisCol, P.op, P.width, P.style);
    if (P.showUp) lineSer('bbU', up, P.upCol, P.op, P.width, P.style);
    if (P.showLo) lineSer('bbL', lo, P.loCol, P.op, P.width, P.style);
    if (P.fillOn && P.showUp && P.showLo) DR.fills.push({ a: up, b: lo, col: rgba(P.fillCol, P.fillOp) });
  };

  MODS.sr = function (X, P) {             // 3. Dynamic S/R (highest high / lowest low of the lookback) - price lines
    var n = X.n, from = Math.max(0, n - P.lookback), h = -Infinity, l = Infinity, i;
    for (i = from; i < n; i++) { h = Math.max(h, X.H[i]); l = Math.min(l, X.L[i]); }
    if (P.showRes) priceLine(h, P.labels ? 'R1: ' + h.toFixed(2) : '', P.resCol, P.op, P.width, P.style);
    if (P.showSup) priceLine(l, P.labels ? 'S1: ' + l.toFixed(2) : '', P.supCol, P.op, P.width, P.style);
  };

  MODS.lux = function (X, P) {            // 5. Lux Trendlines (latest pair, extended to the last bar) + B / S
    var L = X.lux, n = X.n;
    if (P.showUp && L.up) rawSer('luxU', [{ i: L.up.x1, v: L.up.y1 }, { i: n - 1, v: L.u[n - 1] }], P.upCol, P.op, P.width, P.style);
    if (P.showDn && L.lo) rawSer('luxL', [{ i: L.lo.x1, v: L.lo.y1 }, { i: n - 1, v: L.l[n - 1] }], P.dnCol, P.op, P.width, P.style);
    if (P.showSig) {
      for (var i = 0; i < n; i++) {
        if (L.buy[i]) mark(i, 'belowBar', 'arrowUp', '#12A79D', 'B', 0.8);
        if (L.sell[i]) mark(i, 'aboveBar', 'arrowDown', '#F23645', 'S', 0.8);
      }
    }
  };

  function levelLines(X, pv) {            // shared by Traditional Pivots
    var pp = [], r1 = [], r2 = [], r3 = [], s1 = [], s2 = [], s3 = [], i, h, l, c, p, rg;
    for (i = 0; i < X.n; i++) {
      h = pv.h[i]; l = pv.l[i]; c = pv.c[i];
      if (isNa(h)) { pp.push(null); r1.push(null); r2.push(null); r3.push(null); s1.push(null); s2.push(null); s3.push(null); continue; }
      p = (h + l + c) / 3; rg = h - l;
      pp.push(p); r1.push(2 * p - l); r2.push(p + rg); r3.push(h + 2 * (p - l));
      s1.push(2 * p - h); s2.push(p - rg); s3.push(l - 2 * (h - p));
    }
    return { pp: pp, r1: r1, r2: r2, r3: r3, s1: s1, s2: s2, s3: s3 };
  }

  MODS.pivots = function (X, P) {         // 6. Traditional Pivot Points
    var pv = prevSeries(X, P.tf), lv = levelLines(X, pv), o = { brk: pv.brk, far: true };
    if (P.onlyCur) Object.keys(lv).forEach(function (k) { lv[k] = onlyCurrent(lv[k], X, P.tf); });
    if (P.showP) lineSer('pvP', lv.pp, P.pCol, P.op, P.width, P.style, o);
    ['r1', 'r2', 'r3'].forEach(function (k) { if (P['show' + k.toUpperCase()]) lineSer('pv' + k, lv[k], P.rCol, P.op, P.width, P.style, o); });
    ['s1', 's2', 's3'].forEach(function (k) { if (P['show' + k.toUpperCase()]) lineSer('pv' + k, lv[k], P.sCol, P.op, P.width, P.style, o); });
  };

  MODS.dc = function (X, P) {             // 7. Donchian Channels
    var up = rollMax(X.H, P.len, true), lo = rollMax(X.L, P.len, false), mid = [], i;
    for (i = 0; i < X.n; i++) mid.push(isNa(up[i]) ? null : (up[i] + lo[i]) / 2);
    up = shiftBy(up, P.offset); lo = shiftBy(lo, P.offset); mid = shiftBy(mid, P.offset);
    if (P.showUp) lineSer('dcU', up, P.upCol, P.op, P.width, P.style);
    if (P.showLo) lineSer('dcL', lo, P.loCol, P.op, P.width, P.style);
    if (P.showMid) lineSer('dcM', mid, P.midCol, P.op, P.width, P.style);
    if (P.fillOn && P.showUp && P.showLo) DR.fills.push({ a: up, b: lo, col: rgba(P.fillCol, P.fillOp) });
  };

  MODS.fractals = function (X, P) {       // 8. Williams Fractals (marker sits on the pivot bar, like offset=-bars)
    var ph = pivotsOf(X.H, P.bars, P.bars, true), pl = pivotsOf(X.L, P.bars, P.bars, false), i;
    var us = SHAPES[P.upShape] || 'arrowUp', ds = SHAPES[P.dnShape] || 'arrowDown';
    for (i = 0; i < X.n; i++) {
      if (P.showUp && ph[i] !== null) mark(i - P.bars, 'aboveBar', us, P.upCol, '', P.size);
      if (P.showDn && pl[i] !== null) mark(i - P.bars, 'belowBar', ds, P.dnCol, '', P.size);
    }
  };

  MODS.dash = function (X, P) {           // 9. Dashboard & Trend Meter
    var n = X.n, bp = [], up = [], score = [], i, k, s;
    for (i = 0; i < n; i++) bp.push(Math.round((X.C[i] - X.L[i]) / Math.max(X.H[i] - X.L[i], 1) * 100));
    for (i = 0; i < n; i++) {
      s = 0;
      if (i > P.lookback) for (k = 0; k < P.lookback; k++) if (bp[i - k] >= 50) s++;
      score.push(s); up.push(s >= P.thr);
    }
    DR.shade = { arr: up, col: rgba(P.bgCol, P.bgOp) };
    var tb = $('tbl');
    if (P.showTbl && n) {
      var b = bp[n - 1], sc = score[n - 1];
      tb.innerHTML =
        '<tr><td style="background:#787b86">Live Buy %</td><td style="background:' + (b >= 50 ? '#089981' : '#F23645') + '">' + b + '%</td></tr>' +
        '<tr><td style="background:#787b86">Trend</td><td style="background:' + (up[n - 1] ? '#089981' : '#FF9800') + '">' + sc + '/' + P.lookback + '</td></tr>';
      tb.style.display = 'table';
    } else tb.style.display = 'none';
  };

  MODS.yash = function (X, P) {           // 10. Yash Edge - CPR + dual-line R1-R3 / S1-S3 zones (current day only by default)
    if (P.limit && X.dailyTf) return;
    var g = state.tf === 'D' ? 'W' : ((state.tf === 'W' || state.tf === 'M') ? 'M' : 'D');
    var pv = prevSeries(X, g), n = X.n, o = { brk: pv.brk, far: true };
    var cpr = [], z = { r1t: [], r1b: [], r2t: [], r2b: [], r3t: [], r3b: [], s1t: [], s1b: [], s2t: [], s2b: [], s3t: [], s3b: [] };
    var i, h, l, c, p, rg, hz, lv;
    for (i = 0; i < n; i++) {
      h = pv.h[i]; l = pv.l[i]; c = pv.c[i];
      if (isNa(h)) { cpr.push(null); Object.keys(z).forEach(function (k) { z[k].push(null); }); continue; }
      p = (h + l + c) / 3; rg = h - l; hz = rg * P.zw * 0.5;
      cpr.push(p);
      lv = { r1: 2 * p - l, r2: p + rg, r3: h + 2 * (p - l), s1: 2 * p - h, s2: p - rg, s3: l - 2 * (h - p) };
      ['r1', 'r2', 'r3', 's1', 's2', 's3'].forEach(function (k) { z[k + 't'].push(lv[k] + hz); z[k + 'b'].push(lv[k] - hz); });
    }
    if (P.onlyCur) {
      cpr = onlyCurrent(cpr, X, g);
      Object.keys(z).forEach(function (k) { z[k] = onlyCurrent(z[k], X, g); });
    }
    if (P.showCpr) lineSer('yCpr', cpr, P.cprCol, P.op, 2, 'Dotted', o);
    ['r1', 'r2', 'r3', 's1', 's2', 's3'].forEach(function (k) {
      if (!P[k]) return;
      var col = k.charAt(0) === 'r' ? P.resCol : P.supCol;
      lineSer('y' + k + 't', z[k + 't'], col, P.op, P.width, P.style, o);
      lineSer('y' + k + 'b', z[k + 'b'], col, P.op, P.width, P.style, o);
    });
  };

  function vwapOf(X) {                    // daily VWAP, resets every IST date (each bar on D / W / M)
    var o = [], pv = 0, vv = 0, day = null, i, tp;
    for (i = 0; i < X.n; i++) {
      if (X.D[i] !== day || X.dailyTf) { day = X.D[i]; pv = 0; vv = 0; }
      tp = (X.H[i] + X.L[i] + X.C[i]) / 3;
      pv += tp * X.V[i]; vv += X.V[i];
      o.push(vv > 0 ? pv / vv : tp);
    }
    return o;
  }

  MODS.signals = function (X, P) {        // 11. Smart Confluence Buy / Sell Signals
    var fast = ema(X.C, S.ema.len1), slow = ema(X.C, S.ema.len3);
    var sar = sarSeries(X.H, X.L, X.C, P.sarStart, P.sarInc, P.sarMax);
    var vma = sma(X.V, S.vol.maLen), i, vf, buy, sell, above, below;
    for (i = 0; i < X.n; i++) {
      vf = !isNa(vma[i]) && X.V[i] > vma[i];
      above = !isNa(sar[i]) && X.C[i] > sar[i];
      below = !isNa(sar[i]) && X.C[i] < sar[i];
      if (P.mode === 'EMA Crossover Only') {
        buy = crossUp(fast, slow, i); sell = crossDn(fast, slow, i);
      } else if (P.mode === 'Lux Breakout + Vol') {
        buy = X.lux.buy[i] && vf; sell = X.lux.sell[i] && vf;
      } else {
        buy = (crossUp(fast, slow, i) || (crossUp(X.C, fast, i) && above)) && vf;
        sell = (crossDn(fast, slow, i) || (crossDn(X.C, fast, i) && below)) && vf;
      }
      if (P.showBuy && buy) mark(i, 'belowBar', 'arrowUp', P.buyCol, 'B', 1.2);
      if (P.showSell && sell) mark(i, 'aboveBar', 'arrowDown', P.sellCol, 'S', 1.2);
    }
  };

  MODS.pro = function (X, P) {            // 12. Pro Filtering & Risk Management
    var n = X.n, vw = vwapOf(X), i;
    if (P.showVWAP) lineSer('vwap', vw, P.vwapCol, P.vwapOp, P.vwapW, P.vwapStyle);
    var vma = sma(X.V, P.rvolLen), adx = adxOf(X, P.adxLen), atr = atrOf(X, P.atrLen);
    var conf = [], entry = null, sl = null, tp = null, slA = [], tpA = [], rv, trending, hiVol;
    for (i = 0; i < n; i++) {
      rv = isNa(vma[i]) || vma[i] === 0 ? null : X.V[i] / vma[i];
      trending = !P.useADX || (!isNa(adx[i]) && adx[i] >= P.adxMin);
      hiVol = !P.useRVOL || (rv !== null && rv >= P.rvolMin);
      conf.push(X.lux.buy[i] && X.C[i] > vw[i] && trending && hiVol);
      if (conf[i] && !isNa(atr[i])) { entry = X.C[i]; sl = X.C[i] - atr[i] * P.slMult; tp = X.C[i] + atr[i] * P.tpMult; }
      if (entry !== null) { if (X.L[i] <= sl || X.H[i] >= tp) { entry = null; sl = null; tp = null; } }
      slA.push(sl); tpA.push(tp);
      if (P.showSB && conf[i]) mark(i, 'belowBar', 'arrowUp', P.sbCol, 'SB', 1.6);
    }
    if (P.showSLTP) {
      var bs = [], bt = [];
      for (i = 0; i < n; i++) {
        bs.push(slA[i] !== null && (i === 0 || slA[i - 1] === null || slA[i - 1] !== slA[i]));
        bt.push(tpA[i] !== null && (i === 0 || tpA[i - 1] === null || tpA[i - 1] !== tpA[i]));
      }
      lineSer('slL', slA, P.slCol, P.sltpOp, P.sltpW, P.sltpStyle, { brk: bs });
      lineSer('tpL', tpA, P.tpCol, P.sltpOp, P.sltpW, P.sltpStyle, { brk: bt });
    }
  };

  MODS.smc = function (X, P) {            // 13. LuxAlgo Smart Money Concepts + HTF highs / lows
    var n = X.n, ph = pivotsOf(X.H, P.len, P.len, true), pl = pivotsOf(X.L, P.len, P.len, false);
    var lastPh = [], lastPl = [], cph = null, cpl = null, i, obs = [], fvgs = [], gap;
    for (i = 0; i < n; i++) {
      if (ph[i] !== null) cph = ph[i];
      if (pl[i] !== null) cpl = pl[i];
      lastPh.push(cph); lastPl.push(cpl);
    }
    for (i = 0; i < n; i++) {
      if (P.showStruct) {
        if (crossUp(X.C, lastPh, i)) mark(i, 'belowBar', 'arrowUp', P.bullCol, 'BOS', 1);
        if (crossDn(X.C, lastPl, i)) mark(i, 'aboveBar', 'arrowDown', P.bearCol, 'CHoCH', 1);
      }
      if (P.showOB && crossUp(X.C, lastPh, i) && i - P.len >= 0) {
        obs.push({ l: i - P.len, r: i + 10, t: X.H[i - P.len], b: X.L[i - P.len], fill: rgba(P.bullCol, P.obOp), border: rgba(P.bullCol, 100), dash: 'Solid' });
      }
      if (P.showFVG && i >= 2 && X.L[i] > X.H[i - 2]) {
        gap = (X.L[i] - X.H[i - 2]) / X.H[i - 2] * 100;
        if (gap >= P.fvgThr) fvgs.push({ l: i - 2, r: i + 5, t: X.L[i], b: X.H[i - 2], fill: rgba(P.fvgCol, P.fvgOp), border: rgba(P.fvgCol, 100), dash: 'Dashed' });
      }
    }
    obs.slice(-P.maxBoxes).concat(fvgs.slice(-P.maxBoxes)).forEach(function (b) { DR.boxes.push(b); });

    var d = prevOfLast(X, 'D'), w = prevOfLast(X, 'W'), m = prevOfLast(X, 'M');
    function add(on, per, key, name, col) {
      if (!on || !per) return;
      priceLine(per[key], name, col, P.hlOp, P.hlW, P.hlStyle);
    }
    add(P.pdh, d, 'h', 'PDH', P.pdhCol); add(P.pdl, d, 'l', 'PDL', P.pdlCol);
    add(P.pwh, w, 'h', 'PWH', P.pwhCol); add(P.pwl, w, 'l', 'PWL', P.pwlCol);
    add(P.pmh, m, 'h', 'PMH', P.pmhCol); add(P.pml, m, 'l', 'PML', P.pmlCol);
  };

  // ================================================================
  // APPLY EVERYTHING
  // ================================================================
  function cp(i) {
    var c = state.candles[i], p = { time: c[0], open: c[1], high: c[2], low: c[3], close: c[4] };
    var col = CT && CT.hv ? CT.hv[i] : null;
    if (col) { p.color = col.body; p.borderColor = col.edge; p.wickColor = col.edge; }
    return p;
  }
  function vp(i) {
    var c = state.candles[i];
    return { time: c[0], value: c[5], color: rgba(c[4] >= c[1] ? S.chart.upCol : S.chart.dnCol, S.chart.volOp) };
  }
  function CT_isDaily() { return state.tf === 'D' || state.tf === 'W' || state.tf === 'M'; }

  function applyVolMA() {
    if (!volMaS) return;
    var P = S.chart, n = state.candles.length, data = [], i;
    volMaS.applyOptions({ color: rgba(P.volMaCol, 100), lineWidth: P.volMaW });
    if (!P.showVol || !P.showVolMA || !n) { volMaS.setData([]); return; }
    var ma = sma(state.candles.map(function (c) { return c[5]; }), P.volMaLen);
    for (i = 0; i < n; i++) data.push(isNa(ma[i]) ? { time: state.candles[i][0] } : { time: state.candles[i][0], value: ma[i] });
    volMaS.setData(data);
  }

  function applyAll() {
    var n = state.candles.length;
    if (!chart || !n) return;
    var i;
    modErrs = [];
    try {
      candleS.applyOptions({
        upColor: S.chart.upCol, downColor: S.chart.dnCol, wickUpColor: S.chart.upCol, wickDownColor: S.chart.dnCol,
        borderVisible: true, borderUpColor: S.chart.upCol, borderDownColor: S.chart.dnCol
      });
      chart.applyOptions({ timeScale: { rightOffset: S.chart.rightOff, timeVisible: !CT_isDaily() } });
      CT = makeCtx();
      CT.lux = luxCalc(CT);
      touched = {}; MK = []; DR = { fills: [], boxes: [], shade: null };
      clearPriceLines();
      $('tbl').style.display = 'none';
      ['vol', 'ema', 'bb', 'sr', 'lux', 'pivots', 'dc', 'fractals', 'dash', 'yash', 'signals', 'pro', 'smc'].forEach(function (id) {
        if (!S[id].on) return;
        try { MODS[id](CT, S[id]); } catch (e) { modErrs.push(id + ': ' + e.message); }
      });
      Object.keys(SER).forEach(function (k) {
        if (!touched[k]) { try { chart.removeSeries(SER[k]); } catch (e) {} delete SER[k]; }
      });

      var cd = [], vd = [];
      for (i = 0; i < n; i++) { cd.push(cp(i)); vd.push(vp(i)); }
      candleS.setData(cd);
      volS.setData(S.chart.showVol ? vd : []);
      applyVolMA();

      MK.sort(function (a, b) { return a.i - b.i; });
      candleS.setMarkers(MK.filter(function (m) { return m.i >= 0 && m.i < n; }).map(function (m) {
        return { time: CT.T[m.i], position: m.position, shape: m.shape, color: m.color, text: m.text, size: m.size };
      }));
      applyPane();
    } catch (e2) {
      modErrs.push('chart: ' + e2.message);
    }
    if (modErrs.length) setStatus('Indicator error (' + modErrs[0] + ')', 'err');
    drawOv();
  }

  // ================================================================
  // OVERLAY CANVAS (background fills, boxes, trend shading)
  // ================================================================
  function drawOv() {
    try {
      var cv = $('ov'), host = $('cwrap');
      if (!cv || !host) return;
      var w = host.clientWidth, h = host.clientHeight, dpr = window.devicePixelRatio || 1;
      if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
      var g = cv.getContext('2d');
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, w, h);
      if (!chart || !candleS || !state.candles.length || !CT) return;

      var ts = chart.timeScale(), vr = ts.getVisibleLogicalRange();
      if (!vr) return;
      var n = state.candles.length, i0 = Math.max(0, Math.floor(vr.from) - 1), i1 = Math.min(n - 1, Math.ceil(vr.to) + 1);
      var pw = w, ph = h;
      try { pw = w - chart.priceScale('right').width(); ph = h - ts.height(); } catch (e) {}
      g.save();
      g.beginPath(); g.rect(0, 0, pw, ph); g.clip();

      var X = function (i) { return ts.logicalToCoordinate(i); };
      var Y = function (p) { return candleS.priceToCoordinate(p); };
      var step = (i1 > i0) ? (X(i1) - X(i0)) / (i1 - i0) : 6;
      var i, x, y, y2, seg;

      if (DR.shade) {
        g.fillStyle = DR.shade.col;
        for (i = i0; i <= i1; i++) {
          if (DR.shade.arr[i]) { x = X(i); if (x !== null) g.fillRect(x - step / 2, 0, step + 0.5, ph); }
        }
      }

      DR.fills.forEach(function (f) {
        g.fillStyle = f.col;
        var j = i0;
        while (j <= i1) {
          while (j <= i1 && (isNa(f.a[j]) || isNa(f.b[j]))) j++;
          if (j > i1) break;
          seg = j;
          while (j <= i1 && !isNa(f.a[j]) && !isNa(f.b[j])) j++;
          var end = j - 1, k, ok = true, px = [], pa = [], pb = [];
          for (k = seg; k <= end; k++) {
            x = X(k); y = Y(f.a[k]); y2 = Y(f.b[k]);
            if (x === null || y === null || y2 === null) { ok = false; break; }
            px.push(x); pa.push(y); pb.push(y2);
          }
          if (!ok || px.length < 2) continue;
          g.beginPath();
          g.moveTo(px[0], pa[0]);
          for (k = 1; k < px.length; k++) g.lineTo(px[k], pa[k]);
          for (k = px.length - 1; k >= 0; k--) g.lineTo(px[k], pb[k]);
          g.closePath(); g.fill();
        }
      });

      DR.boxes.forEach(function (b) {
        if (b.r < i0 || b.l > i1) return;
        var xl = X(b.l), xr = X(b.r), yt = Y(b.t), yb = Y(b.b);
        if (xl === null || xr === null || yt === null || yb === null) return;
        g.fillStyle = b.fill; g.fillRect(xl, yt, xr - xl, yb - yt);
        g.strokeStyle = b.border; g.lineWidth = 1; g.setLineDash(DASH[b.dash] || []);
        g.strokeRect(xl, yt, xr - xl, yb - yt); g.setLineDash([]);
      });
      g.restore();
      ovErr = '';
    } catch (err) {
      if (ovErr !== err.message) { ovErr = err.message; setStatus('Overlay (fills / boxes) error: ' + err.message, 'err'); }
    }
  }

  // ================================================================
  // LOWER PANE (RSI + EMA / Volume Oscillator)
  // ================================================================
  function destroyPane2() {
    if (chart2) { try { chart2.remove(); } catch (e) {} }
    chart2 = null; pane2 = { kind: null, series: [], lines: [] };
    $('chart2').style.display = 'none';
  }
  function chartOptions() {
    var c = themeCols();
    return {
      autoSize: true,
      layout: { background: { type: 'solid', color: c.bg }, textColor: c.txt },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      crosshair: { mode: 0 },
      rightPriceScale: { borderColor: c.bd, minimumWidth: 72 },
      timeScale: { borderColor: c.bd, timeVisible: !CT_isDaily(), secondsVisible: false, rightOffset: S.chart.rightOff }
    };
  }
  function syncRange(r, other) {
    if (syncing || !r || !other) return;
    syncing = true;
    try { other.timeScale().setVisibleLogicalRange(r); } catch (e) {}
    syncing = false;
  }
  function wsPts(vals, from) {
    var out = [], C = state.candles, i, v;
    for (i = 0; i < C.length; i++) {
      v = vals[i];
      if (i >= from && !isNa(v)) out.push({ time: C[i][0], value: v }); else out.push({ time: C[i][0] });
    }
    return out;
  }
  function applyPane() {
    var P = S.pane, kind = P.kind === 'RSI + its EMA' ? 'rsi' : (P.kind === 'Volume Oscillator' ? 'vo' : 'none');
    if (kind === 'none' || !state.candles.length) { destroyPane2(); return; }
    if (!chart2) {
      $('chart2').style.display = 'block';
      chart2 = LightweightCharts.createChart($('chart2'), chartOptions());
      chart2.timeScale().subscribeVisibleLogicalRangeChange(function (r) { syncRange(r, chart); });
      pane2 = { kind: null, series: [], lines: [] };
    }
    if (pane2.kind !== kind) {
      pane2.series.forEach(function (s) { try { chart2.removeSeries(s); } catch (e) {} });
      pane2.series = []; pane2.lines = [];
      if (kind === 'rsi') {
        var rs = chart2.addLineSeries({ title: 'RSI', priceLineVisible: false, autoscaleInfoProvider: function () { return { priceRange: { minValue: 0, maxValue: 100 } }; } });
        var re = chart2.addLineSeries({ title: 'RSI EMA', priceLineVisible: false, lastValueVisible: false });
        [70, 50, 30].forEach(function (p) { pane2.lines.push(rs.createPriceLine({ price: p, color: '#555a66', lineWidth: 1, lineStyle: 2, axisLabelVisible: false })); });
        pane2.series = [rs, re];
      } else {
        var vs = chart2.addHistogramSeries({ title: 'Vol Osc %', priceLineVisible: false });
        [0, 100].forEach(function (p) { pane2.lines.push(vs.createPriceLine({ price: p, color: '#555a66', lineWidth: 1, lineStyle: 2, axisLabelVisible: false })); });
        pane2.series = [vs];
      }
      pane2.kind = kind;
    }
    var C = state.candles, closes = C.map(function (c) { return c[4]; }), i;
    if (kind === 'rsi') {
      var n = closes.length, alpha = 1 / P.rsiLen, ag = 0, al = 0, rsi = [], d, g2, l2, val;
      for (i = 0; i < n; i++) {
        d = i === 0 ? 0 : closes[i] - closes[i - 1];
        g2 = d > 0 ? d : 0; l2 = d < 0 ? -d : 0;
        if (i === 0) { ag = g2; al = l2; } else { ag = (1 - alpha) * ag + alpha * g2; al = (1 - alpha) * al + alpha * l2; }
        val = 50;
        if (i + 1 >= P.rsiLen && al !== 0) val = 100 - 100 / (1 + ag / al);
        rsi.push(val);
      }
      var re2 = [], a2 = 2 / (P.rsiEma + 1);
      for (i = 0; i < n; i++) re2.push(i === 0 ? rsi[0] : rsi[i] * a2 + re2[i - 1] * (1 - a2));
      var st = { lineWidth: P.rsiW, lineStyle: STYLE_ID[P.rsiStyle] || 0 };
      pane2.series[0].applyOptions({ color: rgba(P.rsiCol, P.rsiOp), lineWidth: st.lineWidth, lineStyle: st.lineStyle });
      pane2.series[1].applyOptions({ color: rgba(P.rsiEmaCol, P.rsiOp), lineWidth: st.lineWidth, lineStyle: st.lineStyle });
      pane2.series[0].setData(wsPts(rsi, P.rsiLen - 1));
      pane2.series[1].setData(wsPts(re2, P.rsiLen - 1));
    } else {
      var es = ema(C.map(function (c) { return c[5]; }), P.voS), el = ema(C.map(function (c) { return c[5]; }), P.voL), vo = [];
      for (i = 0; i < C.length; i++) vo.push(!isNa(es[i]) && !isNa(el[i]) && el[i] ? (es[i] - el[i]) / el[i] * 100 : null);
      var data = wsPts(vo, 0);
      data.forEach(function (dd) { if (dd.value !== undefined) dd.color = dd.value >= 100 ? P.voUp : (dd.value >= 0 ? rgba(P.voMid, 45) : P.voDn); });
      pane2.series[0].setData(data);
    }
    chart2.applyOptions({ timeScale: { timeVisible: !CT_isDaily() } });
    try {
      var vr = chart.timeScale().getVisibleLogicalRange();
      if (vr) { syncing = true; chart2.timeScale().setVisibleLogicalRange(vr); syncing = false; }
    } catch (e) { syncing = false; }
  }

  // ================================================================
  // CHART BUILD + DISPLAY
  // ================================================================
  function loadLib(i, cb) {
    if (window.LightweightCharts) { cb(true); return; }
    if (i >= LIB_URLS.length) { cb(false); return; }
    var s = document.createElement('script');
    s.src = LIB_URLS[i];
    s.onload = function () { cb(!!window.LightweightCharts); };
    s.onerror = function () { loadLib(i + 1, cb); };
    document.head.appendChild(s);
  }

  function saveViewSoon() {
    clearTimeout(viewTimer);
    viewTimer = setTimeout(function () {
      stSet('l', 'lc5_view', JSON.stringify({ spacing: state.spacing, gap: state.gap }));
    }, 400);
  }

  function buildChart() {
    var opts = chartOptions();
    opts.rightPriceScale.scaleMargins = { top: 0.08, bottom: 0.25 };
    chart = LightweightCharts.createChart($('chart'), opts);
    candleS = chart.addCandlestickSeries({ upColor: S.chart.upCol, downColor: S.chart.dnCol, borderVisible: true, borderUpColor: S.chart.upCol, borderDownColor: S.chart.dnCol, wickUpColor: S.chart.upCol, wickDownColor: S.chart.dnCol });
    volS = chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol', lastValueVisible: false, priceLineVisible: false });
    volMaS = chart.addLineSeries({ priceScaleId: 'vol', color: '#2962FF', lineWidth: 1, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false, title: '' });
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    chart.subscribeCrosshairMove(function (p) {
      if (!p || !p.time || !p.seriesData) { legendLast(); return; }
      var cd = p.seriesData.get(candleS);
      if (!cd) { legendLast(); return; }
      var vd = p.seriesData.get(volS);
      legend([p.time, cd.open, cd.high, cd.low, cd.close, vd ? vd.value : 0]);
    });
    chart.timeScale().subscribeVisibleLogicalRangeChange(function (r) {
      syncRange(r, chart2);
      if (!noTrack && r && r.to > r.from) {      // remember the candle size / position the user chose
        try {
          var wd = chart.timeScale().width();
          if (wd > 0) state.spacing = wd / (r.to - r.from);
          state.gap = r.to - (state.candles.length - 1);
          saveViewSoon();
        } catch (e) {}
      }
      drawOv();
    });
    setInterval(drawOv, 150);     // follows vertical price-scale changes that have no event
    applyTheme();
  }

  function legend(c) {
    $('legend').textContent = 'O ' + fmt(c[1]) + '   H ' + fmt(c[2]) + '   L ' + fmt(c[3]) + '   C ' + fmt(c[4]) + '   Vol(' + state.tf + ') ' + fmtVol(c[5]);
  }
  function legendLast() { var n = state.candles.length; if (n) legend(state.candles[n - 1]); }
  function renderHeader() {
    var n = state.candles.length;
    if (!n) return;
    var last = state.candles[n - 1];
    $('sym').textContent = state.symbol + '  ' + state.tf;
    $('ltp').textContent = '₹' + fmt(last[4]);
    var chg = $('chg');
    if (state.prevClose > 0) {
      var d = last[4] - state.prevClose, pct = d / state.prevClose * 100;
      chg.className = d >= 0 ? 'up' : 'down';
      chg.textContent = (d >= 0 ? '+' : '') + fmt(d) + ' (' + (d >= 0 ? '+' : '') + fmt(pct) + '%)';
    } else chg.textContent = '';
    legendLast();
  }
  function renderStats() {
    var s = DATA.stats, el = $('stats');
    if (!s) { el.textContent = 'Scanner data: not available for this symbol.'; return; }
    el.innerHTML = 'Scanner data (' + esc(s.src) + '):&nbsp; Volume <b>' + esc(s.volume) + '</b> &nbsp;|&nbsp; Avg Vol <b>' + esc(s.avgVol) + '</b> &nbsp;|&nbsp; Vol M <b>' + esc(s.volM) + '</b>';
  }
  function showRange() {
    var n = state.candles.length, ts = chart.timeScale();
    var bars = state.tf === 'M' ? 60 : (state.tf === 'W' ? 100 : (state.tf === 'D' ? 160 : 140));
    noTrack = true;
    try {
      if (state.spacing > 0) {          // keep the candle size / position the user chose
        ts.applyOptions({ barSpacing: state.spacing });
        ts.scrollToPosition(state.gap !== null ? state.gap : S.chart.rightOff, false);
      } else {
        ts.setVisibleLogicalRange({ from: Math.max(0, n - bars), to: n + S.chart.rightOff });
      }
    } catch (e) {}
    setTimeout(function () { noTrack = false; }, 700);
  }

  // ---- full screen / focus mode ----
  function focusMode() {
    document.body.classList.toggle('focus');
    setStatus(document.body.classList.contains('focus') ? 'Focus mode (header row hidden). Press the button again to return.' : 'Focus mode off.', '');
    setTimeout(drawOv, 300);
  }
  function toggleFull() {
    try {
      if (document.fullscreenElement) { document.exitFullscreen(); return; }
      var el = document.documentElement;
      if (el.requestFullscreen) {
        var p = el.requestFullscreen();
        if (p && p.catch) p.catch(function () { focusMode(); });
      } else focusMode();
    } catch (e) { focusMode(); }
  }

  function boot() {
    loadSettings();
    state.htf = (DATA.htf || []).map(function (c) { return { d: c[0], o: c[1], h: c[2], l: c[3], c: c[4] }; });
    state.base = DATA.candles || [];
    state.candles = aggregate(state.base, state.tf);

    $('info').textContent = DATA.symbol + ' · ' + DATA.tf + (DATA.refresh > 0 ? ' · auto-refresh ' + DATA.refresh + ' s' : ' · auto-refresh off');

    var panel = $('panel');
    if (stGet('s', 'lc5_panel') === '1') { panel.style.display = 'block'; $('indbtn').className = 'on'; }
    buildPanel();
    var ps = Number(stGet('s', 'lc5_pscroll') || 0);
    if (ps > 0) panel.scrollTop = ps;
    panel.addEventListener('scroll', function () { stSet('s', 'lc5_pscroll', String(panel.scrollTop)); });

    $('indbtn').onclick = function () {
      var open = panel.style.display === 'block';
      panel.style.display = open ? 'none' : 'block';
      $('indbtn').className = open ? '' : 'on';
      stSet('s', 'lc5_panel', open ? '0' : '1');
      setTimeout(drawOv, 200);
    };
    $('savebtn').onclick = saveSettings;
    $('themebtn').onclick = function () {
      S.chart.theme = S.chart.theme === 'Light' ? 'Dark' : 'Light';
      touch(); buildPanel(); applyTheme(); applyAll();
    };
    $('fsbtn').onclick = toggleFull;
    document.addEventListener('fullscreenchange', function () { setTimeout(drawOv, 300); });
    window.addEventListener('resize', function () { setTimeout(drawOv, 200); });
    refreshDirty();

    buildChart();
    renderStats();
    if (!state.candles.length) { setStatus('No candle data for this symbol / timeframe.', 'err'); return; }
    applyAll();
    showRange();
    renderHeader();
    setStatus(DATA.marketOpen ? ('Live - updated ' + DATA.serverTime + ' IST (Kite)') : ('Market closed - last data ' + DATA.serverTime + ' IST (Kite)'), DATA.marketOpen ? 'live' : '');
  }

  loadLib(0, function (ok) {
    if (!ok) { setStatus('Could not load the chart library (network blocked?).', 'err'); return; }
    boot();
  });
})();
</script>
</body>
</html>"""


def build_live_chart_html(payload):
    # "</" is escaped so no value can close the script tag early.
    return LIVE_CHART_HTML.replace("__DATA__", json.dumps(payload).replace("</", "<\\/"))


def draw_live_chart(kite_inst, symbol, instrument_token, prev_close, avg_vol, tf_label, height,
                    refresh_secs, market_is_active, stats_source="Kite live"):
    interval, days = LIVE_CHART_TIMEFRAMES[tf_label]
    hist = fetch_live_chart_candles(kite_inst, instrument_token, interval, days)
    if not hist:
        st.info("No candle data available for this symbol / timeframe.")
        return

    daily = interval == "day"
    rows = _live_chart_rows(hist, daily)
    htf = rows if daily else fetch_live_chart_htf(st.session_state.access_token, API_KEY, instrument_token)

    # Live Volume / Avg Vol / Vol M (the same three numbers as the dashboard columns).
    vol_live = None
    try:
        qd = kite_inst.quote([f"NSE:{symbol}"]).get(f"NSE:{symbol}")
        if qd:
            vol_live = qd.get("volume")
            if not prev_close:
                prev_close = float((qd.get("ohlc") or {}).get("close", 0) or 0)
    except Exception:
        pass

    stats = None
    if vol_live is not None:
        avg_ok = avg_vol and 0 < avg_vol < 999999999
        stats = {
            "volume": format_volume_short(vol_live),
            "avgVol": format_volume_short(avg_vol) if avg_ok else "N/A",
            "volM": format_volume_multiple(vol_live / avg_vol) if avg_ok else "N/A",
            "src": stats_source,
        }

    payload = {
        "symbol": symbol,
        "tf": tf_label,
        "prevClose": float(prev_close or 0),
        "candles": rows,
        "htf": htf,
        "stats": stats,
        "serverTime": datetime.now(IST).strftime("%H:%M:%S"),
        "marketOpen": bool(market_is_active),
        "refresh": int(refresh_secs or 0),
    }
    components.html(build_live_chart_html(payload), height=height, scrolling=False)


def render_live_chart_section(kite_inst, symbol_options, token_lookup, market_is_active,
                              groups=None, prev_close_lookup=None, avg_vol_lookup=None):
    import re as _re

    st.divider()
    st.header("📈 View Live Chart")

    groups = {k: v for k, v in (groups or {"All scanner symbols": symbol_options}).items() if v}
    if not groups:
        st.info("No symbols available to chart yet.")
        return
    prev_close_lookup = prev_close_lookup or {}
    avg_vol_lookup = avg_vol_lookup or {}

    r1a, r1b, r1c = st.columns([2, 3, 2])
    src = r1a.selectbox("Page / list", list(groups), key="livechart_src")
    picked = r1b.selectbox("Symbol", groups[src], key=f"livechart_sym_{src}")
    typed = r1c.text_input("Or type any NSE symbol", key="livechart_typed",
                           help="Leave empty to use the symbol picked on the left.").strip().upper()
    symbol = typed if (typed and _re.fullmatch(r"[A-Z0-9&_.\- ]{1,30}", typed)) else picked

    r2a, r2b, r2c = st.columns([4, 2, 2])
    tf_label = r2a.radio("Timeframe", list(LIVE_CHART_TIMEFRAMES), index=2, horizontal=True, key="livechart_tf")
    height = r2b.slider("Height", 450, 1000, 700, 50, key="livechart_h")
    refresh_secs = r2c.selectbox(
        "Auto-refresh", LIVE_CHART_REFRESH_OPTIONS,
        index=LIVE_CHART_REFRESH_OPTIONS.index(15) if market_is_active else 0,
        format_func=lambda x: "Off" if x == 0 else f"{x} s", key="livechart_refresh",
        help="Re-pulls the forming candle from Kite. Only the chart reruns, not the whole scanner. "
             "Set it to Off while you are changing indicator settings."
    )

    token = token_lookup.get(symbol)
    prev_close = prev_close_lookup.get(symbol, 0.0)
    if not token:
        info = fetch_live_chart_symbol_info(st.session_state.access_token, API_KEY, symbol)
        if info:
            token = info["token"]
            prev_close = prev_close or info["prevClose"]
    if not token:
        st.warning(f"Instrument token not found for {symbol}.")
        return

    avg_vol = avg_vol_lookup.get(symbol, 0)
    src_label = "Kite live" if symbol in avg_vol_lookup else "Kite live, no 20D avg for this symbol"

    def _draw():
        draw_live_chart(kite_inst, symbol, token, prev_close, avg_vol, tf_label, height,
                        refresh_secs, market_is_active, src_label)

    if refresh_secs and hasattr(st, "fragment"):
        st.fragment(run_every=refresh_secs)(_draw)()
    else:
        _draw()

    st.caption("Indicator settings are stored in this browser: changes are kept while you work, "
               "press 💾 Save inside the chart to keep them for good. Full screen: ⛶ button inside the chart.")


# --- 6. SIDEBAR ---

with st.sidebar:
    st.header("🕒 Scanner Status")
    now_ist = datetime.now(IST)
    st.info(f"Last Updated: {now_ist.strftime('%H:%M:%S')}")

    market_active = is_market_open()
    if market_active:
        st.success("Market Status: OPEN (Live Refresh Active) 🟢")
    else:
        st.warning("Market Status: CLOSED (Manual Mode / Backtest) 🔴")

    st.divider()
    st.header("⚙️ Display & Alert Controls")
    show_all_stocks = st.toggle("Show All Stocks (< 1%)", value=False)

    # V6: the new default alert — ON by default (dashboard tab + PC/Telegram).
    notify_hb2 = st.toggle(
        f"Enable Happy Breakout 2.0 {HAPPY_BREAKOUT2_EMOJI} (Vol ≥{HB2_VOL_MULTIPLE}x + "
        f"Volume ≥{format_volume_short(HB2_MIN_VOLUME)} + VO≥100% + 15m DC UB + RSI≥{HB2_RSI_THRESHOLD})",
        value=True,
        help=(
            f"Fires when ALL are true: Vol Multiple ≥ {HB2_VOL_MULTIPLE}x (volume ÷ 20-day Avg Volume), "
            f"Volume ≥ {HB2_MIN_VOLUME:,}, Daily Volume Oscillator (1,20) ≥ +{HB2_VO_THRESHOLD:.0f}%, "
            f"15m Donchian (28, offset 6) close ≥ upper channel, Change % vs PrevClose ≥ "
            f"{PRICE_MOVE_PCT_THRESHOLD}%, 15m RSI(14) ≥ {HB2_RSI_THRESHOLD}, and 15m RSI above its own "
            f"34-EMA. Priority tier — checked first each cycle. This SAME condition drives the "
            f"{HAPPY_BREAKOUT2_EMOJI} Happy Breakout 2.0 dashboard tab. Exits are handled by the "
            f"9/21-EMA exit engine. Every trigger is also listed in the same-day triggered stocks "
            f"section at the bottom of the page."
        )
    )

    # The three older alerts are KEPT but now default to OFF (enable when needed).
    notify_combo = st.toggle(
        f"Enable Happy Breakout {HAPPY_BREAKOUT_EMOJI} ({HAPPY_BREAKOUT_VOL_MULTIPLE}x Avg Vol + "
        f">{format_volume_short(HAPPY_BREAKOUT_MIN_VOLUME)} + RSI>{HAPPY_BREAKOUT_RSI_THRESHOLD} + VO\u2265100%)",
        value=False,
        help=(
            f"Price \u2265{PRICE_MOVE_PCT_THRESHOLD}% vs PrevClose, Volume > {HAPPY_BREAKOUT_VOL_MULTIPLE}x 20-day "
            f"Avg Volume AND > {HAPPY_BREAKOUT_MIN_VOLUME:,} shares, 15m Donchian Upper Breakout, "
            f"15m RSI > {HAPPY_BREAKOUT_RSI_THRESHOLD} AND RSI > its own 34-EMA, Daily Volume Oscillator "
            f"(1,20) \u2265 +100%. Checked after Happy Breakout 2.0 each cycle. "
            f"This SAME condition also drives the \U0001F680 Happy Breakout dashboard tab below — "
            f"the tab and the alert can never disagree. No SL is shown; exits are handled by the "
            f"9/21-EMA exit engine. OFF by default."
        )
    )
    notify_early = st.toggle(
        f"Enable Early Watchlist {EARLY_WATCHLIST_EMOJI} (PDH Break: High \u2265 Prev Day High + "
        f"Vol Multiple > {PDH_VOL_MULTIPLE_THRESHOLD}x + Change \u2265{PRICE_MOVE_PCT_THRESHOLD}%)",
        value=False,
        help=(
            f"Mirrors the Google Sheet's \u2018PDH Break\u2019 page: fires when today's High trades at "
            f"or above the previous trading day's High, Volume Multiple (today's volume \u00f7 20-day "
            f"Avg Volume incl. today's live volume) exceeds {PDH_VOL_MULTIPLE_THRESHOLD}x, AND Change % "
            f"vs PrevClose is \u2265{PRICE_MOVE_PCT_THRESHOLD}%. No RSI, no 15m Donchian breakout, and no "
            f"Daily Volume Oscillator requirement \u2014 those still don't gate this tier, independently "
            f"of Happy Breakout. Only checked if no higher-priority tier already fired this cycle for "
            f"that symbol. No SL is shown; exits are handled by the 9/21-EMA exit engine. OFF by default."
        )
    )
    notify_vol = st.toggle("Enable Individual Volume Alerts", value=False)
    notify_dc = st.toggle("Enable Individual Donchian Alerts (HAPPY SF)", value=False)
    notify_gap_filter = st.toggle(
        f"Enable Gap-Up Filter 🛡️ (Open ≥ {GAP_UP_PCT_THRESHOLD}% above Prev Close → wait for 9:45 High break)",
        value=True,
        help=(
            f"Stocks that open {GAP_UP_PCT_THRESHOLD}% or more above the previous day's close are held back "
            f"from ALL alert types until they trade above the opening-range high (high of the 9:15 and "
            f"9:30 candles, known at 9:45). Before 9:45 they never alert; after 9:45 they alert only while "
            f"LTP is above that high, and then the normal trigger conditions apply. Stocks that did not "
            f"gap up are unaffected and can alert at any time. Turn this off to restore the previous "
            f"behaviour. Dashboard tables are not affected."
        )
    )

    st.divider()
    st.header("🚪 Exit Alert Controls")
    notify_exit1 = st.toggle(
        f"Enable Exit 1 Alerts (15m close < EMA9 − {ATR_EXIT1_MULTIPLIER}×ATR14)",
        value=False,
        help=(
            f"Early-warning exit stage only — the position stays in Active Managed Trades and is "
            f"still watched for Final Exit. Requires the 15m closed candle to close BELOW EMA9 by at "
            f"least {ATR_EXIT1_MULTIPLIER}× the 15m ATR(14) (Wilder-smoothed), instead of a bare EMA "
            f"touch, so Exit 1 doesn't fire on ordinary EMA-hugging noise. Fires once per trade "
            f"lifecycle. Turn this off to silence Exit 1 alerts entirely — Final Exit keeps working "
            f"independently."
        )
    )
    notify_final_exit = st.toggle(
        "Enable Final Exit Alerts (15m close < EMA21)",
        value=False,
        help=(
            "Removes the symbol from Active Managed Trades entirely, freeing it to fire a brand-new "
            "Happy Breakout 2.0 / Happy Breakout / Early Watchlist alert later if conditions become "
            "true again. Logic is unchanged — plain close below the 21-EMA on the last closed 15m "
            "candle, no ATR buffer. Turn this off to stop positions from being auto-closed — they "
            "simply stay listed under Active Managed Trades until you turn this back on or reset the "
            "log manually."
        )
    )
    notify_book_profit = st.toggle(
        f"Enable Book Profit Alerts {BOOK_PROFIT_EMOJI} (15m RSI ≥ {BOOK_PROFIT_RSI_THRESHOLD:.0f})",
        value=False,
        help=(
            f"Sends a one-time 'BOOK PROFIT' PC + Telegram alert for any symbol in Active Managed "
            f"Trades when its 15m RSI reaches {BOOK_PROFIT_RSI_THRESHOLD:.0f} or higher. Does not "
            f"close the trade; Exit 1 / Final Exit keep working independently. OFF by default."
        )
    )

    if 'access_token' in st.session_state:
        st.divider()
        st.success("Kite Connected ✅")
        st.code(st.session_state.access_token, language="text")

    if 'access_token' not in st.session_state:
        st.divider()
        st.link_button("1. Get Login URL", st.session_state.kite.login_url(), use_container_width=True)
        token_in = st.text_input("2. Enter Request Token")
        if st.button("🚀 Activate Session", use_container_width=True):
            try:
                clean_token = token_in.split("request_token=")[-1].split("&")[0]
                data = st.session_state.kite.generate_session(clean_token, api_secret=API_SECRET)
                st.session_state.access_token = data["access_token"]
                with open(TOKEN_FILE, "w") as f:
                    f.write(data["access_token"])
                st.session_state.kite.set_access_token(data["access_token"])
                st.rerun()
            except Exception as e:
                st.error(f"Error: {e}")
    else:
        if st.button("Logout / Reset Session", type="primary", use_container_width=True):
            if os.path.exists(TOKEN_FILE):
                os.remove(TOKEN_FILE)
            st.session_state.clear()
            st.rerun()

# --- 7. MAIN DATA PROCESSING ---

sheet_log_count = 0
df_sheet_log = pd.DataFrame()

if 'access_token' in st.session_state:
    sheets = ["Scanner_Output 1", "Scanner_Output 2", "Scanner_Output 3", "Indices", "GF_Scanner"]
    all_syms = []
    gsheet_pct_map = {}

    for ws in sheets:
        try:
            df_sheet = conn.read(worksheet=ws)
            if not df_sheet.empty:
                syms_in_sheet = df_sheet.iloc[:, 0].dropna().astype(str).tolist()
                all_syms.extend(syms_in_sheet)

                pct_col_idx = None
                for idx, col_name in enumerate(df_sheet.columns):
                    if "%" in str(col_name) or "CHANGE" in str(col_name).upper():
                        pct_col_idx = idx
                        break

                for _, row in df_sheet.iterrows():
                    s_raw = str(row.iloc[0]).strip()
                    if '=' in s_raw:
                        s_raw = s_raw.split('"')[1] if '"' in s_raw else s_raw
                    s_clean = s_raw.replace('=', '').replace('"', '').strip().upper()

                    if s_clean and s_clean not in ['NAN', 'SYMBOL', 'INDEX']:
                        if pct_col_idx is not None:
                            val_str = str(row.iloc[pct_col_idx]).replace('%', '').strip()
                            try:
                                parsed_val = float(val_str)
                                if abs(parsed_val) <= 1.0 and parsed_val != 0:
                                    parsed_val = parsed_val * 100.0
                                gsheet_pct_map[s_clean] = parsed_val
                            except ValueError:
                                pass
        except Exception:
            continue

    # READ GSHEET ALERT_LOG
    try:
        df_sheet_log = conn.read(worksheet="Alert_Log")
        if not df_sheet_log.empty:
            sheet_log_count = len(df_sheet_log)

            if 'Symbol' in df_sheet_log.columns:
                df_sheet_log['Chart'] = df_sheet_log['Symbol'].apply(
                    lambda sym: f"https://in.tradingview.com/chart/?symbol=NSE:{sym}"
                )

            if 'Change %' in df_sheet_log.columns:
                def format_pct(val):
                    try:
                        v = float(val)
                        if abs(v) <= 1.0 and v != 0:
                            v = v * 100.0
                        return f"{v:.2f}%"
                    except (ValueError, TypeError):
                        return str(val)

                df_sheet_log['Change %'] = df_sheet_log['Change %'].apply(format_pct)
    except Exception:
        df_sheet_log = pd.DataFrame()
        sheet_log_count = 0

    clean_symbols = []
    for s in set(all_syms):
        s_str = str(s).strip()
        if '=' in s_str:
            s_str = s_str.split('"')[1] if '"' in s_str else s_str
        s_str = s_str.replace('=', '').replace('"', '').strip().upper()
        if s_str and s_str not in ['NAN', 'SYMBOL', 'INDEX']:
            clean_symbols.append(s_str)

    symbols = ["NSE:" + s for s in clean_symbols]
    total_fetched_count = len(symbols)

    if not symbols:
        st.warning("No symbols found across worksheets.")
        st.stop()

    # V2: Avg Volume is now sourced EXCLUSIVELY from the live 20-day daily
    # average (last 20 daily candles, SMA basis, including today's still-
    # forming volume) — the same basis TradingView's Volume indicator uses
    # (MA Length 20, Volume MA: SMA). The old, separate 22-completed-day
    # average has been removed so every volume condition in this app (Vol
    # Multiple, Volume Breakout, Happy Breakout, Early Watchlist, Happy
    # Breakout 2.0) reads from this one single number.
    avg_vols_20 = get_daily_avg_vol_20(st.session_state.access_token, API_KEY, symbols)
    results = []

    try:
        full_quotes = {}
        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i+100]
            full_quotes.update(st.session_state.kite.quote(chunk))
    except Exception as e:
        st.error(f"Kite API Error: {e}. Please re-login via sidebar.")
        st.stop()

    # Load once per run (not once per symbol) — cheap since it's just a
    # session_state dict backed by one JSON file.
    active_trades_snapshot = load_active_trades()

    for s in symbols:
        try:
            q = full_quotes.get(s)
            if not q:
                continue

            ltp, vol, cl = q['last_price'], q['volume'], q['ohlc']['close']
            today_high = q['ohlc']['high']  # used by the PDH-Break-mirrored Early Watchlist condition below
            sym_short = s.replace("NSE:", "")

            if cl > 0:
                pct = round(((ltp - cl) / cl) * 100, 2)
            elif sym_short in gsheet_pct_map:
                pct = round(gsheet_pct_map[sym_short], 2)
            else:
                pct = 0.0

            # V2: single Avg Volume source (live 20-day SMA incl. today).
            avg_v_20 = avg_vols_20.get(s, 0)

            # V2: Volume Breakout conditions now read from the SAME 20-day
            # live average used everywhere else in this app (avg_v_20),
            # instead of the old, separate 22-completed-day average.
            is_vol_break_500k = (vol > (avg_v_20 * 1.1) and pct >= 1.0 and vol >= 500000)
            is_vol_break_100k = (vol > (avg_v_20 * 1.1) and pct >= 1.0 and vol >= 100000)

            # Volume Multiple is based on current daily volume vs 20-period daily average volume.
            # Computed BEFORE the lazy-evaluation gate below (avg_v_20 comes from the separately
            # cached get_daily_avg_vol_20() call, not from the multi-timeframe candle fetch).
            # NOTE: is_vol_multiple_high still widens should_evaluate to fetch candles for a
            # symbol below 1% Change % (left untouched, unchanged from before) — but since
            # is_early_watchlist below now ALSO requires pct >= PRICE_MOVE_PCT_THRESHOLD, a
            # sub-1% symbol pulled in only via is_vol_multiple_high can no longer actually
            # qualify for Early Watchlist. It's harmless (just an extra candle fetch on some
            # symbols) and left in place since nothing was asked to change here.
            vol_multiple = (vol / avg_v_20) if avg_v_20 > 0 and avg_v_20 < 999999999 else 0.0
            is_vol_multiple_high = vol_multiple > PDH_VOL_MULTIPLE_THRESHOLD

            # LAZY EVALUATION
            is_happy_sf_vol_ok = notify_dc and vol_multiple >= HAPPY_SF_VOL_MULTIPLE
            should_evaluate = show_all_stocks or pct >= 1.0 or is_vol_break_100k or is_vol_multiple_high or is_happy_sf_vol_ok

            rsi_15m_val = 0.0
            rsi_15m_ema34 = 0.0  # used only by the Happy Breakout / Happy Breakout 2.0 alert conditions below

            if should_evaluate:
                df_15m, df_1h, df_day, df_week = fetch_multi_timeframe_candles(st.session_state.access_token, API_KEY, q['instrument_token'])
                vol_osc_pct = calculate_volume_oscillator(df_day, short_len=1, long_len=20)
                star_score_plain, star_score_html, rsi_15m_val = calculate_5star_score(
                    df_15m,
                    df_1h,
                    df_day,
                    df_week,
                    vol_osc_pct=vol_osc_pct,
                    vol_multiple=vol_multiple
                )

                # Independent RSI/EMA34 read, used only for the Happy Breakout
                # / Happy Breakout 2.0 alert conditions — kept separate from the
                # Score/tooltip logic above so the dashboard table's Score column
                # is untouched.
                if df_15m is not None and len(df_15m) >= 34:
                    _, rsi_15m_ema34 = calculate_rsi_and_ema(df_15m['close'])
                else:
                    rsi_15m_ema34 = rsi_15m_val  # can't satisfy "rsi > ema" with insufficient data

                # Donchian 15m Status
                dc_status, is_dc_breakout = get_donchian_status(df_15m, length=28, offset=6)
                dc_short_status = dc_status.replace("🚀 UPPER BREAKOUT", "🚀 UB")

                # Display-only RSI(14) for the dashboard's RSI columns
                # (replaces the old BB Median columns). Independent of the
                # Score / alert logic above — never feeds any alert condition.
                disp_rsi_15m = get_latest_rsi(df_15m)
                disp_rsi_1h = get_latest_rsi(df_1h)
                disp_rsi_day = get_latest_rsi(df_day)
                disp_rsi_wk = get_latest_rsi(df_week)

                # Previous Day High — mirrors the Google Sheet's PDH Break scan
                # (fetchPreviousDayHigh_ / getCachedPreviousDayHigh_): the High of
                # the most recent COMPLETED trading day, from the same daily
                # candles already downloaded above for Daily VO % — no extra API
                # call needed.
                pdh_value = get_previous_day_high(df_day, now_ist.strftime("%Y-%m-%d"))
            else:
                vol_osc_pct = 0.0
                star_score_plain, star_score_html = "0/5", '<div class="score-tooltip">0/5<div class="tooltip-text"><b>5-Star Checklist Breakdown</b><br><hr style="margin:4px 0;">Not Evaluated</div></div>'
                df_15m = None
                dc_short_status, is_dc_breakout = "Below", False
                disp_rsi_15m = float('nan')
                disp_rsi_1h = float('nan')
                disp_rsi_day = float('nan')
                disp_rsi_wk = float('nan')
                pdh_value = 0.0  # not known yet — this symbol is simply judged again on the next refresh

            tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{sym_short}"

            # ============================================================
            # Happy Breakout 2.0 / Early Watchlist / Happy Breakout conditions
            # ============================================================
            vo_ok = vol_osc_pct is not None and pd.notna(vol_osc_pct) and vol_osc_pct >= DAILY_VO_THRESHOLD
            rsi_above_own_ema = rsi_15m_val > rsi_15m_ema34

            # V6: HAPPY BREAKOUT 2.0 — the single new default alert. ALL true:
            #  (1) Vol Multiple >= 1.1x          (2) Volume >= 150,000
            #  (3) Daily VO (1,20) >= +100%      (4) 15m Donchian (28, 6) close >= upper
            #  (5) Change % vs PrevClose >= 1%   (6) 15m RSI(14) >= 60
            #  (7) 15m RSI above its own 34-EMA
            is_happy_breakout2 = (
                avg_v_20 > 0 and avg_v_20 < 999999999
                and vol_multiple >= HB2_VOL_MULTIPLE
                and vol >= HB2_MIN_VOLUME
                and vo_ok
                and is_dc_breakout
                and pct >= PRICE_MOVE_PCT_THRESHOLD
                and rsi_15m_val >= HB2_RSI_THRESHOLD
                and rsi_above_own_ema
            )

            is_happy_breakout = (
                avg_v_20 > 0 and avg_v_20 < 999999999
                and vol_multiple > HAPPY_BREAKOUT_VOL_MULTIPLE
                and vol > HAPPY_BREAKOUT_MIN_VOLUME
                and pct >= PRICE_MOVE_PCT_THRESHOLD
                and is_dc_breakout
                and rsi_15m_val > HAPPY_BREAKOUT_RSI_THRESHOLD
                and vo_ok
                and rsi_above_own_ema
            )

            # PDH BREAK MIRROR (replaces the old Donchian/RSI/VO%-based Early
            # Watchlist condition): trigger when today's High trades at or
            # above the previous trading day's High, Volume Multiple (today's
            # volume ÷ 20-day avg volume incl. today's live volume) exceeds
            # PDH_VOL_MULTIPLE_THRESHOLD, AND Change % vs PrevClose is >=
            # PRICE_MOVE_PCT_THRESHOLD — this last condition is the ADDED
            # logic, shared with Happy Breakout's price-move requirement.
            # RSI, Donchian, and Daily VO% still do NOT gate this tier, same
            # as the Sheet's "PDH Break" page. The "Early Watchlist" name is
            # kept unchanged everywhere below (toggle, Telegram, active-trade
            # dedup, dashboard tab).
            is_early_watchlist = (
                avg_v_20 > 0 and avg_v_20 < 999999999
                and vol_multiple > PDH_VOL_MULTIPLE_THRESHOLD
                and pdh_value > 0
                and today_high >= pdh_value
                and pct >= PRICE_MOVE_PCT_THRESHOLD
            )

            # Persistent, exit-gated dedup: a tier only fires once per
            # lifecycle. A symbol stays "in play" (all tiers unlockable
            # again) only after Final Exit removes it entirely.
            existing_trade = active_trades_snapshot.get(sym_short)
            already_alerted_happy2 = bool(existing_trade and existing_trade.get("alerted_happy2"))
            already_alerted_happy = bool(existing_trade and existing_trade.get("alerted_happy"))
            already_alerted_early = bool(existing_trade and existing_trade.get("alerted_early"))

            # Old, unrelated toggles — unchanged, still session-only dedup.
            alerted_keys = [f"{a['Symbol']}|{a['Type']}" for a in st.session_state.alerts_history]

            # GAP-UP OPENING RANGE FILTER — gate in front of the alert chain only.
            # Open price falls back to LTP when Kite reports open = 0 (pre-open).
            open_price = q['ohlc'].get('open', 0) or ltp
            gap_pct = ((open_price - cl) / cl) * 100.0 if cl > 0 and open_price > 0 else 0.0
            gap_blocked = False
            if notify_gap_filter and gap_pct >= GAP_UP_PCT_THRESHOLD:
                if datetime.now(IST).time() < OPENING_RANGE_END:
                    gap_blocked = True  # before 9:45: gap-up stocks never alert
                else:
                    or_high = get_opening_range_high(df_15m, now_ist.strftime("%Y-%m-%d"))
                    gap_blocked = not (or_high > 0 and ltp > or_high)

            alert_type = ""
            if market_active and not gap_blocked:
                if notify_hb2 and is_happy_breakout2 and not already_alerted_happy2:
                    alert_type = "Happy Breakout 2.0"
                elif notify_combo and is_happy_breakout and not already_alerted_happy:
                    alert_type = "Happy Breakout"
                elif notify_early and is_early_watchlist and not already_alerted_early:
                    alert_type = "Early Watchlist Alert"
                elif notify_vol and is_vol_break_500k and f"{sym_short}|Volume Breakout" not in alerted_keys:
                    alert_type = "Volume Breakout"
                elif (notify_dc and is_dc_breakout
                     and vol_multiple >= HAPPY_SF_VOL_MULTIPLE
                     and f"{sym_short}|HAPPY SF" not in alerted_keys):
                     alert_type = "HAPPY SF"

            if alert_type in ("Happy Breakout 2.0", "Happy Breakout", "Early Watchlist Alert"):
                if alert_type == "Happy Breakout 2.0":
                    score_emoji = HAPPY_BREAKOUT2_EMOJI
                elif alert_type == "Happy Breakout":
                    score_emoji = HAPPY_BREAKOUT_EMOJI
                else:
                    score_emoji = EARLY_WATCHLIST_EMOJI

                active_trades = load_active_trades()
                prior = active_trades.get(sym_short, {})
                active_trades[sym_short] = {
                    "instrument_token": q['instrument_token'],
                    "entry_price": prior.get("entry_price", ltp),
                    "trigger_time": prior.get("trigger_time", datetime.now(IST).isoformat()),
                    "alerted_early": prior.get("alerted_early", False) or (alert_type == "Early Watchlist Alert"),
                    "alerted_happy": prior.get("alerted_happy", False) or (alert_type == "Happy Breakout"),
                    "alerted_happy2": prior.get("alerted_happy2", False) or (alert_type == "Happy Breakout 2.0"),
                    "exit1_triggered": prior.get("exit1_triggered", False),
                    "book_profit_triggered": prior.get("book_profit_triggered", False),
                    "final_exit_triggered": False
                }
                save_active_trades(active_trades)
                active_trades_snapshot = active_trades  # keep in-loop snapshot fresh for subsequent symbols this run

                trigger_alert(sym_short, alert_type, ltp, score_emoji, chart_url=tv_url)
                st.session_state.alerts_history.append({
                    "Symbol": sym_short,
                    "Type": alert_type,
                    "Score": score_emoji,
                    "Time": now_ist.strftime("%H:%M:%S"),
                    "LTP": ltp,
                    "Chart": tv_url
                })
                # V6: same-day triggered stocks list (persisted)
                log_todays_trigger(sym_short, alert_type, score_emoji, ltp, chart_url=tv_url)

            elif alert_type:
                # Old Volume Breakout / Donchian Upper 15m toggles —
                # unchanged behavior (still shows the numeric 5-star score),
                # no active_trades involvement.
                trigger_alert(sym_short, alert_type, ltp, star_score_plain, chart_url=tv_url)
                st.session_state.alerts_history.append({
                    "Symbol": sym_short,
                    "Type": alert_type,
                    "Score": star_score_plain,
                    "Time": now_ist.strftime("%H:%M:%S"),
                    "LTP": ltp,
                    "Chart": tv_url
                })
                # V6: same-day triggered stocks list (persisted)
                log_todays_trigger(sym_short, alert_type, star_score_plain, ltp, chart_url=tv_url)

            results.append({
                "Symbol": sym_short,
                "Score": star_score_html,
                "LTP": ltp,
                "Change %": pct,
                "RSI 15m": disp_rsi_15m,
                "RSI 1H": disp_rsi_1h,
                "RSI Day": disp_rsi_day,
                "RSI Wk": disp_rsi_wk,
                "Daily VO %": vol_osc_pct,
                "DC 15m": dc_short_status,
                "Chart": tv_url,
                "Volume": format_volume_short(vol),
                "Avg Volume": format_volume_short(avg_v_20),
                "Vol Multiple": format_volume_multiple(vol_multiple),
                # Hidden helper fields (dropped before display).
                "_vol_multiple_raw": vol_multiple,
                "_rsi_15m": rsi_15m_val,
                # The actual current Early Watchlist alert condition for this row (same
                # boolean used for Telegram/PC alerts — the PDH-Break mirror), used
                # only to power the Early Watchlist dashboard tab below.
                "_is_early_watchlist": is_early_watchlist,
                # V2: the actual current Happy Breakout alert condition for this row
                # (the SAME boolean used for Telegram/PC alerts), used to power the
                # Happy Breakout dashboard tab below — this tab and the alert can no
                # longer disagree, since both read from this one boolean.
                "_is_happy_breakout": is_happy_breakout,
                # V6: the actual current Happy Breakout 2.0 alert condition for this row
                # (the SAME boolean used for Telegram/PC alerts), used to power the
                # Happy Breakout 2.0 dashboard tab below.
                "_is_happy_breakout2": is_happy_breakout2
            })
        except Exception:
            continue

    # Execute active exit rules engine during live trading hours
    if market_active:
        process_active_trade_exits(
            st.session_state.kite, st.session_state.access_token, API_KEY,
            notify_exit1=notify_exit1, notify_final_exit=notify_final_exit
        )
        # NEW: optional Book Profit alerts (15m RSI >= 90) — toggle OFF by default
        if notify_book_profit:
            process_book_profit_alerts(
                st.session_state.access_token, API_KEY, full_quotes
            )

    # --- 8. DASHBOARD DISPLAY ---

    if results:
        df_full = pd.DataFrame(results).sort_values(by="Change %", ascending=False)
        df_display = df_full if show_all_stocks else df_full[df_full['Change %'] >= 1.0]

        def get_numeric_vol(val):
            try:
                if isinstance(val, (int, float)):
                    return float(val)
                val_str = str(val).replace(',', '').strip().upper()
                if 'K' in val_str:
                    return float(val_str.replace('K', '')) * 1_000
                elif 'M' in val_str:
                    return float(val_str.replace('M', '')) * 1_000_000
                elif 'L' in val_str:
                    return float(val_str.replace('L', '')) * 100_000
                elif 'CR' in val_str:
                    return float(val_str.replace('CR', '')) * 10_000_000
                return float(val_str)
            except Exception:
                return 0.0

        if 'Volume' in df_display.columns:
            df_display['vol_numeric'] = df_display['Volume'].apply(get_numeric_vol)
        elif 'Vol' in df_display.columns:
            df_display['vol_numeric'] = df_display['Vol'].apply(get_numeric_vol)
        else:
            df_display['vol_numeric'] = 0.0

        # V6: Happy Breakout 2.0 tab — filtered on the exact same boolean used
        # for the Telegram/PC Happy Breakout 2.0 alerts (Vol Multiple >=1.1x +
        # Volume >=150k + Daily VO >=100% + 15m Donchian Upper Breakout +
        # Change % >=1% + 15m RSI >=60 + RSI above its own EMA34).
        df_hb2 = df_display[df_display['_is_happy_breakout2'] == True].copy()

        # V2: Happy Breakout tab — filtered on the exact same boolean used
        # for the Telegram/PC Happy Breakout alerts (Vol Multiple >2.5x +
        # Volume >500k + Change % >=1% + 15m Donchian Upper Breakout + 15m
        # RSI >70 + RSI above its own EMA34 + Daily VO% >=100%). This tab
        # and the alert can no longer disagree, since both now read from
        # this one boolean (was previously a separate, looser 1.1x/RSI>65
        # filter, independent of the alert).
        df_combo = df_display[df_display['_is_happy_breakout'] == True].copy()

        # Early Watchlist tab — filtered on the exact same boolean used for
        # the Telegram/PC Early Watchlist alerts (PDH-Break mirror + added
        # Change % condition: today's High \u2265 Previous Day High AND
        # Volume Multiple > PDH_VOL_MULTIPLE_THRESHOLD AND Change % \u2265
        # PRICE_MOVE_PCT_THRESHOLD). Independent of df_combo's Happy
        # Breakout filter.
        df_early = df_display[df_display['_is_early_watchlist'] == True].copy()

        # Clean temporary/hidden helper columns before anything gets displayed
        for df_item in [df_display, df_hb2, df_combo, df_early]:
            df_item.drop(columns=['vol_numeric', '_vol_multiple_raw', '_rsi_15m', '_is_early_watchlist', '_is_happy_breakout', '_is_happy_breakout2'], inplace=True, errors='ignore')

        hb2_count = len(df_hb2)
        combo_count = len(df_combo)
        early_count = len(df_early)
        vol_count = len(df_display[df_display['Daily VO %'] > 0]) if 'Daily VO %' in df_display.columns else 0
        dc_count = len(df_display[df_display['DC 15m'].astype(str).str.contains("🚀", na=False)]) if 'DC 15m' in df_display.columns else 0
        history_count = len(st.session_state.alerts_history)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Sheet Symbols", f"{total_fetched_count}")
        c2.metric("Active Filtered Stocks", f"{len(df_display)}")
        c3.metric("GSheets Alert_Log Count", f"{sheet_log_count}")
        c4.metric("Live PC Alerts Logged", f"{history_count}")

        view_mode = st.radio(
            "Table View Mode:",
            ["Rich View (Popups Enabled)", "Sortable Mode (Backtest)"],
            horizontal=True
        )

        t_hb2, t_combo, t_early, t_main, t_vol, t_dc, t_gsheet_log, t_log = st.tabs([
            f"{HAPPY_BREAKOUT2_EMOJI} Happy Breakout 2.0 ({hb2_count})",
            f"🚀 Happy Breakout ({combo_count})",
            f"🚩 Early Watchlist ({early_count})",
            f"📊 Market ({len(df_display)})",
            f"🔥 Volume ({vol_count})",
            f"📈 Donchian 15m ({dc_count})",
            f"📋 GSheet Alert_Log ({sheet_log_count})",
            f"📜 Live History ({history_count})"
        ])

        col_config = {
            "Score": st.column_config.TextColumn("Score"),
            "LTP": st.column_config.NumberColumn("LTP", format="₹%.2f"),
            "Change %": st.column_config.NumberColumn("Change %", format="%.2f%%"),
            "RSI 15m": st.column_config.NumberColumn("RSI 15m", format="%.2f"),
            "RSI 1H": st.column_config.NumberColumn("RSI 1H", format="%.2f"),
            "RSI Day": st.column_config.NumberColumn("RSI Day", format="%.2f"),
            "RSI Wk": st.column_config.NumberColumn("RSI Wk", format="%.2f"),
            "Daily VO %": st.column_config.NumberColumn("Daily VO %", format="%.2f%%"),
            "Volume": st.column_config.TextColumn("Volume"),
            "Avg Volume": st.column_config.TextColumn("Avg Volume"),
            "Vol Multiple": st.column_config.TextColumn("Vol Multiple"),
            "Chart": st.column_config.LinkColumn("Chart", display_text="Open TV ↗")
        }

        def render_custom_table(df_to_render):
            """Renders HTML table supporting hover tooltips and styled UI elements."""
            if df_to_render.empty:
                st.info("No stocks match the current criteria.")
                return

            df_calc = df_to_render.copy()
            if 'Chart' in df_calc.columns:
                df_calc['Chart'] = df_calc['Chart'].apply(lambda x: f'<a href="{x}" target="_blank">Open TV ↗</a>')

            if 'Daily VO %' in df_calc.columns:
                df_calc['Daily VO %'] = df_calc['Daily VO %'].apply(
                    lambda x: f"+{x:.2f}% 🟢" if isinstance(x, (int, float)) and x >= 100.0 else (f"{x:.2f}%" if isinstance(x, (int, float)) else str(x))
                )

            # RSI columns: 2-decimal value, or "N/A" when not evaluated / not enough candles.
            for rsi_col in ['RSI 15m', 'RSI 1H', 'RSI Day', 'RSI Wk']:
                if rsi_col in df_calc.columns:
                    df_calc[rsi_col] = df_calc[rsi_col].apply(
                        lambda x: f"{x:.2f}" if isinstance(x, (int, float)) and pd.notna(x) else "N/A"
                    )

            html_code = df_calc.to_html(escape=False, index=False, classes="custom-table")
            st.markdown(html_code, unsafe_allow_html=True)

        def render_dataframe_mode(df_to_render):
            """Renders native Streamlit dataframe using defined col_config."""
            if df_to_render.empty:
                st.info("No stocks match the current criteria.")
                return

            df_calc = df_to_render.copy()
            if 'Score' in df_calc.columns:
                df_calc['Score'] = df_calc['Score'].apply(lambda x: str(x).rsplit('>', 1)[-1] if '>' in str(x) else str(x))

            st.dataframe(
                df_calc,
                column_config=col_config,
                hide_index=True,
                use_container_width=True
            )

        def display_data(df_data):
            if view_mode == "Rich View (Popups Enabled)":
                render_custom_table(df_data)
            else:
                render_dataframe_mode(df_data)

        # --- TAB CONTENT RENDERING ---
        with t_hb2:
            st.subheader(f"{HAPPY_BREAKOUT2_EMOJI} Happy Breakout 2.0 Candidates")
            display_data(df_hb2)

        with t_combo:
            st.subheader("🚀 Happy Breakout Candidates")
            display_data(df_combo)

        with t_early:
            st.subheader("🚩 Early Watchlist Candidates")
            display_data(df_early)

        with t_main:
            st.subheader("📊 Full Market Overview")
            display_data(df_display)

        with t_vol:
            st.subheader("🔥 High Volume Oscillator Filter")
            df_vol_filtered = df_display[df_display['Daily VO %'] > 0] if 'Daily VO %' in df_display.columns else pd.DataFrame()
            display_data(df_vol_filtered)

        with t_dc:
            st.subheader("📈 Donchian 15m Upper Breakouts")
            df_dc_filtered = df_display[df_display['DC 15m'].astype(str).str.contains("🚀", na=False)] if 'DC 15m' in df_display.columns else pd.DataFrame()
            display_data(df_dc_filtered)

        with t_gsheet_log:
            st.subheader("📋 Historical Google Sheets Alert Log")
            if not df_sheet_log.empty:
                st.data_editor(
                    df_sheet_log,
                    column_config={
                        "Chart": st.column_config.LinkColumn(
                            "Chart Link",
                            display_text="View Chart 📈",
                            help="Click to open TradingView chart"
                        ),
                    },
                    hide_index=True,
                    disabled=True,
                    use_container_width=True
                )
            else:
                st.info("No historical alerts found in GSheets.")

        with t_log:
            st.subheader("📜 Live PC Session Triggered Alerts Log")
            if st.session_state.alerts_history:
                df_hist = pd.DataFrame(st.session_state.alerts_history)
                st.dataframe(
                    df_hist,
                    column_config=col_config,
                    use_container_width=True,
                    hide_index=True
                )
                if st.button("Clear Live History", type="secondary"):
                    st.session_state.alerts_history = []
                    st.rerun()
            else:
                st.info("No live alerts triggered in this session yet.")

        # --- ACTIVE TRADES MONITORING SECTION ---
        st.divider()
        st.header("🎯 Active Managed Trades (9/21 EMA Exit Monitor)")
        active_trades = load_active_trades()

        if active_trades:
            active_rows = []
            for sym, tdata in active_trades.items():
                tier_label = []
                if tdata.get("alerted_happy2"):
                    tier_label.append(HAPPY_BREAKOUT2_EMOJI)
                if tdata.get("alerted_happy"):
                    tier_label.append(HAPPY_BREAKOUT_EMOJI)
                if tdata.get("alerted_early"):
                    tier_label.append(EARLY_WATCHLIST_EMOJI)
                active_rows.append({
                    "Symbol": sym,
                    "Tier": " ".join(tier_label) if tier_label else "-",
                    "Entry Price": tdata.get("entry_price", 0.0),
                    "Trigger Time": tdata.get("trigger_time", "").replace("T", " ")[:19],
                    "Exit 1": "⚠️ Triggered" if tdata.get("exit1_triggered") else "Active 🟢",
                    "Final Exit": "❌ Closed" if tdata.get("final_exit_triggered") else "Holding 🟢",
                    "Chart": f"https://www.tradingview.com/chart/?symbol=NSE:{sym}"
                })

            df_active = pd.DataFrame(active_rows)
            st.dataframe(
                df_active,
                column_config={
                    "Chart": st.column_config.LinkColumn("Chart", display_text="Open TV ↗"),
                    "Entry Price": st.column_config.NumberColumn("Entry Price", format="₹%.2f")
                },
                hide_index=True,
                use_container_width=True
            )

            if st.button("Reset Active Trades Log", type="secondary"):
                save_active_trades({})
                st.success("Active trades cleared successfully.")
                st.rerun()
        else:
            st.info("No active managed trades currently tracked.")

        # --- LIVE INTERACTIVE CHART (TradingView lightweight-charts, Kite live data) ---
        # (EDIT 2: replaces the old call block at the very bottom of the script; keep the 8-space indent, inside "if results:")
        def _uniq(seq):
            return list(dict.fromkeys([
                str(x).strip().upper() for x in seq
                if str(x).strip() and str(x).strip().upper() not in ("NAN", "SYMBOL", "INDEX")
            ]))

        chart_symbols = _uniq(df_full["Symbol"].tolist())
        token_lookup = {
            _s.replace("NSE:", ""): _q["instrument_token"]
            for _s, _q in full_quotes.items() if "instrument_token" in _q
        }
        prev_close_lookup = {
            _s.replace("NSE:", ""): float((_q.get("ohlc") or {}).get("close", 0) or 0)
            for _s, _q in full_quotes.items()
        }
        avg_vol_lookup = {_s.replace("NSE:", ""): _v for _s, _v in avg_vols_20.items()}

        _active_now = load_active_trades()
        for _s, _t in _active_now.items():
            if _t.get("instrument_token"):
                token_lookup.setdefault(_s, _t["instrument_token"])

        _gs_syms = []
        if not df_sheet_log.empty and "Symbol" in df_sheet_log.columns:
            _gs_syms = _uniq(df_sheet_log["Symbol"].tolist())

        _vol_syms = (
            _uniq(df_display[df_display["Daily VO %"] > 0]["Symbol"].tolist())
            if "Daily VO %" in df_display.columns else []
        )
        _dc_syms = (
            _uniq(df_display[df_display["DC 15m"].astype(str).str.contains("🚀", na=False)]["Symbol"].tolist())
            if "DC 15m" in df_display.columns else []
        )

        _today_entries = load_todays_triggers().get("entries", [])

        chart_groups = {
            "All scanner symbols": chart_symbols,
            f"{HAPPY_BREAKOUT2_EMOJI} Happy Breakout 2.0": _uniq(df_hb2["Symbol"].tolist()),
            "🚀 Happy Breakout": _uniq(df_combo["Symbol"].tolist()),
            "🚩 Early Watchlist": _uniq(df_early["Symbol"].tolist()),
            "📊 Market (filtered)": _uniq(df_display["Symbol"].tolist()),
            "🔥 Volume": _vol_syms,
            "📈 Donchian 15m": _dc_syms,
            "📋 GSheet Alert_Log": _gs_syms,
            "🎯 Active Trades": _uniq(list(_active_now.keys())),
            "📅 Today's Triggered": _uniq([e.get("Symbol", "") for e in _today_entries]),
        }

        render_live_chart_section(
            st.session_state.kite, chart_symbols, token_lookup, market_active,
            groups=chart_groups, prev_close_lookup=prev_close_lookup, avg_vol_lookup=avg_vol_lookup
        )

        # --- V6: SAME-DAY TRIGGERED STOCKS LIST (bottom of the page) ---
        # Every stock that fired a PC/Telegram entry alert TODAY (IST), any tier.
        # Persisted in todays_triggers.json, resets automatically each new day.
        st.divider()
        _td = load_todays_triggers()
        _entries = _td.get("entries", [])
        st.header(f"📅 Today's Triggered Stocks ({len(_entries)}) — {_td.get('date', '')}")

        if _entries:
            _rows = []
            for e in _entries:
                _sym = e.get("Symbol", "")
                _q = full_quotes.get(f"NSE:{_sym}")
                _cur = float(_q["last_price"]) if _q and _q.get("last_price") else None
                _trig = float(e.get("Trigger LTP", 0) or 0)
                _since = round(((_cur - _trig) / _trig) * 100.0, 2) if (_cur is not None and _trig > 0) else None
                _cl = float((_q.get("ohlc") or {}).get("close", 0) or 0) if _q else 0.0
                _chg = round(((_cur - _cl) / _cl) * 100.0, 2) if (_cur is not None and _cl > 0) else None
                _rows.append({
                    "Time": e.get("Time", ""),
                    "Symbol": _sym,
                    "Type": e.get("Type", ""),
                    "Score": e.get("Score", ""),
                    "Trigger LTP": _trig,
                    "Current LTP": _cur,
                    "Since Trigger %": _since,
                    "Change %": _chg,
                    "Chart": e.get("Chart") or f"https://www.tradingview.com/chart/?symbol=NSE:{_sym}"
                })

            df_today = pd.DataFrame(_rows).iloc[::-1].reset_index(drop=True)  # latest trigger first
            st.dataframe(
                df_today,
                column_config={
                    "Trigger LTP": st.column_config.NumberColumn("Trigger LTP", format="₹%.2f"),
                    "Current LTP": st.column_config.NumberColumn("Current LTP", format="₹%.2f"),
                    "Since Trigger %": st.column_config.NumberColumn("Since Trigger %", format="%.2f%%"),
                    "Change %": st.column_config.NumberColumn("Change %", format="%.2f%%"),
                    "Chart": st.column_config.LinkColumn("Chart", display_text="Open TV ↗")
                },
                hide_index=True,
                use_container_width=True
            )
            if st.button("Clear Today's Triggered List", type="secondary"):
                _write_todays_triggers({"date": _td.get("date", ""), "entries": []})
                st.session_state["todays_triggers"] = {"date": _td.get("date", ""), "entries": []}
                st.rerun()
        else:
            st.info("No stocks have triggered an alert today yet.")

else:
    st.info("Click 'Activate Session' or adjust sidebar filters to display market data.")
