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

# 載入環境變數
load_dotenv()

# ================= 絕對必填設定區 =================
TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    raise ValueError("❌ 找不到 DISCORD_TOKEN！")

TARGET_CHANNEL_ID = 1475023963334643793
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
        # 抓取網頁中所有純文字標籤並存入清單
        all_texts = [t.strip() for t in soup.find_all(string=True) if t.strip()]
        
        results = {"外資": None, "投信": None, "自營商": None}
        targets = {
            "外資": ["外資及陸資(不含外資自營商)", "外資"],
            "投信": ["投信"],
            "自營商": ["自營商(合計)", "自營商"]
        }
        
        for key, aliases in targets.items():
            for i, text in enumerate(all_texts):
                if text in aliases:
                    # 找到名稱後，往後找數字。Yahoo 格式通常為：買進、賣出、買賣超
                    found_nums = []
                    # 往後掃描 15 個標籤以尋找該列的數值
                    for j in range(1, 15):
                        if i + j >= len(all_texts): break
                        val = all_texts[i+j].replace(',', '')
                        # 使用正則表達式判定是否為數字格式
                        if re.match(r'^[\+\-]?\d+(\.\d+)?$', val):
                            found_nums.append(float(val))
                        # 抓到第 3 個數字即為該法人的「買賣超」金額
                        if len(found_nums) == 3:
                            results[key] = found_nums[2]
                            break
                    break # 找到該類別數據後，跳出 aliases 迴圈
                    
        return results["外資"], results["投信"], results["自營商"]
            
    except Exception as e:
        print(f"三大法人數據抓取失敗: {e}")
    return None, None, None

def calculate_indicators(df):
    """計算 RSI 與 KD 技術指標"""
    if len(df) < 20: return df # 資料量不足不計算
    
    # 計算 RSI
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    df['RSI'] = 100 - (100 / (1 + (gain / loss).replace(np.nan, 0)))
    
    # 計算 KD
    low_min = df['Low'].rolling(window=9).min()
    high_max = df['High'].rolling(window=9).max()
    df['RSV'] = 100 * ((df['Close'] - low_min) / (high_max - low_min))
    df['K'] = df['RSV'].ewm(com=2, adjust=False).mean()
    df['D'] = df['K'].ewm(com=2, adjust=False).mean()
    
    return df

async def send_daily_report(channel):
    msg = await channel.send("📡 **正在彙整台股盤勢與法人籌碼數據...**")
    
    try:
        # 1. 抓取指數資料 (多抓幾天確保指標運算正確)
        twii_df = yf.Ticker("^TWII").history(period="1mo")
        otc_df = yf.Ticker("^TWOII").history(period="1mo")
        
        # 2. 抓取法人籌碼 (在 thread 中執行避免卡住 Discord)
        f_val, t_val, d_val = await asyncio.to_thread(get_institutional_data)
        
        if twii_df.empty or otc_df.empty:
            return await msg.edit(content="❌ 無法從 Yahoo Finance 取得指數報價，請稍後再試。")
        
        # 3. 計算技術指標
        twii_df = calculate_indicators(twii_df)
        otc_df = calculate_indicators(otc_df)
        
        # 取得最新一筆與前一筆數據做比較
        now_twii = twii_df.iloc[-1]
        pre_twii = twii_df.iloc[-2]
        now_otc = otc_df.iloc[-1]
        pre_otc = otc_df.iloc[-2]
        
        pct_tw = ((now_twii['Close'] - pre_twii['Close']) / pre_twii['Close']) * 100
        pct_otc = ((now_otc['Close'] - pre_otc['Close']) / pre_otc['Close']) * 100
        
        tw_date = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).date()
        embed = discord.Embed(title=f"📊 台股雙引擎盤勢解析 | {tw_date}", color=0xf1c40f)
        
        # --- 資金板塊區 ---
        flow_content = (f"• **加權指數**：`{now_twii['Close']:,.0f}` ({'🔴' if pct_tw > 0 else '🟢'} {pct_tw:+.2f}%)\n"
                        f"• **櫃買指數**：`{now_otc['Close']:,.2f}` ({'🔴' if pct_otc > 0 else '🟢'} {pct_otc:+.2f}%)")
        embed.add_field(name="⚖️ 【資金板塊】", value=flow_content, inline=False)
        
        # --- 法人籌碼區 ---
        if f_val is not None:
            total_net = f_val + t_val + d_val
            inst_content = (f"今日三大法人合計：**{total_net:+.1f} 億元**\n"
                            f"> • **外資**：`{f_val:+.1f} 億` | **投信**：`{t_val:+.1f} 億` | **自營**：`{d_val:+.1f} 億`")
        else:
            inst_content = "今日數據尚未更新 (或逢休市)。"
        embed.add_field(name="💰 【法人籌碼】", value=inst_content, inline=False)
        
        # --- 技術指標區 ---
        rsi_val = now_twii['RSI'] if 'RSI' in now_twii else 0
        k_val = now_twii['K'] if 'K' in now_twii else 0
        d_val_tech = now_twii['D'] if 'D' in now_twii else 0
        
        tech_content = f"• **RSI(14)**：`{rsi_val:.1f}`\n• **KD(9,3,3)**：K `{k_val:.1f}` / D `{d_val_tech:.1f}`"
        embed.add_field(name="🛠️ 【技術指標】", value=tech_content, inline=False)
        
        embed.set_footer(text="⚡ 由 AI 操盤系統自動生成 ｜ 順勢而為，嚴格停損")
        await msg.edit(content=None, embed=embed)
        
    except Exception as e:
        await msg.edit(content=f"❌ 報表生成出錯：`{str(e)}`")

@tasks.loop(minutes=1)
async def schedule_daily_report():
    # 強制使用台灣時間判斷
    tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    # 如果時間到了且是平日 (週一至週五)
    if tw_now.strftime("%H:%M") == DAILY_REPORT_TIME and tw_now.weekday() < 5:
        channel = bot.get_channel(TARGET_CHANNEL_ID)
        if channel: 
            await send_daily_report(channel)
        await asyncio.sleep(61) # 避免同一分鐘重複觸發

@bot.command()
async def report(ctx):
    await send_daily_report(ctx.channel)

@bot.event
async def on_ready():
    print(f'📊 機器人 {bot.user} 已上線 (時區：台北)。')
    if not schedule_daily_report.is_running():
        schedule_daily_report.start()

bot.run(TOKEN)
