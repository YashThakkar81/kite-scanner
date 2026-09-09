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

# --- PERSISTENT ACTIVE TRADES STORAGE UTILS ---
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

# --- TECHNICAL HELPERS: BB MEDIAN WITH OFFSET ---
def calculate_bb_median(df, length=20, offset=6):
    """
    Calculates 20 SMA shifted forward by 6 bars.
    Works universally across 15m, 1h, Daily, and Weekly DataFrames.
    """
    if df is None or len(df) < (length + offset):
        return 0.0
    
    # 20 SMA of Close Price
    sma_20 = df['close'].rolling(window=length).mean()
    
    # Shift forward by offset (6 bars)
    bb_median_shifted = sma_20.shift(offset)
    
    latest_val = bb_median_shifted.iloc[-1]
    return round(float(latest_val), 2) if pd.notna(latest_val) else 0.0

# --- TECHNICAL HELPERS: ATR, PIVOT S1, RSI, EMA & VOLUME OSCILLATOR ---
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

def calculate_atr14(df):
    if df is None or len(df) < 15:
        return 0.0
    df = df.copy()
    df['prev_close'] = df['close'].shift(1)
    df['tr'] = df.apply(
        lambda r: max(
            r['high'] - r['low'],
            abs(r['high'] - r['prev_close']) if pd.notna(r['prev_close']) else 0.0,
            abs(r['low'] - r['prev_close']) if pd.notna(r['prev_close']) else 0.0
        ), axis=1
    )
    atr = df['tr'].rolling(window=14).mean().iloc[-1]
    return float(atr) if pd.notna(atr) else 0.0

@st.cache_data(ttl=3600)
def fetch_pivot_s1(_kite_inst, instrument_token):
    try:
        now = datetime.now(IST)
        from_date = now - timedelta(days=5)
        hist = _kite_inst.historical_data(instrument_token, from_date, now.date() - timedelta(days=1), "day")
        if not hist or len(hist) < 1:
            return 0.0
        prev_day = hist[-1]
        p = (prev_day['high'] + prev_day['low'] + prev_day['close']) / 3.0
        s1 = (2 * p) - prev_day['high']
        return round(float(s1), 2)
    except Exception:
        return 0.0

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

def calculate_5star_score(df_15m, df_1h, df_day, df_week, vol_osc_pct=None):
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

    # Volume Oscillator Formatting with Green Dot threshold (>= 150%)
    if vol_osc_pct is not None and pd.notna(vol_osc_pct):
        vo_str = f"+{vol_osc_pct:.2f}%" if vol_osc_pct >= 0 else f"{vol_osc_pct:.2f}%"
        if vol_osc_pct >= 150.0:
            vo_str += " 🟢"
    else:
        vo_str = "N/A"

    # Pop-up Tooltip HTML with Exact RSI Values Preceding Pass/Fail Icons
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
        f'VO: {vo_str}'
        f'</span></div>'
    )

    return score_plain, html_tooltip

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

def send_telegram_alert(symbol, alert_type, ltp, sl1=0.0, sl2=0.0, score="0/5", chart_url="", vo_val=None):
    if vo_val is not None and pd.notna(vo_val):
        vo_str = f"+{vo_val:.2f}%" if vo_val >= 0 else f"{vo_val:.2f}%"
        if vo_val >= 150.0:
            vo_str += " 🟢"
    else:
        vo_str = "N/A"

    chart_link = f'<a href="{chart_url}">Open TV ↗</a>' if chart_url else ""

    if alert_type == "Happy Breakout":
        message = (
            f"<b>HAPPY BREAKOUT: {symbol}</b>\n"
            f"Score: {score}\n"
            f"VO: {vo_str}\n"
            f"Entry: ₹{ltp}\n"
            f"SL 1: ₹{sl1}\n"
            f"SL 2: ₹{sl2}\n"
            f"Chart: {chart_link}"
        )
    else:
        message = (
            f"<b>{alert_type.upper()}: {symbol}</b>\n"
            f"Score: {score}\n"
            f"VO: {vo_str}\n"
            f"Entry: ₹{ltp}\n"
            f"Chart: {chart_link}"
        )

    send_telegram_raw(message)

def send_telegram_exit(symbol, exit_type, ltp, chart_url=""):
    chart_link = f'<a href="{chart_url}">Open TV ↗</a>' if chart_url else ""
    message = (
        f"<b>{exit_type}: {symbol}</b>\n"
        f"LTP: ₹{ltp}\n"
        f"Chart: {chart_link}"
    )
    send_telegram_raw(message)

def send_telegram_eod_exit(symbols):
    if not symbols:
        return
    sym_list = ", ".join(symbols)
    message = f"<b>EXIT FULL POSITION:</b> {sym_list}"
    send_telegram_raw(message)

def trigger_alert(symbol, alert_type, ltp, sl1=0.0, sl2=0.0, score="0/5", chart_url="", vo_val=None):
    notification_js = f"""
    <script>
    if (Notification.permission === "granted") {{
        const n = new Notification("{alert_type}: {symbol} (Score: {score})", {{
            body: "Price: {ltp}",
            icon: "https://kite.zerodha.com/static/images/kite-logo.svg"
        }});
        new Audio('https://media.geeksforgeeks.org/wp-content/uploads/20190531135120/beep.mp3').play();
        setTimeout(() => n.close(), 5000);
    }}
    </script>
    """
    components.html(notification_js, height=0)
    st.toast(f"{alert_type}: {symbol} (Score: {score})", icon="🚀")

    send_telegram_alert(symbol, alert_type, ltp, sl1=sl1, sl2=sl2, score=score, chart_url=chart_url, vo_val=vo_val)

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

# --- 4. DONCHIAN CHANNEL, BB MEDIAN STATUS & CACHED HISTORICAL DATA ---
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

def get_bb_status(df, ltp, length=20, offset=6):
    """
    Calculates BB Median status:
    - 'Cross Above 🚀' : Recently crossed above BB Median
    - 'Support 🛡️'    : LTP within 0.5% of BB Median while above it
    - 'Above 🟢'       : LTP > BB Median
    - 'Below 🔴'       : LTP < BB Median
    """
    if df is None or df.empty or len(df) < (length + offset):
        return "Below 🔴", False, False

    df_calc = df.iloc[-(length + offset):].copy()
    sma20 = df_calc['close'].rolling(window=length).mean()
    
    current_bb_med = sma20.iloc[-1]
    prev_bb_med = sma20.iloc[-2] if len(sma20) >= 2 else current_bb_med
    prev_close = df_calc['close'].iloc[-2] if len(df_calc) >= 2 else ltp
    
    if pd.isna(current_bb_med) or current_bb_med == 0:
        return "Below 🔴", False, False

    is_above = ltp > current_bb_med
    is_cross_above = (prev_close <= prev_bb_med) and (ltp > current_bb_med)
    
    dist_pct = ((ltp - current_bb_med) / current_bb_med) * 100.0
    is_support = (0.0 <= dist_pct <= 0.5)

    if is_cross_above:
        status_str = "Cross Above 🚀"
    elif is_support:
        status_str = "Support 🛡️"
    elif is_above:
        status_str = "Above 🟢"
    else:
        status_str = "Below 🔴"

    return status_str, is_cross_above, is_support

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

@st.cache_data(ttl=86400, show_spinner=False)
def get_daily_avg_vol(access_token, api_key, symbols):
    kite_inst = KiteConnect(api_key=api_key)
    kite_inst.set_access_token(access_token)
    avg_vol_map = {}
    to_date = datetime.now(IST).date()
    from_date = to_date - timedelta(days=35)

    def process_symbol(s, q):
        try:
            if q:
                hist = kite_inst.historical_data(q['instrument_token'], from_date, to_date - timedelta(days=1), "day")
                return s, sum([day['volume'] for day in hist[-22:]]) / 22 if len(hist) >= 22 else 999999999
            return s, 999999999
        except Exception:
            return s, 999999999

    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i+100]
        try:
            quotes = kite_inst.quote(chunk)
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(process_symbol, s, quotes.get(s)) for s in chunk]
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

# --- Dynamic EMA Exit Monitor Engine ---
def process_active_trade_exits(kite_inst, access_token, api_key):
    now_time = datetime.now(IST).time()
    active_trades = load_active_trades()
    if not active_trades:
        return

    if now_time >= dtime(15, 15):
        eod_exit_symbols = []
        for sym, data in list(active_trades.items()):
            trig_dt = datetime.fromisoformat(data["trigger_time"])
            if trig_dt.time() <= dtime(15, 15):
                eod_exit_symbols.append(sym)
                del active_trades[sym]
            else:
                data["watchlist_next_session"] = True

        if eod_exit_symbols:
            send_telegram_eod_exit(eod_exit_symbols)
        save_active_trades(active_trades)
        return

    updated = False
    for sym, data in list(active_trades.items()):
        inst_token = data.get("instrument_token")
        if not inst_token:
            continue

        df_15m = fetch_15m_candles(access_token, api_key, inst_token)
        if df_15m is None or len(df_15m) < 15:
            continue

        df_15m['ema5'] = df_15m['close'].ewm(span=5, adjust=False).mean()
        df_15m['ema9'] = df_15m['close'].ewm(span=9, adjust=False).mean()

        last_close = df_15m['close'].iloc[-1]
        last_ema5 = df_15m['ema5'].iloc[-1]
        last_ema9 = df_15m['ema9'].iloc[-1]
        tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{sym}"

        if not data.get("exit1_triggered", False):
            if last_close < last_ema5:
                send_telegram_exit(sym, "EXIT 1", round(last_close, 2), chart_url=tv_url)
                data["exit1_triggered"] = True
                updated = True

        if not data.get("final_exit_triggered", False):
            if last_close < last_ema9:
                send_telegram_exit(sym, "FINAL EXIT", round(last_close, 2), chart_url=tv_url)
                data["final_exit_triggered"] = True
                del active_trades[sym]
                updated = True

    if updated:
        save_active_trades(active_trades)

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
    notify_combo = st.toggle("Enable Happy Breakout (500K Vol + Donchian)", value=True)
    notify_early = st.toggle("Enable Early Watchlist Alert (100K Vol + Donchian)", value=True)
    notify_vol = st.toggle("Enable Individual Volume Alerts", value=False)
    notify_dc = st.toggle("Enable Individual Donchian Alerts", value=False)

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
                with open(TOKEN_FILE, "w") as f: f.write(data["access_token"])
                st.session_state.kite.set_access_token(data["access_token"])
                st.rerun()
            except Exception as e: st.error(f"Error: {e}")
    else:
        if st.button("Logout / Reset Session", type="primary", use_container_width=True):
            if os.path.exists(TOKEN_FILE): os.remove(TOKEN_FILE)
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
            
            # Step 1: Generate TradingView Chart URL for each Symbol
            if 'Symbol' in df_sheet_log.columns:
                df_sheet_log['Chart'] = df_sheet_log['Symbol'].apply(
                    lambda sym: f"https://in.tradingview.com/chart/?symbol=NSE:{sym}"
                )

            # Format Change % column from raw decimal (0.1504) to percentage string (15.04%)
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

    avg_vols = get_daily_avg_vol(st.session_state.access_token, API_KEY, symbols)
    results = []

    try:
        full_quotes = {}
        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i+100]
            full_quotes.update(st.session_state.kite.quote(chunk))
    except Exception as e:
        st.error(f"Kite API Error: {e}. Please re-login via sidebar.")
        st.stop()

    for s in symbols:
        try:
            q = full_quotes.get(s)
            if not q:
                continue

            ltp, vol, cl = q['last_price'], q['volume'], q['ohlc']['close']
            sym_short = s.replace("NSE:", "")

            if cl > 0:
                pct = round(((ltp - cl) / cl) * 100, 2)
            elif sym_short in gsheet_pct_map:
                pct = round(gsheet_pct_map[sym_short], 2)
            else:
                pct = 0.0

            avg_v = avg_vols.get(s, 0)

            is_vol_break_500k = (vol > (avg_v * 1.1) and pct >= 1.0 and vol >= 500000)
            is_vol_break_100k = (vol > (avg_v * 1.1) and pct >= 1.0 and vol >= 100000)

            # LAZY EVALUATION
            should_evaluate = show_all_stocks or pct >= 1.0 or is_vol_break_100k

            if should_evaluate:
                df_15m, df_1h, df_day, df_week = fetch_multi_timeframe_candles(st.session_state.access_token, API_KEY, q['instrument_token'])
                vol_osc_pct = calculate_volume_oscillator(df_15m, short_len=1, long_len=20)
                star_score_plain, star_score_html = calculate_5star_score(df_15m, df_1h, df_day, df_week, vol_osc_pct=vol_osc_pct)
                
                # Donchian 15m Status
                dc_status, is_dc_breakout = get_donchian_status(df_15m, length=28, offset=6)
                dc_short_status = dc_status.replace("🚀 UPPER BREAKOUT", "🚀 UB")

                # BB Median Status across all timeframes
                bb_15m_status, _, _ = get_bb_status(df_15m, ltp)
                bb_1h_status,  _, _ = get_bb_status(df_1h, ltp)
                bb_day_status, _, _ = get_bb_status(df_day, ltp)
                bb_wk_status,  _, _ = get_bb_status(df_week, ltp)
            else:
                vol_osc_pct = 0.0
                star_score_plain, star_score_html = "0/5", '<div class="score-tooltip">0/5<div class="tooltip-text"><b>5-Star Checklist Breakdown</b><br><hr style="margin:4px 0;">Not Evaluated</div></div>'
                df_15m = None
                dc_short_status, is_dc_breakout = "Below", False
                bb_15m_status, bb_1h_status = "Below 🔴", "Below 🔴"
                bb_day_status, bb_wk_status = "Below 🔴", "Below 🔴"

            tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{sym_short}"
            alerted_keys = [f"{a['Symbol']}|{a['Type']}" for a in st.session_state.alerts_history]

            is_happy_breakout = is_vol_break_500k and is_dc_breakout
            is_early_alert = is_vol_break_100k and (not is_vol_break_500k) and is_dc_breakout

            alert_type = ""
            if market_active:
                if notify_combo and is_happy_breakout and f"{sym_short}|Happy Breakout" not in alerted_keys:
                    alert_type = "Happy Breakout"
                elif notify_early and is_early_alert and f"{sym_short}|Early Watchlist Alert" not in alerted_keys:
                    alert_type = "Early Watchlist Alert"
                elif notify_vol and is_vol_break_500k and f"{sym_short}|Volume Breakout" not in alerted_keys:
                    alert_type = "Volume Breakout"
                elif notify_dc and is_dc_breakout and f"{sym_short}|Donchian Upper 15m" not in alerted_keys:
                    alert_type = "Donchian Upper 15m"

            if alert_type:
                sl1_val, sl2_val = 0.0, 0.0
                if alert_type == "Happy Breakout":
                    atr_val = calculate_atr14(df_15m)
                    sl1_val = round(ltp - (1.5 * atr_val), 2)
                    sl2_val = fetch_pivot_s1(st.session_state.kite, q['instrument_token'])

                active_trades = load_active_trades()
                active_trades[sym_short] = {
                    "instrument_token": q['instrument_token'],
                    "entry_price": ltp,
                    "sl1": sl1_val,
                    "sl2": sl2_val,
                    "trigger_time": datetime.now(IST).isoformat(),
                    "exit1_triggered": False,
                    "final_exit_triggered": False
                }
                save_active_trades(active_trades)

                trigger_alert(sym_short, alert_type, ltp, sl1=sl1_val, sl2=sl2_val, score=star_score_plain, chart_url=tv_url, vo_val=vol_osc_pct)
                st.session_state.alerts_history.append({
                    "Symbol": sym_short,
                    "Type": alert_type,
                    "Score": star_score_plain,
                    "Time": now_ist.strftime("%H:%M:%S"),
                    "LTP": ltp,
                    "Chart": tv_url
                })

            vol_status_label = "🚀 BREAKOUT" if is_vol_break_500k else ("👀 WATCH (100K)" if is_vol_break_100k else "Normal")

            results.append({
                "Symbol": sym_short,
                "Score": star_score_html,
                "LTP": ltp,
                "Change %": pct,
                "BB Med 15m": bb_15m_status,
                "BB Med 1H": bb_1h_status,
                "BB Med Day": bb_day_status,
                "BB Med Wk": bb_wk_status,
                "Vol Osc %": vol_osc_pct,
                "Vol Status": vol_status_label,
                "DC 15m": dc_short_status,
                "Chart": tv_url,
                "Volume": vol
            })
        except Exception:
            continue

    # Execute active exit rules engine during live trading hours
    if market_active:
        process_active_trade_exits(st.session_state.kite, st.session_state.access_token, API_KEY)

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
        except:
            return 0.0

    if 'Volume' in df_display.columns:
        df_display['vol_numeric'] = df_display['Volume'].apply(get_numeric_vol)
    elif 'Vol' in df_display.columns:
        df_display['vol_numeric'] = df_display['Vol'].apply(get_numeric_vol)
    else:
        df_display['vol_numeric'] = 0.0

    dc_condition = df_display['DC 15m'].astype(str).str.contains("🚀|True|UB", case=False, na=False) if 'DC 15m' in df_display.columns else False

    # Filter Breakout subsets
    df_combo = df_display[dc_condition & (df_display['vol_numeric'] >= 500000)].copy()
    df_early = df_display[dc_condition & (df_display['vol_numeric'] >= 100000) & (df_display['vol_numeric'] < 500000)].copy()

    # Clean temporary numeric column
    for df_item in [df_display, df_combo, df_early]:
        if 'vol_numeric' in df_item.columns:
            df_item.drop(columns=['vol_numeric'], inplace=True, errors='ignore')

    combo_count = len(df_combo)
    early_count = len(df_early)
    vol_count = len(df_display[df_display['Vol Osc %'] > 0]) if 'Vol Osc %' in df_display.columns else 0
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

    t_combo, t_early, t_main, t_vol, t_dc, t_gsheet_log, t_log = st.tabs([
        f"🚀 Happy Breakout ({combo_count})",
        f"👀 Early Watch ({early_count})",
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
        "BB Med 15m": st.column_config.NumberColumn("BB Med 15m", format="₹%.2f"),
        "BB Med 1H": st.column_config.NumberColumn("BB Med 1H", format="₹%.2f"),
        "BB Med Day": st.column_config.TextColumn("BB Med Day"),
        "BB Med Wk": st.column_config.TextColumn("BB Med Wk"),
        "Vol Osc %": st.column_config.NumberColumn("Vol Osc %", format="%.2f%%"),
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
        
        if 'Vol Osc %' in df_calc.columns:
            df_calc['Vol Osc %'] = df_calc['Vol Osc %'].apply(
                lambda x: f"+{x:.2f}% 🟢" if isinstance(x, (int, float)) and x >= 150.0 else (f"{x:.2f}%" if isinstance(x, (int, float)) else str(x))
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
    with t_combo:
        st.subheader("🚀 Happy Breakout Candidates")
        display_data(df_combo)

    with t_early:
        st.subheader("👀 Early Watchlist Candidates")
        display_data(df_early)

    with t_main:
        st.subheader("📊 Full Market Overview")
        display_data(df_display)

    with t_vol:
        st.subheader("🔥 High Volume Oscillator Filter")
        df_vol_filtered = df_display[df_display['Vol Osc %'] > 0] if 'Vol Osc %' in df_display.columns else pd.DataFrame()
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
    st.header("🎯 Active Managed Trades (Dynamic EMA Exit Monitor)")
    active_trades = load_active_trades()

    if active_trades:
        active_rows = []
        for sym, tdata in active_trades.items():
            active_rows.append({
                "Symbol": sym,
                "Entry Price": tdata.get("entry_price", 0.0),
                "SL 1 (1.5 ATR)": tdata.get("sl1", 0.0),
                "SL 2 (Pivot S1)": tdata.get("sl2", 0.0),
                "Trigger Time": tdata.get("trigger_time", "").replace("T", " ")[:19],
                "Exit 1 (Close < EMA5)": "⚠️ Triggered" if tdata.get("exit1_triggered") else "Active 🟢",
                "Final Exit (Close < EMA9)": "❌ Closed" if tdata.get("final_exit_triggered") else "Holding 🟢",
                "Chart": f"https://www.tradingview.com/chart/?symbol=NSE:{sym}"
            })
        
        df_active = pd.DataFrame(active_rows)
        st.dataframe(
            df_active,
            column_config={
                "Chart": st.column_config.LinkColumn("Chart", display_text="Open TV ↗"),
                "Entry Price": st.column_config.NumberColumn("Entry Price", format="₹%.2f"),
                "SL 1 (1.5 ATR)": st.column_config.NumberColumn("SL 1", format="₹%.2f"),
                "SL 2 (Pivot S1)": st.column_config.NumberColumn("SL 2", format="₹%.2f")
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

else:
    st.info("Click 'Activate Session' or adjust sidebar filters to display market data.")
