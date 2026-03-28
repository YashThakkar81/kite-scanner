import streamlit as st
import pandas as pd
from kiteconnect import KiteConnect
from streamlit_gsheets import GSheetsConnection
import streamlit.components.v1 as components
import time
import os
import pytz 
import requests 
from datetime import datetime, timedelta

# --- 1. CONFIGURATION ---
st.set_page_config(page_title="Master Omni-Scanner Pro", layout="wide")
IST = pytz.timezone('Asia/Kolkata')

# --- CSS FOR GLOBAL CENTER ALIGNMENT ---
st.markdown("""
    <style>
    [data-testid="stDataFrame"] td { text-align: center !important; }
    [data-testid="stHeader"] th { text-align: center !important; }
    [data-testid="stDataFrame"] a { justify-content: center !important; }
    .stDataFrame { margin: 0 auto; }
    </style>
    """, unsafe_allow_html=True)

try:
    API_KEY = st.secrets["API_KEY"]
    API_SECRET = st.secrets["API_SECRET"]
    # Pulling Telegram Credentials from Secrets
    TELEGRAM_TOKEN = st.secrets.get("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID = st.secrets.get("TELEGRAM_CHAT_ID", "")
    
    conn = st.connection("gsheets", type=GSheetsConnection)
except Exception as e:
    st.error(f"Setup Error: Check .streamlit/secrets.toml. Error: {e}")
    st.stop()

# --- 2. NOTIFICATION ENGINE ---
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

def send_telegram_msg(token, chat_id, message):
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=5)
    except:
        pass

# --- 3. SESSION STATE ---
if 'kite' not in st.session_state:
    st.session_state.kite = KiteConnect(api_key=API_KEY)
if 'alerts_history' not in st.session_state:
    st.session_state.alerts_history = [] 

TOKEN_FILE = "access_token.txt"
if 'access_token' not in st.session_state and os.path.exists(TOKEN_FILE):
    with open(TOKEN_FILE, "r") as f:
        token = f.read().strip()
        st.session_state.access_token = token
        st.session_state.kite.set_access_token(token)

# --- 4. UTILS ---
def calculate_ema(prices, period):
    return pd.Series(prices).ewm(span=period, adjust=False).mean().iloc[-1]

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
            time.sleep(0.05) 
        except: avg_vol_map[s] = 999999999
    return avg_vol_map

# --- 5. SIDEBAR ---
with st.sidebar:
    st.header("🕒 Scanner Status")
    ist_now = datetime.now(IST).strftime('%H:%M:%S')
    st.info(f"Last Updated: {ist_now} (IST)")
    
    if st.button("🔔 Enable Desktop Alerts"):
        components.html("<script>Notification.requestPermission();</script>", height=0)
        st.success("Permission Requested!")

    st.divider()
    st.header("📲 Telegram Alerts")
    tg_toggle = st.toggle("Enable Telegram Mode", value=True)
    
    if tg_toggle:
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            st.success("✅ Telegram Linked")
            if st.button("🔔 Send Test Message"):
                send_telegram_msg(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, "<b>Scanner Online!</b> 🚀\nTelegram connection verified successfully.")
                st.toast("Test Message Sent!")
        else:
            st.warning("⚠️ Secrets Missing: Add TELEGRAM_TOKEN and TELEGRAM_CHAT_ID to settings.")
    
    st.divider()
    if 'access_token' not in st.session_state:
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
        st.success("✅ Kite Connected")
        if st.button("Logout / Reset", use_container_width=True):
            if os.path.exists(TOKEN_FILE): os.remove(TOKEN_FILE)
            st.session_state.clear(); st.rerun()
    
    st.divider()
    if st.button("🗑️ Clear Alert History", use_container_width=True):
        st.session_state.alerts_history = []
        st.rerun()

# --- 6. DATA PROCESSING ---
if 'access_token' in st.session_state:
    sheets = ["Scanner_Output 1", "Scanner_Output 2", "Scanner_Output 3"]
    all_syms = []
    for ws in sheets:
        try:
            df_sheet = conn.read(worksheet=ws)
            if not df_sheet.empty:
                data = df_sheet.iloc[:, 0].dropna().astype(str).tolist()
                all_syms.extend(data)
        except: continue
    
    symbols = ["NSE:" + s.strip() for s in set(all_syms) if s not in ['nan', 'Symbol']][:200]
    
    if not symbols:
        st.warning("No symbols found in Google Sheets.")
        st.stop()

    avg_vols = get_daily_avg_vol(st.session_state.kite, symbols)
    results = []
    now = datetime.now(IST)
    
    progress = st.empty()
    progress.info(f"Scanning {len(symbols)} Stocks (EMA 20/50)...")

    full_quotes = st.session_state.kite.quote(symbols)

    for s in symbols:
        try:
            q = full_quotes[s]
            ltp, vol, cl = q['last_price'], q['volume'], q['ohlc']['close']
            pct = round(((ltp - cl) / cl) * 100, 2)
            
            is_vol_break = (vol > 500000 and pct >= 1.0 and vol > avg_vols.get(s, 0))
            is_ema_cross = False
            
            if is_vol_break or pct > 0.5: 
                hist_15m = st.session_state.kite.historical_data(q['instrument_token'], now-timedelta(days=4), now, "15minute")
                df_15m = pd.DataFrame(hist_15m)
                if len(df_15m) >= 55:
                    ema20 = calculate_ema(df_15m['close'], 20)
                    ema50 = calculate_ema(df_15m['close'], 50) 
                    is_ema_cross = ema20 > ema50
            
            sym_short = s.replace("NSE:", "")
            tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{sym_short}"
            alerted_keys = [f"{a['Symbol']}|{a['Type']}" for a in st.session_state.alerts_history]

            if (is_vol_break and f"{sym_short}|Volume" not in alerted_keys) or \
               (is_ema_cross and f"{sym_short}|EMA 20/50" not in alerted_keys):
                
                alert_type = "Volume" if is_vol_break else "EMA 20/50"
                trigger_alert(sym_short, alert_type, ltp)
                
                # Automated Telegram Dispatch from Secrets
                if tg_toggle and TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
                    tg_msg = f"🚀 <b>{alert_type} ALERT</b>\nStock: <b>{sym_short}</b>\nPrice: ₹{ltp}\n<a href='{tv_url}'>View Chart 📈</a>"
                    send_telegram_msg(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, tg_msg)

                st.session_state.alerts_history.append({"Symbol": sym_short, "Type": alert_type, "Time": now.strftime("%H:%M:%S"), "LTP": ltp, "Chart": tv_url})

            results.append({
                "Symbol": sym_short, "LTP": ltp, "Change %": pct, 
                "Vol Status": "🚀 BREAKOUT" if is_vol_break else "Normal",
                "EMA Status": "⚡ CROSS" if is_ema_cross else "Below",
                "Chart": tv_url
            })
        except: continue

    progress.empty()
    
    # --- 7. TABS ---
    t_main, t_vol, t_ema, t_log = st.tabs(["📊 Market", "🔥 Volume", "⚡ EMA 15m", "📝 History"])
    col_config = {
        "Symbol": st.column_config.TextColumn("Symbol", width="medium"),
        "LTP": st.column_config.NumberColumn("LTP", format="%.2f", width="small"),
        "Change %": st.column_config.NumberColumn("Change %", format="%.2f%%", width="small"),
        "Vol Status": st.column_config.TextColumn("Vol Status", width="medium"),
        "EMA Status": st.column_config.TextColumn("EMA Status", width="medium"),
        "Chart": st.column_config.LinkColumn("Chart", display_text="Open TV 📈", width="small")
    }

    def display_styled_df(df):
        st.dataframe(df, use_container_width=True, hide_index=True, column_config=col_config)

    if results:
        df_res = pd.DataFrame(results)
        with t_main: display_styled_df(df_res)
        with t_vol: display_styled_df(df_res[df_res['Vol Status'] == "🚀 BREAKOUT"])
        with t_ema: display_styled_df(df_res[df_res['EMA Status'] == "⚡ CROSS"])
    else:
        st.warning("Scanning complete. No live breakout signals found.")

    with t_log: 
        if st.session_state.alerts_history:
            history_config = col_config.copy()
            history_config["Time"] = st.column_config.TextColumn("Time")
            st.dataframe(pd.DataFrame(st.session_state.alerts_history).iloc[::-1], use_container_width=True, hide_index=True, column_config=history_config)

    time.sleep(60)
    st.rerun()
