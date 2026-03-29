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
    TG_TOKEN = st.secrets.get("TELEGRAM_TOKEN")
    TG_ID = st.secrets.get("TELEGRAM_CHAT_ID")
    conn = st.connection("gsheets", type=GSheetsConnection)
except Exception as e:
    st.error(f"Setup Error: {e}")
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
    if not token or not chat_id: return False, "Missing Secrets"
    # STRICT STRING ENFORCEMENT for Chat ID
    chat_id_str = str(chat_id).strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id_str, 
        "text": message, 
        "parse_mode": "HTML"
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            return True, "Success"
        else:
            # Capturing exact error for debugging
            return False, f"Error {resp.status_code}: {resp.json().get('description')}"
    except Exception as e:
        return False, str(e)

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

# --- 4. INDICATOR CALCS ---
def calculate_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def get_bb_median_status(df, period=20, offset=6):
    # Matches TradingView: EMA Basis + 6 Period Offset
    ema_basis = calculate_ema(df['close'], period)
    shifted_median = ema_basis.shift(offset)
    
    if len(df) < 2: return "N/A"
    curr_close, prev_close = df['close'].iloc[-1], df['close'].iloc[-2]
    curr_low, curr_med = df['low'].iloc[-1], shifted_median.iloc[-1]
    prev_med = shifted_median.iloc[-2]
    
    if prev_close < prev_med and curr_close > curr_med:
        return "🚀 CROSS"
    elif curr_low <= curr_med and curr_close > curr_med:
        return "🛡️ SUPPORT"
    return "Above" if curr_close > curr_med else "Below"

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
    now_ist = datetime.now(IST)
    st.info(f"Last Updated: {now_ist.strftime('%H:%M:%S')}")
    
    if st.button("🔔 Enable Desktop Alerts"):
        components.html("<script>Notification.requestPermission();</script>", height=0)
        st.success("Permission Requested!")

    st.divider()
    st.header("📲 Telegram Alerts")
    tg_toggle = st.toggle("Enable Telegram Mode", value=True)
    if tg_toggle:
        if TG_TOKEN and TG_ID:
            st.success("✅ Secrets Configured")
            if st.button("🔔 Send Test Message"):
                test_msg = f"<b>Scanner Check</b>\nTime: {now_ist.strftime('%H:%M:%S')}\nStatus: Online"
                success, log_msg = send_telegram_msg(TG_TOKEN, TG_ID, test_msg)
                if success:
                    st.toast("Success! Check Telegram.")
                else: 
                    # This will now show the exact reason for failure
                    st.error(f"Failed: {log_msg}")
        else:
            st.warning("⚠️ Secrets Missing.")
    
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
    progress = st.empty()
    progress.info(f"Scanning {len(symbols)} Stocks...")

    try:
        full_quotes = st.session_state.kite.quote(symbols)
    except:
        if os.path.exists(TOKEN_FILE): os.remove(TOKEN_FILE)
        st.error("Session Expired.")
        st.stop()

    for s in symbols:
        try:
            q = full_quotes[s]
            ltp, vol, cl = q['last_price'], q['volume'], q['ohlc']['close']
            pct = round(((ltp - cl) / cl) * 100, 2)
            
            # Volume Breakout Logic
            is_vol_break = (vol > 500000 and pct >= 1.0 and vol > avg_vols.get(s, 0))
            
            # BB Median 1H Logic
            hist_1h = st.session_state.kite.historical_data(q['instrument_token'], now_ist-timedelta(days=10), now_ist, "60minute")
            df_1h = pd.DataFrame(hist_1h)
            bb_status = get_bb_median_status(df_1h) if len(df_1h) >= 30 else "N/A"
            
            # EMA 15m Logic
            hist_15m = st.session_state.kite.historical_data(q['instrument_token'], now_ist-timedelta(days=5), now_ist, "15minute")
            df_15m = pd.DataFrame(hist_15m)
            is_ema_cross = False
            if len(df_15m) >= 55:
                ema20 = calculate_ema(df_15m['close'], 20).iloc[-1]
                ema50 = calculate_ema(df_15m['close'], 50).iloc[-1]
                is_ema_cross = ema20 > ema50

            sym_short = s.replace("NSE:", "")
            tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{sym_short}"
            alerted_keys = [f"{a['Symbol']}|{a['Type']}" for a in st.session_state.alerts_history]

            # Alert Handler
            alert_type = ""
            if is_vol_break and f"{sym_short}|Volume" not in alerted_keys:
                alert_type = "Volume Breakout"
            elif "🚀" in bb_status and f"{sym_short}|BB Median" not in alerted_keys:
                alert_type = "BB Median 1H"
            
            if alert_type:
                trigger_alert(sym_short, alert_type, ltp)
                if tg_toggle and TG_TOKEN and TG_ID:
                    tg_msg = f"🚀 <b>{alert_type} ALERT</b>\nStock: <b>{sym_short}</b>\nPrice: ₹{ltp}\n<a href='{tv_url}'>View Chart 📈</a>"
                    send_telegram_msg(TG_TOKEN, TG_ID, tg_msg)
                st.session_state.alerts_history.append({"Symbol": sym_short, "Type": alert_type, "Time": now_ist.strftime("%H:%M:%S"), "LTP": ltp, "Chart": tv_url})

            results.append({
                "Symbol": sym_short, "LTP": ltp, "Change %": pct, 
                "Vol Status": "🚀 BREAKOUT" if is_vol_break else "Normal",
                "EMA Status": "⚡ CROSS" if is_ema_cross else "Below",
                "BB Median (1H)": bb_status,
                "Chart": tv_url
            })
        except: continue

    progress.empty()
    
    # --- 7. UI TABS (Restored Volume) ---
    t_main, t_vol, t_bb, t_ema, t_log = st.tabs(["📊 Market", "🔥 Volume", "🎯 BB Median 1H", "⚡ EMA 15m", "📝 History"])
    
    col_config = {
        "Symbol": st.column_config.TextColumn("Symbol"),
        "LTP": st.column_config.NumberColumn("LTP", format="%.2f"),
        "Change %": st.column_config.NumberColumn("Change %", format="%.2f%%"),
        "Vol Status": st.column_config.TextColumn("Vol Status"),
        "EMA Status": st.column_config.TextColumn("EMA Status"),
        "BB Median (1H)": st.column_config.TextColumn("BB Median (1H)"),
        "Chart": st.column_config.LinkColumn("Chart", display_text="Open TV 📈")
    }

    if results:
        df_res = pd.DataFrame(results).sort_values(by="Change %", ascending=False)
        with t_main: st.dataframe(df_res, use_container_width=True, hide_index=True, column_config=col_config)
        with t_vol: st.dataframe(df_res[df_res['Vol Status'] == "🚀 BREAKOUT"], use_container_width=True, hide_index=True, column_config=col_config)
        with t_bb: st.dataframe(df_res[df_res['BB Median (1H)'].str.contains("🚀|🛡️", na=False)], use_container_width=True, hide_index=True, column_config=col_config)
        with t_ema: st.dataframe(df_res[df_res['EMA Status'] == "⚡ CROSS"], use_container_width=True, hide_index=True, column_config=col_config)
    
    with t_log: 
        if st.session_state.alerts_history:
            st.dataframe(pd.DataFrame(st.session_state.alerts_history).iloc[::-1], use_container_width=True, hide_index=True)

    time.sleep(60)
    st.rerun()
