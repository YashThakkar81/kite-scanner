import streamlit as st
import pandas as pd
from kiteconnect import KiteConnect
from streamlit_gsheets import GSheetsConnection
import streamlit.components.v1 as components
import time
import os
import pytz 
import requests 
from datetime import datetime, timedelta

# --- 1. CONFIGURATION ---
st.set_page_config(page_title="Master Omni-Scanner Pro", layout="wide")
IST = pytz.timezone('Asia/Kolkata')

# CSS for UI Polish
st.markdown("""
    <style>
    [data-testid="stDataFrame"] td { text-align: center !important; }
    [data-testid="stHeader"] th { text-align: center !important; }
    .stTabs [data-baseweb="tab-list"] { gap: 10px; }
    .stTabs [data-baseweb="tab"] { 
        padding: 8px 16px; 
        background-color: #f0f2f6; 
        border-radius: 5px; 
    }
    </style>
    """, unsafe_allow_html=True)

try:
    API_KEY = st.secrets["API_KEY"]
    API_SECRET = st.secrets["API_SECRET"]
    TG_TOKEN = st.secrets.get("TELEGRAM_TOKEN")
    TG_ID = st.secrets.get("TELEGRAM_CHAT_ID")
    conn = st.connection("gsheets", type=GSheetsConnection)
except Exception as e:
    st.error(f"Setup Error: {e}")
    st.stop()

# --- 2. ALERT & NOTIFICATION ENGINE ---
def trigger_desktop_alert(symbol, alert_type, ltp):
    """Restores the Desktop Notification logic."""
    notification_js = f"""
    <script>
    if (Notification.permission === "granted") {{
        const n = new Notification("{alert_type}: {symbol}", {{ 
            body: "Price: ₹{ltp}",
            icon: "https://kite.zerodha.com/static/images/kite-logo.svg" 
        }});
        new Audio('https://media.geeksforgeeks.org/wp-content/uploads/20190531135120/beep.mp3').play();
        setTimeout(() => n.close(), 5000);
    }}
    </script>
    """
    components.html(notification_js, height=0)
    st.toast(f"{alert_type} on {symbol}", icon="🔔")

def send_telegram_msg(token, chat_id, message):
    if not token or not chat_id: return False, "Missing Secrets"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": str(chat_id).strip(), "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200: return True, "Success"
        return False, f"Error {resp.status_code}: {resp.json().get('description')}"
    except Exception as e: return False, str(e)

# --- 3. INDICATOR CALCULATION ---
def calculate_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def get_bb_median_status(df, period=20, offset=6):
    """EMA Basis with 6-period Offset as per your TradingView Settings."""
    ema_basis = calculate_ema(df['close'], period)
    shifted_median = ema_basis.shift(offset)
    
    if len(df) < 2: return "N/A"
    curr_close, prev_close = df['close'].iloc[-1], df['close'].iloc[-2]
    curr_low, curr_med = df['low'].iloc[-1], shifted_median.iloc[-1]
    prev_med = shifted_median.iloc[-2]
    
    if prev_close < prev_med and curr_close > curr_med:
        return "🚀 CROSS"
    elif curr_low <= curr_med and curr_close > curr_med:
        return "🛡️ SUPPORT"
    return "Above" if curr_close > curr_med else "Below"

# --- 4. SIDEBAR ---
with st.sidebar:
    st.header("🕒 Scanner Status")
    now_ist = datetime.now(IST)
    st.info(f"Last Updated: {now_ist.strftime('%H:%M:%S')}")
    
    # Critical Fix: Manual permission request button
    st.header("🔔 Alert Permissions")
    if st.button("Enable Desktop Alerts"):
        components.html("<script>Notification.requestPermission();</script>", height=0)
        st.success("Permission Requested!")
    
    if st.button("Test Sound & Alert"):
        trigger_desktop_alert("TEST", "Sound Check", "0.00")

    st.divider()
    st.header("📲 Telegram Debug")
    tg_toggle = st.toggle("Enable Telegram Notification", value=True)
    if tg_toggle and TG_TOKEN and TG_ID:
        if st.button("Send Test Telegram"):
            success, log_msg = send_telegram_msg(TG_TOKEN, TG_ID, "<b>Scanner Online</b>")
            if success: st.toast("Success!")
            else: st.error(f"Failed: {log_msg}")
    
    st.divider()
    if 'access_token' not in st.session_state:
        st.link_button("1. Get Login URL", st.session_state.kite.login_url(), use_container_width=True)
        token_in = st.text_input("2. Enter Request Token")
        if st.button("🚀 Activate Session"):
            try:
                clean_token = token_in.split("request_token=")[-1].split("&")[0]
                data = st.session_state.kite.generate_session(clean_token, api_secret=API_SECRET)
                st.session_state.access_token = data["access_token"]
                st.session_state.kite.set_access_token(data["access_token"])
                st.rerun()
            except Exception as e: st.error(f"Error: {e}")
    else:
        st.success("✅ Kite Connected")
        if st.button("Logout / Reset"):
            st.session_state.clear(); st.rerun()

# --- 5. DATA PROCESSING ---
if 'access_token' in st.session_state:
    # Load symbols from GSheets
    sheets = ["Scanner_Output 1", "Scanner_Output 2", "Scanner_Output 3"]
    all_syms = []
    for ws in sheets:
        try:
            df_sheet = conn.read(worksheet=ws)
            if not df_sheet.empty:
                all_syms.extend(df_sheet.iloc[:, 0].dropna().astype(str).tolist())
        except: continue
    
    symbols = ["NSE:" + s.strip() for s in set(all_syms) if s not in ['nan', 'Symbol']][:200]
    
    results = []
    try:
        full_quotes = st.session_state.kite.quote(symbols)
        for s in symbols:
            q = full_quotes[s]
            ltp, vol, cl = q['last_price'], q['volume'], q['ohlc']['close']
            pct = round(((ltp - cl) / cl) * 100, 2)
            
            # Volume Breakout (Restored)
            is_vol_break = (vol > 500000 and pct >= 1.0)
            
            # BB Median 1H Calculation
            hist_1h = st.session_state.kite.historical_data(q['instrument_token'], now_ist-timedelta(days=7), now_ist, "60minute")
            bb_status = get_bb_median_status(pd.DataFrame(hist_1h)) if len(hist_1h) > 25 else "N/A"
            
            # EMA 15M Calculation
            hist_15m = st.session_state.kite.historical_data(q['instrument_token'], now_ist-timedelta(days=3), now_ist, "15minute")
            df_15m = pd.DataFrame(hist_15m)
            ema_status = "Below"
            if len(df_15m) > 50:
                ema20 = calculate_ema(df_15m['close'], 20).iloc[-1]
                ema50 = calculate_ema(df_15m['close'], 50).iloc[-1]
                ema_status = "⚡ CROSS" if ema20 > ema50 else "Below"

            sym_short = s.replace("NSE:", "")
            tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{sym_short}"

            # Trigger Alerts for New Signals
            if "🚀" in bb_status:
                trigger_desktop_alert(sym_short, "BB MEDIAN CROSS", ltp)
            elif is_vol_break:
                trigger_desktop_alert(sym_short, "VOLUME BREAKOUT", ltp)

            results.append({
                "Symbol": sym_short, "LTP": ltp, "Change %": pct, 
                "Vol Status": "🚀 BREAKOUT" if is_vol_break else "Normal",
                "EMA 15m": ema_status,
                "BB Median (1H)": bb_status, "Chart": tv_url
            })
    except Exception as e: st.error(f"Scan Error: {e}")

    # --- 6. TABBED DISPLAY (WITH FILTERING) ---
    t_main, t_vol, t_bb, t_ema = st.tabs(["📊 Market", "🔥 Volume", "🎯 BB Median 1H", "⚡ EMA 15m"])
    
    if results:
        df_res = pd.DataFrame(results).sort_values(by="Change %", ascending=False)
        
        with t_main: st.dataframe(df_res, use_container_width=True, hide_index=True)
        
        with t_vol: 
            # Filtered: Only show Volume Breakouts
            vol_df = df_res[df_res['Vol Status'] == "🚀 BREAKOUT"]
            st.dataframe(vol_df, use_container_width=True, hide_index=True)
            
        with t_bb:
            # Filtered: Only show BB Median Signals (Cross or Support)
            bb_df = df_res[df_res['BB Median (1H)'].str.contains("🚀|🛡️", na=False)]
            st.dataframe(bb_df, use_container_width=True, hide_index=True)
            
        with t_ema:
            # Filtered: Only show EMA 15m Crosses
            ema_df = df_res[df_res['EMA 15m'] == "⚡ CROSS"]
            st.dataframe(ema_df, use_container_width=True, hide_index=True)

    time.sleep(60)
    st.rerun()
