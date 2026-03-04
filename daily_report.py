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
    """無敵行列切割法：不受 Yahoo 版面與廣告干擾"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    url = "https://tw.stock.yahoo.com/institutional-trading"
    
    try:
        res = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        results = {"外資": None, "投信": None, "自營商": None}
        
        # 掃描所有可能的行元素 (li 或 div)，用 '|' 把同一列的文字切開
        for el in soup.find_all(['li', 'div']):
            # 略過整個網頁的 body
            if not el.parent or el.name == 'body': continue
            
            text_parts = el.get_text(separator='|', strip=True).split('|')
            if len(text_parts) < 4: continue
            
            name = text_parts[0].strip()
            
            # 從該列中萃取所有純數字
            nums = []
            for p in text_parts[1:]:
                # 清除億和逗號，留下純數字與正負號
                clean_p = p.replace(',', '').replace('億', '').replace('+', '').strip()
                if re.match(r'^-?\d+(\.\d+)?$', clean_p):
                    nums.append(float(clean_p))
                    
            # Yahoo 的順序是：買進、賣出、買賣超 (所以取第三個數字 nums[2])
            if len(nums) >= 3:
                net_val = nums[2] 
                
                # 精準將數據分配給對應法人，避免重複
                if name == "外資及陸資(不含外資自營商)" or name == "外資":
                    results["外資"] = net_val
                elif name == "投信":
                    results["投信"] = net_val
                elif name == "自營商(自行買賣)":
                    if results["自營商"] is None: results["自營商"] = 0.0
                    results["自營商"] += net_val
                elif name == "自營商(避險)":
                    if results["自營商"] is None: results["自營商"] = 0.0
                    results["自營商"] += net_val
                elif name == "自營商":
                    results["自營商"] = net_val
                    
        # 只要有抓到任何一個，就回傳結果
        if any(v is not None for v in results.values()):
            return results["外資"] or 0.0, results["投信"] or 0.0, results["自營商"] or 0.0
            
    except Exception as e:
        print(f"Yahoo 籌碼抓取失敗: {e}")
        
    return None, None, None

def calculate_indicators(df):
    """計算 RSI 與 KD 技術指標"""
    # 🚨 之前的 Bug 就在這！如果抓太少天數 (<20)，就不會算指標，直接吐出 0
    if len(df) < 20: 
        return df 
    
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
    msg = await channel.send("📡 **正在彙整台股盤勢與精準法人籌碼數據...**")
    
    try:
        # 🚨 修正：一次抓 3 個月的資料，確保 RSI 與 KD 有足夠的歷史天數可以運算！
        twii_df = yf.Ticker("^TWII").history(period="3mo")
        otc_df = yf.Ticker("^TWOII").history(period="3mo")
        
        # 背景抓取法人籌碼
        f_val, t_val, d_val = await asyncio.to_thread(get_institutional_data)
        
        if twii_df.empty or otc_df.empty:
            return await msg.edit(content="❌ 無法從 Yahoo Finance 取得大盤指數，請稍後再試。")
        
        # 執行技術指標運算
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
    print(f'📊 機器人 {bot.user} 已上線 (時區：台北，搭載終極修復雷達)。')
    if not schedule_daily_report.is_running():
        schedule_daily_report.start()

bot.run(TOKEN)
