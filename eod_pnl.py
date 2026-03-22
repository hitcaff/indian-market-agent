"""
EOD P&L Calculator
Runs at 4:00 PM IST — calculates actual P&L for today's trade
Records to trade journal and triggers learning
"""

import requests
import json
import os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
NIFTY_LOT = 75
BANKNIFTY_LOT = 35
LOTS = 2
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; indian-market-agent/1.0)"}


def get_ist_now():
    return datetime.now(IST)


def fetch_eod_data(symbol: str) -> dict:
    """Fetch today's OHLC data from Yahoo Finance."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {"interval": "1d", "range": "2d"}
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        result = data["chart"]["result"][0]
        quotes = result["indicators"]["quote"][0]

        highs = [h for h in quotes.get("high", []) if h is not None]
        lows = [l for l in quotes.get("low", []) if l is not None]
        closes = [c for c in quotes.get("close", []) if c is not None]
        opens = [o for o in quotes.get("open", []) if o is not None]

        return {
            "symbol": symbol,
            "today_open": opens[-1] if opens else 0,
            "today_high": highs[-1] if highs else 0,
            "today_low": lows[-1] if lows else 0,
            "today_close": closes[-1] if closes else 0,
        }
    except Exception as e:
        print(f"[WARN] EOD fetch failed for {symbol}: {e}")
        return {}


def calculate_pnl(trade: dict, eod: dict) -> dict:
    """Calculate actual P&L based on EOD data."""
    direction = trade.get("direction", "NO TRADE")
    if direction == "NO TRADE":
        return {"result": "NO TRADE", "pnl_points": 0, "pnl_inr": 0}

    instrument = trade.get("instrument", "NIFTY")
    lot_size = NIFTY_LOT if instrument == "NIFTY" else BANKNIFTY_LOT

    entry = trade.get("entry_mid", 0)
    sl = trade.get("stop_loss", 0)
    t1 = trade.get("target_1", 0)
    t2 = trade.get("target_2", 0)

    today_high = eod.get("today_high", 0)
    today_low = eod.get("today_low", 0)
    today_close = eod.get("today_close", 0)

    # Simulate trade assuming entry at open or entry_mid
    entry_price = eod.get("today_open", entry)

    if direction == "LONG":
        # Check if SL hit first
        sl_hit = today_low <= sl
        t1_hit = today_high >= t1
        t2_hit = today_high >= t2

        if sl_hit and not t1_hit:
            result = "SL_HIT"
            exit_price = sl
        elif t2_hit:
            result = "T2_HIT"
            exit_price = t2
        elif t1_hit:
            result = "T1_HIT"
            exit_price = t1
        else:
            result = "OPEN_EOD"
            exit_price = today_close

        pnl_points = exit_price - entry_price

    elif direction == "SHORT":
        sl_hit = today_high >= sl
        t1_hit = today_low <= t1
        t2_hit = today_low <= t2

        if sl_hit and not t1_hit:
            result = "SL_HIT"
            exit_price = sl
        elif t2_hit:
            result = "T2_HIT"
            exit_price = t2
        elif t1_hit:
            result = "T1_HIT"
            exit_price = t1
        else:
            result = "OPEN_EOD"
            exit_price = today_close

        pnl_points = entry_price - exit_price

    pnl_inr = round(pnl_points * lot_size * LOTS)
    win = pnl_points > 0

    return {
        "result": result,
        "entry_price": round(entry_price, 2),
        "exit_price": round(exit_price, 2),
        "pnl_points": round(pnl_points, 2),
        "pnl_inr": pnl_inr,
        "win": win,
        "today_high": today_high,
        "today_low": today_low,
        "today_close": today_close,
    }


def update_trade_journal(trade: dict, pnl: dict, signal: dict):
    """Append completed trade to journal."""
    os.makedirs("data", exist_ok=True)
    trades = []
    if os.path.exists("data/trades.json"):
        with open("data/trades.json") as f:
            trades = json.load(f)

    record = {
        "date": get_ist_now().strftime("%Y-%m-%d"),
        "instrument": trade.get("instrument", "NIFTY"),
        "direction": trade.get("direction", "NO TRADE"),
        "entry_mid": trade.get("entry_mid", 0),
        "stop_loss": trade.get("stop_loss", 0),
        "target_1": trade.get("target_1", 0),
        "target_2": trade.get("target_2", 0),
        "confidence": trade.get("confidence", 0),
        "factor_signals": signal.get("factor_signals", {}),
        "result": pnl.get("result", ""),
        "entry_price": pnl.get("entry_price", 0),
        "exit_price": pnl.get("exit_price", 0),
        "pnl_points": pnl.get("pnl_points", 0),
        "pnl_inr": pnl.get("pnl_inr", 0),
        "win": pnl.get("win", False),
        "today_high": pnl.get("today_high", 0),
        "today_low": pnl.get("today_low", 0),
    }

    trades.append(record)
    with open("data/trades.json", "w") as f:
        json.dump(trades, f, indent=2)
    print(f"[INFO] Trade recorded: {record['instrument']} {record['direction']} | {record['result']} | ₹{record['pnl_inr']:,}")
    return record


def update_performance(trades: list):
    """Recalculate performance stats."""
    completed = [t for t in trades if t.get("result") not in ["NO TRADE", None]]
    if not completed:
        return

    wins = [t for t in completed if t.get("win")]
    losses = [t for t in completed if not t.get("win")]

    total_pnl = sum(t.get("pnl_inr", 0) for t in completed)
    win_rate = len(wins) / len(completed) * 100 if completed else 0

    # Streak
    streak = 0
    streak_type = None
    for t in reversed(completed):
        if streak == 0:
            streak_type = "WIN" if t.get("win") else "LOSS"
            streak = 1
        elif (t.get("win") and streak_type == "WIN") or (not t.get("win") and streak_type == "LOSS"):
            streak += 1
        else:
            break

    # Per instrument
    nifty_trades = [t for t in completed if t.get("instrument") == "NIFTY"]
    bnf_trades = [t for t in completed if t.get("instrument") == "BANKNIFTY"]

    best = max(completed, key=lambda x: x.get("pnl_inr", 0)) if completed else None
    worst = min(completed, key=lambda x: x.get("pnl_inr", 0)) if completed else None

    perf = {
        "total_trades": len(completed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "total_pnl_inr": total_pnl,
        "avg_pnl_per_trade": round(total_pnl / len(completed)) if completed else 0,
        "best_trade": best,
        "worst_trade": worst,
        "current_streak": streak,
        "streak_type": streak_type,
        "nifty_stats": {
            "trades": len(nifty_trades),
            "wins": len([t for t in nifty_trades if t.get("win")]),
            "pnl_inr": sum(t.get("pnl_inr", 0) for t in nifty_trades),
        },
        "banknifty_stats": {
            "trades": len(bnf_trades),
            "wins": len([t for t in bnf_trades if t.get("win")]),
            "pnl_inr": sum(t.get("pnl_inr", 0) for t in bnf_trades),
        },
    }

    with open("data/performance.json", "w") as f:
        json.dump(perf, f, indent=2)

    print(f"[INFO] Performance updated: {len(completed)} trades | WR: {win_rate:.1f}% | P&L: ₹{total_pnl:,}")
    return perf


def send_pnl_telegram(records: list, perf: dict, bot_token: str, chat_id: str):
    """Send EOD P&L summary to Telegram."""
    lines = [f"📊 *EOD P&L Report — {get_ist_now().strftime('%B %d, %Y')}*\n"]

    total_day_pnl = 0
    for r in records:
        if r.get("result") == "NO TRADE":
            continue
        emoji = "✅" if r.get("win") else "❌"
        lines.append(f"{emoji} *{r['instrument']}* {r['direction']}")
        lines.append(f"Entry: {r['entry_price']} → Exit: {r['exit_price']}")
        lines.append(f"Result: {r['result']} | Points: {r['pnl_points']:+.0f} | P&L: ₹{r['pnl_inr']:,}\n")
        total_day_pnl += r.get("pnl_inr", 0)

    day_emoji = "🟢" if total_day_pnl > 0 else "🔴" if total_day_pnl < 0 else "🟡"
    lines.append(f"{day_emoji} *Today's Total: ₹{total_day_pnl:,}*\n")

    lines += [
        f"*📈 Running Scorecard*",
        f"Total Trades: {perf.get('total_trades', 0)}",
        f"Win Rate: {perf.get('win_rate', 0)}%",
        f"Total P&L: ₹{perf.get('total_pnl_inr', 0):,}",
        f"Avg/Trade: ₹{perf.get('avg_pnl_per_trade', 0):,}",
        f"Streak: {perf.get('current_streak', 0)} {perf.get('streak_type', '')}s",
    ]

    message = "\n".join(lines)
    if len(message) > 4096:
        message = message[:4090] + "\n..."

    resp = requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
        timeout=15)
    if resp.ok:
        print("[INFO] P&L sent to Telegram.")
    else:
        print(f"[WARN] Telegram failed: {resp.text[:200]}")


def run_eod():
    """Run EOD P&L calculation."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    print(f"[START] EOD P&L Calculator — {get_ist_now().strftime('%Y-%m-%d %H:%M IST')}")

    # Load today's final signal
    if not os.path.exists("data/latest_signal.json"):
        print("[WARN] No signal found for today.")
        return

    with open("data/latest_signal.json") as f:
        signal = json.load(f)

    # Check signal is from today
    signal_date = signal.get("date", "")
    today = get_ist_now().strftime("%Y-%m-%d")
    if signal_date != today:
        print(f"[WARN] Signal date {signal_date} doesn't match today {today}.")

    # Fetch EOD data
    nifty_eod = fetch_eod_data("^NSEI")
    banknifty_eod = fetch_eod_data("^NSEBANK")

    # Extract trades from signal
    nifty_trade_sig = signal.get("nifty_trade", {})
    bnf_trade_sig = signal.get("banknifty_trade", {})

    records = []

    # Calculate P&L for Nifty
    if nifty_trade_sig.get("direction") not in ["NO TRADE", None]:
        # Build trade dict from signal format
        entry_zone = nifty_trade_sig.get("entry_zone", "0-0")
        try:
            parts = entry_zone.replace(",", "").split("-")
            entry_mid = (float(parts[0].strip()) + float(parts[1].strip())) / 2
        except:
            entry_mid = nifty_eod.get("today_open", 0)

        nifty_trade = {
            "instrument": "NIFTY",
            "direction": nifty_trade_sig.get("direction"),
            "entry_mid": entry_mid,
            "stop_loss": nifty_trade_sig.get("stop_loss", 0),
            "target_1": nifty_trade_sig.get("target_1", 0),
            "target_2": nifty_trade_sig.get("target_2", 0),
            "confidence": nifty_trade_sig.get("confidence_pct", 0),
        }
        nifty_pnl = calculate_pnl(nifty_trade, nifty_eod)
        record = update_trade_journal(nifty_trade, nifty_pnl, signal)
        records.append(record)

    # Calculate P&L for Bank Nifty
    if bnf_trade_sig.get("direction") not in ["NO TRADE", None]:
        entry_zone = bnf_trade_sig.get("entry_zone", "0-0")
        try:
            parts = entry_zone.replace(",", "").split("-")
            entry_mid = (float(parts[0].strip()) + float(parts[1].strip())) / 2
        except:
            entry_mid = banknifty_eod.get("today_open", 0)

        bnf_trade = {
            "instrument": "BANKNIFTY",
            "direction": bnf_trade_sig.get("direction"),
            "entry_mid": entry_mid,
            "stop_loss": bnf_trade_sig.get("stop_loss", 0),
            "target_1": bnf_trade_sig.get("target_1", 0),
            "target_2": bnf_trade_sig.get("target_2", 0),
            "confidence": bnf_trade_sig.get("confidence_pct", 0),
        }
        bnf_pnl = calculate_pnl(bnf_trade, banknifty_eod)
        record = update_trade_journal(bnf_trade, bnf_pnl, signal)
        records.append(record)

    # Update performance
    with open("data/trades.json") as f:
        all_trades = json.load(f)
    perf = update_performance(all_trades)

    # Send Telegram
    if bot_token and chat_id and perf:
        send_pnl_telegram(records, perf, bot_token, chat_id)

    print("[DONE] EOD P&L complete.")


if __name__ == "__main__":
    run_eod()
