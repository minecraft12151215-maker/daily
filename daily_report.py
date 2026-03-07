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
import urllib3

# 隱藏憑證警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

# ================= 絕對必填設定區 =================
TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    raise ValueError("❌ 找不到 DISCORD_TOKEN！請確認 .env 設定。")

TARGET_CHANNEL_ID = 1475023963334643793  
DAILY_REPORT_TIME = "17:00"   
# ==================================================

intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!', intents=intents)

def get_yahoo_quote(ticker):
    """【純血 Yahoo 引擎】直接讀取 Yahoo 專屬報價頁面的網頁標題，保證與畫面 100% 同步"""
    url = f"https://tw.stock.yahoo.com/quote/{ticker}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    try:
        res = requests.get(url, headers=headers, verify=False, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        # 直接抓取網頁的 <title> 標籤 (例如: "櫃買指數 (^TWOII) 306.49 漲跌幅 +0.75% - Yahoo奇摩股市")
        title = soup.find('title').text
        
        price, pct = None, None
        
        # 1. 抓取價格 (尋找括號後面的數字)
        p_match = re.search(r'\)\s*([0-9,]+\.\d+)', title)
        if p_match:
            price = float(p_match.group(1).replace(',', ''))
            
        # 2. 抓取漲跌幅 (尋找 % 前面的數字與符號)
        pct_match = re.search(r'([-+▽△▼▲]*[0-9,]+\.\d+)%', title)
        if pct_match:
            pct_str = pct_match.group(1).replace('▼', '-').replace('▽', '-').replace('▲', '').replace('△', '').replace('+', '')
            pct = float(pct_str)
            
        if price is not None and pct is not None:
            return price, pct
            
    except Exception as e:
        print(f"Yahoo {ticker} 抓取失敗: {e}")
        
    return None, None

def get_institutional_data():
    """【純血 Yahoo 引擎】從 Yahoo 股市抓取三大法人買賣超"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        url = "https://tw.stock.yahoo.com/institutional-trading"
        res = requests.get(url, headers=headers, verify=False, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        strings = list(soup.stripped_strings)
        
        for i in range(len(strings) - 4):
            if strings[i] == "日期" and "外資" in strings[i+1] and "投信" in strings[i+2] and "自營商" in strings[i+3]:
                for j in range(i+4, i+20):
                    if re.match(r'^\d{4}/\d{2}/\d{2}$', strings[j]):
                        try:
                            f_val = float(strings[j+1].replace(',', '').replace('+', '').replace('億', '').strip())
                            t_val = float(strings[j+2].replace(',', '').replace('+', '').replace('億', '').strip())
                            d_val = float(strings[j+3].replace(',', '').replace('+', '').replace('億', '').strip())
                            return round(f_val, 2), round(t_val, 2), round(d_val, 2)
                        except ValueError:
                            break
    except Exception as e:
        print(f"Yahoo 法人資料抓取失敗: {e}")

    return None, None, None

def calculate_technical_indicators(df):
    if df.empty or len(df) < 20: 
        for col in ['RSI', 'K', 'D', 'MACD', 'Signal', 'Hist', 'MA20']:
            df[col] = 50.0 if col in ['RSI', 'K', 'D'] else df.get('Close', 0)
        return df 
    
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss.replace(0, 0.0001) 
    df['RSI'] = 100 - (100 / (1 + rs))
    df['RSI'] = df['RSI'].fillna(50)

    low_min = df['Low'].rolling(window=9).min()
    high_max = df['High'].rolling(window=9).max()
    denom = (high_max - low_min).replace(0, 0.0001)
    df['RSV'] = 100 * ((df['Close'] - low_min) / denom)
    df['K'] = df['RSV'].ewm(com=2, adjust=False).mean().fillna(50)
    df['D'] = df['K'].ewm(com=2, adjust=False).mean().fillna(50)
    
    exp1 = df['Close'].ewm(span=12, adjust=False).mean()
    exp2 = df['Close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = exp1 - exp2
    df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    df['Hist'] = df['MACD'] - df['Signal'] 
    
    df['MA20'] = df['Close'].rolling(window=20).mean()
    return df

def generate_market_text():
    print("🔄 正在透過純 Yahoo 引擎抓取資料...")
    
    twii = yf.Ticker("^TWII").history(period="3mo")
    sp500 = yf.Ticker("^GSPC").history(period="1mo")
    vix = yf.Ticker("^VIX").history(period="1mo")
    
    foreign, trust, dealer = get_institutional_data()

    if twii.empty or sp500.empty or vix.empty: 
        return None

    # === 👉 關鍵修正：完全使用 Yahoo 抓取最新指數，徹底捨棄其他 API 與防呆的 0.00 ===
    twii_rt_price, twii_rt_pct = get_yahoo_quote("^TWII")
    otc_rt_price, otc_rt_pct = get_yahoo_quote("^TWOII")

    # 如果抓不到，終止回傳，避免顯示錯誤的數字
    if twii_rt_price is None or otc_rt_price is None:
        return None

    # 將最新的精準價格塞進歷史資料表，確保計算技術指標時是準確的
    twii.loc[twii.index[-1], 'Close'] = twii_rt_price
    twii.loc[twii.index[-1], 'High'] = max(twii.loc[twii.index[-1], 'High'], twii_rt_price)
    twii.loc[twii.index[-1], 'Low'] = min(twii.loc[twii.index[-1], 'Low'], twii_rt_price)

    twii = calculate_technical_indicators(twii)
    
    c_sp, p_sp = sp500.iloc[-1], sp500.iloc[-2]
    c_vix, p_vix = vix.iloc[-1], vix.iloc[-2]

    c_tw_price = twii_rt_price
    pct_tw = twii_rt_pct

    c_otc_price = otc_rt_price
    pct_otc = otc_rt_pct

    # --- 1. 加權 vs 櫃買 ---
    tw_icon = "🔴" if pct_tw > 0 else "🟢"
    otc_icon = "🔴" if pct_otc > 0 else "🟢"
    
    if pct_otc > pct_tw and pct_otc > 0:
        market_style = "【內資作帳，中小型股活潑】櫃買漲幅勝過大盤，顯示本土資金與主力大戶非常活躍，是進場做多中小型飆股的好時機！"
    elif pct_tw > pct_otc and pct_tw > 0:
        market_style = "【外資控盤，拉抬權值股】大盤漲幅勝過櫃買，資金集中在台積電等大型權值股，中小型股可能面臨資金排擠效應 (拉積盤)。"
    elif pct_otc < 0 and pct_tw < 0:
        market_style = "【泥沙俱下，系統性風險】大盤與櫃買同步下跌，市場恐慌情緒蔓延，請嚴控資金水位，多看少做。"
    else:
        market_style = "【資金輪動，多空震盪】大盤與櫃買走勢分歧，市場處於資金轉換期，建議挑選強勢族群，縮短操作週期。"

    kline_text = (f"• **加權指數 (大型股)**：`{c_tw_price:,.0f}` 點 ({tw_icon} {pct_tw:+.2f}%)\n"
                  f"• **櫃買指數 (中小型)**：`{c_otc_price:,.2f}` 點 ({otc_icon} {pct_otc:+.2f}%)\n"
                  f"> 💡 **盤勢研判**：{market_style}")

    # --- 2. 三大法人 ---
    if foreign is not None:
        total_net = foreign + trust + dealer
        inst_text = (f"今日三大法人合計：**{total_net:+.2f} 億元**\n"
                     f"> • **外資**：`{foreign:+.2f} 億` ｜ **投信**：`{trust:+.2f} 億` ｜ **自營**：`{dealer:+.2f} 億`\n"
                     f"> 籌碼點評：{'外資大舉掃貨，熱錢湧入' if foreign > 50 else '投信土洋對作，內資護盤' if (foreign < 0 and trust > 0) else '外資無情提款，權值股承壓' if foreign < -50 else '法人動作不大，回歸基本面'}。")
    else:
        inst_text = "今日法人數據尚未更新 (或逢假日休市)。"

    # --- 3. 大盤技術指標 ---
    rsi = twii.iloc[-1].get('RSI', 50.0)
    k = twii.iloc[-1].get('K', 50.0)
    d = twii.iloc[-1].get('D', 50.0)
    macd_hist = twii.iloc[-1].get('Hist', 0.0)
    p_macd_hist = twii.iloc[-2].get('Hist', 0.0)
    
    rsi_desc = "⚠️ 過熱警報 (隨時面臨修正)" if rsi > 75 else "🟢 落底反彈 (超賣區浮現買點)" if rsi < 30 else "🟡 中性震盪"
    macd_trend = "紅柱擴大，多頭動能強勁" if macd_hist > 0 and macd_hist > p_macd_hist else "紅柱縮減，多方力竭" if macd_hist > 0 else "綠柱縮減，空方力道衰退" if macd_hist < p_macd_hist else "綠柱擴大，空方主導"
    
    tech_text = (f"• **RSI (14)**：`{rsi:.1f}` ｜ {rsi_desc}\n"
                 f"• **KD (9,3,3)**：K `{k:.1f}` / D `{d:.1f}` ｜ {'高檔鈍化' if k>80 else '低檔金叉' if (k>d and twii.iloc[-2].get('K', 50)<twii.iloc[-2].get('D', 50)) else '偏多格局' if k>d else '偏空格局'}\n"
                 f"• **MACD 動能**：{macd_trend}")

    # --- 4. 國際總經與情緒 ---
    vix_trend = "下降" if c_vix['Close'] < p_vix['Close'] else "飆升"
    intl_text = (f"昨夜美股 S&P 500 **{'收紅' if c_sp['Close'] > p_sp['Close'] else '收黑'}** (收 {c_sp['Close']:,.0f} 點)。\n"
                 f"華爾街 VIX 恐慌指數目前來到 **{c_vix['Close']:.2f}** ({vix_trend})。\n"
                 f"> 總經視野：{'VIX回落顯示外資避險情緒降溫，有利資金動能' if vix_trend == '下降' else '恐慌情緒升溫，外資可能加速提款'}。")

    # --- 5. 實戰操盤策略 ---
    support = twii['Low'].tail(10).min() 
    resistance = twii['High'].tail(10).max() 
    ma20 = twii.iloc[-1].get('MA20', c_tw_price)
    
    if c_tw_price > ma20:
        adv = "大盤穩居月線之上，屬於多頭格局。配合櫃買強弱，可積極在底部起漲的強勢族群中尋找機會。"
    else:
        adv = "大盤跌破月線，趨勢偏弱。建議提高現金水位，嚴格設定防守價，並以短進短出為主。"
        
    eval_text = (f"🎯 **大盤短線支撐**：`{support:,.0f} 點` ｜ 🎯 **上檔壓力**：`{resistance:,.0f} 點`\n"
                 f"> **操盤建議**：{adv}")

    return kline_text, inst_text, tech_text, intl_text, eval_text

async def send_daily_report(channel):
    msg = await channel.send("📡 **正在彙整大盤、櫃買指數與三大法人籌碼...**")
    data = await asyncio.to_thread(generate_market_text) 
    if not data:
        await msg.edit(content="⚠️ Yahoo 資料抓取失敗，請稍後再試。")
        return
        
    kline, inst, tech, intl, eval_text = data
    
    description = (
        "### ⚖️ 【加權 vs 櫃買：資金板塊解析】\n" + kline + "\n\n"
        "### 💰 【三大法人籌碼動向】\n" + inst + "\n\n"
        "### 🛠️ 【大盤技術指標與位階】\n" + tech + "\n\n"
        "### 🌍 【國際總經與情緒】\n" + intl + "\n\n"
        "### 🎯 【實戰操盤與支撐壓力】\n" + eval_text
    )
    
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
    tw_time = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    if tw_time.strftime("%H:%M") == DAILY_REPORT_TIME and tw_time.weekday() < 5:
        channel = bot.get_channel(TARGET_CHANNEL_ID)
        if channel:
            await send_daily_report(channel)
        await asyncio.sleep(61) 

@bot.command()
async def report(ctx):
    await send_daily_report(ctx.channel)

@bot.event
async def on_ready():
    print(f'📊 大盤分析機器人 {bot.user} 已上線！(搭載純血 Yahoo 標題解析器)')
    if not schedule_daily_report.is_running():
        schedule_daily_report.start()

bot.run(TOKEN)
