
"""
FLWS JAN 29: LIVE BATTLE MONITOR
=================================
Dedicated monitor for the Jan 29 "Free Roll" Squeeze Event.
Runs anywhere (Local or Cloud).

Checks:
1. Price vs "Pain Chain" Levels ($5.03, $5.46, $6.00).
2. Volume velocity vs 1.5M Vacuum Target.
3. Volatility compression (The Pin).

Usage:
    python production/flws_live_monitor_jan29.py
"""

import os
import sys
import time
import requests
import json
import toml
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd

# Try to use yfinance for easy portability (no API keys needed for delayed/live approx)
try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
    print("⚠️ yfinance not installed. Install with: pip install yfinance")

# Project Setup
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import argparse

# -----------------------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------------------
DISCORD_ENABLED = True  # SAFETY SWITCH: ARMED AND READY
TICKER = "FLWS"
LEVELS = {
    "NATURAL_FLOOR": 4.80,
    "PIN_BREAK": 5.03,
    "MARGIN_STRESS": 5.46,
    "NUCLEAR": 6.00
}
VACUUM_VOLUME_TARGET = 1_500_000  # 1.5M shares

# -----------------------------------------------------------------------------
# SECRETS & SETUP
# -----------------------------------------------------------------------------
def load_secrets():
    # Try multiple paths for secrets
    paths = [
        PROJECT_ROOT / "config" / "secrets.toml",
        PROJECT_ROOT / "secrets.toml"
    ]
    for p in paths:
        if p.exists():
            return toml.load(p)
    return {}

SECRETS = load_secrets()
POLYGON_KEY = SECRETS.get('POLYGON_API_KEY') or os.environ.get('POLYGON_API_KEY')

def get_webhook_url():
    # Priority 1: Environment Variable (GitHub Actions / Cloud)
    env_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if env_url:
        return env_url

    # Priority 2: Local secrets.toml
    return SECRETS.get("DISCORD_WEBHOOK_URL") or SECRETS.get("discord", {}).get("webhook_url")

WEBHOOK_URL = get_webhook_url()


# -----------------------------------------------------------------------------
# DATA FETCHING (INSTITUTIONAL GRADE)
# -----------------------------------------------------------------------------
def get_polygon_snapshot():
    """Get millisecond-latency data from Polygon.io"""
    if not POLYGON_KEY:
        return None
    
    url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{TICKER}?apiKey={POLYGON_KEY}"
    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            data = r.json()
            ticker_data = data['ticker']
            
            # Extract precise metrics
            day = ticker_data['day']
            last_trade = ticker_data['lastTrade']
            last_quote = ticker_data.get('lastQuote', {})
            
            # --- MODEL METRICS ---
            # 1. Spread Vacuum (Widening spread = Liquidity drying up)
            bid = last_quote.get('P', 0)
            ask = last_quote.get('p', 0)
            spread_cents = (ask - bid) * 100 if bid and ask else 0
            
            # 2. Order Flow Imbalance (Buying Pressure vs Selling Wall)
            bid_size = last_quote.get('s', 0)
            ask_size = last_quote.get('S', 0)
            imbalance = 0
            if (bid_size + ask_size) > 0:
                imbalance = (bid_size - ask_size) / (bid_size + ask_size)
            
            return {
                "source": "POLYGON (Real-Time)",
                "price": round(last_trade['p'], 2),
                "prev_close": round(ticker_data['prevDay']['c'], 2),
                "change_pct": round(ticker_data['todaysChangePerc'], 2),
                "volume": day['v'],
                "vwap": day.get('vw', 0),
                "high": day.get('h', last_trade['p']),
                "low": day.get('l', last_trade['p']),
                "spread": round(spread_cents, 1),
                "bid_size": bid_size,
                "ask_size": ask_size,
                "imbalance": round(imbalance, 2)
            }
    except Exception as e:
        print(f"⚠️ Polygon Fetch Error: {e}")
        return None

def get_live_data():
    """Hybrid Fetcher: Tries Polygon (Pro) -> yfinance (Backup)"""
    
    # 1. Try Polygon (Institutional Feed)
    poly_data = get_polygon_snapshot()
    if poly_data:
         # Calculate Velocity Proxy (Volume / Minutes Open)
        # Market open 9:30 ET.
        now = datetime.now()
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        minutes_open = max(1, (now - market_open).total_seconds() / 60)
        
        # Only valid during market hours
        if 9 <= now.hour <= 16:
             poly_data['velocity'] = poly_data['volume'] / minutes_open
        else:
             poly_data['velocity'] = 0
             
        return poly_data

    # 2. Fallback to yfinance (Retail Feed)
    if not YFINANCE_AVAILABLE:
        return None
    
    print("⚠️ Using Retail Data Feed (yfinance) - Expect 15min Delays")
    try:
        ticker = yf.Ticker(TICKER)

        # Get fast info
        info = ticker.fast_info
        price = info.last_price
        
        # Determine previous close safely
        prev_close = info.previous_close
        
        # Get volume (intraday) from history
        todays_data = ticker.history(period="1d")
        if not todays_data.empty:
            volume = todays_data['Volume'].iloc[-1]
            current_price = todays_data['Close'].iloc[-1] # Prefer this if available
            high = todays_data['High'].iloc[-1]
            low = todays_data['Low'].iloc[-1]
        else:
            volume = 0
            current_price = price
            high = price
            low = price

        return {
            "source": "YFINANCE (Retail)",
            "price": round(current_price, 2),
            "prev_close": round(prev_close, 2),
            "change_pct": round(((current_price - prev_close) / prev_close) * 100, 2),
            "volume": volume,
            "high": round(high, 2),
            "low": round(low, 2)
        }
    except Exception as e:
        print(f"❌ Data fetch error: {e}")
        return None

# -----------------------------------------------------------------------------
# ANALYSIS & ALERTING
# -----------------------------------------------------------------------------
def generate_status_report(data):
    price = data['price']
    volume = data['volume']
    
    # 1. Determine Status Zone
    status = "🟢 SAFE (ACCUMULATION)"
    color = 0x2ECC71 # Green
    
    if price > LEVELS['NUCLEAR']:
        status = "☢️ NUCLEAR LIQUIDATION (T+1 DELIVERY RISK)"
        color = 0xFF0000 # Red
    elif price > LEVELS['MARGIN_STRESS']:
        status = "🟠 MARGIN STRESS (FORCED BUYING)"
        color = 0xE67E22 # Orange
    elif price > LEVELS['PIN_BREAK']:
        status = "🟡 PIN BROKEN (WEAK SHORTS COVERING)"
        color = 0xF1C40F # Yellow
    elif price < LEVELS['NATURAL_FLOOR']:
        status = "🔵 DISCOUNT (BELOW NATURAL FLOOR)"
        color = 0x3498DB # Blue

    # 2. Volume Velocity
    vol_pct = (volume / VACUUM_VOLUME_TARGET) * 100
    
    # 3. Message Construction
    # Imbalance description
    if data.get('imbalance', 0) > 0.3:
        pressure = "🟢 BUYING PRESSURE"
    elif data.get('imbalance', 0) < -0.3:
        pressure = "🔴 SELLING WALL"
    else:
        pressure = "⚪ BALANCED"

    description = (
        f"**Price:** ${price} ({data['change_pct']}%)\n"
        f"**Volume:** {volume:,.0f} ({vol_pct:.1f}%)\n"
        f"**Order Book:** {pressure} (Imbal: {data.get('imbalance', 0)})"
    )
    
    embed = {
        "title": f"FLWS LIVE MONITOR: {status}",
        "description": description,
        "color": color,
        "fields": [
            {
                "name": "🎯 Key Levels Watch",
                "value": (
                    f"• $6.00 (Nuclear): {('✅ BREACHED' if price >= 6.00 else 'Wait...')}\n"
                    f"• $5.46 (Stress): {('✅ BREACHED' if price >= 5.46 else 'Wait...')}\n"
                    f"• $5.03 (Pin Break): {('✅ BREACHED' if price >= 5.03 else 'Wait...')}\n"
                    f"• $4.80 (Floor): {('✅ HELD' if price >= 4.80 else '⚠️ AT RISK')}"
                ),
                "inline": False
            },
            {
                "name": "🌊 Liquidity Vacuum Model",
                "value": (
                    f"**Spread Width:** {data.get('spread', 'N/A')}¢ "
                    f"{'⚠️ (WIDENING - Vacuum Forming)' if data.get('spread', 0) > 5 else '✅ (Tight - Algo Controlled)'}\n"
                    f"**Bid Stack:** {data.get('bid_size', 0)} shares\n"
                    f"**Ask Wall:** {data.get('ask_size', 0)} shares\n"
                    f"**Filled:** {vol_pct:.1f}% of 1.5M Target"
                ),
                "inline": False
            }
        ],
        "footer": {
            "text": f"Kurrupt Research | {data.get('source')} | {datetime.now().strftime('%H:%M:%S ET')}"
        }
    }

    return {"embeds": [embed]}

def main():
    print(f"Starting FLWS Monitor at {datetime.now()}")
    
    if not WEBHOOK_URL:
        print("❌ No Webhook URL found. Set DISCORD_WEBHOOK_URL env var or checking secrets.toml.")
        return

    data = get_live_data()
    if not data:
        print("❌ Failed to get live data.")
        return

    print("="*60)
    print(f"FLWS INSTITUTIONAL MONITOR | {datetime.now().strftime('%H:%M:%S')}")
    print(f"Source: {data.get('source', 'Unknown')}")
    print(f"Price:  ${data['price']} ({data['change_pct']}%)")
    print(f"Volume: {data['volume']:,} (Goal: {VACUUM_VOLUME_TARGET:,})")
    print("="*60)

    payload = generate_status_report(data)
    
    if not DISCORD_ENABLED:
        print("🔒 DISCORD NOTIFICATIONS MUTED (Safety Switch Active)")
        print("   -> To enable, set DISCORD_ENABLED = True in script")
        return

    try:
        response = requests.post(WEBHOOK_URL, json=payload)

        if response.status_code in [200, 204]:
            print("✅ Discord Alert Sent Successfully.")
        else:
            print(f"❌ Discord Send Failed: {response.status_code} {response.text}")
    except Exception as e:
        print(f"❌ Connection Error: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Run in continuous loop mode (9 mins)")
    args = parser.parse_args()

    if args.loop:
        print("🔁 STARTING CONTINUOUS MONITOR (9 Minute Session)")
        # Run for ~9 minutes to fit inside 10-min Cron Schedule
        # 9 * 60 = 540 seconds
        end_time = time.time() + 540 
        
        while time.time() < end_time:
            try:
                main()
            except Exception as e:
                print(f"Loop Error: {e}")
            
            print("⏳ Sleeping 60s...")
            time.sleep(60)
            print("-" * 30)
    else:
        main()
