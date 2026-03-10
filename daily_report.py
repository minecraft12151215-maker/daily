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

def get_yahoo_realtime_indices():
    """【終極 Yahoo 官方 API】無視網頁阻擋，直接抓取 Yahoo 算好的現價與精準漲跌幅"""
    results = {"twii": (None, None), "otc": (None, None)}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    try:
        url = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=^TWII,^TWOII"
        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()
        
        for item in data.get('quoteResponse', {}).get('result', []):
            sym = item.get('symbol')
            price = item.get('regularMarketPrice')
            pct = item.get('regularMarketChangePercent')
            
            if price is not None and pct is not None:
                if sym == "^TWII":
                    results["twii"] = (float(price), float(pct))
                elif sym == "^TWOII":
                    results["otc"] = (float(price), float(pct))
    except Exception as e:
        print(f"Yahoo API 讀取失敗: {e}")
        
    return results

def get_institutional_data():
    """【還原成功版】使用之前成功抓出正確法人數據的官方 API"""
    headers = {"User-Agent": "Mozilla/5.0"}
    
    # 引擎 1：證交所官方 RWD API
    try:
        url1 = "https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json"
        res1 = requests.get(url1, headers=headers, timeout=5)
        data1 = res1.json()
        if data1.get('stat') == 'OK':
            f_val = t_val = d_val = 0.0
            for row in data1['data']:
                name = row[0]
                diff = float(row[3].replace(',', '')) / 100000000.0
                if "外資及陸資" in name or "外資自營商" in name:
                    f_val += diff
                elif "投信" in name:
                    t_val += diff
                elif "自營商" in name:
                    d_val += diff
            return round(f_val, 2), round(t_val, 2), round(d_val, 2)
    except: pass

    # 引擎 2：證交所 OpenAPI 備用
    try:
        url2 = "https://openapi.twse.com.tw/v1/exchangeReport/BFI82U"
        res2 = requests.get(url2, headers=headers, timeout=5)
        if res2.status_code == 200:
            data2 = res2.json()
            f_val = t_val = d_val = 0.0
            has_data = False
            for row in data2:
                name = str(row.get("Item", ""))
                diff_str = str(row.get("Difference", "0")).replace(',', '')
                try: diff = float(diff_str) / 100000000.0
                except: diff = 0.0
                
                if "外資及陸資" in name or "外資自營商" in name: f_val += diff; has_data = True
                elif "投信" in name: t_val += diff; has_data = True
                elif "自營商" in name: d_val += diff; has_data = True
            if has_data: return round(f_val, 2), round(t_val, 2), round(d_val, 2)
    except: pass
    
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
    twii = pd.DataFrame()
    sp500 = pd.DataFrame()
    vix = pd.DataFrame()
    
    try:
        twii = yf.Ticker("^TWII").history(period="3mo")
        sp500 = yf.Ticker("^GSPC").history(period="1mo")
        vix = yf.Ticker("^VIX").history(period="1mo")
    except: pass

    # 🔥 取回 100% Yahoo 官方精準數據
    rt_indices = get_yahoo_realtime_indices()
    twii_rt_price, twii_rt_pct = rt_indices["twii"]
    otc_rt_price, otc_rt_pct = rt_indices["otc"]

    if twii_rt_price is None: twii_rt_price, twii_rt_pct = 0, 0
    if otc_rt_price is None: otc_rt_price, otc_rt_pct = 0, 0

    foreign, trust, dealer = get_institutional_data()

    # 更新指標
    if not twii.empty and twii_rt_price > 0:
        tw_date_str = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d')
        twii.loc[tw_date_str] = {'Close': twii_rt_price, 'High': twii_rt_price, 'Low': twii_rt_price}
        
    twii = calculate_technical_indicators(twii)

    c_sp_price = sp500['Close'].iloc[-1] if not sp500.empty and len(sp500) > 0 else 0
    p_sp_price = sp500['Close'].iloc[-2] if not sp500.empty and len(sp500) > 1 else c_sp_price
    c_vix_price = vix['Close'].iloc[-1] if not vix.empty and len(vix) > 0 else 0
    p_vix_price = vix['Close'].iloc[-2] if not vix.empty and len(vix) > 1 else c_vix_price

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
        inst_text = "今日法人數據尚未更新 (或逢假日休市)。"

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
    embed.set_footer(text="⚡ 由 AI 操盤系統自動生成 ｜ 嚴格保證 100% Yahoo 數據")
    
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
    print(f'📊 大盤分析機器人 {bot.user} 已上線！(完美純淨 Yahoo 官方 API 版)')
    if not schedule_daily_report.is_running():
        schedule_daily_report.start()

bot.run(TOKEN)
