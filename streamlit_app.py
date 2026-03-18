import streamlit as st
import pandas as pd
from kiteconnect import KiteConnect
from streamlit_gsheets import GSheetsConnection
import time
import os
from datetime import datetime

# --- 1. CONFIGURATION ---
st.set_page_config(page_title="Master Omni-Scanner Pro", layout="wide")

# File to store session so it survives refreshes
TOKEN_FILE = "access_token.txt"

try:
    API_KEY = st.secrets["API_KEY"]
    API_SECRET = st.secrets["API_SECRET"]
    conn = st.connection("gsheets", type=GSheetsConnection)
except Exception as e:
    st.error(f"Setup Error: {e}")
    st.stop()

# --- 2. SESSION PERSISTENCE ENGINE ---
if 'kite' not in st.session_state:
    st.session_state.kite = KiteConnect(api_key=API_KEY)

# Try to reload token from file if session state is empty
if 'access_token' not in st.session_state and os.path.exists(TOKEN_FILE):
    with open(TOKEN_FILE, "r") as f:
        saved_token = f.read().strip()
        st.session_state.access_token = saved_token
        st.session_state.kite.set_access_token(saved_token)

# --- 3. LOGIN INTERFACE ---
with st.sidebar:
    st.header("🔑 Session Manager")
    
    # Show status
    if 'access_token' in st.session_state:
        st.success("✅ Session Active")
        if st.button("Logout / Clear Cache"):
            if os.path.exists(TOKEN_FILE): os.remove(TOKEN_FILE)
            st.session_state.clear()
            st.rerun()
    else:
        login_url = st.session_state.kite.login_url()
        st.link_button("1. Get Login URL", login_url, use_container_width=True)
        
        # IMPORTANT: Do not paste the whole URL, just the token after 'request_token='
        token_input = st.text_input("2. Enter New Request Token")
        
        if st.button("Activate Session", use_container_width=True):
            try:
                # Clean the input in case user pasted the whole URL
                clean_token = token_input.split("request_token=")[-1].split("&")[0]
                data = st.session_state.kite.generate_session(clean_token, api_secret=API_SECRET)
                
                # Save to both Session and File
                st.session_state.access_token = data["access_token"]
                with open(TOKEN_FILE, "w") as f:
                    f.write(data["access_token"])
                
                st.session_state.kite.set_access_token(data["access_token"])
                st.success("Login Successful!")
                st.rerun()
            except Exception as e:
                st.error(f"Login Failed: {e}. Get a NEW token.")

# --- 4. SCANNER LOGIC (ONLY RUNS IF LOGGED IN) ---
if 'access_token' in st.session_state:
    try:
        # Load Symbols from your Sheet
        sheet_data = conn.read(worksheet="Scanner_Output 1", ttl=600)
        symbols = ["NSE:" + str(s).strip() for s in sheet_data.iloc[:, 0].tolist() if s]

        # Fetch in Chunks (Task 5: 700+ Symbols)
        all_quotes = {}
        for i in range(0, len(symbols), 450):
            chunk = symbols[i:i+450]
            all_quotes.update(st.session_state.kite.quote(chunk))
        
        # Process Logic (Task 3: Chartink Volume)
        results = []
        for s, d in all_quotes.items():
            change = round(((d['last_price'] - d['ohlc']['close']) / d['ohlc']['close']) * 100, 2)
            results.append({
                "Symbol": s.replace("NSE:", ""),
                "LTP": d['last_price'],
                "Volume": d['volume'],
                "Change %": change,
                "Trend": "🚀 STR" if d['volume'] > 500000 and change >= 1.0 else "Normal"
            })
        
        df = pd.DataFrame(results)

        # --- THE TABS YOU WERE LOOKING FOR ---
        t1, t2, t3 = st.tabs(["Scanner 1 (1-250)", "Scanner 2 (251-500)", "Scanner 3 (501+)"])
        with t1: st.dataframe(df.iloc[:250], use_container_width=True)
        with t2: st.dataframe(df.iloc[250:500], use_container_width=True)
        with t3: st.dataframe(df.iloc[500:], use_container_width=True)

        time.sleep(60)
        st.rerun()

    except Exception as e:
        st.error(f"Scanner Error: {e}")
else:
    st.info("👋 Please complete the Login in the sidebar to load the 700-Symbol Scanners.")
