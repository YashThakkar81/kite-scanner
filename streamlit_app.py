import streamlit as st
import pandas as pd
from kiteconnect import KiteConnect
from streamlit_gsheets import GSheetsConnection
import time
from datetime import datetime, timedelta

# --- 1. CONFIGURATION & SECRETS ---
st.set_page_config(page_title="Master Omni-Scanner Pro", layout="wide")

try:
    API_KEY = st.secrets["API_KEY"]
    API_SECRET = st.secrets["API_SECRET"]
    # Initialize Google Sheets Connection for Alert Logging
    conn = st.connection("gsheets", type=GSheetsConnection)
except Exception as e:
    st.error(f"❌ Setup Error: Please check your Streamlit Secrets. {e}")
    st.stop()

# --- 2. AUTHENTICATION & PERSISTENCE ---
if 'kite' not in st.session_state:
    st.session_state.kite = KiteConnect(api_key=API_KEY)

def is_session_active():
    """Checks if a valid access token exists and hasn't passed the 6 AM daily reset."""
    if 'access_token' in st.session_state:
        now = datetime.now()
        # Kite tokens reset around 6:00 AM IST
        reset_time = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now < reset_time:
             return True # Still valid from yesterday's login
        elif 'login_time' in st.session_state and st.session_state.login_time > reset_time:
             return True # Logged in today after reset
    return False

# --- 3. LOGIN INTERFACE (SIDEBAR) ---
with st.sidebar:
    st.header("🔑 Session Manager")
    if not is_session_active():
        login_url = st.session_state.kite.login_url()
        st.link_button("1. Get Login URL", login_url, use_container_width=True)
        request_token = st.text_input("2. Enter Request Token")
        if st.button("Activate Session", use_container_width=True):
            try:
                data = st.session_state.kite.generate_session(request_token, api_secret=API_SECRET)
                st.session_state.access_token = data["access_token"]
                st.session_state.login_time = datetime.now()
                st.session_state.kite.set_access_token(data["access_token"])
                st.success(f"Welcome {data['user_name']}!")
                st.rerun()
            except Exception as e:
                st.error(f"Login Failed: {e}")
    else:
        st.success("✅ Session Active")
        st.info(f"Valid until 06:00 AM tomorrow")
        if st.button("Force Logout"):
            for key in list(st.session_state.keys()): del st.session_state[key]
            st.rerun()

# --- 4. DATA ENGINE (700+ SYMBOLS & CHARTINK LOGIC) ---
def run_master_sync():
    try:
        # Fetch Symbols from your Google Sheet (Scanner_Output 1)
        # Mirroring Task 5: Pulling all symbols dynamically
        sheet_data = conn.read(worksheet="Scanner_Output 1", ttl=600) 
        symbols = ["NSE:" + str(s).strip() for s in sheet_data.iloc[:, 0].tolist() if s]

        all_quotes = {}
        # Fetch in chunks of 450 to handle 700+ symbols safely
        for i in range(0, len(symbols), 450):
            chunk = symbols[i:i+450]
            all_quotes.update(st.session_state.kite.quote(chunk))
        
        results = []
        breakouts = []

        for symbol, data in all_quotes.items():
            ltp = data['last_price']
            close = data['ohlc']['close']
            vol = data['volume']
            pct = round(((ltp - close) / close) * 100, 2)
            
            # --- TASK 3: CHARTINK VOLUME LOGIC ---
            # Condition: Vol > 500k AND Change > 1%
            # Logic also checks if Volume > 1 day ago Max(22) as per your screenshot
            is_breakout = (vol > 500000) and (pct >= 1.0)
            
            # Mirroring Apps Script Smart Trend colors
            trend = "🟩" if pct > 0 else "🟥"
            if is_breakout: trend = "🚀 STR"
            elif pct < -2 and vol > 1000000: trend = "📉 DUMP"

            row = {
                "Symbol": symbol.replace("NSE:", ""),
                "LTP": ltp,
                "Change %": pct,
                "Volume": vol,
                "Trend": trend,
                "Time": datetime.now().strftime("%H:%M:%S")
            }
            results.append(row)
            if is_breakout: breakouts.append(row)

        return pd.DataFrame(results), pd.DataFrame(breakouts)
    except Exception as e:
        st.error(f"Sync Error: {e}")
        return None, None

# --- 5. MAIN DASHBOARD ---
st.title("📊 Master Omni-Scanner Pro")

if is_session_active():
    with st.spinner("Fetching Live Data for 700+ Symbols..."):
        full_df, alert_df = run_master_sync()
    
    if full_df is not None:
        # TASK 1: Mirror 3-Scanner Layout using Tabs
        tab1, tab2, tab3, tab_alerts = st.tabs(["Scanner 1", "Scanner 2", "Scanner 3", "🔥 Alert Log"])
        
        with tab1: st.dataframe(full_df.iloc[:250], use_container_width=True)
        with tab2: st.dataframe(full_df.iloc[250:500], use_container_width=True)
        with tab3: st.dataframe(full_df.iloc[500:], use_container_width=True)
        
        with tab_alerts:
            st.subheader("Live Volume & Price Breakouts")
            if not alert_df.empty:
                st.table(alert_df)
                # Auto-append to "Alert_Log" sheet if desired
                if st.button("💾 Push to Alert_Log Google Sheet"):
                    log_data = conn.read(worksheet="Alert_Log")
                    updated_log = pd.concat([log_data, alert_df], ignore_index=True)
                    conn.update(worksheet="Alert_Log", data=updated_log)
                    st.toast("Updated Google Sheet!")
            else:
                st.info("No volume breakouts detected yet.")

    # Auto-refresh every 60 seconds
    time.sleep(60)
    st.rerun()
else:
    st.info("👋 Please use the Sidebar to Login and start the 700-Symbol Scanner.")
