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

# --- 4. DATA FETCHING (Multi-Tab) ---
@st.cache_data(ttl=600)
def get_all_symbols():
    # Fetching from all 3 scanner tabs
    s1 = conn.read(worksheet="Scanner_Output 1").iloc[:, 0].tolist()
    s2 = conn.read(worksheet="Scanner_Output 2").iloc[:, 0].tolist()
    s3 = conn.read(worksheet="Scanner_Output 3").iloc[:, 0].tolist()
    combined = list(set([str(s).strip() for s in (s1 + s2 + s3) if s and str(s) != 'nan']))
    return ["NSE:" + s for s in combined]

# --- 5. MASTER SCANNER ENGINE ---
if 'access_token' in st.session_state:
    try:
        symbols = get_all_symbols()
        
        # Fetch Live Data in Chunks
        all_quotes = {}
        for i in range(0, len(symbols), 450):
            chunk = symbols[i:i+450]
            all_quotes.update(st.session_state.kite.quote(chunk))
        
        results = []
        alerts = []
        
        # Date range for 22-day Volume Check
        to_date = datetime.now().date()
        from_date = to_date - timedelta(days=35) # Over-fetching to ensure 22 trading days

        for s, d in all_quotes.items():
            instrument_token = d['instrument_token']
            ltp = d['last_price']
            close = d['ohlc']['close']
            curr_vol = d['volume']
            pct_change = round(((ltp - close) / close) * 100, 2)
            
            # CHARTINK LOGIC:
            # 1. Daily Volume > 500,000
            # 2. Daily % Change > 1%
            # 3. Daily Volume > Max(22, Daily Volume) 1 day ago
            
            is_breakout = False
            if curr_vol > 500000 and pct_change > 1.0:
                try:
                    # Fetching previous 22 days to find Max Volume
                    hist = st.session_state.kite.historical_data(instrument_token, from_date, to_date - timedelta(days=1), "day")
                    if len(hist) >= 22:
                        last_22_vols = [day['volume'] for day in hist[-22:]]
                        max_22_vol = max(last_22_vols)
                        if curr_vol > max_22_vol:
                            is_breakout = True
                except:
                    # Fallback if historical API is busy
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

        full_df = pd.DataFrame(results)
        
        # --- UI LAYOUT ---
        t1, t2, t3, t_alert = st.tabs(["Scanner 1", "Scanner 2", "Scanner 3", "🔥 Alert Log"])
        
        with t1: st.dataframe(full_df.iloc[:250], use_container_width=True)
        with t2: st.dataframe(full_df.iloc[250:500], use_container_width=True)
        with t3: st.dataframe(full_df.iloc[500:], use_container_width=True)
        
        with t_alert:
            st.subheader("Chartink Master Breakouts")
            if alerts:
                st.table(pd.DataFrame(alerts))
            else:
                st.info("No stocks currently crossing Max(22) Volume + 1% Price Change.")

        time.sleep(60)
        st.rerun()

    except Exception as e:
        st.error(f"Scanner Sync Error: {e}")
