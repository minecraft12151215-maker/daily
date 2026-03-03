import discord
from discord.ext import commands, tasks
import yfinance as yf
import pandas as pd
import numpy as np
import datetime
import asyncio
import requests
import os
from dotenv import load_dotenv

# 載入 .env 檔案 (在 Railway 上執行時，這行不會報錯，會自動去抓 Railway 後台的變數)
load_dotenv()

# ================= 絕對必填設定區 =================
# 1. 透過環境變數安全讀取 Token (請在 .env 或 Railway 後台設定 DISCORD_TOKEN)
TOKEN = os.getenv('DISCORD_TOKEN')

# 安全機制：如果抓不到 Token，直接停止執行並報錯
if not TOKEN:
    raise ValueError("❌ 找不到 DISCORD_TOKEN！請確認 .env 檔案或 Railway 環境變數是否已設定。")

# 2. 這裡填入你要發送報告的頻道數字 ID (不要加引號)
target_channel_id = 1475023963334643793

# 自動播報時間設定 (24小時制，台灣時間)
DAILY_REPORT_TIME = "17:00"   
# ==================================================

intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!', intents=intents)

def get_institutional_data():
    """抓取三大法人 (使用最穩定的主網站 API)"""
    url = "https://www.twse.com.tw/fund/BFI82U?response=json"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"}
    import time
    for attempt in range(3):
        try:
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                data = res.json()
                if data.get("stat") != "OK" or "data" not in data:
                    time.sleep(3); continue 
                foreign = trust = dealer = 0
                for row in data["data"]:
                    name = row[0]
                    try: net = int(row[3].replace(",", ""))
                    except: net = 0
                    if "外資" in name and "不含" in name: foreign += net
                    elif "投信" in name: trust += net
                    elif "自營商" in name: dealer += net
                return round(foreign / 100000000, 1), round(trust / 100000000, 1), round(dealer / 100000000, 1)
        except: time.sleep(3)
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
    """自動抓取大盤與櫃買數據並生成分析文字"""
    print("🔄 正在抓取雙引擎大盤(加權+櫃買)與國際資料...")
    # ✨ 新增：抓取櫃買指數 (^TWOII)
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

    # --- 1. 雙引擎比較：加權 vs 櫃買 (NEW!) ---
    pct_tw = ((c_twii['Close'] - p_twii['Close']) / p_twii['Close']) * 100
    pct_otc = ((c_otc['Close'] - p_otc['Close']) / p_otc['Close']) * 100
    
    tw_icon = "🔴" if pct_tw > 0 else "🟢"
    otc_icon = "🔴" if pct_otc > 0 else "🟢"
    
    # AI 動態判斷資金流向
    if pct_otc > pct_tw and pct_otc > 0:
        market_style = "【內資作帳，中小型股活潑】櫃買漲幅勝過大盤，顯示本土資金與主力大戶非常活躍，選股不選市，是進場做多中小型飆股的好時機！"
    elif pct_tw > pct_otc and pct_tw > 0:
        market_style = "【外資控盤，拉抬權值股】大盤漲幅勝過櫃買，資金集中在台積電等大型權值股，中小型股可能面臨資金排擠效應 (拉積盤)。"
    elif pct_otc < 0 and pct_tw < 0:
        market_style = f"【泥沙俱下，系統性風險】大盤與櫃買同步下跌，市場恐慌情緒蔓延，請嚴控資金水位，多看少做。"
    else:
        market_style = "【資金輪動，多空震盪】大盤與櫃買走勢分歧，市場處於資金轉換期，建議挑選強勢族群，縮短操作週期。"

    kline_text = (f"• **加權指數 (大型股)**：`{c_twii['Close']:,.0f}` 點 ({tw_icon} {pct_tw:+.2f}%)\n"
                  f"• **櫃買指數 (中小型)**：`{c_otc['Close']:,.2f}` 點 ({otc_icon} {pct_otc:+.2f}%)\n"
                  f"> 💡 **盤勢研判**：{market_style}")

    # --- 2. 法人籌碼 (修復版) ---
    if foreign is not None:
        total_net = foreign + trust + dealer
        inst_text = (f"今日三大法人合計：**{total_net:+.1f} 億元**\n"
                     f"> • **外資**：`{foreign:+.1f} 億` ｜ **投信**：`{trust:+.1f} 億` ｜ **自營**：`{dealer:+.1f} 億`\n"
                     f"> 籌碼點評：{'外資大舉掃貨，熱錢湧入' if foreign > 50 else '投信土洋對作，內資護盤' if (foreign < 0 and trust > 0) else '外資無情提款，權值股承壓' if foreign < -50 else '法人動作不大，回歸基本面'}。")
    else:
        inst_text = "今日證交所法人數據尚未更新 (或逢假日未開盤)。"

    # --- 3. 大盤技術指標深度分析 ---
    rsi, k, d, macd_hist = c_twii['RSI'], c_twii['K'], c_twii['D'], c_twii['Hist']
    p_macd_hist = p_twii['Hist']
    
    rsi_desc = "⚠️ 過熱警報 (隨時面臨修正)" if rsi > 75 else "🟢 落底反彈 (超賣區浮現買點)" if rsi < 30 else "🟡 中性震盪"
    macd_trend = "紅柱擴大，多頭動能強勁" if macd_hist > 0 and macd_hist > p_macd_hist else "紅柱縮減，多方力竭" if macd_hist > 0 else "綠柱縮減，空方力道衰退" if macd_hist < p_macd_hist else "綠柱擴大，空方主導"
    
    tech_text = (f"• **RSI (14)**：`{rsi:.1f}` ｜ {rsi_desc}\n"
                 f"• **KD (9,3,3)**：K `{k:.1f}` / D `{d:.1f}` ｜ {'高檔鈍化' if k>80 else '低檔金叉' if (k>d and p_twii['K']<p_twii['D']) else '偏多格局' if k>d else '偏空格局'}\n"
                 f"• **MACD 動能**：{macd_trend}")

    # --- 4. 國際總經與情緒 ---
    vix_trend = "下降" if c_vix['Close'] < p_vix['Close'] else "飆升"
    intl_text = (f"昨夜美股 S&P 500 **{'收紅' if c_sp['Close'] > p_sp['Close'] else '收黑'}** (收 {c_sp['Close']:,.0f} 點)。\n"
                 f"華爾街 VIX 恐慌指數目前來到 **{c_vix['Close']:.2f}** ({vix_trend})。\n"
                 f"> 總經視野：{'VIX回落顯示外資避險情緒降溫，有利資金動能' if vix_trend == '下降' else '恐慌情緒升溫，外資可能加速提款'}。")

    # --- 5. 實戰操盤策略 ---
    support = twii['Low'].tail(10).min() 
    resistance = twii['High'].tail(10).max() 
    
    if c_twii['Close'] > c_twii['MA20']:
        adv = "大盤穩居月線之上，屬於多頭格局。配合櫃買強弱，可積極在底部起漲的強勢族群中尋找機會。"
    else:
        adv = "大盤跌破月線，趨勢偏弱。建議提高現金水位，嚴格設定防守價，並以短進短出為主。"
        
    eval_text = (f"🎯 **大盤短線支撐**：`{support:,.0f} 點` ｜ 🎯 **上檔壓力**：`{resistance:,.0f} 點`\n"
                 f"> **操盤建議**：{adv}")

    return kline_text, inst_text, tech_text, intl_text, eval_text

async def send_daily_report(channel):
    msg = await channel.send("📡 **正在彙整大盤、櫃買指數與三大法人籌碼...**")
    data = generate_market_text()
    if not data:
        await msg.edit(content="⚠️ 資料抓取失敗，請檢查 Yahoo Finance 或連線狀態。")
        return
        
    kline, inst, tech, intl, eval_text = data
    
    description = (
        "### ⚖️ 【加權 vs 櫃買：資金板塊解析】\n" + kline + "\n\n"
        "### 💰 【三大法人籌碼動向】\n" + inst + "\n\n"
        "### 🛠️ 【大盤技術指標與位階】\n" + tech + "\n\n"
        "### 🌍 【國際總經與情緒】\n" + intl + "\n\n"
        "### 🎯 【實戰操盤與支撐壓力】\n" + eval_text
    )
    
    # 取得台灣時間作為標題日期
    tw_date = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).date()
    
    embed = discord.Embed(
        title=f"📊 台股雙引擎盤勢深度解析 | {tw_date.strftime('%Y/%m/%d')}",
        description=description,
        color=0xf1c40f 
    )
    embed.set_footer(text="⚡ 由 AI 操盤系統自動生成 ｜ 嚴格執行停損停利，順勢而為")
    
    await msg.edit(content=None, embed=embed)

@tasks.loop(minutes=1)
async def schedule_daily_report():
    # 強制使用台灣時間 (UTC+8) 判斷
    tw_time = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    now = tw_time.strftime("%H:%M")
    
    if now == DAILY_REPORT_TIME and target_channel_id:
        channel = bot.get_channel(target_channel_id)
        if channel:
            await send_daily_report(channel)
            await asyncio.sleep(61) 

@bot.command()
async def report(ctx):
    await send_daily_report(ctx.channel)

@bot.event
async def on_ready():
    print(f'📊 雙引擎大盤分析機器人 {bot.user} 已上線！')
    if target_channel_id:
        channel = bot.get_channel(target_channel_id)
        if channel:
            await channel.send(f"✅ **系統廣播**：頂級法人盤勢分析(含櫃買與大盤對照)已連線！每日 `{DAILY_REPORT_TIME}` 自動發布。")
    if not schedule_daily_report.is_running():
        schedule_daily_report.start()

bot.run(TOKEN)
