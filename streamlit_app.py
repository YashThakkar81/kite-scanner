import streamlit as st
import pandas as pd
from kiteconnect import KiteConnect
from streamlit_gsheets import GSheetsConnection
import streamlit.components.v1 as components
import time
import os
import json
import pytz
import requests
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

ACTIVE_TRADES_FILE = "active_trades.json"

# --- PERSISTENT ACTIVE TRADES STORAGE UTILS ---
def load_active_trades():
    if os.path.exists(ACTIVE_TRADES_FILE):
        try:
            with open(ACTIVE_TRADES_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_active_trades(trades):
    try:
        with open(ACTIVE_TRADES_FILE, "w") as f:
            json.dump(trades, f, indent=4)
    except Exception:
        pass

# --- TECHNICAL HELPERS: ATR & PIVOT S1 ---
def calculate_atr14(df):
    if df is None or len(df) < 15:
        return 0.0
    df = df.copy()
    df['prev_close'] = df['close'].shift(1)
    df['tr'] = df.apply(
        lambda r: max(
            r['high'] - r['low'],
            abs(r['high'] - r['prev_close']) if pd.notna(r['prev_close']) else 0.0,
            abs(r['low'] - r['prev_close']) if pd.notna(r['prev_close']) else 0.0
        ), axis=1
    )
    atr = df['tr'].rolling(window=14).mean().iloc[-1]
    return float(atr) if pd.notna(atr) else 0.0

def fetch_pivot_s1(kite_inst, instrument_token):
    try:
        now = datetime.now(IST)
        from_date = now - timedelta(days=5)
        hist = kite_inst.historical_data(instrument_token, from_date, now.date() - timedelta(days=1), "day")
        if not hist or len(hist) < 1:
            return 0.0
        prev_day = hist[-1]
        p = (prev_day['high'] + prev_day['low'] + prev_day['close']) / 3.0
        s1 = (2 * p) - prev_day['high']
        return round(float(s1), 2)
    except Exception:
        return 0.0

# --- 2. PC & TELEGRAM NOTIFICATION ENGINE ---
def send_telegram_raw(message):
    try:
        bot_token = st.secrets["TELEGRAM_BOT_TOKEN"]
        chat_id = st.secrets["TELEGRAM_CHAT_ID"]
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": True
        }
        requests.post(url, data=payload, timeout=5)
    except Exception:
        pass

def send_telegram_alert(symbol, alert_type, ltp, sl1=0.0, sl2=0.0):
    if alert_type == "Happy Breakout":
        message = (
            f"HAPPY BREAKOUT: {symbol}\n\n"
            f"Entry: ₹{ltp}\n"
            f"SL 1: ₹{sl1}\n"
            f"SL 2: ₹{sl2}"
        )
    else:
        message = (
            f"{alert_type.upper()}: {symbol}\n\n"
            f"Entry: ₹{ltp}"
        )
    send_telegram_raw(message)

def send_telegram_exit(symbol, exit_type, ltp):
    message = (
        f"{exit_type}: {symbol}\n\n"
        f"LTP: ₹{ltp}"
    )
    send_telegram_raw(message)

def send_telegram_eod_exit(symbols):
    if not symbols:
        return
    sym_list = ", ".join(symbols)
    message = f"EXIT FULL POSITION: {sym_list}"
    send_telegram_raw(message)

def trigger_alert(symbol, alert_type, ltp, sl1=0.0, sl2=0.0):
    notification_js = f"""
    <script>
    if (Notification.permission === "granted") {{
        const n = new Notification("{alert_type}: {symbol}", {{ 
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
    
    send_telegram_alert(symbol, alert_type, ltp, sl1=sl1, sl2=sl2)

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
    except Exception:
        pass

# --- 4. DONCHIAN CHANNEL & CACHED HISTORICAL DATA ---
def get_donchian_status(df, length=28, offset=6):
    if df is None or len(df) < (length + offset):
        return "N/A", False

    upper_channel = df['high'].rolling(window=length).max().shift(offset)
    curr_close = df['close'].iloc[-1]
    curr_upper = upper_channel.iloc[-1]

    if pd.isna(curr_upper):
        return "N/A", False

    is_breakout = curr_close >= curr_upper
    status_str = "🚀 UPPER BREAKOUT" if is_breakout else "Below"
    return status_str, is_breakout

@st.cache_data(ttl=300, show_spinner=False)
def fetch_15m_candles(access_token, api_key, instrument_token):
    try:
        kite_inst = KiteConnect(api_key=api_key)
        kite_inst.set_access_token(access_token)
        now = datetime.now(IST)
        hist = kite_inst.historical_data(instrument_token, now - timedelta(days=10), now, "15minute")
        return pd.DataFrame(hist)
    except Exception:
        return None

@st.cache_data(ttl=86400, show_spinner=False)
def get_daily_avg_vol(access_token, api_key, symbols):
    kite_inst = KiteConnect(api_key=api_key)
    kite_inst.set_access_token(access_token)
    avg_vol_map = {}
    to_date = datetime.now(IST).date()
    from_date = to_date - timedelta(days=35)
    
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i+100]
        try:
            quotes = kite_inst.quote(chunk)
            for s in chunk:
                try:
                    q = quotes.get(s)
                    if q:
                        hist = kite_inst.historical_data(q['instrument_token'], from_date, to_date - timedelta(days=1), "day")
                        avg_vol_map[s] = sum([day['volume'] for day in hist[-22:]]) / 22 if len(hist) >= 22 else 999999999
                    else:
                        avg_vol_map[s] = 999999999
                except Exception:
                    avg_vol_map[s] = 999999999
        except Exception:
            pass
    return avg_vol_map

# --- 5. MARKET HOURS UTILITY ---
def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    market_start = dtime(9, 7)
    market_end = dtime(15, 30)
    return market_start <= now.time() <= market_end

# --- Dynamic EMA Exit Monitor Engine ---
def process_active_trade_exits(kite_inst, access_token, api_key):
    now_time = datetime.now(IST).time()
    active_trades = load_active_trades()
    if not active_trades:
        return

    # 3:15 PM EOD Consolidated Square-Off Trigger
    if now_time >= dtime(15, 15):
        eod_exit_symbols = []
        for sym, data in list(active_trades.items()):
            trig_dt = datetime.fromisoformat(data["trigger_time"])
            # Evaluate trades triggered before 3:15 PM today
            if trig_dt.time() <= dtime(15, 15):
                eod_exit_symbols.append(sym)
                del active_trades[sym]
            else:
                # Late triggers (> 3:15 PM) saved cleanly for next session watchlist
                data["watchlist_next_session"] = True

        if eod_exit_symbols:
            send_telegram_eod_exit(eod_exit_symbols)
        save_active_trades(active_trades)
        return

    # Regular intraday 15m candle-close check
    updated = False
    for sym, data in list(active_trades.items()):
        inst_token = data.get("instrument_token")
        if not inst_token:
            continue

        df_15m = fetch_15m_candles(access_token, api_key, inst_token)
        if df_15m is None or len(df_15m) < 15:
            continue

        df_15m['ema5'] = df_15m['close'].ewm(span=5, adjust=False).mean()
        df_15m['ema9'] = df_15m['close'].ewm(span=9, adjust=False).mean()

        last_close = df_15m['close'].iloc[-1]
        last_ema5 = df_15m['ema5'].iloc[-1]
        last_ema9 = df_15m['ema9'].iloc[-1]

        # EXIT 1: 15m Candle Close Below 5 EMA
        if not data.get("exit1_triggered", False):
            if last_close < last_ema5:
                send_telegram_exit(sym, "EXIT 1", round(last_close, 2))
                data["exit1_triggered"] = True
                updated = True

        # FINAL EXIT: 15m Candle Close Below 9 EMA
        if not data.get("final_exit_triggered", False):
            if last_close < last_ema9:
                send_telegram_exit(sym, "FINAL EXIT", round(last_close, 2))
                data["final_exit_triggered"] = True
                del active_trades[sym]
                updated = True

    if updated:
        save_active_trades(active_trades)

# --- 6. SIDEBAR ---
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
    notify_combo = st.toggle("Enable Happy Breakout (500K Vol + Donchian)", value=True)
    notify_early = st.toggle("Enable Early Watchlist Alert (100K Vol + Donchian)", value=True)
    notify_vol = st.toggle("Enable Individual Volume Alerts", value=False)
    notify_dc = st.toggle("Enable Individual Donchian Alerts", value=False)

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

# --- 7. MAIN DATA PROCESSING ---
if 'access_token' in st.session_state:
    sheets = ["Scanner_Output 1", "Scanner_Output 2", "Scanner_Output 3", "Indices", "GF_Scanner"]
    all_syms = []
    gsheet_pct_map = {}
    
    for ws in sheets:
        try:
            df_sheet = conn.read(worksheet=ws)
            if not df_sheet.empty:
                syms_in_sheet = df_sheet.iloc[:, 0].dropna().astype(str).tolist()
                all_syms.extend(syms_in_sheet)
                
                pct_col_idx = None
                for idx, col_name in enumerate(df_sheet.columns):
                    if "%" in str(col_name) or "CHANGE" in str(col_name).upper():
                        pct_col_idx = idx
                        break
                
                for _, row in df_sheet.iterrows():
                    s_raw = str(row.iloc[0]).strip()
                    if '="' in s_raw:
                        s_raw = s_raw.split('"')[1] if '"' in s_raw else s_raw
                    s_clean = s_raw.replace('="', '').replace('"', '').strip().upper()
                    
                    if s_clean and s_clean not in ['NAN', 'SYMBOL', 'INDEX']:
                        if pct_col_idx is not None:
                            val_str = str(row.iloc[pct_col_idx]).replace('%', '').strip()
                            try:
                                parsed_val = float(val_str)
                                if abs(parsed_val) < 0.20 and parsed_val != 0:
                                    parsed_val = parsed_val * 100.0
                                gsheet_pct_map[s_clean] = parsed_val
                            except ValueError:
                                pass
        except Exception:
            continue
    
    clean_symbols = []
    for s in set(all_syms):
        s_str = str(s).strip()
        if '="' in s_str:
            s_str = s_str.split('"')[1] if '"' in s_str else s_str
        s_str = s_str.replace('="', '').replace('"', '').strip().upper()
        if s_str and s_str not in ['NAN', 'SYMBOL', 'INDEX']:
            clean_symbols.append(s_str)

    symbols = ["NSE:" + s for s in clean_symbols]
    total_fetched_count = len(symbols)
    
    if not symbols:
        st.warning("No symbols found across worksheets.")
        st.stop()

    avg_vols = get_daily_avg_vol(st.session_state.access_token, API_KEY, symbols)
    results = []

    try:
        full_quotes = {}
        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i+100]
            full_quotes.update(st.session_state.kite.quote(chunk))
    except Exception as e:
        st.error(f"Kite API Error: {e}. Please re-login via sidebar.")
        st.stop()

    for s in symbols:
        try:
            q = full_quotes.get(s)
            if not q: continue
            
            ltp, vol, cl = q['last_price'], q['volume'], q['ohlc']['close']
            sym_short = s.replace("NSE:", "")
            
            # Accurate real-time Kite % calculation, falling back on parsed GSheet value
            if cl > 0:
                pct = round(((ltp - cl) / cl) * 100, 2)
            elif sym_short in gsheet_pct_map:
                pct = round(gsheet_pct_map[sym_short], 2)
            else:
                pct = 0.0
            
            avg_v = avg_vols.get(s, 0)
            
            # Dual Volume Threshold Logic
            is_vol_break_500k = (vol > (avg_v * 1.1) and pct >= 1.0 and vol > 500000)
            is_vol_break_100k = (vol > (avg_v * 1.1) and pct >= 1.0 and vol >= 100000)
            
            if show_all_stocks or pct >= 1.0 or is_vol_break_100k:
                df_15m = fetch_15m_candles(st.session_state.access_token, API_KEY, q['instrument_token'])
                dc_status, is_dc_breakout = get_donchian_status(df_15m, length=28, offset=6)
            else:
                df_15m = None
                dc_status, is_dc_breakout = "Below", False

            tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{sym_short}"
            alerted_keys = [f"{a['Symbol']}|{a['Type']}" for a in st.session_state.alerts_history]

            # Signal conditions
            is_happy_breakout = is_vol_break_500k and is_dc_breakout
            is_early_alert = is_vol_break_100k and (not is_vol_break_500k) and is_dc_breakout

            alert_type = ""
            if market_active:
                if notify_combo and is_happy_breakout and f"{sym_short}|Happy Breakout" not in alerted_keys:
                    alert_type = "Happy Breakout"
                elif notify_early and is_early_alert and f"{sym_short}|Early Watchlist Alert" not in alerted_keys:
                    alert_type = "Early Watchlist Alert"
                elif notify_vol and is_vol_break_500k and f"{sym_short}|Volume Breakout" not in alerted_keys:
                    alert_type = "Volume Breakout"
                elif notify_dc and is_dc_breakout and f"{sym_short}|Donchian Upper 15m" not in alerted_keys:
                    alert_type = "Donchian Upper 15m"
                
                if alert_type:
                    sl1_val, sl2_val = 0.0, 0.0
                    if alert_type == "Happy Breakout":
                        atr_val = calculate_atr14(df_15m)
                        sl1_val = round(ltp - (1.5 * atr_val), 2)
                        sl2_val = fetch_pivot_s1(st.session_state.kite, q['instrument_token'])
                        
                        # Store in persistent background tracking list for EMA dynamic exits
                        active_trades = load_active_trades()
                        active_trades[sym_short] = {
                            "instrument_token": q['instrument_token'],
                            "entry_price": ltp,
                            "sl1": sl1_val,
                            "sl2": sl2_val,
                            "trigger_time": datetime.now(IST).isoformat(),
                            "exit1_triggered": False,
                            "final_exit_triggered": False
                        }
                        save_active_trades(active_trades)

                    trigger_alert(sym_short, alert_type, ltp, sl1=sl1_val, sl2=sl2_val)
                    st.session_state.alerts_history.append({
                        "Symbol": sym_short, 
                        "Type": alert_type, 
                        "Time": now_ist.strftime("%H:%M:%S"), 
                        "LTP": ltp, 
                        "Chart": tv_url
                    })

            vol_status_label = "🚀 BREAKOUT" if is_vol_break_500k else ("👀 WATCH (100K)" if is_vol_break_100k else "Normal")

            results.append({
                "Symbol": sym_short, 
                "LTP": ltp, 
                "Change %": pct, 
                "Vol Status": vol_status_label, 
                "Donchian 15m (28,6)": dc_status, 
                "Chart": tv_url
            })
        except Exception:
            continue

    # Process background 15m EMA Candle-Close Exit Alerts
    if market_active:
        process_active_trade_exits(st.session_state.kite, st.session_state.access_token, API_KEY)

    try:
        df_sheet_log = conn.read(worksheet="Alert_Log")
        if not df_sheet_log.empty:
            for col in df_sheet_log.columns:
                if "%" in str(col) or "CHANGE" in str(col).upper():
                    df_sheet_log[col] = pd.to_numeric(df_sheet_log[col].astype(str).str.replace('%', ''), errors='coerce')
                    df_sheet_log[col] = df_sheet_log[col].apply(lambda x: x * 100.0 if abs(x) < 0.20 and x != 0 else x)
        sheet_log_count = len(df_sheet_log) if not df_sheet_log.empty else 0
    except Exception:
        df_sheet_log = pd.DataFrame()
        sheet_log_count = 0

    # --- 8. DASHBOARD DISPLAY ---
    if results:
        df_full = pd.DataFrame(results).sort_values(by="Change %", ascending=False)
        df_display = df_full if show_all_stocks else df_full[df_full['Change %'] >= 1.0]
        
        df_combo = df_display[
            (df_display['Vol Status'] == "🚀 BREAKOUT") & 
            (df_display['Donchian 15m (28,6)'].str.contains("🚀", na=False))
        ]
        
        df_early = df_display[
            (df_display['Vol Status'] == "👀 WATCH (100K)") & 
            (df_display['Donchian 15m (28,6)'].str.contains("🚀", na=False))
        ]

        combo_count = len(df_combo)
        early_count = len(df_early)
        vol_count = len(df_display[df_display['Vol Status'].str.contains("BREAKOUT|WATCH", regex=True, na=False)])
        dc_count = len(df_display[df_display['Donchian 15m (28,6)'].str.contains("🚀", na=False)])
        history_count = len(st.session_state.alerts_history)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Sheet Symbols", f"{total_fetched_count}")
        c2.metric("Active Filtered Stocks", f"{len(df_display)}")
        c3.metric("GSheets Alert_Log Count", f"{sheet_log_count}")
        c4.metric("Live PC Alerts Logged", f"{history_count}")

        t_combo, t_early, t_main, t_vol, t_dc, t_gsheet_log, t_log = st.tabs([
            f"🎯 Happy Breakout ({combo_count})",
            f"👀 Early Watch ({early_count})",
            f"📊 Market ({len(df_display)})", 
            f"🔥 Volume ({vol_count})", 
            f"📈 Donchian 15m ({dc_count})",
            f"📋 GSheet Alert_Log ({sheet_log_count})",
            f"📝 Live History ({history_count})"
        ])

        col_config = {
            "LTP": st.column_config.NumberColumn("LTP", format="%.2f"),
            "Change %": st.column_config.NumberColumn("Change %", format="%.2f%%"),
            "Chart": st.column_config.LinkColumn("Chart", display_text="Open TV 📈")
        }

        with t_combo:
            st.dataframe(df_combo, use_container_width=True, hide_index=True, column_config=col_config)
        with t_early:
            st.dataframe(df_early, use_container_width=True, hide_index=True, column_config=col_config)
        with t_main: 
            st.dataframe(df_display, use_container_width=True, hide_index=True, column_config=col_config)
        with t_vol: 
            st.dataframe(df_display[df_display['Vol Status'].str.contains("BREAKOUT|WATCH", regex=True, na=False)], use_container_width=True, hide_index=True, column_config=col_config)
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

    if market_active:
        time.sleep(60)
        st.rerun()
