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

load_dotenv()

# ================= 絕對必填設定區 =================
TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    raise ValueError("❌ 找不到 DISCORD_TOKEN！")

target_channel_id = 1475023963334643793
DAILY_REPORT_TIME = "17:00"   
# ==================================================

intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!', intents=intents)

def get_institutional_data():
    """終極修復：標籤比對法，精準抓取 Yahoo 股市三大法人數據"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    url = "https://tw.stock.yahoo.com/institutional-trading"
    
    try:
        res = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        # 抓取所有包含文字的標籤
        all_elements = soup.find_all(string=True)
        
        results = {"外資": None, "投信": None, "自營商": None}
        
        # 建立掃描邏輯：找到關鍵字後，向後找第一個符合數字格式的標籤
        for i, text in enumerate(all_elements):
            clean_text = text.strip()
            target_key = None
            if "外資及陸資(不含外資自營商)" in clean_text or clean_text == "外資": target_key = "外資"
            elif clean_text == "投信": target_key = "投信"
            elif clean_text == "自營商(合計)": target_key = "自營商"
            
            if target_key and results[target_key] is None:
                # 往後搜尋 20 個標籤尋找買賣超數值
                for j in range(1, 20):
                    next_val = all_elements[i+j].strip().replace(',', '')
                    # 匹配整數或小數，可能帶有正負號
                    if re.match(r'^[\+\-]?\d+(\.\d+)?$', next_val):
                        # Yahoo 的表格順序通常是：買進、賣出、買賣超。我們需要第三個數字
                        # 但為保險起見，有些版面會直接顯示買賣超，這裡抓取該區段邏輯
                        count = 0
                        for k in range(j, j+10):
                            val_check = all_elements[i+k].strip().replace(',', '')
                            if re.match(r'^[\+\-]?\d+(\.\d+)?$', val_check):
                                count += 1
                                if count == 3: # 買賣超通常是該列的第三個數字
                                    results[target_key] = float(val_check)
                                    break
                        break

        if any(v is not None for v in results.values()):
            return results["外資"] or 0.0, results["投信"] or 0.0, results["自營商"] or 0.0
            
    except Exception as e:
        print(f"籌碼抓取失敗: {e}")
    return None, None, None

def calculate_technical_indicators(df):
    if len(df) < 14: return df
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    df['RSI'] = 100 - (100 / (1 + gain / loss))
    low_min, high_max = df['Low'].rolling(9).min(), df['High'].rolling(9).max()
    df['RSV'] = 100 * ((df['Close'] - low_min) / (high_max - low_min))
    df['K'] = df['RSV'].ewm(com=2, adjust=False).mean()
    df['D'] = df['K'].ewm(com=2, adjust=False).mean()
    return df

async def generate_market_text():
    # 增加抓取天數，確保假日手動查詢時能抓到上週五的資料
    twii = yf.Ticker("^TWII").history(period="10d")
    otc = yf.Ticker("^TWOII").history(period="10d")
    sp500 = yf.Ticker("^GSPC").history(period="5d")
    vix = yf.Ticker("^VIX").history(period="5d")
    foreign, trust, dealer = get_institutional_data()

    if twii.empty or otc.empty: return None

    twii = calculate_technical_indicators(twii)
    otc = calculate_technical_indicators(otc)
    
    c_twii, p_twii = twii.iloc[-1], twii.iloc[-2]
    c_otc, p_otc = otc.iloc[-1], otc.iloc[-2]
    
    pct_tw = ((c_twii['Close'] - p_twii['Close']) / p_twii['Close']) * 100
    pct_otc = ((c_otc['Close'] - p_otc['Close']) / p_otc['Close']) * 100
    
    kline_text = (f"• **加權指數**：`{c_twii['Close']:,.0f}` ({'🔴' if pct_tw > 0 else '🟢'} {pct_tw:+.2f}%)\n"
                  f"• **櫃買指數**：`{c_otc['Close']:,.2f}` ({'🔴' if pct_otc > 0 else '🟢'} {pct_otc:+.2f}%)")

    if foreign is not None:
        total = foreign + trust + dealer
        inst_text = (f"今日三大法人合計：**{total:+.1f} 億元**\n"
                     f"> • **外資**：`{foreign:+.1f} 億` | **投信**：`{trust:+.1f} 億` | **自營**：`{dealer:+.1f} 億`")
    else:
        inst_text = "今日籌碼數據尚未更新 (或逢休市)。"

    tech_text = f"• **RSI(14)**：`{c_twii['RSI']:.1f}`\n• **KD(9,3,3)**：K `{c_twii['K']:.1f}` / D `{c_twii['D']:.1f}`"
    
    return kline_text, inst_text, tech_text

async def send_daily_report(channel):
    msg = await channel.send("📡 **正在彙整即時盤勢與法人籌碼...**")
    data = await asyncio.to_thread(generate_morning_report_logic) # 借用邏輯
    # 這裡簡化呼叫上面的產生函數
    res = await generate_market_text()
    if not res: return await msg.edit(content="⚠️ 資料讀取失敗。")
    
    kline, inst, tech = res
    tw_date = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).date()
    
    embed = discord.Embed(title=f"📊 台股雙引擎盤勢解析 | {tw_date}", color=0xf1c40f)
    embed.add_field(name="⚖️ 【資金板塊】", value=kline, inline=False)
    embed.add_field(name="💰 【法人籌碼】", value=inst, inline=False)
    embed.add_field(name="🛠️ 【技術指標】", value=tech, inline=False)
    
    await msg.edit(content=None, embed=embed)

@tasks.loop(minutes=1)
async def schedule_daily_report():
    tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    if tw_now.strftime("%H:%M") == DAILY_REPORT_TIME and tw_now.weekday() < 5:
        channel = bot.get_channel(target_channel_id)
        if channel: await send_daily_report(channel)
        await asyncio.sleep(61)

@bot.command()
async def report(ctx):
    await send_daily_report(ctx.channel)

@bot.event
async def on_ready():
    print(f'📊 機器人 {bot.user} 在線 (時區校準：台北)')
    if not schedule_daily_report.is_running(): schedule_daily_report.start()

bot.run(TOKEN)
