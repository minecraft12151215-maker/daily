import discord
from discord.ext import commands, tasks
import pandas as pd
import numpy as np
import datetime
import asyncio
import requests
import os
import re
from dotenv import load_dotenv
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    raise ValueError("❌ 找不到 DISCORD_TOKEN！")

TARGET_CHANNEL_ID = 1475023963334643793  
DAILY_REPORT_TIME = "17:00"   

intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!', intents=intents)

def get_realtime_indices():
    """【防卡死官方引擎】直連證交所/櫃買中心，手動計算 Yahoo 同款精準漲跌幅"""
    results = {"twii": (None, None), "otc": (None, None)}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    # 1. 大盤：台灣證交所官方 API
    try:
        res = requests.get("https://www.twse.com.tw/rwd/zh/TAIEX/MI_INDEX?response=json&type=IND", headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data.get("stat") == "OK":
                for row in data.get("data", []):
                    if row[0] == "發行量加權股價指數":
                        p = float(row[1].replace(',', ''))
                        change_str = row[3].replace(',', '')
                        change = float(change_str)
                        sign = -1 if "-" in row[2] else 1
                        actual_change = change * sign
                        prev = p - actual_change
                        pct = (actual_change / prev) * 100 if prev > 0 else 0
                        results["twii"] = (p, pct)
                        break
    except: pass

    # 2. 櫃買：台灣櫃買中心官方 API
    try:
        res = requests.get("https://www.tpex.org.tw/web/stock/aftertrading/index_summary/summary_result.php?l=zh-tw", headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            for row in data.get('aaData', []):
                name = re.sub(r'<[^>]+>', '', str(row[0])).strip()
                if name == '櫃買指數':
                    p = float(re.sub(r'<[^>]+>', '', str(row[1])).replace(',', ''))
                    change_str = re.sub(r'<[^>]+>', '', str(row[2])).replace(',', '')
                    change = float(change_str)
                    sign = -1 if "-" in change_str else 1
                    actual_change = abs(change) * sign
                    prev = p - actual_change
                    pct = (actual_change / prev) * 100 if prev > 0 else 0
                    results["otc"] = (p, pct)
                    break
    except: pass

    return results

def get_institutional_data():
    """三大法人：台灣證交所官方 RWD API (穩定抓取 -154.93億 的版本)"""
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        url = "https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json"
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data.get('stat') == 'OK':
                f_val = t_val = d_val = 0.0
                for row in data['data']:
                    name = row[0]
                    diff = float(row[3].replace(',', '')) / 100000000.0
                    if "外資及陸資" in name or "外資自營商" in name: f_val += diff
                    elif "投信" in name: t_val += diff
                    elif "自營商" in name: d_val += diff
                return round(f_val, 2), round(t_val, 2), round(d_val, 2)
    except: pass
    
    return None, None, None

def get_historical_data(symbol):
    """自建歷史資料抓取器，徹底取代會卡死當機的 yfinance"""
    df = pd.DataFrame()
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?range=3mo&interval=1d"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res.status_code == 200:
            data = res.json()['chart']['result'][0]
            quotes = data['indicators']['quote'][0]
            df = pd.DataFrame({'Close': quotes['close'], 'High': quotes['high'], 'Low': quotes['low']})
            df = df.dropna().reset_index(drop=True)
    except: pass
    return df

def get_us_index(symbol):
    """美股報價專用，不依賴 yfinance"""
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res.status_code == 200:
            quotes = res.json()['chart']['result'][0]['indicators']['quote'][0]['close']
            quotes = [q for q in quotes if q is not None]
            if len(quotes) >= 2: return quotes[-1], quotes[-2]
            elif len(quotes) == 1: return quotes[-1], quotes[-1]
    except: pass
    return 0, 0

def calculate_technical_indicators(df):
    if df.empty or len(df) < 20: 
        for col in ['RSI', 'K', 'D', 'MACD', 'Signal', 'Hist', 'MA20']:
            df[col] = 50.0 if col in ['RSI', 'K', 'D'] else (df['Close'].iloc[-1] if not df.empty else 0.0)
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
    # 🔥 以 5 秒嚴格超時限制抓取，保證機器人絕對不卡死
    twii = get_historical_data("^TWII")
    c_sp_price, p_sp_price = get_us_index("^GSPC")
    c_vix_price, p_vix_price = get_us_index("^VIX")

    rt_indices = get_realtime_indices()
    twii_rt_price, twii_rt_pct = rt_indices["twii"]
    otc_rt_price, otc_rt_pct = rt_indices["otc"]

    if twii_rt_price is None: twii_rt_price, twii_rt_pct = 0, 0
    if otc_rt_price is None: otc_rt_price, otc_rt_pct = 0, 0

    foreign, trust, dealer = get_institutional_data()

    if not twii.empty and twii_rt_price > 0:
        twii.loc[len(twii)] = {'Close': twii_rt_price, 'High': twii_rt_price, 'Low': twii_rt_price}
        
    twii = calculate_technical_indicators(twii)

    # 排版
    tw_icon = "🔴" if twii_rt_pct > 0 else "🟢"
    otc_icon = "🔴" if otc_rt_pct > 0 else "🟢"
    
    if otc_rt_pct > twii_rt_pct and otc_rt_pct > 0:
        market_style = "【內資作帳，中小型股活潑】櫃買漲幅勝過大盤，顯示本土資金與主力大戶非常活躍，是進場做多中小型飆股的好時機！"
    elif twii_rt_pct > otc_rt_pct and twii_rt_pct > 0:
        market_style = "【外資控盤，拉抬權值股】大盤漲幅勝過櫃買，資金集中在台積電等大型權值股，中小型股可能面臨資金排擠效應 (拉積盤)。"
    elif otc_rt_pct < 0 and twii_rt_pct < 0:
        market_style = "【泥沙俱下，系統性風險】大盤與櫃買同步下跌，市場恐慌情緒蔓延，請嚴控資金水位，多看少做。"
    else:
        market_style = "【資金輪動，多空震盪】大盤與櫃買走勢分歧，市場處於資金轉換期，建議挑選強勢族群，縮短操作週期。"

    kline_text = (f"• **加權指數 (大型股)**：`{twii_rt_price:,.2f}` 點 ({tw_icon} {twii_rt_pct:+.2f}%)\n"
                  f"• **櫃買指數 (中小型)**：`{otc_rt_price:,.2f}` 點 ({otc_icon} {otc_rt_pct:+.2f}%)\n"
                  f"> 💡 **盤勢研判**：{market_style}")

    if foreign is not None:
        total_net = foreign + trust + dealer
        inst_text = (f"今日三大法人合計：**{total_net:+.2f} 億元**\n"
                     f"> • **外資**：`{foreign:+.2f} 億` ｜ **投信**：`{trust:+.2f} 億` ｜ **自營**：`{dealer:+.2f} 億`\n"
                     f"> 籌碼點評：{'外資大舉掃貨，熱錢湧入' if foreign > 50 else '投信土洋對作，內資護盤' if (foreign < 0 and trust > 0) else '外資無情提款，權值股承壓' if foreign < -50 else '法人動作不大，回歸基本面'}。")
    else:
        inst_text = "今日法人數據尚未發布或假日休市。"

    rsi = twii['RSI'].iloc[-1] if not twii.empty and 'RSI' in twii.columns else 50.0
    k = twii['K'].iloc[-1] if not twii.empty and 'K' in twii.columns else 50.0
    d = twii['D'].iloc[-1] if not twii.empty and 'D' in twii.columns else 50.0
    macd_hist = twii['Hist'].iloc[-1] if not twii.empty and 'Hist' in twii.columns else 0.0
    p_macd_hist = twii['Hist'].iloc[-2] if not twii.empty and 'Hist' in twii.columns and len(twii) > 1 else 0.0
    
    rsi_desc = "⚠️ 過熱警報 (隨時面臨修正)" if rsi > 75 else "🟢 落底反彈 (超賣區浮現買點)" if rsi < 30 else "🟡 中性震盪"
    macd_trend = "紅柱擴大，多頭動能強勁" if macd_hist > 0 and macd_hist > p_macd_hist else "紅柱縮減，多方力竭" if macd_hist > 0 else "綠柱縮減，空方力道衰退" if macd_hist < p_macd_hist else "綠柱擴大，空方主導"
    
    tech_text = (f"• **RSI (14)**：`{rsi:.1f}` ｜ {rsi_desc}\n"
                 f"• **KD (9,3,3)**：K `{k:.1f}` / D `{d:.1f}` ｜ {'高檔鈍化' if k>80 else '低檔金叉' if (k>d and (twii['K'].iloc[-2] if not twii.empty and len(twii)>1 else 50) < (twii['D'].iloc[-2] if not twii.empty and len(twii)>1 else 50)) else '偏多格局' if k>d else '偏空格局'}\n"
                 f"• **MACD 動能**：{macd_trend}")

    vix_trend = "下降" if c_vix_price < p_vix_price else "飆升"
    intl_text = (f"昨夜美股 S&P 500 **{'收紅' if c_sp_price > p_sp_price else '收黑'}** (收 {c_sp_price:,.0f} 點)。\n"
                 f"華爾街 VIX 恐慌指數目前來到 **{c_vix_price:.2f}** ({vix_trend})。\n"
                 f"> 總經視野：{'VIX回落顯示外資避險情緒降溫，有利資金動能' if vix_trend == '下降' else '恐慌情緒升溫，外資可能加速提款'}。")

    support = twii['Low'].tail(10).min() if not twii.empty else 0
    resistance = twii['High'].tail(10).max() if not twii.empty else 0
    ma20 = twii['MA20'].iloc[-1] if not twii.empty and 'MA20' in twii.columns else twii_rt_price
    
    if twii_rt_price > ma20:
        adv = "大盤穩居月線之上，屬於多頭格局。配合櫃買強弱，可積極在底部起漲的強勢族群中尋找機會。"
    else:
        adv = "大盤跌破月線，趨勢偏弱。建議提高現金水位，嚴格設定防守價，並以短進短出為主。"
        
    eval_text = (f"🎯 **大盤短線支撐**：`{support:,.0f} 點` ｜ 🎯 **上檔壓力**：`{resistance:,.0f} 點`\n"
                 f"> **操盤建議**：{adv}")

    return {"data": (kline_text, inst_text, tech_text, intl_text, eval_text)}

async def send_daily_report(channel):
    msg = await channel.send("📡 **正在彙整當日最新大盤、櫃買指數與三大法人籌碼...**")
    result = await asyncio.to_thread(generate_market_text) 
    
    kline, inst, tech, intl, eval_text = result["data"]
    
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
    embed.set_footer(text="⚡ 由 AI 操盤系統自動生成 ｜ 採用無延遲官方直連計算")
    
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
    print(f'📊 大盤分析機器人 {bot.user} 已上線！(永不當機官方直連版)')
    if not schedule_daily_report.is_running():
        schedule_daily_report.start()

bot.run(TOKEN)
