import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
import time
import requests
import os

# ============================================================
# CONFIG — APNA TOKEN AUR ID YAHAN DAALO
# ============================================================
TELEGRAM_TOKEN = "APNA_TOKEN_YAHAN"
CHAT_ID        = "APNA_CHAT_ID_YAHAN"

# ============================================================
# SETTINGS
# ============================================================
ACTIVE_COINS = [
    'BTC/USDT',
    'ETH/USDT',
    'BNB/USDT',
    'SOL/USDT',
]
SCAN_INTERVAL = 300  # har 5 minute mein scan
SL_BUFFER     = 0.003  # 0.3% SL buffer
CONFIRM_WAIT  = 120    # 2 minute confirmation

exchange = ccxt.okx({'enableRateLimit': True})

# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(text):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {'chat_id': CHAT_ID, 'text': text, 'parse_mode': 'Markdown'}
    try:
        r = requests.post(url, data=data, timeout=10)
        if r.status_code == 200:
            print(f"  📤 Telegram sent ✅")
        else:
            print(f"  ⚠️ Telegram error: {r.text}")
    except Exception as e:
        print(f"  ⚠️ {e}")

# ============================================================
# DATA FETCH
# ============================================================
def fetch_ohlcv(symbol, timeframe, days=3):
    all_data = []
    since = exchange.parse8601(
        (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')
    )
    while True:
        try:
            data = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=300)
            if not data:
                break
            all_data += data
            since = data[-1][0] + 1
            if len(data) < 300:
                break
            time.sleep(0.3)
        except Exception as e:
            print(f"⚠️ {symbol} {timeframe}: {e}")
            break
    if not all_data:
        return pd.DataFrame()
    df = pd.DataFrame(all_data, columns=['timestamp','open','high','low','close','volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    df = df[~df.index.duplicated(keep='first')]
    return df

def get_live_price(symbol):
    try:
        ticker = exchange.fetch_ticker(symbol)
        return float(ticker['last'])
    except:
        return None

# ============================================================
# STRATEGY LOGIC
# ============================================================
def get_previous_day_levels(daily_df, date):
    prev_days = daily_df[pd.to_datetime(daily_df.index).date < date]
    if len(prev_days) == 0:
        return None, None
    prev = prev_days.iloc[-1]
    return float(prev['high']), float(prev['low'])

def find_signal_candle(m1_slice, direction):
    for i in range(len(m1_slice)):
        c = m1_slice.iloc[i]
        if direction == 'sell' and c['close'] < c['open']:
            return i, c
        elif direction == 'buy' and c['close'] > c['open']:
            return i, c
    return None, None

def find_entry(m1_slice, signal_candle, direction):
    sl = float(signal_candle['low'])
    sh = float(signal_candle['high'])
    for i in range(1, len(m1_slice)):
        c = m1_slice.iloc[i]
        if direction == 'sell' and c['low'] < sl:
            return sl, sh
        elif direction == 'buy' and c['high'] > sh:
            return sh, sl
    return None, None

def get_swing_target(m1_df, entry_idx, direction, lookback=200):
    start  = max(0, entry_idx - lookback)
    window = m1_df.iloc[start:entry_idx]
    if len(window) == 0:
        return None
    return float(window['low'].min()) if direction == 'sell' else float(window['high'].max())

def confidence_label(dist_pct):
    if dist_pct >= 1.0:
        return "🔥 HIGH"
    elif dist_pct >= 0.5:
        return "✅ MEDIUM"
    else:
        return "⚠️ LOW"

# ============================================================
# CONFIRM SIGNAL (2 MIN WATCH)
# ============================================================
def confirm_signal(symbol, direction, entry_price):
    print(f"  ⏳ 2 min confirmation...")
    time.sleep(CONFIRM_WAIT)
    live = get_live_price(symbol)
    if live is None:
        return False
    if direction == 'sell':
        confirmed = live < entry_price
    else:
        confirmed = live > entry_price
    if confirmed:
        print(f"  ✅ Confirmed! Live: {live:.4f}")
    else:
        print(f"  ❌ Not confirmed. Live: {live:.4f}")
    return confirmed

# ============================================================
# SCAN COIN
# ============================================================
def scan_coin(symbol):
    try:
        today  = datetime.now(timezone.utc).date()
        daily  = fetch_ohlcv(symbol, '1d', days=5)
        m1     = fetch_ohlcv(symbol, '1m', days=2)

        if len(daily) < 2 or len(m1) < 10:
            return None

        pdh, pdl = get_previous_day_levels(daily, today)
        if pdh is None:
            return None

        day_m1 = m1[m1.index.date == today].copy()
        if len(day_m1) < 5:
            return None

        # Sirf last 5 min candles
        now_utc   = datetime.now(timezone.utc)
        cutoff    = now_utc - timedelta(minutes=5)
        recent_m1 = day_m1[day_m1.index >= pd.Timestamp(cutoff).tz_localize(None)]

        if len(recent_m1) < 2:
            return None

        for i in range(1, len(recent_m1)):
            candle = recent_m1.iloc[i]

            # ---- SELL SETUP ----
            if float(candle['high']) > pdh:
                slice_s   = recent_m1.iloc[i:]
                sig_i, sc = find_signal_candle(slice_s, 'sell')
                if sig_i is not None:
                    result = find_entry(slice_s.iloc[sig_i:], sc, 'sell')
                    if result[0]:
                        entry, raw_sl = result
                        sl_price  = raw_sl * (1 + SL_BUFFER)
                        sl_dist   = abs(entry - sl_price) / entry * 100
                        if sl_dist > 0.5:
                            continue
                        actual_i  = i + sig_i
                        tp1       = get_swing_target(day_m1, actual_i, 'sell')
                        if tp1 and tp1 < entry:
                            dist_pct  = abs(entry - tp1) / entry * 100
                            risk      = abs(entry - sl_price)
                            tp2       = entry - (risk * 10)
                            live_p    = get_live_price(symbol)
                            return {
                                'symbol'   : symbol,
                                'direction': 'sell',
                                'entry'    : live_p or entry,
                                'sl'       : round(sl_price, 6),
                                'tp1'      : round(tp1, 6),
                                'tp2'      : round(tp2, 6),
                                'dist_pct' : dist_pct,
                            }

            # ---- BUY SETUP ----
            if float(candle['low']) < pdl:
                slice_b   = recent_m1.iloc[i:]
                sig_i, sc = find_signal_candle(slice_b, 'buy')
                if sig_i is not None:
                    result = find_entry(slice_b.iloc[sig_i:], sc, 'buy')
                    if result[0]:
                        entry, raw_sl = result
                        sl_price  = raw_sl * (1 - SL_BUFFER)
                        sl_dist   = abs(entry - sl_price) / entry * 100
                        if sl_dist > 0.5:
                            continue
                        actual_i  = i + sig_i
                        tp1       = get_swing_target(day_m1, actual_i, 'buy')
                        if tp1 and tp1 > entry:
                            dist_pct  = abs(tp1 - entry) / entry * 100
                            risk      = abs(tp1 - entry)
                            tp2       = entry + (risk * 10)
                            live_p    = get_live_price(symbol)
                            return {
                                'symbol'   : symbol,
                                'direction': 'buy',
                                'entry'    : live_p or entry,
                                'sl'       : round(sl_price, 6),
                                'tp1'      : round(tp1, 6),
                                'tp2'      : round(tp2, 6),
                                'dist_pct' : dist_pct,
                            }
    except Exception as e:
        print(f"  ⚠️ {symbol}: {e}")
    return None

# ============================================================
# MAIN LOOP
# ============================================================
def run():
    print("=" * 50)
    print("🤖 TRADE VISION BOT — LIVE!")
    print(f"   Coins    : {ACTIVE_COINS}")
    print(f"   Interval : {SCAN_INTERVAL}s")
    print("=" * 50)

    send_telegram(
        "🤖 *Trade Vision Bot Started!*\n\n"
        "✅ 4 Coins Monitor Ho Rahe Hain\n"
        "⚡ Precision Liquidity Strategy\n"
        "🎯 Best Setup Signal Aayega"
    )

    scan_num  = 0
    last_date = datetime.now(timezone.utc).date()
    sent_today = set()

    while True:
        scan_num += 1
        now   = datetime.now().strftime('%H:%M:%S')
        today = datetime.now(timezone.utc).date()

        # Naya din — reset
        if today != last_date:
            sent_today = set()
            last_date  = today
            print(f"🔄 New day — reset!")

        print(f"\n[Scan #{scan_num}] {now}")

        all_signals = []

        for symbol in ACTIVE_COINS:
            key = f"{symbol}_{today}"
            if key in sent_today:
                print(f"  {symbol} — already sent today")
                continue
            print(f"  Scanning {symbol}...", end=' ')
            result = scan_coin(symbol)
            if result:
                print(f"✅ Setup! dist:{result['dist_pct']:.2f}%")
                all_signals.append(result)
            else:
                print(f"❌")
            time.sleep(0.5)

        if not all_signals:
            print("  📭 No signals")
        else:
            # Best coin — highest TP1 distance
            best = max(all_signals, key=lambda x: x['dist_pct'])
            coin = best['symbol'].replace('/USDT', '')

            print(f"\n  🏆 Best: {best['symbol']} {best['direction'].upper()}")

            # 2 min confirmation
            confirmed = confirm_signal(best['symbol'], best['direction'], best['entry'])

            if confirmed:
                emoji = "📈" if best['direction'] == 'buy' else "📉"
                arrow = "🟢 BUY" if best['direction'] == 'buy' else "🔴 SELL"
                conf  = confidence_label(best['dist_pct'])

                msg = (
                    f"🚨 *TRADE VISION SIGNAL* 🚨\n\n"
                    f"💰 *{coin}/USDT*\n"
                    f"{emoji} *{arrow}*\n\n"
                    f"📌 *Entry* : `{best['entry']:.4f}`\n"
                    f"🛑 *SL*    : `{best['sl']:.4f}`\n"
                    f"🎯 *TP1*   : `{best['tp1']:.4f}` _(80% close)_\n"
                    f"🚀 *TP2*   : `{best['tp2']:.4f}` _(20% hold)_\n\n"
                    f"📊 *Confidence* : {conf}\n\n"
                    f"⚡ _Precision Liquidity Strategy_"
                )

                send_telegram(msg)
                sent_today.add(f"{best['symbol']}_{today}")
                print(f"  ✅ Signal sent!")
            else:
                print(f"  ❌ Not confirmed — skip")

        print(f"  Next scan: {SCAN_INTERVAL}s...")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    run()
