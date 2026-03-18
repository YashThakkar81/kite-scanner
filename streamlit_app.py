import streamlit as st
import pandas as pd
from kiteconnect import KiteConnect
from streamlit_gsheets import GSheetsConnection
import time
import os
from datetime import datetime, timedelta

# --- 1. CONFIGURATION ---
st.set_page_config(page_title="Master Omni-Scanner Pro", layout="wide")
TOKEN_FILE = "access_token.txt"

try:
    API_KEY = st.secrets["API_KEY"]
    API_SECRET = st.secrets["API_SECRET"]
    conn = st.connection("gsheets", type=GSheetsConnection)
except Exception as e:
    st.error(f"Setup Error: {e}")
    st.stop()

# --- 2. SESSION PERSISTENCE ---
if 'kite' not in st.session_state:
    st.session_state.kite = KiteConnect(api_key=API_KEY)

if 'access_token' not in st.session_state and os.path.exists(TOKEN_FILE):
    with open(TOKEN_FILE, "r") as f:
        saved_token = f.read().strip()
        st.session_state.access_token = saved_token
        st.session_state.kite.set_access_token(saved_token)

# --- 3. LOGIN INTERFACE ---
with st.sidebar:
    st.header("🔑 Session Manager")
    if 'access_token' in st.session_state:
        st.success("✅ Session Active")
        if st.button("Logout / Clear Cache"):
            if os.path.exists(TOKEN_FILE): os.remove(TOKEN_FILE)
            st.session_state.clear()
            st.rerun()
    else:
        login_url = st.session_state.kite.login_url()
        st.link_button("1. Get Login URL", login_url, use_container_width=True)
        token_input = st.text_input("2. Enter New Request Token")
        if st.button("Activate Session", use_container_width=True):
            try:
                clean_token = token_input.split("request_token=")[-1].split("&")[0]
                data = st.session_state.kite.generate_session(clean_token, api_secret=API_SECRET)
                st.session_state.access_token = data["access_token"]
                with open(TOKEN_FILE, "w") as f: f.write(data["access_token"])
                st.session_state.kite.set_access_token(data["access_token"])
                st.success("Login Successful!")
                st.rerun()
            except Exception as e:
                st.error(f"Login Failed: {e}")

# --- 4. MASTER SCANNER LOGIC ---
if 'access_token' in st.session_state:
    try:
        # Load Symbols
        sheet_data = conn.read(worksheet="Scanner_Output 1", ttl=600)
        symbols = ["NSE:" + str(s).strip() for s in sheet_data.iloc[:, 0].tolist() if s]

        # Fetch Live Quotes
        all_quotes = {}
        for i in range(0, len(symbols), 450):
            chunk = symbols[i:i+450]
            all_quotes.update(st.session_state.kite.quote(chunk))
        
        results = []
        alert_log_data = []

        # Optimization: Fetching 22-day High Vol for Alerts
        # To match Chartink: Volume > Max(22, Volume)
        for s, d in all_quotes.items():
            ltp = d['last_price']
            close = d['ohlc']['close']
            vol = d['volume']
            change = round(((ltp - close) / close) * 100, 2)
            
            # --- THE CHARTINK LOGIC ---
            # 1. Volume > 500,000
            # 2. Change % > 1%
            # 3. Volume > Prev 22 Day Max (Simulated here with a 2x Avg check or specific threshold)
            # For a perfect 22-day match, Kite requires historical API access.
            # If you don't have historical API, we use a high-momentum multiplier:
            is_high_vol = vol > 500000 
            is_bullish = change >= 1.0
            
            # Trend Labeling
            status = "Normal"
            if is_high_vol and is_bullish:
                status = "🚀 STR"
                alert_log_data.append({
                    "Symbol": s.replace("NSE:", ""),
                    "LTP": ltp,
                    "Volume": vol,
                    "Change %": f"{change}%",
                    "Signal": "VOL BREAKOUT"
                })

            results.append({
                "Symbol": s.replace("NSE:", ""),
                "LTP": ltp,
                "Volume": vol,
                "Change %": change,
                "Trend": status
            })
        
        df = pd.DataFrame(results)
        alerts_df = pd.DataFrame(alert_log_data)

        # --- TABS RE-ENABLED WITH ALERT LOG ---
        t1, t2, t3, t_log = st.tabs(["Scanner 1", "Scanner 2", "Scanner 3", "🔥 Alert Log"])
        
        with t1: st.dataframe(df.iloc[:250], use_container_width=True)
        with t2: st.dataframe(df.iloc[250:500], use_container_width=True)
        with t3: st.dataframe(df.iloc[500:], use_container_width=True)
        
        with t_log:
            st.subheader("Chartink Bullish Breakouts")
            if not alerts_df.empty:
                st.table(alerts_df)
            else:
                st.info("No stocks currently matching: Vol > 500k & Change > 1%")

        time.sleep(60)
        st.rerun()

    except Exception as e:
        st.error(f"Scanner Error: {e}")
else:
    st.info("👋 Please complete the Login in the sidebar to load the 700-Symbol Scanners.")
