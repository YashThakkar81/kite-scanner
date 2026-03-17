import streamlit as st
import pandas as pd
from kiteconnect import KiteConnect
import time

# --- 1. SETTINGS & SECURE AUTH ---
st.set_page_config(page_title="Kite Market Scanner", layout="wide")

# Fetch keys securely from Streamlit Cloud Secrets
try:
    API_KEY = st.secrets["API_KEY"]
    API_SECRET = st.secrets["API_SECRET"]
except Exception:
    st.error("❌ API Keys not found! Please add API_KEY and API_SECRET to your Streamlit 'Secrets' settings.")
    st.stop()

# --- 2. AUTHENTICATION LOGIC ---
if 'kite' not in st.session_state:
    st.session_state.kite = KiteConnect(api_key=API_KEY)

def login_flow():
    st.sidebar.header("🔑 Authentication")
    
    # Generate the Login URL using your API Key
    try:
        login_url = st.session_state.kite.login_url()
        st.sidebar.link_button("1. Get Login URL", login_url)
    except Exception as e:
        st.sidebar.error(f"Error generating URL: {e}")

    request_token = st.sidebar.text_input("2. Paste Request Token/URL here")
    
    if st.sidebar.button("Activate Session"):
        try:
            # Extract token if user pastes the full redirect URL or just the code
            token = request_token.split("request_token=")[1].split("&")[0] if "request_token=" in request_token else request_token
            
            # Generate the session using the Secret
            data = st.session_state.kite.generate_session(token, api_secret=API_SECRET)
            st.session_state.access_token = data["access_token"]
            st.session_state.kite.set_access_token(data["access_token"])
            st.sidebar.success(f"✅ Welcome, {data.get('user_name', 'User')}!")
            st.rerun() # Refresh app to show the scanner
        except Exception as e:
            st.sidebar.error(f"Login Failed: {e}")

# --- 3. THE SCANNER ENGINE ---
def run_scanner(symbols):
    try:
        # Fetch live quotes
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

# Show the login sidebar
login_flow()

if 'access_token' in st.session_state:
    # Watchlist
    sample_symbols = ["NSE:RELIANCE", "NSE:TCS", "NSE:INFY", "NSE:HDFCBANK", "NSE:ICICIBANK"]
    
    st.write("### Live Watchlist")
    placeholder = st.empty() # Container for live updates
    
    # Simple loop for live updates
    while True:
        df = run_scanner(sample_symbols)
        if df is not None:
            with placeholder.container():
                # Show the top gainer as a metric
                top_mover = df.sort_values(by="Change %", ascending=False).iloc[0]
                st.metric(label=f"Top Mover: {top_mover['Symbol']}", 
                          value=f"₹{top_mover['LTP']}", 
                          delta=f"{top_mover['Change %']}%")
                
                # Display the data table
                st.dataframe(df, use_container_width=True, hide_index=True)
                st.caption(f"Last updated: {time.strftime('%H:%M:%S')}")
        
        time.sleep(60) # Refresh data every 60 seconds
else:
    st.warning("👈 Please complete the login steps in the sidebar to start the live scan.")
    st.info("Note: Ensure your Redirect URL in Zerodha matches this app's URL.")
