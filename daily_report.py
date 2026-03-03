import discord
from discord.ext import commands, tasks
import yfinance as yf
import pandas as pd
import numpy as np
import datetime
import asyncio
import requests
import os
import re
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# 載入 .env 檔案 (在 Railway 上執行時，這行不會報錯，會自動去抓 Railway 後台的變數)
load_dotenv()

# ================= 絕對必填設定區 =================
# 1. 透過環境變數安全讀取 Token (請在 .env 或 Railway 後台設定 DISCORD_TOKEN)
TOKEN = os.getenv('DISCORD_TOKEN')

# 安全機制：如果抓不到 Token，直接停止執行並報錯
if not TOKEN:
    raise ValueError("❌ 找不到 DISCORD_TOKEN！請確認 .env 檔案或 Railway 環境變數是否已設定。")

# 2. 這裡填入你要發送報告的頻道數字 ID
target_channel_id = 1475023963334643793

# 自動播報時間設定 (24小時制，台灣時間)
DAILY_REPORT_TIME = "17:00"   
# ==================================================

intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!', intents=intents)

def get_institutional_data():
    """修正版：精準定位 Yahoo 股市法人買賣超表格，防止數據錯置"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    }
    url = "https://tw.stock.yahoo.com/institutional-trading"
    
    try:
        res = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        # 抓取表格中所有的行 (Yahoo 股市的特定 CSS 結構)
        rows = soup.select('ul.table-body-wrapper li')
        
        results = {"外資": None, "投信": None, "自營商": None}
        
        for row in rows:
            cols = row.select('div')
            if len(cols) < 4: continue
            
            name = cols[0].get_text(strip=True)
            # 抓取第四個欄位：買賣超金額 (單位：億)
            net_value_str = cols[3].get_text(strip=True).replace(',', '').replace('億', '')
            
            try:
                # 處理帶有正負號的字串
                net_value = float(net_value_str)
                if "外資" in name: 
                    results["外資"] = net_value
                elif "投信" in name: 
                    results["投信"] = net_value
                elif "自營商" in name: 
                    # Yahoo 會分開列出自營商(自行)與(避險)，這裡將其加總
                    if results["自營商"] is None: results["自營商"] = 0.0
                    results["自營商"] += net_value
            except ValueError:
                continue

        if any(v is not None for v in results.values()):
            # 若自營商仍為 None 則給 0
            f = results["外資"] if results["外資"] is not None else 0.0
            t = results["投信"] if results["投信"] is not None else 0.0
            d = results["自營商"] if results["自營商"] is not None else 0.0
            return round(f, 1), round(t, 1), round(d, 1)
            
    except Exception as e:
        print(f"Yahoo 籌碼抓取失敗: {e}")
    
    return None, None, None

def calculate_technical_indicators(df):
    """計算 RSI, KD, MACD"""
    if len(df) < 14: return df
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    df['RSI'] = 100 - (100 / (1 + gain / loss))

    low_min = df['Low'].rolling(window=9).min()
    high_max = df['High'].rolling(window=9).max()
    df['RSV'] = 100 * ((df['Close'] - low_min) / (high_max - low_min))
    df['K'] = df['RSV'].ewm(com=2, adjust=False).mean()
    df['D'] = df['K'].ewm(com=2, adjust=False).mean()
    
    exp1 = df['Close'].ewm(span=12, adjust=False).mean()
    exp2 = df['Close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = exp1 - exp2
    df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    df['Hist'] = df['MACD'] - df['Signal'] 
    
    df['MA5'] = df['Close'].rolling(window=5).mean()
    df['MA10'] = df['Close'].rolling(window=10).mean()
    df['MA20'] = df['Close'].rolling(window=20).mean()
    return df

def generate_market_text():
    """自動抓取大盤數據並生成分析文字"""
    twii = yf.Ticker("^TWII").history(period="3mo")
    otc = yf.Ticker("^TWOII").history(period="3mo")
    sp500 = yf.Ticker("^GSPC").history(period="1mo")
    vix = yf.Ticker("^VIX").history(period="1mo")
    foreign, trust, dealer = get_institutional_data()

    if twii.empty or otc.empty or sp500.empty or vix.empty: return None

    twii = calculate_technical_indicators(twii)
    otc = calculate_technical_indicators(otc)
    
    c_twii, p_twii = twii.iloc[-1], twii.iloc[-2]
    c_otc, p_otc = otc.iloc[-1], otc.iloc[-2]
    c_sp, p_sp = sp500.iloc[-1], sp500.iloc[-2]
    c_vix, p_vix = vix.iloc[-1], vix.iloc[-2]

    pct_tw = ((c_twii['Close'] - p_twii['Close']) / p_twii['Close']) * 100
    pct_otc = ((c_otc['Close'] - p_otc['Close']) / p_otc['Close']) * 100
    
    tw_icon = "🔴" if pct_tw > 0 else "🟢"
    otc_icon = "🔴" if pct_otc > 0 else "🟢"
    
    if pct_otc > pct_tw and pct_otc > 0:
        market_style = "【內資作帳，中小型股活潑】櫃買漲幅勝過大盤，本土資金活躍。"
    elif pct_tw > pct_otc and pct_tw > 0:
        market_style = "【外資控盤，拉抬權值股】大盤漲幅勝過櫃買，資金集中在大型股。"
    else:
        market_style = "【多空震盪】市場處於資金轉換期，建議挑選強勢族群。"

    kline_text = (f"• **加權指數**：`{c_twii['Close']:,.0f}` ({tw_icon} {pct_tw:+.2f}%)\n"
                  f"• **櫃買指數**：`{c_otc['Close']:,.2f}` ({otc_icon} {pct_otc:+.2f}%)\n"
                  f"> 💡 **盤勢研判**：{market_style}")

    if foreign is not None:
        total_net = foreign + trust + dealer
        inst_text = (f"今日三大法人合計：**{total_net:+.1f} 億元**\n"
                     f"> • **外資**：`{foreign:+.1f} 億` ｜ **投信**：`{trust:+.1f} 億` ｜ **自營**：`{dealer:+.1f} 億`")
    else:
        inst_text = "今日法人數據尚未更新 (或逢假日休市)。"

    rsi, k, d = c_twii['RSI'], c_twii['K'], c_twii['D']
    tech_text = (f"• **RSI (14)**：`{rsi:.1f}`\n"
                 f"• **KD (9,3,3)**：K `{k:.1f}` / D `{d:.1f}`")

    intl_text = f"昨夜美股 S&P 500 **{'收紅' if c_sp['Close'] > p_sp['Close'] else '收黑'}**，VIX 指數為 `{c_vix['Close']:.2f}`。"

    return kline_text, inst_text, tech_text, intl_text

async def send_daily_report(channel):
    msg = await channel.send("📡 **正在彙整大盤與精準籌碼數據...**")
    data = await asyncio.to_thread(generate_market_text) 
    if not data:
        await msg.edit(content="⚠️ 資料抓取失敗。")
        return
        
    kline, inst, tech, intl = data
    tw_date = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).date()
    
    embed = discord.Embed(
        title=f"📊 台股雙引擎盤勢解析 | {tw_date.strftime('%Y/%m/%d')}",
        description=f"### ⚖️ 【資金板塊】\n{kline}\n\n### 💰 【法人籌碼】\n{inst}\n\n### 🛠️ 【技術指標】\n{tech}\n\n### 🌍 【國際情緒】\n{intl}",
        color=0xf1c40f 
    )
    embed.set_footer(text="⚡ AI 操盤系統自動生成")
    await msg.edit(content=None, embed=embed)

@tasks.loop(minutes=1)
async def schedule_daily_report():
    tw_time = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    now = tw_time.strftime("%H:%M")
    if now == DAILY_REPORT_TIME and target_channel_id:
        if tw_time.weekday() < 5: # 週一至週五執行
            channel = bot.get_channel(target_channel_id)
            if channel: await send_daily_report(channel)
        await asyncio.sleep(61) 

@bot.command()
async def report(ctx):
    await send_daily_report(ctx.channel)

@bot.event
async def on_ready():
    print(f'📊 機器人 {bot.user} 已上線！')
    if not schedule_daily_report.is_running():
        schedule_daily_report.start()

bot.run(TOKEN)
