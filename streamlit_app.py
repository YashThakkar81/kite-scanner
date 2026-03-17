import streamlit as st
import pandas as pd
from kiteconnect import KiteConnect
import time

# --- 1. SETTINGS ---
API_KEY = "your_api_key"
API_SECRET = "your_api_secret"

st.set_page_config(page_title="Kite Market Scanner", layout="wide")

# --- 2. AUTHENTICATION LOGIC ---
if 'kite' not in st.session_state:
    st.session_state.kite = KiteConnect(api_key=API_KEY)

def login_flow():
    st.sidebar.header("🔑 Authentication")
    login_url = st.session_state.kite.login_url()
    st.sidebar.link_button("1. Get Login URL", login_url)
    
    request_token = st.sidebar.text_input("2. Paste Request Token/URL here")
    if st.sidebar.button("Activate Session"):
        try:
            # Extract token if user pastes the full redirect URL
            token = request_token.split("request_token=")[1].split("&")[0] if "request_token=" in request_token else request_token
            data = st.session_state.kite.generate_session(token, api_secret=API_SECRET)
            st.session_state.access_token = data["access_token"]
            st.session_state.kite.set_access_token(data["access_token"])
            st.success("✅ Authenticated Successfully!")
        except Exception as e:
            st.error(f"Login Failed: {e}")

# --- 3. THE SCANNER ENGINE ---
def run_scanner(symbols):
    try:
        # Batching: Kite allows up to 500 symbols per quote call
        # Using .quote() gives us OHLC + Volume + LTP
        quotes = st.session_state.kite.quote(symbols)
        
        results = []
        for symbol, data in quotes.items():
            ltp = data['last_price']
            prev_close = data['ohlc']['close']
            volume = data['volume']
            change_pct = ((ltp - prev_close) / prev_close) * 100 if prev_close != 0 else 0
            
            results.append({
                "Symbol": symbol.replace("NSE:", ""),
                "LTP": ltp,
                "Change %": round(change_pct, 2),
                "Volume": volume,
                "Status": "🚀 BREAKOUT" if change_pct > 2.0 else "Normal"
            })
        return pd.DataFrame(results)
    except Exception as e:
        st.error(f"Scanner Error: {e}")
        return None

# --- 4. DASHBOARD UI ---
st.title("📊 Kite Live Market Scanner")

login_flow()

if 'access_token' in st.session_state:
    # Use a small sample list or upload your symbol list here
    sample_symbols = ["NSE:RELIANCE", "NSE:TCS", "NSE:INFY", "NSE:HDFCBANK", "NSE:ICICIBANK"]
    
    st.write("### Live Watchlist")
    placeholder = st.empty() # Used for live updates
    
    while True:
        df = run_scanner(sample_symbols)
        if df is not None:
            with placeholder.container():
                # Display metrics for top movers
                top_mover = df.sort_values(by="Change %", ascending=False).iloc[0]
                st.metric(label=f"Top Mover: {top_mover['Symbol']}", value=f"₹{top_mover['LTP']}", delta=f"{top_mover['Change %']}%")
                
                # Display the main table
                st.dataframe(df, use_container_width=True, hide_index=True)
                st.write(f"Last updated: {time.strftime('%H:%M:%S')}")
        
        time.sleep(60) # Refresh every minute
else:
    st.info("Please login using the sidebar to start the scanner.")