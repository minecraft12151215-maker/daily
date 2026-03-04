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
    """終極完美版：鎖定日期列，直接抓取後方三個欄位數值，保證不抓錯！"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    # === 引擎 1：Yahoo 股市 (日期定位法) ===
    try:
        url = "https://tw.stock.yahoo.com/institutional-trading"
        res = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        # 取得網頁內所有乾淨的純文字
        strings = list(soup.stripped_strings)
        
        for i, s in enumerate(strings):
            # 1. 尋找表格第一欄的「日期」 (格式：YYYY/MM/DD)
            if re.match(r'^\d{4}/\d{2}/\d{2}$', s):
                nums = []
                # 2. 找到日期後，往後找 3 個數字 (這在表格中絕對對應：外資、投信、自營商)
                for j in range(1, 15):
                    if i + j >= len(strings): break
                    
                    # 清理字串，只保留數字、小數點和正負號
                    val_str = strings[i+j].replace(',', '').replace('+', '').replace('億', '').strip()
                    
                    if re.match(r'^-?\d+(\.\d+)?$', val_str):
                        nums.append(float(val_str))
                        
                    # 抓滿三個數字就立刻回傳！(對應截圖中的順序)
                    if len(nums) == 3:
                        return nums[0], nums[1], nums[2]
    except Exception as e:
        print(f"Yahoo 引擎抓取失敗: {e}")

    # === 引擎 2：證交所 OpenAPI (備用) ===
    try:
        open_url = "https://openapi.twse.com.tw/v1/exchangeReport/BFI82U"
        res = requests.get(open_url, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json()
            f_val = t_val = d_val = 0.0
            has_data = False
            for row in data:
                name = str(row.get("單位名稱", row.get("Item", "")))
                diff_str = str(row.get("買賣差額", row.get("Difference", "0"))).replace(',', '')
                try: diff = float(diff_str) / 100000000.0
                except: diff = 0.0
                    
                if "外資及陸資" in name and "不含" in name:
                    f_val += diff
                    has_data = True
                elif "投信" in name:
                    t_val += diff
                    has_data = True
                elif "自營商" in name:
                    d_val += diff
                    has_data = True
            
            if has_data:
                return round(f_val, 1), round(t_val, 1), round(d_val, 1)
    except Exception as e:
        print(f"OpenAPI 備用引擎抓取失敗: {e}")

    return None, None, None

def calculate_indicators(df):
    if len(df) < 20: return df 
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    df['RSI'] = 100 - (100 / (1 + (gain / loss).replace(np.nan, 0)))
    low_min = df['Low'].rolling(window=9).min()
    high_max = df['High'].rolling(window=9).max()
    df['RSV'] = 100 * ((df['Close'] - low_min) / (high_max - low_min))
    df['K'] = df['RSV'].ewm(com=2, adjust=False).mean()
    df['D'] = df['K'].ewm(com=2, adjust=False).mean()
    return df

async def send_daily_report(channel):
    msg = await channel.send("📡 **正在彙整台股盤勢與精準法人籌碼數據...**")
    
    try:
        # 確保抓取足夠資料以供指標運算
        twii_df = yf.Ticker("^TWII").history(period="3mo")
        otc_df = yf.Ticker("^TWOII").history(period="3mo")
        
        f_val, t_val, d_val = await asyncio.to_thread(get_institutional_data)
        
        if twii_df.empty or otc_df.empty:
            return await msg.edit(content="❌ 無法從 Yahoo Finance 取得大盤指數，請稍後再試。")
        
        twii_df = calculate_indicators(twii_df)
        otc_df = calculate_indicators(otc_df)
        
        now_twii = twii_df.iloc[-1]
        pre_twii = twii_df.iloc[-2]
        now_otc = otc_df.iloc[-1]
        pre_otc = otc_df.iloc[-2]
        
        pct_tw = ((now_twii['Close'] - pre_twii['Close']) / pre_twii['Close']) * 100
        pct_otc = ((now_otc['Close'] - pre_otc['Close']) / pre_otc['Close']) * 100
        
        tw_date = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).date()
        embed = discord.Embed(title=f"📊 台股雙引擎盤勢解析 | {tw_date}", color=0xf1c40f)
        
        flow_content = (f"• **加權指數**：`{now_twii['Close']:,.0f}` ({'🔴' if pct_tw > 0 else '🟢'} {pct_tw:+.2f}%)\n"
                        f"• **櫃買指數**：`{now_otc['Close']:,.2f}` ({'🔴' if pct_otc > 0 else '🟢'} {pct_otc:+.2f}%)")
        embed.add_field(name="⚖️ 【資金板塊】", value=flow_content, inline=False)
        
        if f_val is not None:
            total_net = f_val + t_val + d_val
            inst_content = (f"今日三大法人合計：**{total_net:+.1f} 億元**\n"
                            f"> • **外資**：`{f_val:+.1f} 億` | **投信**：`{t_val:+.1f} 億` | **自營**：`{d_val:+.1f} 億`")
        else:
            inst_content = "今日數據尚未更新 (或逢休市)。"
        embed.add_field(name="💰 【法人籌碼】", value=inst_content, inline=False)
        
        rsi_val = now_twii['RSI'] if pd.notna(now_twii.get('RSI')) else 0.0
        k_val = now_twii['K'] if pd.notna(now_twii.get('K')) else 0.0
        d_val_tech = now_twii['D'] if pd.notna(now_twii.get('D')) else 0.0
        
        tech_content = f"• **RSI(14)**：`{rsi_val:.1f}`\n• **KD(9,3,3)**：K `{k_val:.1f}` / D `{d_val_tech:.1f}`"
        embed.add_field(name="🛠️ 【技術指標】", value=tech_content, inline=False)
        
        embed.set_footer(text="⚡ 由 AI 操盤系統自動生成 ｜ 順勢而為，嚴格停損")
        await msg.edit(content=None, embed=embed)
        
    except Exception as e:
        await msg.edit(content=f"❌ 報表生成出錯：`{str(e)}`")

@tasks.loop(minutes=1)
async def schedule_daily_report():
    tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    if tw_now.strftime("%H:%M") == DAILY_REPORT_TIME and tw_now.weekday() < 5:
        channel = bot.get_channel(TARGET_CHANNEL_ID)
        if channel: 
            await send_daily_report(channel)
        await asyncio.sleep(61)

@bot.command()
async def report(ctx):
    await send_daily_report(ctx.channel)

@bot.event
async def on_ready():
    print(f'📊 機器人 {bot.user} 已上線 (搭載【日期錨點】終極版雷達)。')
    if not schedule_daily_report.is_running():
        schedule_daily_report.start()

bot.run(TOKEN)
