# 文件名：macd_early_alert_app.py
import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import time
from datetime import datetime, timedelta
import requests
import threading
from collections import defaultdict
import pytz
import os
from threading import Lock

# ==================== 安全讀取 Secrets ====================
try:
    TELEGRAM_BOT_TOKEN = st.secrets["telegram"]["bot_token"]
    TELEGRAM_CHAT_IDS = st.secrets["telegram"].get("chat_ids", [st.secrets["telegram"]["chat_id"]])
except:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id_str = os.getenv("TELEGRAM_CHAT_ID")
    TELEGRAM_CHAT_IDS = [cid.strip() for cid in chat_id_str.split(",")] if chat_id_str else []
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        st.error("請設定 Telegram Bot Token 與 Chat ID")
        st.stop()

# ==================== 側邊欄設定 ====================
st.sidebar.header("自訂監控股票")
default_tickers = "TSLA, NVDA, AAPL, META, AMD, SMCI, COIN, NIO, XPEV, TSLL"
input_tickers = st.sidebar.text_input(
    "輸入股票代號（逗號分隔）", value=default_tickers,
    help="支援 .TW、.HK、.SS 等"
)

raw_symbols = [s.strip().upper() for s in input_tickers.split(",") if s.strip()]
if not raw_symbols:
    st.error("請至少輸入一檔股票！")
    st.stop()

SYMBOLS = raw_symbols
st.sidebar.success(f"監控 {len(SYMBOLS)} 檔：{', '.join(SYMBOLS[:10])}{'...' if len(SYMBOLS)>10 else ''}")

interval_options = ["1m","2m","5m","15m","30m","60m","90m","1h","1d","5d","1wk","1mo"]
period_options   = ["1d","5d","7d","30d","60d","6mo","1y","2y","5y","ytd","max"]

selected_interval = st.sidebar.selectbox("K線週期", interval_options, index=2)   # 預設 5m
selected_period   = st.sidebar.selectbox("資料範圍", period_options, index=2)   # 預設 7d

refresh_options = [20, 30, 45, 60, 90, 120, 180, 300]
REFRESH_INTERVAL = st.sidebar.selectbox("刷新間隔（秒）", refresh_options, index=3)  # 預設 60s

# 自動修正 period（避免 400 錯誤）
def get_valid_period(interval: str, requested: str) -> str:
    limits = {"1m":"7d", "2m":"60d", "5m":"60d", "15m":"60d", "30m":"60d", "60m":"730d", "90m":"730d", "1h":"730d"}
    max_p = limits.get(interval, "max")
    if max_p != "max" and requested not in ["1d","5d","7d","30d","60d","730d"]:
        st.sidebar.warning(f"{interval} 最多 {max_p}，已自動調整")
        return max_p if max_p in period_options else "60d"
    return requested

selected_period = get_valid_period(selected_interval, selected_period)

# ==================== 全域快取 + 鎖定（解決同時大量 request）===================
if "data_cache" not in st.session_state:
    st.session_state.data_cache = {}
if "cache_lock" not in st.session_state:
    st.session_state.cache_lock = Lock()
if "sent_signals" not in st.session_state:
    st.session_state.sent_signals = {}   # {symbol: {bull: timestamp, bear: timestamp}}

MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9

# ==================== Telegram 推送 ====================
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            requests.post(url, data={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }, timeout=10)
        except:
            pass

# ==================== MACD 提前訊號（已優化判斷邏輯）===================
def macd_early_signal(df: pd.DataFrame):
    close = df['Close']
    dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    histogram = (dif - dea) * 2

    if len(df) < 30:
        return {"bull_early": False, "bear_early": False, "dif": dif, "signal": dea, "histogram": histogram}

    d2, d1, d0 = dif.iloc[-3], dif.iloc[-2], dif.iloc[-1]
    s0 = dea.iloc[-1]
    h2, h1, h0 = histogram.iloc[-3], histogram.iloc[-2], histogram.iloc[-1]

    # Hook + 柱子收縮 + 極接近
    hook_up   = (d1 <= d2) and (d0 > d1) and (d0 - d1) > 0.5 * abs(d1 - d2)   # 加速上彎
    hook_down = (d1 >= d2) and (d0 < d1) and (d1 - d0) > 0.5 * abs(d1 - d2)

    shrink_red   = h2 > h1 > 0 and 0 < h0 < h1 * 0.8
    shrink_green = h2 < h1 < 0 and 0 > h0 > h1 * 1.2

    distance = abs(d0 - s0)
    std20 = (dif - dea).rolling(20).std().iloc[-1]
    very_close = distance < std20 * 0.35 if not pd.isna(std20) else True

    bull_early = (hook_up or shrink_green) and very_close and d0 < s0
    bear_early = (hook_down or shrink_red) and very_close and d0 > s0

    return {
        "dif": dif, "signal": dea, "histogram": histogram,
        "bull_early": bull_early, "bear_early": bear_early
    }

# ==================== 繪圖 ====================
def plot_macd(symbol, macd_data, df):
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        subplot_titles=(f"{symbol} 價格走勢", f"MACD 提前訊號 ({selected_interval})"),
                        vertical_spacing=0.05, row_heights=[0.65, 0.35])

    fig.add_trace(go.Candlestick(x=df.index, open=df['Open'], high=df['High'],
                                 low=df['Low'], close=df['Close'], name="K線"), row=1, col=1)
    
    fig.add_trace(go.Scatter(x=macd_data["dif"].index, y=macd_data["dif"], name="DIF", line=dict(color="#ff9f0a")), row=2, col=1)
    fig.add_trace(go.Scatter(x=macd_data["signal"].index, y=macd_data["signal"], name="DEA", line=dict(color="#4169e1")), row=2, col=1)
    
    colors = ['red' if v <= 0 else 'green' for v in macd_data["histogram"]]
    fig.add_trace(go.Bar(x=macd_data["histogram"].index, y=macd_data["histogram"], 
                         name="MACD柱", marker_color=colors), row=2, col=1)

    last_time = df.index[-1]
    if macd_data["bull_early"]:
        fig.add_annotation(x=last_time, y=df['Close'].iloc[-1]*0.98, text="即將金叉！", 
                           showarrow=True, arrowcolor="lime", bgcolor="darkgreen", 
                           font=dict(color="white"), row=1, col=1)
    if macd_data["bear_early"]:
        fig.add_annotation(x=last_time, y=df['Close'].iloc[-1]*1.02, text="即將死叉！", 
                           showarrow=True, arrowcolor="red", bgcolor="darkred", 
                           font=dict(color="white"), row=1, col=1)

    fig.update_layout(height=720, showlegend=False, 
                      title=f"{symbol} • 更新時間 {datetime.now(pytz.timezone('Asia/Taipei')).strftime('%H:%M:%S')}")
    return fig

# ==================== 背景監控（單例 + 鎖定 + 快取）===================
def background_monitor():
    while True:
        interval = st.session_state.get("refresh_interval", 60)
        tw_now = datetime.now(pytz.timezone('Asia/Taipei'))
        
        for symbol in SYMBOLS:
            try:
                # 從快取拿（最多 30 秒舊資料）
                cache_key = f"{symbol}_{selected_interval}_{selected_period}"
                cached = st.session_state.data_cache.get(cache_key)
                if cached and (datetime.now() - cached["time"]).total_seconds() < 30:
                    df = cached["data"]
                else:
                    df = yf.download(symbol, period=selected_period, interval=selected_interval,
                                     progress=False, auto_adjust=True, threads=False, proxy=None)
                    if df.empty or len(df) < 30:
                        continue
                    with st.session_state.cache_lock:
                        st.session_state.data_cache[cache_key] = {"data": df, "time": datetime.now()}

                result = macd_early_signal(df)
                last_ts = df.index[-1]

                # 精確去重：同一根 K 線只發一次
                signal_key = f"{symbol}_{last_ts.strftime('%Y%m%d%H%M')}"

                if result["bull_early"]:
                    if st.session_state.sent_signals.get(signal_key) != "bull":
                        msg = (f"多頭預警\n"
                               f"<b>{symbol}</b> {selected_interval}\n"
                               f"時間 {tw_now.strftime('%m/%d %H:%M')}\n"
                               f"<u>MACD 極強提前金叉訊號</u>\n"
                               f"預計 1~5 根內金叉！")
                        send_telegram(msg)
                        st.session_state.sent_signals[signal_key] = "bull"

                if result["bear_early"]:
                    if st.session_state.sent_signals.get(signal_key) != "bear":
                        msg = (f"空頭預警\n"
                               f"<b>{symbol}</b> {selected_interval}\n"
                               f"時間 {tw_now.strftime('%m/%d %H:%M')}\n"
                               f"<u>MACD 極強提前死叉訊號</u>\n"
                               f"準備反轉向下！")
                        send_telegram(msg)
                        st.session_state.sent_signals[signal_key] = "bear"

            except Exception as e:
                # print(f"[背景] {symbol} 錯誤: {e}")
                continue

        time.sleep(interval)

# 啟動背景執行緒（只啟動一次）
if 'bg_started' not in st.session_state:
    st.session_state.refresh_interval = REFRESH_INTERVAL
    thread = threading.Thread(target=background_monitor, daemon=True)
    thread.start()
    st.session_state.bg_started = True

# 更新刷新間隔
if st.session_state.refresh_interval != REFRESH_INTERVAL:
    st.session_state.refresh_interval = REFRESH_INTERVAL
    st.sidebar.success(f"刷新間隔已更新 → {REFRESH_INTERVAL} 秒")

# ==================== 主畫面顯示 ====================
st.set_page_config(page_title="MACD 極早金死叉監控", layout="wide")
st.title("MACD 極早金死叉即時監控系統 v6.5")

cols = st.columns(3)
for i, symbol in enumerate(SYMBOLS):
    with cols[i % 3]:
        placeholder = st.empty()
        try:
            cache_key = f"{symbol}_{selected_interval}_{selected_period}"
            cached = st.session_state.data_cache.get(cache_key)
            if cached and (datetime.now() - cached["time"]).total_seconds() < 60:
                df = cached["data"]
            else:
                df = yf.download(symbol, period=selected_period, interval=selected_interval,
                                 progress=False, auto_adjust=True, threads=False)
                if not df.empty and len(df) >= 30:
                    with st.session_state.cache_lock:
                        st.session_state.data_cache[cache_key] = {"data": df, "time": datetime.now()}

            if len(df) >= 30:
                macd = macd_early_signal(df)
                fig = plot_macd(symbol, macd, df)
                placeholder.plotly_chart(fig, use_container_width=True)

                if macd["bull_early"]:
                    placeholder.success("極強提前金叉！1~5根內必過")
                elif macd["bear_early"]:
                    placeholder.error("極強提前死叉！準備反轉")
                else:
                    placeholder.info("觀察中")
            else:
                placeholder.warning("資料不足")
        except:
            placeholder.error(f"{symbol} 載入失敗")

st.caption(f"背景監控執行中 • {len(SYMBOLS)} 檔股票 • 刷新 {REFRESH_INTERVAL}s • "
           f"台北時間 {datetime.now(pytz.timezone('Asia/Taipei')).strftime('%Y-%m-%d %H:%M:%S')}")
