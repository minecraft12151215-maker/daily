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
    """精準定位法：搜尋標籤關鍵字，並偏移抓取第三個數字（買賣超）"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    url = "https://tw.stock.yahoo.com/institutional-trading"
    
    try:
        res = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        all_texts = [t.strip() for t in soup.find_all(string=True) if t.strip()]
        
        results = {"外資": 0.0, "投信": 0.0, "自營商": 0.0}
        targets = {
            "外資": ["外資及陸資(不含外資自營商)", "外資"],
            "投信": ["投信"],
            "自營商": ["自營商(合計)", "自營商"]
        }
        
        for key, aliases in targets.items():
            for i, text in enumerate(all_texts):
                if text in aliases:
                    # 找到標籤後，往後找數字。Yahoo 格式通常為：買進、賣出、買賣超
                    found_nums = []
                    for j in range(1, 15): # 往後掃描 15 個標籤
                        val = all_texts[i+j].replace(',', '')
                        if re.match(r'^[\+\-]?\d+(\.\d+)?$', val):
                            found_nums.append(float(val))
                        if len(found_nums) == 3: # 抓到第三個數字即為買賣超
                            results[key] = found_nums[2]
                            break
                    break # 找到該類別後跳出 alias 迴圈
                    
        return results["外資"], results["投信"], results["自營商"]
            
    except Exception as e:
        print(f"籌碼抓取失敗: {e}")
    return None, None, None

def calculate_indicators(df):
    """計算 RSI 與 KD"""
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

async def send_daily_report(channel):
    msg = await channel.send("📡 **正在彙整大盤技術指標與法人籌碼數據...**")
    
    # 執行耗時爬蟲
    twii_df = yf.Ticker("^TWII").history(period="10d")
    otc_df = yf.Ticker("^TWOII").history(period="10d")
    inst_data = await asyncio.to_thread(get_institutional_data)
    
    if twii_df.empty or otc_df.empty:
        return await msg.edit(content="⚠️ 指數資料抓取失敗。")
    
    twii = calculate_indicators(twii_df).iloc[-1]
    otc = calculate_indicators(otc_df).iloc[-1]
    p_twii = twii_df.iloc[-2]
    p_otc = otc_df.iloc[-2]
    
    pct_tw = ((twii['Close'] - p_twii['Close']) / p_twii['Close']) * 100
    pct_otc = ((otc['Close'] - p_otc['Close']) / p_otc['Close']) * 100
    
    tw_date = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).date()
    embed = discord.Embed(title=f"📊 台股雙引擎盤勢解析 | {tw_date}", color=0xf1c40f)
    
    # 指數與資金
    flow_content = (f"• **加權指數**：`{twii['Close']:,.0f}` ({'🔴' if pct_tw > 0 else '🟢'} {pct_tw:+.2f}%)\n"
                    f"• **櫃買指數**：`{otc['Close']:,.2f}` ({'🔴' if pct_otc > 0 else '🟢'} {pct_otc:+.2f}%)")
    embed.add_field(name="⚖️ 【資金板塊】", value=flow_content, inline=False)
    
    # 三大法人
    if inst_data[0] is not None:
        f, t, d = inst_data
        inst_content = (f"今日三大法人合計：**{f+t+d:+.1f} 億元**\n"
                        f"> • **外資**：`{f:+.1f} 億` | **投信**：`{t:+.1f} 億` | **自營**：`{d:+.1f} 億`")
    else:
        inst_content = "今日數據尚未更新 (或逢休市)。"
    embed.add_field(name="💰 【法人籌碼】", value=inst_content, inline=False)
    
    # 技術指標
    tech_content = f"• **RSI(14)**：`{twii['RSI']:.1f}`\n• **KD(9,3,3)**：K `{twii['K']:.1f}` / D `{twii['D']:.1f}`"
    embed.add_field(name="🛠️ 【技術指標】", value=tech_content, inline=False)
    
    embed.set_footer(text="⚡ 由 AI 操盤系統自動生成")
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
    print(f'📊 大盤分析機器人 {bot.user} 已啟動。')
    if not schedule_daily_report.is_running(): schedule_daily_report.start()

bot.run(TOKEN)
