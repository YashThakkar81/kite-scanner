import streamlit as st
import pandas as pd
from kiteconnect import KiteConnect
from streamlit_gsheets import GSheetsConnection
import streamlit.components.v1 as components
import time
import os
import pytz 
from datetime import datetime, timedelta, time as dtime

# --- 1. CONFIGURATION ---
st.set_page_config(page_title="Master Omni-Scanner Pro", layout="wide")
IST = pytz.timezone('Asia/Kolkata')

st.markdown("""
    <style>
    [data-testid="stDataFrame"] td { text-align: center !important; }
    [data-testid="stHeader"] th { text-align: center !important; }
    [data-testid="stDataFrame"] a { justify-content: center !important; }
    .stDataFrame { margin: 0 auto; }
    </style>
    """, unsafe_allow_html=True)

try:
    API_KEY = st.secrets["API_KEY"]
    API_SECRET = st.secrets["API_SECRET"]
    conn = st.connection("gsheets", type=GSheetsConnection)
except Exception as e:
    st.error(f"Setup Error: {e}")
    st.stop()

# --- 2. PC NOTIFICATION ENGINE ---
def trigger_alert(symbol, alert_type, ltp):
    notification_js = f"""
    <script>
    if (Notification.permission === "granted") {{
        const n = new Notification("{alert_type} ALERT: {symbol}", {{ 
            body: "Price: {ltp}",
            icon: "https://kite.zerodha.com/static/images/kite-logo.svg" 
        }});
        new Audio('https://media.geeksforgeeks.org/wp-content/uploads/20190531135120/beep.mp3').play();
        setTimeout(() => n.close(), 5000);
    }}
    </script>
    """
    components.html(notification_js, height=0)
    st.toast(f"{alert_type}: {symbol}", icon="🚀")

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
    except: pass

# --- 4. DONCHIAN CHANNEL (15m TF, LENGTH 28, OFFSET 6) ---
def get_donchian_status(df, length=28, offset=6):
    if len(df) < (length + offset):
        return "N/A", False

    # Upper channel calculation: 28-period max shifted forward by offset 6
    upper_channel = df['high'].rolling(window=length).max().shift(offset)
    
    curr_close = df['close'].iloc[-1]
    curr_upper = upper_channel.iloc[-1]

    if pd.isna(curr_upper):
        return "N/A", False

    is_breakout = curr_close >= curr_upper
    status_str = "🚀 UPPER BREAKOUT" if is_breakout else "Below"
    
    return status_str, is_breakout

@st.cache_data(ttl="1d")
def get_daily_avg_vol(_kite, symbols):
    avg_vol_map = {}
    to_date = datetime.now(IST).date()
    from_date = to_date - timedelta(days=35)
    for s in symbols:
        try:
            q = _kite.quote(s)[s]
            hist = _kite.historical_data(q['instrument_token'], from_date, to_date - timedelta(days=1), "day")
            avg_vol_map[s] = sum([day['volume'] for day in hist[-22:]]) / 22 if len(hist) >= 22 else 999999999
            time.sleep(0.02) 
        except: avg_vol_map[s] = 999999999
    return avg_vol_map

# --- 5. MARKET HOURS UTILITY ---
def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5: # Saturday or Sunday
        return False
    market_start = dtime(9, 7)
    market_end = dtime(15, 30)
    return market_start <= now.time() <= market_end

# --- 6. SIDEBAR (AUTHENTICATION & STATUS) ---
with st.sidebar:
    st.header("🕒 Scanner Status")
    now_ist = datetime.now(IST)
    st.info(f"Last Updated: {now_ist.strftime('%H:%M:%S')}")
    
    market_active = is_market_open()
    if market_active:
        st.success("Market Status: OPEN (Live Refresh Active) 🟢")
    else:
        st.warning("Market Status: CLOSED (Manual Mode / Backtest) 🔴")

    if 'access_token' in st.session_state:
        st.divider()
        st.success("Kite Connected ✅")
        st.code(st.session_state.access_token, language="text")

    if 'access_token' not in st.session_state:
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

# --- 7. MAIN DATA PROCESSING & EXECUTION ---
if 'access_token' in st.session_state:
    sheets = ["Scanner_Output 1", "Scanner_Output 2", "Scanner_Output 3"]
    all_syms = []
    
    for ws in sheets:
        try:
            df_sheet = conn.read(worksheet=ws)
            if not df_sheet.empty:
                all_syms.extend(df_sheet.iloc[:, 0].dropna().astype(str).tolist())
        except: continue
    
    symbols = ["NSE:" + s.strip() for s in set(all_syms) if s not in ['nan', 'Symbol']][:200]
    if not symbols:
        st.warning("No symbols found in Google Sheet.")
        st.stop()

    avg_vols = get_daily_avg_vol(st.session_state.kite, symbols)
    results = []

    try:
        full_quotes = st.session_state.kite.quote(symbols)
    except:
        st.error("Kite Session Expired. Please re-login via sidebar.")
        st.stop()

    for s in symbols:
        try:
            q = full_quotes[s]
            ltp, vol, cl = q['last_price'], q['volume'], q['ohlc']['close']
            pct = round(((ltp - cl) / cl) * 100, 2)
            is_vol_break = (vol > 500000 and pct >= 1.0 and vol > avg_vols.get(s, 0))
            
            # Historical 15m data for Donchian Channel calculation
            hist_15m = st.session_state.kite.historical_data(q['instrument_token'], now_ist-timedelta(days=10), now_ist, "15minute")
            df_15m = pd.DataFrame(hist_15m)
            
            # Donchian Upper Breakout Check
            dc_status, is_dc_breakout = get_donchian_status(df_15m, length=28, offset=6)

            sym_short = s.replace("NSE:", "")
            tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{sym_short}"
            alerted_keys = [f"{a['Symbol']}|{a['Type']}" for a in st.session_state.alerts_history]

            # Trigger Browser Alert only during live market hours
            alert_type = ""
            if market_active:
                if is_vol_break and f"{sym_short}|Volume" not in alerted_keys:
                    alert_type = "Volume Breakout"
                elif is_dc_breakout and f"{sym_short}|Donchian Upper" not in alerted_keys:
                    alert_type = "Donchian Upper 15m"
                
                if alert_type:
                    trigger_alert(sym_short, alert_type, ltp)
                    st.session_state.alerts_history.append({
                        "Symbol": sym_short, 
                        "Type": alert_type, 
                        "Time": now_ist.strftime("%H:%M:%S"), 
                        "LTP": ltp, 
                        "Chart": tv_url
                    })

            results.append({
                "Symbol": sym_short, 
                "LTP": ltp, 
                "Change %": pct, 
                "Vol Status": "🚀 BREAKOUT" if is_vol_break else "Normal", 
                "Donchian 15m (28,6)": dc_status, 
                "Chart": tv_url
            })
        except: continue

    # --- 8. DASHBOARD DISPLAY ---
    t_main, t_vol, t_dc, t_log = st.tabs(["📊 Market", "🔥 Volume", "🎯 Donchian 15m", "📝 History"])
    col_config = {
        "LTP": st.column_config.NumberColumn("LTP", format="%.2f"),
        "Change %": st.column_config.NumberColumn("Change %", format="%.2f%%"),
        "Chart": st.column_config.LinkColumn("Chart", display_text="Open TV 📈")
    }

    if results:
        df_res = pd.DataFrame(results).sort_values(by="Change %", ascending=False)
        with t_main: st.dataframe(df_res, use_container_width=True, hide_index=True, column_config=col_config)
        with t_vol: st.dataframe(df_res[df_res['Vol Status'] == "🚀 BREAKOUT"], use_container_width=True, hide_index=True, column_config=col_config)
        with t_dc: st.dataframe(df_res[df_res['Donchian 15m (28,6)'].str.contains("🚀", na=False)], use_container_width=True, hide_index=True, column_config=col_config)
    
    with t_log: 
        if st.session_state.alerts_history:
            st.dataframe(pd.DataFrame(st.session_state.alerts_history).iloc[::-1], use_container_width=True, hide_index=True, column_config=col_config)

    # Auto-refresh loop only executes during live market hours (9:07 AM to 3:30 PM IST)
    if market_active:
        time.sleep(60)
        st.rerun()
