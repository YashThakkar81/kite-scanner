import streamlit as st
import pandas as pd
from kiteconnect import KiteConnect
from streamlit_gsheets import GSheetsConnection
import streamlit.components.v1 as components
import time
import os
import pytz 
from datetime import datetime, timedelta, time as dtime

# --- 1. CONFIGURATION & BLUE TOGGLE STYLING ---
st.set_page_config(page_title="Master Omni-Scanner Pro", layout="wide")
IST = pytz.timezone('Asia/Kolkata')

st.markdown("""
    <style>
    [data-testid="stDataFrame"] td { text-align: center !important; }
    [data-testid="stHeader"] th { text-align: center !important; }
    [data-testid="stDataFrame"] a { justify-content: center !important; }
    .stDataFrame { margin: 0 auto; }
    
    /* Blue Toggle Switch Styling */
    span[aria-checked="true"] {
        background-color: #1E88E5 !important;
    }
    div[data-testid="stCheckbox"] input:checked + div {
        background-color: #1E88E5 !important;
    }
    div[class*="st-"] [aria-checked="true"] {
        background-color: #1E88E5 !important;
    }
    </style>
    """, unsafe_allow_html=True)

try:
    API_KEY = st.secrets["API_KEY"]
    API_SECRET = st.secrets["API_SECRET"]
    conn = st.connection("gsheets", type=GSheetsConnection)
except Exception as e:
    st.error(f"Setup Error: {e}")
    st.stop()

# --- 2. PC NOTIFICATION ENGINE ---
def trigger_alert(symbol, alert_type, ltp):
    notification_js = f"""
    <script>
    if (Notification.permission === "granted") {{
        const n = new Notification("{alert_type} ALERT: {symbol}", {{ 
            body: "Price: {ltp}",
            icon: "https://kite.zerodha.com/static/images/kite-logo.svg" 
        }});
        new Audio('https://media.geeksforgeeks.org/wp-content/uploads/20190531135120/beep.mp3').play();
        setTimeout(() => n.close(), 5000);
    }}
    </script>
    """
    components.html(notification_js, height=0)
    st.toast(f"{alert_type}: {symbol}", icon="🚀")

# --- 3. SESSION STATE ---
if 'kite' not in st.session_state:
    st.session_state.kite = KiteConnect(api_key=API_KEY)
if 'alerts_history' not in st.session_state:
    st.session_state.alerts_history = [] 

TOKEN_FILE = "access_token.txt"
if 'access_token' not in st.session_state and os.path.exists(TOKEN_FILE):
    try:
        with open(TOKEN_FILE, "r") as f:
            saved_token = f.read().strip()
            st.session_state.kite.set_access_token(saved_token)
            st.session_state.access_token = saved_token
    except: pass

# --- 4. DONCHIAN CHANNEL (15m TF, LENGTH 28, OFFSET 6) ---
def get_donchian_status(df, length=28, offset=6):
    if len(df) < (length + offset):
        return "N/A", False

    upper_channel = df['high'].rolling(window=length).max().shift(offset)
    curr_close = df['close'].iloc[-1]
    curr_upper = upper_channel.iloc[-1]

    if pd.isna(curr_upper):
        return "N/A", False

    is_breakout = curr_close >= curr_upper
    status_str = "🚀 UPPER BREAKOUT" if is_breakout else "Below"
    
    return status_str, is_breakout

@st.cache_data(ttl="1d")
def get_daily_avg_vol(_kite, symbols):
    avg_vol_map = {}
    to_date = datetime.now(IST).date()
    from_date = to_date - timedelta(days=35)
    for s in symbols:
        try:
            q = _kite.quote(s)[s]
            hist = _kite.historical_data(q['instrument_token'], from_date, to_date - timedelta(days=1), "day")
            avg_vol_map[s] = sum([day['volume'] for day in hist[-22:]]) / 22 if len(hist) >= 22 else 999999999
            time.sleep(0.01) 
        except: avg_vol_map[s] = 999999999
    return avg_vol_map

# --- 5. MARKET HOURS UTILITY ---
def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5: # Saturday or Sunday
        return False
    market_start = dtime(9, 7)
    market_end = dtime(15, 30)
    return market_start <= now.time() <= market_end

# --- 6. SIDEBAR (AUTHENTICATION, STATUS & TOGGLES) ---
with st.sidebar:
    st.header("🕒 Scanner Status")
    now_ist = datetime.now(IST)
    st.info(f"Last Updated: {now_ist.strftime('%H:%M:%S')}")
    
    market_active = is_market_open()
    if market_active:
        st.success("Market Status: OPEN (Live Refresh Active) 🟢")
    else:
        st.warning("Market Status: CLOSED (Manual Mode / Backtest) 🔴")

    st.divider()
    st.header("⚙️ Display & Alert Controls")
    show_all_stocks = st.toggle("Show All Stocks (< 1%)", value=False)
    notify_vol = st.toggle("Enable Volume Alerts", value=True)
    notify_dc = st.toggle("Enable Donchian Alerts", value=False)

    if 'access_token' in st.session_state:
        st.divider()
        st.success("Kite Connected ✅")
        st.code(st.session_state.access_token, language="text")

    if 'access_token' not in st.session_state:
        st.divider()
        st.link_button("1. Get Login URL", st.session_state.kite.login_url(), use_container_width=True)
        token_in = st.text_input("2. Enter Request Token")
        if st.button("🚀 Activate Session", use_container_width=True):
            try:
                clean_token = token_in.split("request_token=")[-1].split("&")[0]
                data = st.session_state.kite.generate_session(clean_token, api_secret=API_SECRET)
                st.session_state.access_token = data["access_token"]
                with open(TOKEN_FILE, "w") as f: f.write(data["access_token"])
                st.session_state.kite.set_access_token(data["access_token"])
                st.rerun()
            except Exception as e: st.error(f"Error: {e}")
    else:
        if st.button("Logout / Reset Session", type="primary", use_container_width=True):
            if os.path.exists(TOKEN_FILE): os.remove(TOKEN_FILE)
            st.session_state.clear()
            st.rerun()

# --- 7. MAIN DATA PROCESSING & EXECUTION ---
if 'access_token' in st.session_state:
    # 5 Sheets mapped identically to Google Apps Script
    sheets = ["Scanner_Output 1", "Scanner_Output 2", "Scanner_Output 3", "Indices", "GF_Scanner"]
    all_syms = []
    
    for ws in sheets:
        try:
            df_sheet = conn.read(worksheet=ws)
            if not df_sheet.empty:
                all_syms.extend(df_sheet.iloc[:, 0].dropna().astype(str).tolist())
        except: continue
    
    # Clean symbol parsing matching Apps Script REGEX
    clean_symbols = []
    for s in set(all_syms):
        s_str = str(s).strip()
        if '=" ' in s_str or '="' in s_str:
            s_str = s_str.split('"')[1] if '"' in s_str else s_str
        s_str = s_str.replace('="', '').replace('"', '').strip().upper()
        if s_str and s_str not in ['NAN', 'SYMBOL', 'INDEX']:
            clean_symbols.append(s_str)

    symbols = ["NSE:" + s for s in clean_symbols]
    total_fetched_count = len(symbols)
    
    if not symbols:
        st.warning("No symbols found across worksheets.")
        st.stop()

    avg_vols = get_daily_avg_vol(st.session_state.kite, symbols)
    results = []

    try:
        # Batch Fetching in 100-item chunks (identical to Apps Script chunking)
        full_quotes = {}
        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i+100]
            full_quotes.update(st.session_state.kite.quote(chunk))
    except Exception as e:
        st.error(f"Kite API Error: {e}. Please re-login via sidebar.")
        st.stop()

    for s in symbols:
        try:
            q = full_quotes[s]
            ltp, vol, cl = q['last_price'], q['volume'], q['ohlc']['close']
            pct = round(((ltp - cl) / cl) * 100, 2)
            
            # Exact Volume Breakout Logic matching Apps Script V39.3
            avg_v = avg_vols.get(s, 0)
            is_vol_break = (vol > (avg_v * 1.1) and pct >= 1.0 and vol > 500000)
            
            # 15m Donchian Channel Check
            hist_15m = st.session_state.kite.historical_data(q['instrument_token'], now_ist-timedelta(days=10), now_ist, "15minute")
            df_15m = pd.DataFrame(hist_15m)
            dc_status, is_dc_breakout = get_donchian_status(df_15m, length=28, offset=6)

            sym_short = s.replace("NSE:", "")
            tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{sym_short}"
            alerted_keys = [f"{a['Symbol']}|{a['Type']}" for a in st.session_state.alerts_history]

            # Trigger Browser Alert based on sidebar toggles
            alert_type = ""
            if market_active:
                if notify_vol and is_vol_break and f"{sym_short}|Volume Breakout" not in alerted_keys:
                    alert_type = "Volume Breakout"
                elif notify_dc and is_dc_breakout and f"{sym_short}|Donchian Upper 15m" not in alerted_keys:
                    alert_type = "Donchian Upper 15m"
                
                if alert_type:
                    trigger_alert(sym_short, alert_type, ltp)
                    st.session_state.alerts_history.append({
                        "Symbol": sym_short, 
                        "Type": alert_type, 
                        "Time": now_ist.strftime("%H:%M:%S"), 
                        "LTP": ltp, 
                        "Chart": tv_url
                    })

            results.append({
                "Symbol": sym_short, 
                "LTP": ltp, 
                "Change %": pct, 
                "Vol Status": "🚀 BREAKOUT" if is_vol_break else "Normal", 
                "Donchian 15m (28,6)": dc_status, 
                "Chart": tv_url
            })
        except: continue

    # Load direct Google Sheet Alert_Log for full 120+ symbol sync
    try:
        df_sheet_log = conn.read(worksheet="Alert_Log")
        sheet_log_count = len(df_sheet_log) if not df_sheet_log.empty else 0
    except:
        df_sheet_log = pd.DataFrame()
        sheet_log_count = 0

    # --- 8. DASHBOARD DISPLAY & SYMBOL COUNTERS ---
    if results:
        df_full = pd.DataFrame(results).sort_values(by="Change %", ascending=False)
        
        # Apply < 1% filter if toggle is disabled
        df_display = df_full if show_all_stocks else df_full[df_full['Change %'] >= 1.0]
        
        vol_count = len(df_display[df_display['Vol Status'] == "🚀 BREAKOUT"])
        dc_count = len(df_display[df_display['Donchian 15m (28,6)'].str.contains("🚀", na=False)])
        history_count = len(st.session_state.alerts_history)

        # Header Metrics Bar
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Sheet Symbols", f"{total_fetched_count}")
        c2.metric("Active Filtered Stocks", f"{len(df_display)}")
        c3.metric("GSheets Alert_Log Count", f"{sheet_log_count}")
        c4.metric("Live PC Alerts Logged", f"{history_count}")

        # Dynamic Tabs with Item Count Badges
        t_main, t_vol, t_dc, t_gsheet_log, t_log = st.tabs([
            f"📊 Market ({len(df_display)})", 
            f"🔥 Volume ({vol_count})", 
            f"🎯 Donchian 15m ({dc_count})",
            f"📋 GSheet Alert_Log ({sheet_log_count})",
            f"📝 Live History ({history_count})"
        ])

        col_config = {
            "LTP": st.column_config.NumberColumn("LTP", format="%.2f"),
            "Change %": st.column_config.NumberColumn("Change %", format="%.2f%%"),
            "Chart": st.column_config.LinkColumn("Chart", display_text="Open TV 📈")
        }

        with t_main: 
            st.dataframe(df_display, use_container_width=True, hide_index=True, column_config=col_config)
        with t_vol: 
            st.dataframe(df_display[df_display['Vol Status'] == "🚀 BREAKOUT"], use_container_width=True, hide_index=True, column_config=col_config)
        with t_dc: 
            st.dataframe(df_display[df_display['Donchian 15m (28,6)'].str.contains("🚀", na=False)], use_container_width=True, hide_index=True, column_config=col_config)
        with t_gsheet_log:
            if not df_sheet_log.empty:
                st.dataframe(df_sheet_log, use_container_width=True, hide_index=True)
            else:
                st.info("No records in Google Sheet Alert_Log yet.")
        with t_log: 
            if st.session_state.alerts_history:
                st.dataframe(pd.DataFrame(st.session_state.alerts_history).iloc[::-1], use_container_width=True, hide_index=True, column_config=col_config)

    # Auto-refresh loop during live market hours
    if market_active:
        time.sleep(60)
        st.rerun()
