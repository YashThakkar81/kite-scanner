import streamlit as st
import pandas as pd
from kiteconnect import KiteConnect
from streamlit_gsheets import GSheetsConnection
import time
from datetime import datetime

# --- 1. CONFIGURATION & SECRETS ---
st.set_page_config(page_title="Kite Live Breakout Scanner", layout="wide")

try:
    API_KEY = st.secrets["API_KEY"]
    API_SECRET = st.secrets["API_SECRET"]
    # Initialize Google Sheets Connection
    conn = st.connection("gsheets", type=GSheetsConnection)
except Exception as e:
    st.error(f"❌ Setup Error: Please check your Streamlit Secrets. {e}")
    st.stop()

# --- 2. AUTHENTICATION LOGIC ---
if 'kite' not in st.session_state:
    st.session_state.kite = KiteConnect(api_key=API_KEY)

def login_flow():
    st.sidebar.header("🔑 Zerodha Authentication")
    try:
        login_url = st.session_state.kite.login_url()
        st.sidebar.link_button("1. Get Login URL", login_url)
    except Exception as e:
        st.sidebar.error(f"Kite Error: {e}")

    request_token = st.sidebar.text_input("2. Paste Request Token here")
    
    if st.sidebar.button("Activate Session"):
        try:
            token = request_token.split("request_token=")[1].split("&")[0] if "request_token=" in request_token else request_token
            data = st.session_state.kite.generate_session(token, api_secret=API_SECRET)
            st.session_state.access_token = data["access_token"]
            st.session_state.kite.set_access_token(data["access_token"])
            st.sidebar.success(f"✅ Logged in: {data.get('user_name')}")
            st.rerun()
        except Exception as e:
            st.sidebar.error(f"Login Failed: {e}")

# --- 3. GOOGLE SHEETS LOGGING ---
def log_to_gsheets(breakout_df):
    """Appends breakout stocks to the Google Sheet backup."""
    try:
        if not breakout_df.empty:
            # Read current sheet data
            existing_data = conn.read()
            
            # Combine with new breakouts
            updated_df = pd.concat([existing_data, breakout_df], ignore_index=True)
            
            # Push back to Google Sheets
            conn.update(data=updated_df)
            st.toast(f"🚀 {len(breakout_df)} stocks logged to Google Sheets!")
    except Exception as e:
        st.warning(f"Could not log to Sheets: {e}")

# --- 4. SCANNER ENGINE ---
def run_scanner(symbols):
    try:
        quotes = st.session_state.kite.quote(symbols)
        results = []
        
        for symbol, data in quotes.items():
            ltp = data['last_price']
            prev_close = data['ohlc']['close']
            volume = data['volume']
            change_pct = round(((ltp - prev_close) / prev_close) * 100, 2) if prev_close != 0 else 0
            
            results.append({
                "Time": datetime.now().strftime("%H:%M:%S"),
                "Symbol": symbol.replace("NSE:", ""),
                "LTP": ltp,
                "Change %": change_pct,
                "Volume": volume,
                "Status": "🚀 BREAKOUT" if change_pct > 2.0 else "Normal"
            })
        
        df = pd.DataFrame(results)
        
        # Trigger Logging if breakouts exist
        breakouts = df[df['Status'] == "🚀 BREAKOUT"].copy()
        if not breakouts.empty:
            log_to_gsheets(breakouts[["Time", "Symbol", "LTP", "Change %"]])
            
        return df
    except Exception as e:
        st.error(f"Scanner Error: {e}")
        return None

# --- 5. MAIN INTERFACE ---
st.title("📊 Kite Live Market Scanner")
login_flow()

if 'access_token' in st.session_state:
    watchlist = ["NSE:RELIANCE", "NSE:TCS", "NSE:INFY", "NSE:HDFCBANK", "NSE:ICICIBANK"]
    
    st.subheader("Live Watchlist (Refreshing every 60s)")
    placeholder = st.empty()
    
    while True:
        df = run_scanner(watchlist)
        if df is not None:
            with placeholder.container():
                # Display Top Gainer Metric
                top = df.sort_values(by="Change %", ascending=False).iloc[0]
                st.metric(label=f"Top Mover: {top['Symbol']}", value=f"₹{top['LTP']}", delta=f"{top['Change %']}%")
                
                # Main Table
                st.dataframe(df, use_container_width=True, hide_index=True)
                st.caption(f"Last successful scan: {datetime.now().strftime('%H:%M:%S')}")
        
        time.sleep(60)
else:
    st.info("👈 Please authenticate via the sidebar to begin scanning.")
