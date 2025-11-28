# 文件名：macd_early_alert_app.py
import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import time
from datetime import datetime
import requests
import threading
from collections import defaultdict
import pytz
import os

# ==================== 1. 安全讀取 Telegram Secrets ====================
try:
    TELEGRAM_BOT_TOKEN = st.secrets["telegram"]["bot_token"]
    TELEGRAM_CHAT_IDS = st.secrets["telegram"].get("chat_ids", [st.secrets["telegram"]["chat_id"]])
except:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    TELEGRAM_CHAT_IDS = [chat_id] if chat_id else []
    if not TELEGRAM_BOT_TOKEN:
        st.error("請設定 Telegram Bot Token（.streamlit/secrets.toml 或 Streamlit Cloud Secrets）")
        st.stop()

# ==================== 2. 側邊欄：動態股票 + 週期 + 刷新間隔 ====================
st.sidebar.header("自訂監控股票")
default_tickers = "TSLA, NVDA, AAPL, META, AMD, SMCI, COIN, NIO, XPEV, TSLL"
input_tickers = st.sidebar.text_input(
    "輸入股票代號（逗號分隔，支持 .TW .HK）",
    value=default_tickers,
    help="範例：2330.TW, 0700.HK, TSLA, NVDA"
)

# 解析股票
raw_symbols = [s.strip().upper() for s in input_tickers.split(",") if s.strip()]
if not raw_symbols:
    st.error("請至少輸入一檔股票！")
    st.stop()
SYMBOLS = raw_symbols
st.sidebar.success(f"監控中：{len(SYMBOLS)} 檔 → {', '.join(SYMBOLS[:8])}{'...' if len(SYMBOLS)>8 else ''}")

# 週期選擇
interval_options = ["1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo", "3mo"]
period_options   = ["1d", "5d", "7d", "30d", "60d", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"]

selected_interval = st.sidebar.selectbox("K線週期", interval_options, index=2)
selected_period   = st.sidebar.selectbox("資料範圍", period_options, index=1)

# 加入你要求的「自訂刷新間隔」
refresh_options = [10, 20, 30, 45, 60, 90, 120, 180, 300, 600]  # 10秒 到 10分鐘
REFRESH_INTERVAL = st.sidebar.selectbox(
    "選擇刷新間隔 (秒)",
    options=refresh_options,
    index=refresh_options.index(60),  # 預設 60 秒（超穩又即時）
    help="建議 1m/2m 線用 20~30 秒，5m 以上用 60 秒最順"
)

# 自動修正 Period（防 yfinance 400 錯誤）
def get_valid_period(interval: str, requested: str) -> str:
    limits = {"1m":"7d", "2m":"60d", "5m":"60d", "15m":"60d", "30m":"60d",
              "60m":"730d", "90m":"730d", "1h":"730d"}
    max_p = limits.get(interval, "max")
    if max_p == "max": return requested
    if interval == "1m" and requested not in ["1d","5d","7d"]:
        st.sidebar.warning("1m 最多7天 → 自動調整")
        return "7d"
    if interval in ["2m","5m","15m","30m"] and requested not in ["1d","5d","7d","30d","60d"]:
        st.sidebar.warning(f"{interval} 最多60天 → 自動調整")
        return "60d"
    return requested

selected_period = get_valid_period(selected_interval, selected_period)

# 全域變數：讓背景執行緒也能讀到最新的刷新間隔
if 'current_refresh' not in st.session_state:
    st.session_state.current_refresh = REFRESH_INTERVAL

# 當使用者改間隔時，即時更新
if st.session_state.current_refresh != REFRESH_INTERVAL:
    st.session_state.current_refresh = REFRESH_INTERVAL
    st.sidebar.success(f"刷新間隔已更新為 {REFRESH_INTERVAL} 秒")

MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
sent_signals = defaultdict(lambda: {"bull": None, "bear": None})

# ==================== 3. Telegram 推送 ====================
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            requests.post(url, data={"chat_id": chat_id, "text": message,
                                   "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=10)
        except:
            pass

# ==================== 4. MACD 提前訊號核心 ====================
def macd_early_signal(df):
    close = df['Close']
    dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    histogram = (dif - dea) * 2

    if len(dif) < 5:
        return {"bull_early": False, "bear_early": False, "dif": dif, "signal": dea, "histogram": histogram}

    d2, d1, d0 = dif.iloc[-3], dif.iloc[-2], dif.iloc[-1]
    s0 = dea.iloc[-1]
    h2, h1, h0 = histogram.iloc[-3], histogram.iloc[-2], histogram.iloc[-1]

    hook_up   = (d1 - d2) <= 0 and (d0 - d1) > (d1 - d2) * 0.6
    hook_down = (d1 - d2) >= 0 and (d0 - d1) < (d1 - d2) * 0.6
    shrink_red   = h1 > h2 > 0 and h0 < h1 and h0 > 0
    shrink_green = h1 < h2 < 0 and h0 > h1 and h0 < 0
    distance = abs(d0 - s0)
    very_close = distance < abs(dif - dea).rolling(20).std().iloc[-1] * 0.4

    bull_early = (hook_up or shrink_green) and very_close and d0 < s0
    bear_early = (hook_down or shrink_red) and very_close and d0 > s0

    return {"dif": dif, "signal": dea, "histogram": histogram,
            "bull_early": bull_early, "bear_early": bear_early}

# ==================== 5. 繪圖 ====================
def plot_macd(symbol, macd_data, df):
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        subplot_titles=(f"{symbol} 價格", f"MACD 提前訊號 ({selected_interval})"),
                        vertical_spacing=0.05, row_heights=[0.6, 0.4])

    fig.add_trace(go.Candlestick(x=df.index, open=df['Open'], high=df['High'],
                                 low=df['Low'], close=df['Close'], name="K線"), row=1, col=1)
    fig.add_trace(go.Scatter(x=macd_data["dif"].index, y=macd_data["dif"], name="DIF", line=dict(color="#ff9f0a")), row=2, col=1)
    fig.add_trace(go.Scatter(x=macd_data["signal"].index, y=macd_data["signal"], name="DEA", line=dict(color="#4169e1")), row=2, col=1)
    colors = ['red' if v <= 0 else 'green' for v in macd_data["histogram"]]
    fig.add_trace(go.Bar(x=macd_data["histogram"].index, y=macd_data["histogram"], name="柱狀體", marker_color=colors), row=2, col=1)

    last_time = df.index[-1]
    if macd_data["bull_early"]:
        fig.add_annotation(x=last_time, y=macd_data["dif"].iloc[-1], text="即將金叉！", showarrow=True,
                           arrowcolor="lime", bgcolor="darkgreen", font=dict(color="white"), row=2, col=1)
    if macd_data["bear_early"]:
        fig.add_annotation(x=last_time, y=macd_data["dif"].iloc[-1], text="即將死叉！", showarrow=True,
                           arrowcolor="red", bgcolor="darkred", font=dict(color="white"), row=2, col=1)

    fig.update_layout(height=720, showlegend=False,
                      title=f"{symbol} • {selected_interval} • {datetime.now(pytz.timezone('Asia/Taipei')).strftime('%H:%M:%S')}")
    return fig

# ==================== 6. 背景監控執行緒（即時讀取最新刷新間隔）===================
def background_monitor():
    while True:
        current_interval = st.session_state.current_refresh
        tw_time = datetime.now(pytz.timezone('Asia/Taipei')).strftime("%H:%M")
        for symbol in SYMBOLS:
            try:
                df = yf.download(symbol, period=selected_period, interval=selected_interval,
                                 progress=False, auto_adjust=True, threads=False)
                if len(df) < 50: continue

                result = macd_early_signal(df)
                key = df.index[-1].strftime("%m/%d %H:%M")

                if result["bull_early"] and sent_signals[symbol]["bull"] != key:
                    msg = f"多頭訊號\n<b>{symbol}</b> {selected_interval}\n時間 {tw_time}\n<u>MACD 極強提前金叉</u>\n1~5根內必過！"
                    send_telegram(msg)
                    sent_signals[symbol]["bull"] = key

                if result["bear_early"] and sent_signals[symbol]["bear"] != key:
                    msg = f"空頭訊號\n<b>{symbol}</b> {selected_interval}\n時間 {tw_time}\n<u>MACD 極強提前死叉</u>\n準備反轉！"
                    send_telegram(msg)
                    sent_signals[symbol]["bear"] = key
            except:
                continue
        time.sleep(current_interval)

# 啟動背景執行緒（只啟動一次）
if 'monitor_started' not in st.session_state:
    thread = threading.Thread(target=background_monitor, daemon=True)
    thread.start()
    st.session_state.monitor_started = True

# ==================== 7. 主畫面 ====================
st.set_page_config(page_title="MACD 專業監控系統", layout="wide")
st.title("MACD 極早金死叉即時監控系統 v6.0")

st.sidebar.success(f"當前刷新：{REFRESH_INTERVAL} 秒")
st.sidebar.info(f"週期：{selected_interval} │ 範圍：{selected_period}\n背景推送已啟動")

cols = st.columns(3)
for i, symbol in enumerate(SYMBOLS):
    with cols[i % 3]:
        ph = st.empty()
        with ph.container():
            try:
                df = yf.download(symbol, period=selected_period, interval=selected_interval,
                                 progress=False, auto_adjust=True, threads=False)
                if len(df) >= 50:
                    macd = macd_early_signal(df)
                    fig = plot_macd(symbol, macd, df)
                    st.plotly_chart(fig, use_container_width=True)
                    if macd["bull_early"]:
                        st.success("極強提前金叉！")
                    elif macd["bear_early"]:
                        st.error("極強提前死叉！")
                    else:
                        st.info("觀察中")
                else:
                    st.warning("資料載入中...")
            except:
                st.error(f"{symbol} 無法取得")
        time.sleep(0.2)

st.caption(f"背景監控執行中 • 監控 {len(SYMBOLS)} 檔 • 刷新間隔 {REFRESH_INTERVAL} 秒 • 永不中斷")
