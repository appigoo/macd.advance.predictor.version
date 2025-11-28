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

# ==================== 2. 動態股票輸入框 ====================
st.sidebar.header("自訂監控股票")
default_tickers = "TSLA, NVDA, AAPL, META, AMD, SMCI, COIN, HOOD, NIO, XPEV, TSLL, MARA"
input_tickers = st.sidebar.text_input(
    "請輸入股票代號（逗號分隔，支持美股、港股、台股加.TW）",
    value=default_tickers,
    help="例如：TSLA, 0700.HK, 2330.TW, NVDA"
)

# 自動解析並清理輸入
raw_symbols = [s.strip().upper() for s in input_tickers.split(",") if s.strip()]
if not raw_symbols:
    st.error("請至少輸入一檔股票！")
    st.stop()

# 動態股票清單（每次輸入都會即時更新）
SYMBOLS = raw_symbols
st.sidebar.success(f"正在監控 {len(SYMBOLS)} 檔股票：{', '.join(SYMBOLS)}")

# ==================== 3. 週期選擇 ====================
interval_options = ["1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo", "3mo"]
period_options   = ["1d", "5d", "7d", "30d", "60d", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"]

selected_interval = st.sidebar.selectbox("K線週期 (Interval)", interval_options, index=2)  # 預設 5m
selected_period   = st.sidebar.selectbox("資料範圍 (Period)", period_options, index=1)    # 預設 5d

# ==================== 4. 自動修正 Period（永不出錯）===================
def get_valid_period(interval: str, requested: str) -> str:
    limits = {
        "1m": "7d",
        "2m": "60d", "5m": "60d", "15m": "60d", "30m": "60d",
        "60m": "730d", "90m": "730d", "1h": "730d"
    }
    max_allowed = limits.get(interval, "max")
    if max_allowed == "max":
        return requested
    if interval == "1m" and requested not in ["1d", "5d", "7d"]:
        st.sidebar.warning("1分鐘K線最多7天 → 已自動調整")
        return "7d"
    if interval in ["2m", "5m", "15m", "30m"] and requested not in ["1d", "5d", "7d", "30d", "60d"]:
        st.sidebar.warning(f"{interval} 最多60天 → 已自動調整")
        return "60d"
    return requested

selected_period = get_valid_period(selected_interval, selected_period)
REFRESH_SECONDS = 20 if selected_interval in ["1m","2m"] else 40 if selected_interval in ["5m","15m"] else 60

MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
sent_signals = defaultdict(lambda: {"bull": None, "bear": None})

# ==================== 5. Telegram 推送 ====================
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chat_id in TELEGRAM_CHAT_IDS:
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
        try:
            requests.post(url, data=payload, timeout=10)
        except:
            pass

# ==================== 6. MACD 提前訊號核心 ====================
def macd_early_signal(df):
    close = df['Close']
    ema12 = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema26 = close.ewm(span=MACD_SLOW, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=MACD_SIGNAL, adjust=False).mean()
    histogram = dif - dea

    if len(dif) < 5:
        return {"bull_early": False, "bear_early": False, "dif": dif, "signal": dea, "histogram": histogram}

    d2, d1, d0 = dif.iloc[-3], dif.iloc[-2], dif.iloc[-1]
    s0 = dea.iloc[-1]
    h2, h1, h0 = histogram.iloc[-3], histogram.iloc[-2], histogram.iloc[-1]

    hook_up   = (d1 - d2) <= 0 and (d0 - d1) > (d1 - d2) * 0.5
    hook_down = (d1 - d2) >= 0 and (d0 - d1) < (d1 - d2) * 0.5
    shrink_red   = h1 > h2 > 0 and h0 < h1 and h0 > 0
    shrink_green = h1 < h2 < 0 and h0 > h1 and h0 < 0
    distance = abs(d0 - s0)
    std = abs(dif - dea).rolling(30).std().iloc[-1]
    very_close = std > 0 and distance < std * 0.4

    bull_early = (hook_up or shrink_green) and very_close and d0 < s0
    bear_early = (hook_down or shrink_red) and very_close and d0 > s0

    return {"dif": dif, "signal": dea, "histogram": histogram, "bull_early": bull_early, "bear_early": bear_early}

# ==================== 7. 繪圖 ====================
def plot_macd(symbol, macd_data, df):
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        subplot_titles=(f"{symbol} 價格", f"MACD 提前訊號 ({selected_interval})"),
                        vertical_spacing=0.05, row_heights=[0.6, 0.4])

    fig.add_trace(go.Candlestick(x=df.index, open=df['Open'], high=df['High'], low=df['Low'], close=df['Close'], name="K線"), row=1, col=1)
    fig.add_trace(go.Scatter(x=macd_data["dif"].index, y=macd_data["dif"], name="DIF", line=dict(color="#ff9500")), row=2, col=1)
    fig.add_trace(go.Scatter(x=macd_data["signal"].index, y=macd_data["signal"], name="DEA", line=dict(color="#4169e1")), row=2, col=1)
    colors = ['red' if v <= 0 else 'green' for v in macd_data["histogram"]]
    fig.add_trace(go.Bar(x=macd_data["histogram"].index, y=macd_data["histogram"], name="柱狀體", marker_color=colors), row=2, col=1)

    last_time = df.index[-1]
    if macd_data["bull_early"]:
        fig.add_annotation(x=last_time, y=macd_data["dif"].iloc[-1], text="即將金叉！", showarrow=True,
                           arrowcolor="lime", bgcolor="green", font=dict(color="white"), row=2, col=1)
    if macd_data["bear_early"]:
        fig.add_annotation(x=last_time, y=macd_data["dif"].iloc[-1], text="即將死叉！", showarrow=True,
                           arrowcolor="red", bgcolor="darkred", font=dict(color="white"), row=2, col=1)

    fig.update_layout(height=720, showlegend=False, title=f"{symbol} • {selected_interval} • {datetime.now(pytz.timezone('Asia/Taipei')).strftime('%H:%M:%S')}")
    return fig

# ==================== 8. 背景監控（支援動態股票）===================
def background_monitor():
    while True:
        tw_time = datetime.now(pytz.timezone('Asia/Taipei')).strftime("%H:%M")
        for symbol in SYMBOLS:
            try:
                df = yf.download(symbol, period=selected_period, interval=selected_interval,
                                 progress=False, auto_adjust=True, threads=False)
                if len(df) < 50: continue

                result = macd_early_signal(df)
                key = df.index[-1].strftime("%m/%d %H:%M")

                if result["bull_early"] and sent_signals[symbol]["bull"] != key:
                    msg = f"MACD提前金叉\n<b>{symbol}</b> {selected_interval}線\n時間 {tw_time}\n<u>極強多頭訊號</u>\n預計 1~5根內金叉！"
                    send_telegram(msg)
                    sent_signals[symbol]["bull"] = key

                if result["bear_early"] and sent_signals[symbol]["bear"] != key:
                    msg = f"MACD提前死叉\n<b>{symbol}</b> {selected_interval}線\n時間 {tw_time}\n<u>極強空頭訊號</u>\n預計 1~5根內死叉！"
                    send_telegram(msg)
                    sent_signals[symbol]["bear"] = key
            except:
                continue
        time.sleep(REFRESH_SECONDS)

if 'monitor_started' not in st.session_state:
    thread = threading.Thread(target=background_monitor, daemon=True)
    thread.start()
    st.session_state.monitor_started = True

# ==================== 9. 主畫面 ====================
st.set_page_config(page_title="MACD 動態股票監控", layout="wide")
st.title("MACD 極早金死叉即時監控系統（支援自訂股票）")

st.sidebar.success(f"目前週期：{selected_interval} │ 範圍：{selected_period}")
st.sidebar.info(f"自動更新：每 {REFRESH_SECONDS} 秒\n背景推送已啟動")

# 顯示所有股票圖表
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
                        st.success("極強提前金叉訊號！")
                    elif macd["bear_early"]:
                        st.error("極強提前死叉訊號！")
                    else:
                        st.info("觀察中")
                else:
                    st.warning(f"{symbol} 資料不足")
            except Exception as e:
                st.error(f"{symbol} 無法載入")
        time.sleep(0.2)

st.caption(f"背景監控執行中 • 監控 {len(SYMBOLS)} 檔 • {selected_interval}/{selected_period} • 自動更新中...")
