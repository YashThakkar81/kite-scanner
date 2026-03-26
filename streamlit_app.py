import streamlit as st
import pandas as pd
from kiteconnect import KiteConnect
from streamlit_gsheets import GSheetsConnection
import streamlit.components.v1 as components
import time
import os
from datetime import datetime, timedelta

# --- 1. CONFIGURATION ---
st.set_page_config(page_title="Master Omni-Scanner Pro", layout="wide")

try:
    API_KEY = st.secrets["API_KEY"]
    API_SECRET = st.secrets["API_SECRET"]
    conn = st.connection("gsheets", type=GSheetsConnection)
except Exception as e:
    st.error(f"Setup Error: {e}")
    st.stop()

# --- 2. NOTIFICATION ENGINE ---
def trigger_alert(symbol, alert_type, ltp):
    notification_js = f"""
    <script>
    if (Notification.permission === "granted") {{
        new Notification("{alert_type} ALERT: {symbol}", {{ body: "Price: {ltp}" }});
        new Audio('https://media.geeksforgeeks.org/wp-content/uploads/20190531135120/beep.mp3').play();
    }}
    </script>
    """
    components.html(notification_js, height=0)
    st.toast(f"{alert_type}: {symbol}", icon="📈")

# --- 3. SESSION STATE ---
if 'kite' not in st.session_state:
    st.session_state.kite = KiteConnect(api_key=API_KEY)
if 'alerts_history' not in st.session_state:
    st.session_state.alerts_history = [] 

TOKEN_FILE = "access_token.txt"
if 'access_token' not in st.session_state and os.path.exists(TOKEN_FILE):
    with open(TOKEN_FILE, "r") as f:
        st.session_state.access_token = f.read().strip()
        st.session_state.kite.set_access_token(st.session_state.access_token)

# --- 4. UTILS ---
def calculate_ema(prices, period):
    return pd.Series(prices).ewm(span=period, adjust=False).mean().iloc[-1]

@st.cache_data(ttl="1d")
def get_daily_max_vol(_kite, symbols):
    max_vol_map = {}
    to_date = datetime.now().date()
    from_date = to_date - timedelta(days=35)
    quotes = _kite.quote(symbols)
    for s, d in quotes.items():
        try:
            hist = _kite.historical_data(d['instrument_token'], from_date, to_date - timedelta(days=1), "day")
            max_vol_map[s] = max([day['volume'] for day in hist[-22:]]) if len(hist) >= 22 else 999999999
            time.sleep(0.1) 
        except: max_vol_map[s] = 999999999
    return max_vol_map

# --- 5. SIDEBAR ---
with st.sidebar:
    st.header("🔑 Session")
    if 'access_token' not in st.session_state:
        st.link_button("Login to Kite", st.session_state.kite.login_url())
        token_in = st.text_input("Enter Token")
        if st.button("Activate"):
            data = st.session_state.kite.generate_session(token_in.split("token=")[-1], api_secret=API_SECRET)
            st.session_state.access_token = data["access_token"]
            with open(TOKEN_FILE, "w") as f: f.write(data["access_token"])
            st.rerun()
    else:
        st.success("Kite Connected")
        if st.button("Logout"):
            if os.path.exists(TOKEN_FILE): os.remove(TOKEN_FILE)
            st.session_state.clear(); st.rerun()
    
    if st.button("🗑️ Clear Alert History"):
        st.session_state.alerts_history = []
        st.rerun()

# --- 6. DATA PROCESSING ---
if 'access_token' in st.session_state:
    sheets = ["Scanner_Output 1", "Scanner_Output 2", "Scanner_Output 3"]
    all_syms = []
    for ws in sheets:
        try: all_syms.extend(conn.read(worksheet=ws).iloc[:, 0].dropna().astype(str).tolist())
        except: continue
    
    # CRITICAL: Limit to 100 for stability. 600+ is too many for real-time 15m EMA.
    symbols = ["NSE:" + s.strip() for s in set(all_syms) if s != 'nan'][:100]
    
    max_vols = get_daily_max_vol(st.session_state.kite, symbols)
    results = []
    now = datetime.now()
    
    progress = st.empty()
    progress.info(f"Scanning {len(symbols)} Stocks...")

    for s in symbols:
        try:
            q = st.session_state.kite.quote(s)[s]
            ltp, vol, cl = q['last_price'], q['volume'], q['ohlc']['close']
            pct = round(((ltp - cl) / cl) * 100, 2)
            
            hist_15m = st.session_state.kite.historical_data(q['instrument_token'], now-timedelta(days=2), now, "15minute")
            df_15m = pd.DataFrame(hist_15m)
            
            if len(df_15m) >= 40:
                ema20 = calculate_ema(df_15m['close'], 20)
                ema40 = calculate_ema(df_15m['close'], 40)
                
                is_ema_cross = ema20 > ema40
                is_vol_break = (vol > 500000 and pct > 1.0 and vol > max_vols.get(s, 0))
                
                sym_short = s.replace("NSE:", "")
                tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{sym_short}"
                
                alerted_keys = [f"{a['Symbol']}|{a['Type']}" for a in st.session_state.alerts_history]

                if is_vol_break and f"{sym_short}|Volume" not in alerted_keys:
                    trigger_alert(sym_short, "Volume", ltp)
                    st.session_state.alerts_history.append({"Symbol": sym_short, "Type": "Volume", "Time": now.strftime("%H:%M:%S"), "LTP": ltp, "Chart": tv_url})
                
                if is_ema_cross and f"{sym_short}|EMA 20/40" not in alerted_keys:
                    trigger_alert(sym_short, "EMA 20/40", ltp)
                    st.session_state.alerts_history.append({"Symbol": sym_short, "Type": "EMA 20/40", "Time": now.strftime("%H:%M:%S"), "LTP": ltp, "Chart": tv_url})

                results.append({
                    "Symbol": sym_short, "LTP": ltp, "Change %": pct, 
                    "Vol Status": "🚀 BREAKOUT" if is_vol_break else "Normal",
                    "EMA Status": "⚡ CROSS" if is_ema_cross else "Below",
                    "Chart": tv_url
                })
            time.sleep(0.05) 
        except: continue

    progress.empty()
    
    # --- 7. SAFETY RENDER ---
    t_main, t_vol, t_ema, t_log = st.tabs(["📊 Market", "🔥 Volume", "⚡ EMA 15m", "📝 History"])
    col_config = {"Chart": st.column_config.LinkColumn("View Chart", display_text="Open TV 📈")}

    if results:
        df_res = pd.DataFrame(results)
        with t_main: st.dataframe(df_res, use_container_width=True, hide_index=True, column_config=col_config)
        with t_vol: st.dataframe(df_res[df_res['Vol Status'] == "🚀 BREAKOUT"], use_container_width=True, hide_index=True, column_config=col_config)
        with t_ema: st.dataframe(df_res[df_res['EMA Status'] == "⚡ CROSS"], use_container_width=True, hide_index=True, column_config=col_config)
    else:
        st.warning("No data returned from scan. Check your symbol list or Kite connection.")

    with t_log: 
        if st.session_state.alerts_history:
            st.dataframe(pd.DataFrame(st.session_state.alerts_history).iloc[::-1], use_container_width=True, hide_index=True, column_config=col_config)
        else: st.info("Waiting for signals...")

    time.sleep(60)
    st.rerun()
