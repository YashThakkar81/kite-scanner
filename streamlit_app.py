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

# --- 3. LOGIN INTERFACE (SIDEBAR) ---
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

# --- 4. DATA FETCHING FUNCTION ---
@st.cache_data(ttl=600)
def get_all_symbols():
    try:
        s1 = conn.read(worksheet="Scanner_Output 1").iloc[:, 0].tolist()
        s2 = conn.read(worksheet="Scanner_Output 2").iloc[:, 0].tolist()
        s3 = conn.read(worksheet="Scanner_Output 3").iloc[:, 0].tolist()
        combined = list(set([str(s).strip() for s in (s1 + s2 + s3) if s and str(s) != 'nan']))
        return ["NSE:" + s for s in combined]
    except:
        return []

# --- 5. MASTER SCANNER ENGINE ---
if 'access_token' in st.session_state:
    try:
        symbols = get_all_symbols()
        if not symbols:
            st.warning("No symbols found in Google Sheets.")
            st.stop()
        
        # Fetch Live Data
        all_quotes = {}
        for i in range(0, len(symbols), 450):
            chunk = symbols[i:i+450]
            all_quotes.update(st.session_state.kite.quote(chunk))
        
        results = []
        alerts = []
        
        to_date = datetime.now().date()
        from_date = to_date - timedelta(days=35)

        for s, d in all_quotes.items():
            ltp = d['last_price']
            close = d['ohlc']['close']
            curr_vol = d['volume']
            pct_change = round(((ltp - close) / close) * 100, 2)
            
            # --- CHARTINK LOGIC ---
            is_breakout = False
            if curr_vol > 500000 and pct_change > 1.0:
                try:
                    hist = st.session_state.kite.historical_data(d['instrument_token'], from_date, to_date - timedelta(days=1), "day")
                    if len(hist) >= 22:
                        max_22_vol = max([day['volume'] for day in hist[-22:]])
                        if curr_vol > max_22_vol:
                            is_breakout = True
                except:
                    is_breakout = False

            row = {
                "Symbol": s.replace("NSE:", ""),
                "LTP": ltp,
                "Change %": pct_change,
                "Volume": curr_vol,
                "Trend": "🚀 STR" if is_breakout else ("🟩" if pct_change > 0 else "🟥")
            }
            results.append(row)
            if is_breakout:
                alerts.append(row)

        # CREATE THE DATAFRAME FIRST
        full_df = pd.DataFrame(results)
        
        # --- 6. FILTER LOGIC (Now correctly placed AFTER full_df is created) ---
        st.sidebar.divider()
        st.sidebar.header("🔍 Filter Symbols")
        available_symbols = sorted(full_df['Symbol'].unique().tolist())
        selected_symbols = st.sidebar.multiselect("Select Specific Stocks:", available_symbols)

        # Apply Filter to display_df
        if selected_symbols:
            display_df = full_df[full_df['Symbol'].isin(selected_symbols)]
        else:
            display_df = full_df

        # --- 7. UI LAYOUT ---
        t1, t2, t3, t_alert = st.tabs(["Scanner 1", "Scanner 2", "Scanner 3", "🔥 Alert Log"])
        
        with t1: st.dataframe(display_df.iloc[:250], use_container_width=True)
        with t2: st.dataframe(display_df.iloc[250:500], use_container_width=True)
        with t3: st.dataframe(display_df.iloc[500:], use_container_width=True)
        
        with t_alert:
            st.subheader("Chartink Master Breakouts")
            if alerts:
                st.table(pd.DataFrame(alerts))
            else:
                st.info("No stocks currently crossing Max(22) Volume + 1% Price Change.")

        # Auto-refresh
        time.sleep(60)
        st.rerun()

    except Exception as e:
        st.error(f"Scanner Sync Error: {e}")
else:
    st.info("👋 Please complete the Login in the sidebar to load the 700-Symbol Scanners.")
