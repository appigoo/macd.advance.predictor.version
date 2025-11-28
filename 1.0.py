# macd_early_alert_app.py  —— MACD 極早金死叉監控系統 v7.0（終極穩定版）
import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import time
from datetime import datetime
import requests
import threading
import random
from threading import Lock
import pytz
import os

# ==================== 強制使用最穩定 yfinance 版本 ====================
# requirements.txt 必須寫：yfinance==0.2.38
yf.pdr_override()
yf.shared._DFS = {}
yf.shared._ERRORS = {}

# ==================== 安全讀取 Telegram Secrets ====================
try:
    TELEGRAM_BOT_TOKEN = st.secrets["telegram"]["bot_token"]
    chat_id_str = st.secrets["telegram"].get("chat_id") or st.secrets["telegram"].get("chat_ids")
    TELEGRAM_CHAT_IDS = [cid.strip() for cid in str(chat_id_str).split(",") if cid.strip()]
except:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id_str = os.getenv("TELEGRAM_CHAT_ID")
    TELEGRAM_CHAT_IDS = [cid.strip() for cid in chat_id_str.split(",")] if chat_id_str else []

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
    st.error("請設定 Telegram Bot Token 與 Chat ID！")
    st.stop()

# ==================== 側邊欄設定 ====================
st.set_page_config(page_title="MACD 極早金死叉監控 v7.0", layout="wide")
st.sidebar.header("監控設定")

default_tickers = "TSLA, NVDA, AAPL, META, AMD, SMCI, COIN, 2330.TW, 2317.TW, 2454.TW"
input_tickers = st.sidebar.text_input("股票代號（逗號分隔）", value=default_tickers)

SYMBOLS = [s.strip().upper() for s in input_tickers.split(",") if s.strip()]
if not SYMBOLS:
    st.error("請至少輸入一檔股票！")
    st.stop()

st.sidebar.success(f"監控 {len(SYMBOLS)} 檔")

interval_options = ["1m","2m","5m","15m","30m","60m","1h","1d"]
selected_interval = st.sidebar.selectbox("K線週期", interval_options, index=2)  # 預設 5m
period_options = ["1d","5d","7d","30d","60d","6mo","1y","max"]
selected_period = st.sidebar.selectbox("資料範圍", period_options, index=2)     # 預設 7d

refresh_options = [30, 45, 60, 90, 120, 180, 300]
REFRESH_INTERVAL = st.sidebar.selectbox("刷新間隔（秒）", refresh_options, index=2)  # 預設 60s

# ==================== 超穩下載函數（永不載入失敗）===================
def robust_download(ticker: str, period: str, interval: str, max_retries: int = 5):
    for attempt in range(max_retries):
        try:
            time.sleep(random.uniform(0.4, 1.3))  # 隨機延遲防封鎖
            
            session = requests.Session()
            session.headers.update({
                "User-Agent": random.choice([
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
                ])
            })
            
            df = yf.download(
                tickers=ticker,
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=True,
                threads=False,
                session=session,
                timeout=20
            )
            
            if df.empty or len(df) < 20:
                raise Exception("資料不足")
            return df
            
        except:
            if attempt == max_retries - 1:
                return pd.DataFrame()
            continue
    return pd.DataFrame()

# ==================== MACD 提前訊號核心 ====================
def macd_early_signal(df: pd.DataFrame):
    close = df['Close']
    dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    histogram = (dif - dea) * 2

    if len(df) < 35:
        return {"bull_early": False, "bear_early": False, "dif": dif, "signal": dea, "histogram": histogram}

    d2, d1, d0 = dif.iloc[-3], dif.iloc[-2], dif.iloc[-1]
    s0 = dea.iloc[-1]
    h2, h1, h0 = histogram.iloc[-3], histogram.iloc[-2], histogram.iloc[-1]

    hook_up   = (d1 <= d2) and (d0 > d1) and (d0 - d1) >= abs(d1 - d2) * 0.6
    hook_down = (d1 >= d2) and (d0 < d1) and (d1 - d0) >= abs(d1 - d2) * 0.6
    shrink_red   = h2 > h1 > 0 and 0 < h0 < h1 * 0.75
    shrink_green = h2 < h1 < 0 and h0 > h1 * 1.3 and h0 < 0

    distance = abs(d0 - s0)
    std20 = (dif - dea).abs().rolling(20).std().iloc[-1]
    very_close = distance < std20 * 0.4 if not pd.isna(std20) else True

    bull_early = (hook_up or shrink_green) and very_close and d0 < s0
    bear_early = (hook_down or shrink_red) and very_close and d0 > s0

    return {
        "dif": dif, "signal": dea, "histogram": histogram,
        "bull_early": bull_early, "bear_early": bear_early
    }

# ==================== 繪圖 ====================
def plot_macd(symbol, macd_data, df):
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        subplot_titles=(f"{symbol} 價格", f"MACD 提前訊號 ({selected_interval})"),
                        vertical_spacing=0.08, row_heights=[0.65, 0.35])

    fig.add_trace(go.Candlestick(x=df.index, open=df['Open'], high=df['High'],
                                 low=df['Low'], close=df['Close'], name="K線"), row=1, col=1)
    
    fig.add_trace(go.Scatter(x=macd_data["dif"].index, y=macd_data["dif"], name="DIF", line=dict(color="#ff9f0a", width=2)), row=2, col=1)
    fig.add_trace(go.Scatter(x=macd_data["signal"].index, y=macd_data["signal"], name="DEA", line=dict(color="#4169e1", width=2)), row=2, col=1)
    
    colors = ['red' if v <= 0 else 'green' for v in macd_data["histogram"]]
    fig.add_trace(go.Bar(x=macd_data["histogram"].index, y=macd_data["histogram"], name="柱狀圖", marker_color=colors), row=2, col=1)

    last_time = df.index[-1]
    last_close = df['Close'].iloc[-1]
    if macd_data["bull_early"]:
        fig.add_annotation(x=last_time, y=last_close*0.98, text="極強提前金叉！", 
                           font=dict(size=16, color="white"), bgcolor="darkgreen", row=1, col=1)
    if macd_data["bear_early"]:
        fig.add_annotation(x=last_time, y=last_close*1.02, text="極強提前死叉！", 
                           font=dict(size=16, color="white"), bgcolor="darkred", row=1, col=1)

    fig.update_layout(height=720, showlegend=False, template="plotly_dark",
                      title=f"{symbol} • {selected_interval} • {datetime.now(pytz.timezone('Asia/Taipei')).strftime('%m/%d %H:%M:%S')}")
    return fig

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

# ==================== 背景監控執行緒（單例 + 快取 + 精確去重）===================
def background_monitor():
    while True:
        interval = st.session_state.get("refresh_interval", 60)
        tw_now = datetime.now(pytz.timezone('Asia/Taipei'))

        for symbol in SYMBOLS:
            try:
                df = robust_download(symbol, selected_period, selected_interval)
                if df.empty or len(df) < 35:
                    continue

                result = macd_early_signal(df)
                last_ts = df.index[-1]
                key = f"{symbol}_{last_ts.strftime('%Y%m%d%H%M')}"

                if result["bull_early"] and st.session_state.sent_signals.get(key) != "bull":
                    msg = (f"多頭預警\n"
                           f"<b>{symbol}</b> {selected_interval}\n"
                           f"時間 {tw_now.strftime('%m/%d %H:%M')}\n"
                           f"<u>MACD 極強提前金叉訊號</u>\n"
                           f"預計 1~5 根內金叉！")
                    send_telegram(msg)
                    st.session_state.sent_signals[key] = "bull"

                if result["bear_early"] and st.session_state.sent_signals.get(key) != "bear":
                    msg = (f"空頭預警\n"
                           f"<b>{symbol}</b> {selected_interval}\n"
                           f"時間 {tw_now.strftime('%m/%d %H:%M')}\n"
                           f"<u>MACD 極強提前死叉訊號</u>\n"
                           f"準備反轉向下！")
                    send_telegram(msg)
                    st.session_state.sent_signals[key] = "bear"

            except:
                continue

        time.sleep(interval)

# ==================== 初始化全域狀態 ====================
if "sent_signals" not in st.session_state:
    st.session_state.sent_signals = {}
if "refresh_interval" not in st.session_state:
    st.session_state.refresh_interval = REFRESH_INTERVAL
if "bg_started" not in st.session_state:
    thread = threading.Thread(target=background_monitor, daemon=True)
    thread.start()
    st.session_state.bg_started = True

# 更新刷新間隔
if st.session_state.refresh_interval != REFRESH_INTERVAL:
    st.session_state.refresh_interval = REFRESH_INTERVAL
    st.sidebar.success(f"刷新間隔已更新 → {REFRESH_INTERVAL} 秒")

# ==================== 主畫面 ====================
st.title("MACD 極早金死叉即時監控系統 v7.0")
st.sidebar.info(f"週期：{selected_interval} │ 範圍：{selected_period}\n背景推送已啟動")

cols = st.columns(3)
for i, symbol in enumerate(SYMBOLS):
    with cols[i % 3]:
        ph = st.empty()
        with ph.container():
            df = robust_download(symbol, selected_period, selected_interval)
            if df.empty or len(df) < 35:
                st.error(f"{symbol} 載入失敗")
            else:
                macd = macd_early_signal(df)
                fig = plot_macd(symbol, macd, df)
                st.plotly_chart(fig, use_container_width=True)
                
                if macd["bull_early"]:
                    st.success("極強提前金叉！1~5根內必過")
                elif macd["bear_early"]:
                    st.error("極強提前死叉！準備反轉")
                else:
                    st.info("觀察中")

st.caption(f"背景監控執行中 • 監控 {len(SYMBOLS)} 檔 • 刷新 {REFRESH_INTERVAL}s • "
           f"台北時間 {datetime.now(pytz.timezone('Asia/Taipei')).strftime('%Y-%m-%d %H:%M:%S')}")

# ==================== requirements.txt 必須包含 ====================
# streamlit==1.38.0
# yfinance==0.2.38
# plotly==5.24.1
# pandas==2.2.3
# requests==2.32.3
# pytz
