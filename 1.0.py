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
    TELEGRAM_CHAT_IDS = st.secrets["telegram"].get("chat_ids",
                           [st.secrets["telegram"]["chat_id"]])
except:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    TELEGRAM_CHAT_IDS = [chat_id] if chat_id else []
    if not TELEGRAM_BOT_TOKEN:
        st.error("請設定 Telegram Bot Token（.streamlit/secrets.toml 或 Streamlit Cloud Secrets）")
        st.stop()

# ==================== 2. 股票與時間框架選項 ====================
SYMBOLS = ["TSLA", "AAPL", "NVDA", "META", "AMD", "SMCI", "COIN", "HOOD", "MARA", "RIOT"]

# 所有 yfinance 支援的 interval 與 period
interval_options = ["1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo", "3mo"]
period_options   = ["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"]

# yfinance 對不同 interval 的最大 period 限制（避免 400 錯誤）
MAX_PERIOD_FOR_INTERVAL = {
    "1m":  "7d",
    "2m":  "60d",
    "5m":  "60d",
    "15m": "60d",
    "30m": "60d",
    "60m": "730d",
    "90m": "730d",
    "1h":  "730d",
    "1d":  "max",
    "5d":  "max",
    "1wk": "max",
    "1mo": "max",
    "3mo": "max"
}

# ==================== 3. 側邊欄選擇器 ====================
st.sidebar.header("即時參數設定")
selected_interval = st.sidebar.selectbox("K線週期 (Interval)", interval_options, index=2)  # 預設 5m
selected_period   = st.sidebar.selectbox("資料範圍 (Period)", period_options, index=1)    # 預設 5d

# 自動修正 Period（若使用者選太大會自動降級）
max_allowed = MAX_PERIOD_FOR_INTERVAL.get(selected_interval.split()[0], "max")
if selected_period not in ["max", "ytd"] and period_options.index(selected_period) > period_options.index(max_allowed):
    st.sidebar.warning(f"{selected_interval} 最多只能抓 {max_allowed}，已自動調整")
    selected_period = max_allowed if max_allowed in period_options else "5d"

REFRESH_SECONDS = 30 if selected_interval in ["1m","2m","5m"] else 60

MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
sent_signals = defaultdict(lambda: {"bull": None, "bear": None})

# ==================== 4. Telegram 發送 ====================
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chat_id in TELEGRAM_CHAT_IDS:
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
        try:
            requests.post(url, data=payload, timeout=10)
        except:
            pass

# ==================== 5. MACD 提前訊號核心 ====================
def macd_early_signal(df):
    close = df['Close']
    ema12 = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema26 = close.ewm(span=MACD_SLOW, adjust=False).mean()
    dif = ema12 - ema26
    signal = dif.ewm(span=MACD_SIGNAL, adjust=False).mean()
    histogram = dif - signal

    if len(dif) < 5:
        return {"bull_early": False, "bear_early": False, "dif": dif, "signal": signal, "histogram": histogram}

    d2, d1, d0 = dif.iloc[-3], dif.iloc[-2], dif.iloc[-1]
    s0 = signal.iloc[-1]
    h2, h1, h0 = histogram.iloc[-3], histogram.iloc[-2], histogram.iloc[-1]

    slope_before = d1 - d2
    slope_now    = d0 - d1
    dif_hook_up   = slope_before <= 0 and slope_now > slope_before * 0.5
    dif_hook_down = slope_before >= 0 and slope_now < slope_before * 0.5

    hist_shrink_red   = h1 > h2 > 0 and h0 < h1 and h0 > 0
    hist_shrink_green = h1 < h2 < 0 and h0 > h1 and h0 < 0

    distance = abs(d0 - s0)
    distance_std = abs(dif - signal).rolling(30).std().iloc[-1]
    very_close = distance_std > 0 and distance < distance_std * 0.4

    bull_early = (dif_hook_up or hist_shrink_green) and very_close and d0 < s0
    bear_early = (dif_hook_down or hist_shrink_red) and very_close and d0 > s0

    return {
        "dif": dif, "signal": signal, "histogram": histogram,
        "bull_early": bull_early, "bear_early": bear_early
    }

# ==================== 6. 繪圖 ====================
def plot_macd(symbol, macd_data, df):
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        subplot_titles=(f"{symbol} 價格", f"MACD 提前訊號 ({selected_interval})"),
                        vertical_spacing=0.05, row_heights=[0.6, 0.4])

    fig.add_trace(go.Candlestick(x=df.index, open=df['Open'], high=df['High'],
                                 low=df['Low'], close=df['Close'], name="K線"), row=1, col=1)

    fig.add_trace(go.Scatter(x=macd_data["dif"].index, y=macd_data["dif"], name="DIF", line=dict(color="#ffaa00")), row=2, col=1)
    fig.add_trace(go.Scatter(x=macd_data["signal"].index, y=macd_data["signal"], name="Signal", line=dict(color="#4169e1")), row=2, col=1)
    colors = ['red' if v <= 0 else 'green' for v in macd_data["histogram"]]
    fig.add_trace(go.Bar(x=macd_data["histogram"].index, y=macd_data["histogram"], name="柱狀體", marker_color=colors), row=2, col=1)

    last_time = df.index[-1]
    if macd_data["bull_early"]:
        fig.add_annotation(x=last_time, y=macd_data["dif"].iloc[-1], text="即將金叉！", showarrow=True,
                           arrowcolor="lime", bgcolor="green", font=dict(color="white"), row=2, col=1)
    if macd_data["bear_early"]:
        fig.add_annotation(x=last_time, y=macd_data["dif"].iloc[-1], text="即將死叉！", showarrow=True,
                           arrowcolor="red", bgcolor="darkred", font=dict(color="white"), row=2, col=1)

    fig.update_layout(height=720, showlegend=False,
                      title=f"{symbol} • {selected_interval} • 更新：{datetime.now(pytz.timezone('Asia/Taipei')).strftime('%H:%M:%S')}")
    return fig

# ==================== 7. 背景監控（跟使用者選擇的 interval 同步）===================
def background_monitor():
    while True:
        current_time = datetime.now(pytz.timezone('Asia/Taipei')).strftime("%H:%M")
        for symbol in SYMBOLS:
            try:
                df = yf.download(symbol, period=selected_period, interval=selected_interval,
                                 progress=False, auto_adjust=True, threads=False)
                if len(df) < MACD_SLOW + 20:
                    continue

                result = macd_early_signal(df)
                key_time = df.index[-1].strftime("%m/%d %H:%M")

                if result["bull_early"] and sent_signals[symbol]["bull"] != key_time:
                    msg = f"多頭訊號\n" \
                          f"<b>{symbol}</b> {selected_interval} 線\n" \
                          f"時間 {current_time}\n" \
                          f"<u>MACD 極強提前金叉</u>\n" \
                          f"預計 1~5 根 K 內金叉！"
                    send_telegram(msg)
                    sent_signals[symbol]["bull"] = key_time

                if result["bear_early"] and sent_signals[symbol]["bear"] != key_time:
                    msg = f"空頭訊號\n" \
                          f"<b>{symbol}</b> {selected_interval} 線\n" \
                          f"時間 {current_time}\n" \
                          f"<u>MACD 極強提前死叉</u>\n" \
                          f"預計 1~5 根 K 內死叉！"
                    send_telegram(msg)
                    sent_signals[symbol]["bear"] = key_time
            except:
                continue
        time.sleep(REFRESH_SECONDS)

# 啟動背景執行緒（只啟動一次）
if 'monitor_started' not in st.session_state:
    thread = threading.Thread(target=background_monitor, daemon=True)
    thread.start()
    st.session_state.monitor_started = True

# ==================== 8. 主畫面 ====================
st.set_page_config(page_title="MACD 多週期提前監控", layout="wide")
st.title("MACD 多週期極早金死叉監控系統")

st.sidebar.success(f"目前週期：{selected_interval}　範圍：{selected_period}")
st.sidebar.info(f"監控 {len(SYMBOLS)} 檔股票\n自動更新：每 {REFRESH_SECONDS} 秒")

# 動態顯示所有股票圖表
cols = st.columns(3)
for i, symbol in enumerate(SYMBOLS):
    with cols[i % 3]:
        placeholder = st.empty()
        with placeholder.container():
            try:
                df = yf.download(symbol, period=selected_period, interval=selected_interval,
                                 progress=False, auto_adjust=True, threads=False)
                if len(df) >= MACD_SLOW + 20:
                    macd = macd_early_signal(df)
                    fig = plot_macd(symbol, macd, df)
                    st.plotly_chart(fig, use_container_width=True)

                    status = "觀察中"
                    if macd["bull_early"]:
                        status = "極強提前金叉！"
                        st.success(status)
                    elif macd["bear_early"]:
                        status = "極強提前死叉！"
                        st.error(status)
                    else:
                        st.info(status)
                else:
                    st.warning("資料不足")
            except Exception as e:
                st.error("載入失敗")
        time.sleep(0.3)

st.caption(f"背景監控執行中 • 目前設定：{selected_interval} / {selected_period} • 自動更新中...")
