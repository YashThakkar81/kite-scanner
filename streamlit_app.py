import streamlit as st
from streamlit_gsheets import GSheetsConnection
import pandas as pd
from datetime import datetime

# 1. Establish the Connection
conn = st.connection("gsheets", type=GSheetsConnection)

# 2. Function to log breakouts
def log_to_gsheet(symbol, ltp, change):
    try:
        # Read existing data to find the next empty row
        existing_data = conn.read()
        
        # Create the new row
        new_row = pd.DataFrame([{
            "Time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Symbol": symbol,
            "LTP": ltp,
            "Change %": change
        }])
        
        # Combine and update
        updated_df = pd.concat([existing_data, new_row], ignore_index=True)
        conn.update(data=updated_df)
        st.toast(f"✅ Logged {symbol} to Google Sheets!")
    except Exception as e:
        st.error(f"Error logging to Sheets: {e}")

# --- YOUR EXISTING SCANNER LOGIC STARTS HERE ---
# (Inside your price loop, add this check:)

# Example: If change is greater than 2%
if change_percent > 2.0:
    st.write(f"🚀 Breakout detected in {symbol}!")
    log_to_gsheet(symbol, last_price, change_percent)
