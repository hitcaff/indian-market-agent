"""
Live Market Monitor
Runs on PC during market hours: 9:00 AM - 3:35 PM IST
Monitors price against SL/T1/T2 every 2 minutes
Sends real-time Telegram alerts when levels are hit
"""

import requests
import json
import os
import time
import sys
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# FVG candle lookback for 15min data
FVG_LOOKBACK_CANDLES = 20

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; indian-market-agent/1.0)"}
CHECK_INTERVAL = 120  # seconds between price checks

MARKET_OPEN = (9, 15)   # 9:15 AM IST
MARKET_CLOSE = (15, 30)  # 3:30 PM IST


def get_ist_now():
    return datetime.now(IST)


def is_market_open() -> bool:
    now = get_ist_now()
    # Skip weekends
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    open_t = MARKET_OPEN[0] * 60 + MARKET_OPEN[1]
    close_t = MARKET_CLOSE[0] * 60 + MARKET_CLOSE[1]
    return open_t <= t <= close_t


def fetch_live_price(symbol: str) -> float:
    """Fetch current live price from Yahoo Finance."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {"interval": "2m", "range": "1d"}
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        meta = data["chart"]["result"][0]["meta"]
        return meta.get("regularMarketPrice", 0)
    except Exception as e:
        print(f"[WARN] Live price fetch failed for {symbol}: {e}")
        return 0


def send_alert(message: str, bot_token: str, chat_id: str):
    """Send immediate alert to Telegram."""
    try:
        resp = requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10)
        if resp.ok:
            print(f"[ALERT] Sent: {message[:80]}")
        else:
            print(f"[WARN] Alert failed: {resp.text[:100]}")
    except Exception as e:
        print(f"[WARN] Alert error: {e}")




def fetch_15min_ohlc(symbol: str) -> dict:
    """Fetch 15min intraday OHLC from Yahoo Finance."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {"interval": "15m", "range": "1d"}
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        result = data["chart"]["result"][0]
        quotes = result["indicators"]["quote"][0]
        highs  = [h for h in quotes.get("high", [])  if h is not None]
        lows   = [l for l in quotes.get("low", [])   if l is not None]
        closes = [c for c in quotes.get("close", []) if c is not None]
        return {"highs": highs, "lows": lows, "closes": closes}
    except Exception as e:
        print(f"[WARN] 15min OHLC failed for {symbol}: {e}")
        return {}


def compute_fvg_live(highs: list, lows: list, closes: list) -> list:
    """Compute FVGs from 15min candles."""
    fvgs = []
    n = len(closes)
    if n < 3:
        return fvgs
    current = closes[-1] if closes else 0
    # Only last FVG_LOOKBACK_CANDLES candles
    start = max(1, n - FVG_LOOKBACK_CANDLES)
    for i in range(start, n - 1):
        h_prev, l_prev = highs[i-1], lows[i-1]
        h_next, l_next = highs[i+1], lows[i+1]
        if h_prev < l_next:
            gap_low, gap_high = round(h_prev, 2), round(l_next, 2)
            filled = current <= gap_low
            if not filled:
                fvgs.append({
                    "type": "bullish", "gap_low": gap_low, "gap_high": gap_high,
                    "midpoint": round((gap_low + gap_high) / 2, 2),
                    "size": round(gap_high - gap_low, 2),
                    "price_inside": gap_low <= current <= gap_high,
                    "label": "15min",
                })
        elif l_prev > h_next:
            gap_high, gap_low = round(l_prev, 2), round(h_next, 2)
            filled = current >= gap_high
            if not filled:
                fvgs.append({
                    "type": "bearish", "gap_high": gap_high, "gap_low": gap_low,
                    "midpoint": round((gap_low + gap_high) / 2, 2),
                    "size": round(gap_high - gap_low, 2),
                    "price_inside": gap_low <= current <= gap_high,
                    "label": "15min",
                })
    return fvgs


def save_fvg_zones(instrument: str, fvgs: list):
    """Save active 15min FVGs to file for research.py to use."""
    os.makedirs("data", exist_ok=True)
    path = f"data/fvg_{instrument.lower()}_15min.json"
    with open(path, "w") as f:
        json.dump(fvgs, f, indent=2)


def check_fvg_alert(price: float, fvgs: list, direction: str) -> str:
    """Check if price has entered a relevant FVG zone."""
    for fvg in fvgs:
        if fvg.get("price_inside"):
            fvg_type = fvg["type"]
            gap_low = fvg["gap_low"]
            gap_high = fvg["gap_high"]
            if (direction == "LONG" and fvg_type == "bullish") or                (direction == "SHORT" and fvg_type == "bearish"):
                return f"Price inside {fvg_type} FVG {gap_low}-{gap_high} — potential reversal zone"
    return ""

def monitor_trade(trade: dict, symbol: str, instrument: str, bot_token: str, chat_id: str) -> str:
    """
    Monitor a single trade. Returns: 'SL_HIT', 'T1_HIT', 'T2_HIT', 'ACTIVE', 'NO_TRADE'
    """
    direction = trade.get("direction", "NO TRADE")
    if direction == "NO TRADE":
        return "NO_TRADE"

    sl = trade.get("stop_loss", 0)
    t1 = trade.get("target_1", 0)
    t2 = trade.get("target_2", 0)
    entry = trade.get("entry_mid", 0)

    price = fetch_live_price(symbol)
    if not price:
        return "ACTIVE"

    now_str = get_ist_now().strftime("%H:%M IST")
    print(f"[LIVE] {instrument}: {price:,.2f} | SL: {sl} | T1: {t1} | T2: {t2}")

    if direction == "LONG":
        if price <= sl:
            msg = f"🔴 *{instrument} SL HIT* ⚠️\nPrice: {price:,.2f} | SL: {sl}\nTime: {now_str}\n_Stop loss triggered — exit position_"
            send_alert(msg, bot_token, chat_id)
            return "SL_HIT"
        elif price >= t2:
            msg = f"🎯 *{instrument} TARGET 2 HIT* 🏆\nPrice: {price:,.2f} | T2: {t2}\nTime: {now_str}\n_Consider booking full profits_"
            send_alert(msg, bot_token, chat_id)
            return "T2_HIT"
        elif price >= t1:
            msg = f"✅ *{instrument} TARGET 1 HIT*\nPrice: {price:,.2f} | T1: {t1}\nTime: {now_str}\n_Consider booking partial profits, trail SL to entry_"
            send_alert(msg, bot_token, chat_id)
            return "T1_HIT"

    elif direction == "SHORT":
        if price >= sl:
            msg = f"🔴 *{instrument} SL HIT* ⚠️\nPrice: {price:,.2f} | SL: {sl}\nTime: {now_str}\n_Stop loss triggered — exit position_"
            send_alert(msg, bot_token, chat_id)
            return "SL_HIT"
        elif price <= t2:
            msg = f"🎯 *{instrument} TARGET 2 HIT* 🏆\nPrice: {price:,.2f} | T2: {t2}\nTime: {now_str}\n_Consider booking full profits_"
            send_alert(msg, bot_token, chat_id)
            return "T2_HIT"
        elif price <= t1:
            msg = f"✅ *{instrument} TARGET 1 HIT*\nPrice: {price:,.2f} | T1: {t1}\nTime: {now_str}\n_Consider booking partial profits, trail SL to entry_"
            send_alert(msg, bot_token, chat_id)
            return "T1_HIT"

    return "ACTIVE"




def fetch_intraday_candles(symbol: str, interval: str = "15m") -> dict:
    """Fetch intraday OHLC candles from Yahoo Finance."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {"interval": interval, "range": "1d"}
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
        quotes = result["indicators"]["quote"][0]
        timestamps = result.get("timestamp", [])
        highs  = [h for h in quotes.get("high",  []) if h is not None]
        lows   = [l for l in quotes.get("low",   []) if l is not None]
        closes = [c for c in quotes.get("close", []) if c is not None]
        opens  = [o for o in quotes.get("open",  []) if o is not None]
        return {"highs": highs, "lows": lows, "closes": closes, "opens": opens, "timestamps": timestamps}
    except Exception as e:
        print(f"[WARN] Intraday candles failed for {symbol}: {e}")
        return {}


def compute_fvg(highs: list, lows: list, closes: list, label: str = "15min") -> list:
    """Detect Fair Value Gaps from OHLC data."""
    fvgs = []
    if len(highs) < 3:
        return fvgs
    current = closes[-1] if closes else 0
    for i in range(1, len(highs) - 1):
        h_prev, l_prev = highs[i-1], lows[i-1]
        h_next, l_next = highs[i+1], lows[i+1]
        # Bullish FVG
        if h_prev < l_next:
            filled = current <= h_prev
            if not filled:
                fvgs.append({"type": "bullish", "gap_low": round(h_prev, 2),
                    "gap_high": round(l_next, 2), "mid": round((h_prev + l_next) / 2, 2),
                    "size": round(l_next - h_prev, 2), "timeframe": label, "filled": False})
        # Bearish FVG
        elif l_prev > h_next:
            filled = current >= l_prev
            if not filled:
                fvgs.append({"type": "bearish", "gap_low": round(h_next, 2),
                    "gap_high": round(l_prev, 2), "mid": round((h_next + l_prev) / 2, 2),
                    "size": round(l_prev - h_next, 2), "timeframe": label, "filled": False})
    fvgs.sort(key=lambda x: abs(x["mid"] - current))
    return fvgs


def check_fvg_entry(price: float, fvgs: list, direction: str) -> dict:
    """Check if price has entered an FVG zone — signals high-probability entry."""
    for fvg in fvgs:
        if fvg["gap_low"] <= price <= fvg["gap_high"]:
            if (direction == "LONG" and fvg["type"] == "bullish") or                (direction == "SHORT" and fvg["type"] == "bearish"):
                return fvg
    return None


def save_fvg_zones(nifty_fvgs: list, bnf_fvgs: list):
    """Save today's open FVGs for post-market research to use."""
    os.makedirs("data", exist_ok=True)
    today = get_ist_now().strftime("%Y-%m-%d")
    data = {
        "date": today,
        "saved_at": get_ist_now().strftime("%H:%M IST"),
        "NIFTY": {"fvgs": nifty_fvgs, "count": len(nifty_fvgs)},
        "BANKNIFTY": {"fvgs": bnf_fvgs, "count": len(bnf_fvgs)},
    }
    with open("data/fvg_zones.json", "w") as f:
        json.dump(data, f, indent=2)
    print(f"[INFO] Saved {len(nifty_fvgs)} Nifty + {len(bnf_fvgs)} BNF FVGs to data/fvg_zones.json")

def run_live_monitor():
    """Main live monitoring loop."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        print("[ERROR] Telegram credentials not set.")
        sys.exit(1)

    print(f"[START] Live Monitor — {get_ist_now().strftime('%Y-%m-%d %H:%M IST')}")

    # Wait for market open
    while not is_market_open():
        now = get_ist_now()
        print(f"[INFO] Market closed. Current time: {now.strftime('%H:%M IST')}. Waiting...")
        time.sleep(60)
        # Exit if past market hours
        t = now.hour * 60 + now.minute
        if t > 15 * 60 + 35:
            print("[INFO] Market closed for the day. Exiting.")
            sys.exit(0)

    # Load today's signal
    if not os.path.exists("data/latest_signal.json"):
        print("[WARN] No signal found. Run research.py first.")
        send_alert("⚠️ *Live Monitor*: No trade signal found for today. Run research first.", bot_token, chat_id)
        sys.exit(1)

    with open("data/latest_signal.json") as f:
        signal = json.load(f)

    nifty_trade = signal.get("nifty_trade", {})
    bnf_trade = signal.get("banknifty_trade", {})

    # Parse entry zones from signal format
    def parse_trade(trade_sig: dict, instrument: str) -> dict:
        entry_zone = trade_sig.get("entry_zone", "0-0")
        try:
            parts = entry_zone.replace(",", "").split("-")
            entry_mid = (float(parts[0].strip()) + float(parts[1].strip())) / 2
        except:
            entry_mid = 0
        return {
            "instrument": instrument,
            "direction": trade_sig.get("direction", "NO TRADE"),
            "entry_mid": entry_mid,
            "stop_loss": trade_sig.get("stop_loss", 0),
            "target_1": trade_sig.get("target_1", 0),
            "target_2": trade_sig.get("target_2", 0),
        }

    nifty = parse_trade(nifty_trade, "NIFTY")
    bnf = parse_trade(bnf_trade, "BANKNIFTY")

    # Send market open alert
    open_msg = f"🔔 *Market Open — Live Monitor Active*\n"
    if nifty["direction"] != "NO TRADE":
        open_msg += f"\n*NIFTY {nifty['direction']}*\nEntry: {nifty_trade.get('entry_zone')} | SL: {nifty['stop_loss']} | T1: {nifty['target_1']} | T2: {nifty['target_2']}"
    if bnf["direction"] != "NO TRADE":
        open_msg += f"\n\n*BANKNIFTY {bnf['direction']}*\nEntry: {bnf_trade.get('entry_zone')} | SL: {bnf['stop_loss']} | T1: {bnf['target_1']} | T2: {bnf['target_2']}"
    send_alert(open_msg, bot_token, chat_id)

    # Track status to avoid duplicate alerts
    nifty_status = "ACTIVE"
    bnf_status = "ACTIVE"
    t1_alerted_nifty = False
    t1_alerted_bnf = False
    fvg_cycle = 0  # fetch FVGs every 4 cycles (~8 min)

    # Main monitoring loop
    while is_market_open():
        # ── FVG tracking every 4 cycles ───────────────────────────────────────
        if fvg_cycle % 4 == 0:
            nifty_ohlc = fetch_15min_ohlc("^NSEI")
            bnf_ohlc = fetch_15min_ohlc("^NSEBANK")

            if nifty_ohlc:
                nifty_fvgs = compute_fvg_live(nifty_ohlc["highs"], nifty_ohlc["lows"], nifty_ohlc["closes"])
                save_fvg_zones("nifty", nifty_fvgs)
                # Alert if price enters FVG zone aligned with trade
                if nifty["direction"] != "NO TRADE":
                    fvg_alert = check_fvg_alert(fetch_live_price("^NSEI"), nifty_fvgs, nifty["direction"])
                    if fvg_alert:
                        send_alert(f"📐 *NIFTY FVG Alert*\n{fvg_alert}\nTime: {get_ist_now().strftime('%H:%M IST')}", bot_token, chat_id)

            if bnf_ohlc:
                bnf_fvgs = compute_fvg_live(bnf_ohlc["highs"], bnf_ohlc["lows"], bnf_ohlc["closes"])
                save_fvg_zones("banknifty", bnf_fvgs)
                if bnf["direction"] != "NO TRADE":
                    fvg_alert = check_fvg_alert(fetch_live_price("^NSEBANK"), bnf_fvgs, bnf["direction"])
                    if fvg_alert:
                        send_alert(f"📐 *BANKNIFTY FVG Alert*\n{fvg_alert}\nTime: {get_ist_now().strftime('%H:%M IST')}", bot_token, chat_id)

        fvg_cycle += 1

        # Monitor Nifty
        if nifty_status == "ACTIVE" and nifty["direction"] != "NO TRADE":
            status = monitor_trade(nifty, "^NSEI", "NIFTY", bot_token, chat_id)
            if status == "T1_HIT" and not t1_alerted_nifty:
                t1_alerted_nifty = True
            elif status in ["SL_HIT", "T2_HIT"]:
                nifty_status = status

        time.sleep(5)

        # Monitor Bank Nifty
        if bnf_status == "ACTIVE" and bnf["direction"] != "NO TRADE":
            status = monitor_trade(bnf, "^NSEBANK", "BANKNIFTY", bot_token, chat_id)
            if status == "T1_HIT" and not t1_alerted_bnf:
                t1_alerted_bnf = True
            elif status in ["SL_HIT", "T2_HIT"]:
                bnf_status = status

        # Both trades done
        if nifty_status in ["SL_HIT", "T2_HIT"] and bnf_status in ["SL_HIT", "T2_HIT", "NO_TRADE"]:
            # Save final FVGs before exit
            print("[INFO] Both trades completed. Saving final FVG zones.")
            if nifty_ohlc:
                save_fvg_zones("nifty", compute_fvg_live(nifty_ohlc["highs"], nifty_ohlc["lows"], nifty_ohlc["closes"]))
            if bnf_ohlc:
                save_fvg_zones("banknifty", compute_fvg_live(bnf_ohlc["highs"], bnf_ohlc["lows"], bnf_ohlc["closes"]))
            break

        print(f"[INFO] Next check in {CHECK_INTERVAL}s... [{get_ist_now().strftime('%H:%M:%S IST')}]")
        time.sleep(CHECK_INTERVAL)

    # Save final FVG zones for post-market research
    try:
        nifty_ohlc_final = fetch_15min_ohlc("^NSEI")
        bnf_ohlc_final = fetch_15min_ohlc("^NSEBANK")
        if nifty_ohlc_final:
            save_fvg_zones("nifty", compute_fvg_live(nifty_ohlc_final["highs"], nifty_ohlc_final["lows"], nifty_ohlc_final["closes"]))
        if bnf_ohlc_final:
            save_fvg_zones("banknifty", compute_fvg_live(bnf_ohlc_final["highs"], bnf_ohlc_final["lows"], bnf_ohlc_final["closes"]))
        print("[INFO] Final FVG zones saved for post-market research.")
    except Exception as e:
        print(f"[WARN] Final FVG save failed: {e}")

    # Final FVG save at close
    save_fvg_zones(nifty_fvgs, bnf_fvgs)

    # Market close alert — concise
    close_msg = (f"🔔 *Market Closed {get_ist_now().strftime('%H:%M IST')}*\n"
                 f"NIFTY: {nifty_status} | BNF: {bnf_status}\n"
                 f"FVGs saved: {len(nifty_fvgs)} Nifty, {len(bnf_fvgs)} BNF\n"
                 f"_EOD P&L at 4:00 PM IST_")
    send_alert(close_msg, bot_token, chat_id)
    print("[DONE] Live monitor complete.")


if __name__ == "__main__":
    run_live_monitor()
