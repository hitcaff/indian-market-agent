"""
Indian Market Research Agent
Runs 4x daily — generates trade signals for Nifty 50 & Bank Nifty
Sources: Yahoo Finance, NSE public API, RSS news feeds, Fear & Greed
"""

import requests
import feedparser
import json
import os
import re
import time
import math
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── Constants ──────────────────────────────────────────────────────────────────
NIFTY_LOT = 75
BANKNIFTY_LOT = 35
LOTS = 2

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; indian-market-agent/1.0)"}

NEWS_FEEDS = [
    {"name": "Economic Times Markets", "url": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"},
    {"name": "Moneycontrol",           "url": "https://www.moneycontrol.com/rss/latestnews.xml"},
    {"name": "LiveMint Markets",       "url": "https://www.livemint.com/rss/markets"},
    {"name": "Business Standard",      "url": "https://www.business-standard.com/rss/markets-106.rss"},
    {"name": "NDTV Profit",            "url": "https://feeds.feedburner.com/ndtvprofit-latest"},
]

IST = timezone(timedelta(hours=5, minutes=30))
CONFIDENCE_THRESHOLD = 65  # minimum confidence % to take a trade

# ── High-risk event dates ──────────────────────────────────────────────────────
HIGH_RISK_EVENTS = {
    "rbi": ["2026-02-07", "2026-04-09", "2026-06-06", "2026-08-08", "2026-10-08", "2026-12-05"],
    "budget": ["2026-02-01"],
    "fed": ["2026-01-29", "2026-03-19", "2026-05-07", "2026-06-18", "2026-07-30", "2026-09-17", "2026-11-05", "2026-12-16"],
}


def get_ist_now():
    return datetime.now(IST)


def get_run_session():
    """Determine which of the 4 daily sessions this is."""
    hour = get_ist_now().hour
    minute = get_ist_now().minute
    t = hour * 60 + minute
    if t < 8 * 60:
        return "pre_market"
    elif t < 13 * 60:
        return "mid_morning"
    elif t < 15 * 60:
        return "afternoon"
    else:
        return "post_market"


def check_event_calendar() -> dict:
    """Check if today is a high-risk event day."""
    today = get_ist_now().strftime("%Y-%m-%d")
    today_weekday = get_ist_now().weekday()  # 0=Mon, 3=Thu

    events = []
    risk_level = "normal"

    # Check known event dates
    for event_type, dates in HIGH_RISK_EVENTS.items():
        if isinstance(dates, list) and today in dates:
            events.append(event_type.upper())
            risk_level = "high"

    # Check if monthly expiry (last Thursday of month)
    now = get_ist_now()
    if today_weekday == 3:  # Thursday
        # Check if it's the last Thursday
        next_thursday = now + timedelta(days=7)
        if next_thursday.month != now.month:
            events.append("MONTHLY_EXPIRY")
            risk_level = "high"
        else:
            events.append("WEEKLY_EXPIRY")
            risk_level = "elevated" if risk_level == "normal" else risk_level

    result = {
        "today": today,
        "events": events,
        "risk_level": risk_level,
        "trade_recommended": risk_level == "normal",
        "warning": f"HIGH RISK DAY: {', '.join(events)}" if events else "Normal trading day",
    }

    if events:
        print(f"[WARN] {result['warning']} — confidence will be penalized")
    else:
        print(f"[INFO] Event calendar: Normal trading day")

    return result


def check_market_regime(nifty_quote: dict, banknifty_quote: dict) -> dict:
    """Detect if market is trending or ranging based on recent price action."""
    nifty_highs = nifty_quote.get("highs", [])
    nifty_lows = nifty_quote.get("lows", [])
    nifty_closes = nifty_quote.get("closes", [])

    if len(nifty_highs) < 3 or len(nifty_lows) < 3:
        return {"regime": "unknown", "atr_pct": 0, "trending": False}

    # ATR (Average True Range) as % of price
    ranges = [h - l for h, l in zip(nifty_highs[-5:], nifty_lows[-5:])]
    avg_range = sum(ranges) / len(ranges) if ranges else 0
    current = nifty_quote.get("current", 1)
    atr_pct = (avg_range / current * 100) if current else 0

    # Today's range vs average
    today_range = nifty_quote.get("day_high", 0) - nifty_quote.get("day_low", 0)
    today_range_pct = (today_range / current * 100) if current else 0

    # Trend check: higher highs + higher lows = uptrend
    if len(nifty_highs) >= 3 and len(nifty_lows) >= 3:
        higher_highs = nifty_highs[-1] > nifty_highs[-2] > nifty_highs[-3]
        higher_lows = nifty_lows[-1] > nifty_lows[-2] > nifty_lows[-3]
        lower_highs = nifty_highs[-1] < nifty_highs[-2] < nifty_highs[-3]
        lower_lows = nifty_lows[-1] < nifty_lows[-2] < nifty_lows[-3]
        trending = higher_highs and higher_lows or lower_highs and lower_lows
    else:
        trending = False

    # Regime classification
    if atr_pct > 1.2:
        regime = "high_volatility"
    elif trending:
        regime = "trending"
    elif atr_pct < 0.5:
        regime = "tight_range"
    else:
        regime = "ranging"

    regime_signal = "favorable" if regime == "trending" else                     "caution" if regime in ["high_volatility", "tight_range"] else "neutral"

    print(f"[INFO] Market regime: {regime} | ATR%: {atr_pct:.2f}% | Trending: {trending}")

    return {
        "regime": regime,
        "atr_pct": round(atr_pct, 2),
        "today_range_pct": round(today_range_pct, 2),
        "trending": trending,
        "regime_signal": regime_signal,
    }


def apply_filters(nifty_trade: dict, banknifty_trade: dict, event_calendar: dict, market_regime: dict) -> tuple:
    """Apply event calendar, market regime, and confidence threshold filters."""

    def filter_trade(trade: dict, instrument: str) -> dict:
        if trade.get("direction") == "NO TRADE":
            return trade

        confidence = trade.get("confidence", 0)
        reasons = []

        # Filter 1: Confidence threshold
        if confidence < CONFIDENCE_THRESHOLD:
            reasons.append(f"confidence {confidence}% below {CONFIDENCE_THRESHOLD}% threshold")

        # Filter 2: Event calendar penalty
        if event_calendar.get("risk_level") == "high":
            confidence = max(0, confidence - 20)  # penalize 20 points
            trade["confidence"] = confidence
            reasons.append(f"high-risk event day ({event_calendar.get('warning', '')})")
            if confidence < CONFIDENCE_THRESHOLD:
                reasons.append(f"post-penalty confidence {confidence}% still below threshold")

        elif event_calendar.get("risk_level") == "elevated":
            confidence = max(0, confidence - 10)
            trade["confidence"] = confidence

        # Filter 3: Market regime
        if market_regime.get("regime") == "tight_range":
            reasons.append("tight range regime — intraday moves likely insufficient for targets")
        elif market_regime.get("regime") == "high_volatility" and confidence < 70:
            reasons.append("high volatility regime with moderate confidence — skip")

        # Final decision
        skip = len([r for r in reasons if "threshold" in r or "skip" in r]) > 0

        if skip:
            print(f"[FILTER] {instrument} trade skipped: {' | '.join(reasons)}")
            return {
                "direction": "NO TRADE",
                "reason": f"Filtered: {' | '.join(reasons)}",
                "original_confidence": confidence,
            }

        # Add filter context to trade
        trade["filter_notes"] = {
            "event_risk": event_calendar.get("risk_level", "normal"),
            "market_regime": market_regime.get("regime", "unknown"),
            "confidence_after_filters": confidence,
            "warnings": reasons if reasons else [],
        }
        return trade

    nifty_filtered = filter_trade(nifty_trade.copy(), "NIFTY")
    bnf_filtered = filter_trade(banknifty_trade.copy(), "BANKNIFTY")

    return nifty_filtered, bnf_filtered



# ══════════════════════════════════════════════════════════════════════════════
# DATA FETCHERS
# ══════════════════════════════════════════════════════════════════════════════

def fetch_yahoo_quote(symbol: str) -> dict:
    """Fetch quote data from Yahoo Finance."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {"interval": "1d", "range": "5d"}
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        result = data["chart"]["result"][0]
        meta = result["meta"]
        closes = result["indicators"]["quote"][0].get("close", [])
        highs = result["indicators"]["quote"][0].get("high", [])
        lows = result["indicators"]["quote"][0].get("low", [])
        volumes = result["indicators"]["quote"][0].get("volume", [])

        closes = [c for c in closes if c is not None]
        highs = [h for h in highs if h is not None]
        lows = [l for l in lows if l is not None]

        return {
            "symbol": symbol,
            "current": meta.get("regularMarketPrice", closes[-1] if closes else 0),
            "prev_close": meta.get("previousClose", closes[-2] if len(closes) > 1 else 0),
            "day_high": meta.get("regularMarketDayHigh", highs[-1] if highs else 0),
            "day_low": meta.get("regularMarketDayLow", lows[-1] if lows else 0),
            "closes": closes[-5:],
            "highs": highs[-5:],
            "lows": lows[-5:],
            "volume": volumes[-1] if volumes else 0,
        }
    except Exception as e:
        print(f"[WARN] Yahoo fetch failed for {symbol}: {e}")
        return {}


def compute_technicals(data: dict) -> dict:
    """Compute technical indicators from OHLC data."""
    closes = data.get("closes", [])
    highs = data.get("highs", [])
    lows = data.get("lows", [])
    current = data.get("current", 0)
    prev_close = data.get("prev_close", 0)

    if not closes or len(closes) < 2:
        return {}

    # EMA calculation
    def ema(prices, period):
        if len(prices) < period:
            return prices[-1] if prices else 0
        k = 2 / (period + 1)
        e = prices[0]
        for p in prices[1:]:
            e = p * k + e * (1 - k)
        return e

    # RSI
    def rsi(prices, period=14):
        if len(prices) < 2:
            return 50
        gains, losses = [], []
        for i in range(1, len(prices)):
            diff = prices[i] - prices[i-1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))
        avg_gain = sum(gains) / len(gains) if gains else 0
        avg_loss = sum(losses) / len(losses) if losses else 1
        rs = avg_gain / avg_loss if avg_loss != 0 else 100
        return 100 - (100 / (1 + rs))

    all_closes = closes + [current]
    ema9 = ema(all_closes, 9)
    ema21 = ema(all_closes, 21)

    rsi_val = rsi(all_closes)

    # Support & Resistance (simple pivot)
    if highs and lows:
        pivot = (highs[-1] + lows[-1] + closes[-1]) / 3
        r1 = 2 * pivot - lows[-1]
        s1 = 2 * pivot - highs[-1]
        r2 = pivot + (highs[-1] - lows[-1])
        s2 = pivot - (highs[-1] - lows[-1])
    else:
        pivot = current
        r1 = current * 1.005
        s1 = current * 0.995
        r2 = current * 1.01
        s2 = current * 0.99

    # Change %
    change_pct = ((current - prev_close) / prev_close * 100) if prev_close else 0

    # Trend determination
    trend = "bullish" if current > ema21 and ema9 > ema21 else \
            "bearish" if current < ema21 and ema9 < ema21 else "neutral"

    rsi_signal = "overbought" if rsi_val > 70 else "oversold" if rsi_val < 30 else \
                 "bullish" if rsi_val > 55 else "bearish" if rsi_val < 45 else "neutral"

    return {
        "ema9": round(ema9, 2),
        "ema21": round(ema21, 2),
        "rsi": round(rsi_val, 2),
        "rsi_signal": rsi_signal,
        "pivot": round(pivot, 2),
        "r1": round(r1, 2),
        "r2": round(r2, 2),
        "s1": round(s1, 2),
        "s2": round(s2, 2),
        "trend": trend,
        "change_pct": round(change_pct, 2),
        "above_ema9": current > ema9,
        "above_ema21": current > ema21,
    }


def compute_fvg(highs: list, lows: list, closes: list, label: str = "daily") -> list:
    """
    Detect Fair Value Gaps from OHLC data.
    Bullish FVG: candle[i-1].high < candle[i+1].low  → gap above
    Bearish FVG: candle[i-1].low  > candle[i+1].high → gap below
    Returns list of open (unfilled) FVGs.
    """
    fvgs = []
    if len(highs) < 3 or len(lows) < 3:
        return fvgs

    for i in range(1, len(highs) - 1):
        h_prev, l_prev = highs[i-1], lows[i-1]
        h_curr, l_curr = highs[i],   lows[i]
        h_next, l_next = highs[i+1], lows[i+1]

        # Bullish FVG: gap between prev candle high and next candle low
        if h_prev < l_next:
            gap_low  = h_prev
            gap_high = l_next
            # Check if already filled by current close
            filled = closes[-1] <= gap_low if closes else False
            if not filled:
                fvgs.append({
                    "type": "bullish",
                    "gap_low":  round(gap_low,  2),
                    "gap_high": round(gap_high, 2),
                    "mid":      round((gap_low + gap_high) / 2, 2),
                    "size":     round(gap_high - gap_low, 2),
                    "candle_index": i,
                    "timeframe": label,
                    "filled": False,
                })

        # Bearish FVG: gap between prev candle low and next candle high
        elif l_prev > h_next:
            gap_high = l_prev
            gap_low  = h_next
            filled = closes[-1] >= gap_high if closes else False
            if not filled:
                fvgs.append({
                    "type": "bearish",
                    "gap_low":  round(gap_low,  2),
                    "gap_high": round(gap_high, 2),
                    "mid":      round((gap_low + gap_high) / 2, 2),
                    "size":     round(gap_high - gap_low, 2),
                    "candle_index": i,
                    "timeframe": label,
                    "filled": False,
                })

    # Sort by proximity to current price
    current = closes[-1] if closes else 0
    fvgs.sort(key=lambda x: abs(x["mid"] - current))
    return fvgs[:5]  # return closest 5


def fetch_fvg_analysis(symbol: str, instrument: str) -> dict:
    """Fetch daily + available intraday data and compute FVGs."""
    print(f"[INFO] Computing FVGs for {instrument}...")

    # Daily FVGs (5 day data)
    daily = fetch_yahoo_quote(symbol)
    daily_fvgs = []
    if daily:
        daily_fvgs = compute_fvg(
            daily.get("highs", []),
            daily.get("lows", []),
            daily.get("closes", []),
            label="daily"
        )

    # Intraday FVGs — try 15min from Yahoo (limited but available)
    intraday_fvgs = []
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {"interval": "15m", "range": "2d"}
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        if resp.ok:
            result = resp.json()["chart"]["result"][0]
            quotes = result["indicators"]["quote"][0]
            highs  = [h for h in quotes.get("high",  []) if h is not None]
            lows   = [l for l in quotes.get("low",   []) if l is not None]
            closes = [c for c in quotes.get("close", []) if c is not None]
            if len(highs) >= 3:
                intraday_fvgs = compute_fvg(highs, lows, closes, label="15min")
    except Exception as e:
        print(f"[WARN] Intraday FVG fetch failed: {e}")

    all_fvgs = daily_fvgs + intraday_fvgs
    current  = daily.get("current", 0) if daily else 0

    # Find nearest FVG and check if price is inside one
    nearest_fvg  = None
    inside_fvg   = None
    for fvg in all_fvgs:
        if fvg["gap_low"] <= current <= fvg["gap_high"]:
            inside_fvg = fvg
            break
        if nearest_fvg is None:
            nearest_fvg = fvg

    # Best entry FVG for tomorrow
    bullish_fvgs = [f for f in all_fvgs if f["type"] == "bullish"]
    bearish_fvgs = [f for f in all_fvgs if f["type"] == "bearish"]

    result = {
        "instrument": instrument,
        "current_price": current,
        "daily_fvg_count": len(daily_fvgs),
        "intraday_fvg_count": len(intraday_fvgs),
        "all_fvgs": all_fvgs,
        "nearest_fvg": nearest_fvg,
        "inside_fvg": inside_fvg,
        "best_bullish_fvg": bullish_fvgs[0] if bullish_fvgs else None,
        "best_bearish_fvg": bearish_fvgs[0] if bearish_fvgs else None,
        "fvg_bias": "bullish" if len(bullish_fvgs) > len(bearish_fvgs) else
                    "bearish" if len(bearish_fvgs) > len(bullish_fvgs) else "neutral",
    }

    # Load intraday FVGs saved by live_monitor if available
    fvg_file = "data/fvg_zones.json"
    if os.path.exists(fvg_file):
        try:
            with open(fvg_file) as f:
                saved = json.load(f)
            saved_date = saved.get("date", "")
            today = get_ist_now().strftime("%Y-%m-%d")
            if saved_date == today:
                live_fvgs = saved.get(instrument, {}).get("fvgs", [])
                result["live_monitor_fvgs"] = live_fvgs
                result["all_fvgs"] = all_fvgs + live_fvgs
                print(f"[INFO] Loaded {len(live_fvgs)} live FVGs from monitor")
        except Exception as e:
            print(f"[WARN] Could not load saved FVGs: {e}")

    bias_str = result['fvg_bias']
    best = result.get('best_bullish_fvg') or result.get('best_bearish_fvg')
    if best:
        print(f"[INFO] {instrument} FVG: {bias_str} | Nearest: {best['gap_low']}-{best['gap_high']} ({best['timeframe']})")
    else:
        print(f"[INFO] {instrument} FVG: no significant gaps found")

    return result



def fetch_global_context() -> dict:
    """Fetch global market context."""
    print("[INFO] Fetching global context...")
    symbols = {
        "SGX Nifty (Gift)": "^NSEI",
        "Dow Futures": "YM=F",
        "Nasdaq Futures": "NQ=F",
        "Nikkei": "^N225",
        "Hang Seng": "^HSI",
        "Crude Oil": "CL=F",
        "DXY (Dollar)": "DX=F",
        "Gold": "GC=F",
    }
    context = {}
    for name, sym in symbols.items():
        data = fetch_yahoo_quote(sym)
        if data:
            change = ((data["current"] - data["prev_close"]) / data["prev_close"] * 100) if data.get("prev_close") else 0
            context[name] = {"price": data["current"], "change_pct": round(change, 2)}
            print(f"[INFO] {name}: {data['current']:,.2f} ({change:+.2f}%)")
        time.sleep(0.3)
    return context


def fetch_india_vix() -> dict:
    """Fetch India VIX."""
    print("[INFO] Fetching India VIX...")
    data = fetch_yahoo_quote("^INDIAVIX")
    if data:
        vix = data["current"]
        interpretation = "extreme_fear" if vix > 25 else "fear" if vix > 18 else \
                         "neutral" if vix > 13 else "complacent"
        return {"value": vix, "interpretation": interpretation, "change_pct": data.get("change_pct", 0)}
    return {}


def fetch_nse_options_data(symbol: str = "NIFTY") -> dict:
    """Fetch options chain data from NSE public API."""
    print(f"[INFO] Fetching NSE options for {symbol}...")
    try:
        session = requests.Session()
        session.get("https://www.nseindia.com", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }, timeout=10)
        time.sleep(1)

        url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
        resp = session.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Referer": "https://www.nseindia.com/",
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        records = data.get("records", {})
        underlying = records.get("underlyingValue", 0)
        expiry_dates = records.get("expiryDates", [])
        nearest_expiry = expiry_dates[0] if expiry_dates else None

        # Filter for nearest expiry
        chain = [r for r in records.get("data", []) if r.get("expiryDate") == nearest_expiry]

        total_call_oi = sum(r.get("CE", {}).get("openInterest", 0) for r in chain)
        total_put_oi = sum(r.get("PE", {}).get("openInterest", 0) for r in chain)

        pcr = round(total_put_oi / total_call_oi, 2) if total_call_oi > 0 else 1.0

        # Max pain — strike with maximum OI on both sides
        strike_pain = {}
        for r in chain:
            strike = r.get("strikePrice", 0)
            call_oi = r.get("CE", {}).get("openInterest", 0)
            put_oi = r.get("PE", {}).get("openInterest", 0)
            strike_pain[strike] = call_oi + put_oi

        max_pain = max(strike_pain, key=strike_pain.get) if strike_pain else underlying

        # Find key resistance (highest call OI) and support (highest put OI)
        call_oi_by_strike = {r.get("strikePrice", 0): r.get("CE", {}).get("openInterest", 0) for r in chain}
        put_oi_by_strike = {r.get("strikePrice", 0): r.get("PE", {}).get("openInterest", 0) for r in chain}

        call_resistance = max(call_oi_by_strike, key=call_oi_by_strike.get) if call_oi_by_strike else 0
        put_support = max(put_oi_by_strike, key=put_oi_by_strike.get) if put_oi_by_strike else 0

        pcr_signal = "bullish" if pcr > 1.2 else "bearish" if pcr < 0.8 else "neutral"

        print(f"[INFO] {symbol} PCR: {pcr} ({pcr_signal}) | Max Pain: {max_pain} | Resistance: {call_resistance} | Support: {put_support}")

        return {
            "symbol": symbol,
            "underlying": underlying,
            "pcr": pcr,
            "pcr_signal": pcr_signal,
            "max_pain": max_pain,
            "call_resistance": call_resistance,
            "put_support": put_support,
            "total_call_oi": total_call_oi,
            "total_put_oi": total_put_oi,
            "expiry": nearest_expiry,
        }

    except Exception as e:
        print(f"[WARN] NSE options fetch failed for {symbol}: {e}")
        return {"symbol": symbol, "pcr": 1.0, "pcr_signal": "neutral", "max_pain": 0, "error": str(e)}


def fetch_fii_dii() -> dict:
    """Fetch FII/DII data from NSE."""
    print("[INFO] Fetching FII/DII data...")
    try:
        session = requests.Session()
        session.get("https://www.nseindia.com", headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        time.sleep(1)
        resp = session.get("https://www.nseindia.com/api/fiidiiTradeReact", headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com/"
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data and len(data) > 0:
            latest = data[0]
            fii_net = latest.get("fiiNet", 0) or 0
            dii_net = latest.get("diiNet", 0) or 0
            fii_signal = "bullish" if fii_net > 500 else "bearish" if fii_net < -500 else "neutral"
            print(f"[INFO] FII Net: {fii_net} Cr | DII Net: {dii_net} Cr")
            return {"fii_net_cr": fii_net, "dii_net_cr": dii_net, "fii_signal": fii_signal, "date": latest.get("date", "")}
    except Exception as e:
        print(f"[WARN] FII/DII fetch failed: {e}")
    return {"fii_net_cr": 0, "dii_net_cr": 0, "fii_signal": "neutral"}


def fetch_news() -> list[dict]:
    """Fetch Indian market news from RSS feeds."""
    print("[INFO] Fetching market news...")
    articles = []
    for feed in NEWS_FEEDS:
        try:
            f = feedparser.parse(feed["url"])
            for entry in f.entries[:5]:
                title = entry.get("title", "")
                summary = re.sub(r"<[^>]+>", "", entry.get("summary", ""))[:200]
                articles.append({"source": feed["name"], "title": title, "summary": summary})
            print(f"[INFO] {feed['name']}: {min(len(f.entries), 5)} articles")
        except Exception as e:
            print(f"[WARN] {feed['name']} failed: {e}")
        time.sleep(0.3)
    return articles[:25]


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def compute_confluence(global_ctx, nifty_tech, banknifty_tech, nifty_opts, banknifty_opts, fii_dii, weights) -> dict:
    """Compute confluence score for trade direction."""

    scores = {"bullish": 0, "bearish": 0, "neutral": 0}

    # ── Global context score ───────────────────────────────────────────────────
    global_bullish = sum(1 for v in global_ctx.values() if v.get("change_pct", 0) > 0.2)
    global_bearish = sum(1 for v in global_ctx.values() if v.get("change_pct", 0) < -0.2)
    global_signal = "bullish" if global_bullish > global_bearish else \
                    "bearish" if global_bearish > global_bullish else "neutral"
    scores[global_signal] += weights["global"]

    # ── Technical score ────────────────────────────────────────────────────────
    tech_signals = []
    for tech in [nifty_tech, banknifty_tech]:
        if tech:
            tech_signals.append(tech.get("trend", "neutral"))
            tech_signals.append(tech.get("rsi_signal", "neutral"))
    tech_bull = tech_signals.count("bullish")
    tech_bear = tech_signals.count("bearish")
    tech_signal = "bullish" if tech_bull > tech_bear else \
                  "bearish" if tech_bear > tech_bull else "neutral"
    scores[tech_signal] += weights["technical"]

    # ── Options score ──────────────────────────────────────────────────────────
    opts_signals = [nifty_opts.get("pcr_signal", "neutral"), banknifty_opts.get("pcr_signal", "neutral")]
    opts_bull = opts_signals.count("bullish")
    opts_bear = opts_signals.count("bearish")
    opts_signal = "bullish" if opts_bull > opts_bear else \
                  "bearish" if opts_bear > opts_bull else "neutral"
    scores[opts_signal] += weights["options"]

    # ── Sentiment/FII score ────────────────────────────────────────────────────
    fii_signal = fii_dii.get("fii_signal", "neutral")
    scores[fii_signal] += weights["sentiment"]

    # ── Final direction ────────────────────────────────────────────────────────
    direction = max(scores, key=scores.get)
    confidence = round(scores[direction] * 100)

    return {
        "direction": direction,
        "confidence": confidence,
        "scores": scores,
        "factor_signals": {
            "global": global_signal,
            "technical": tech_signal,
            "options": opts_signal,
            "sentiment": fii_signal,
        }
    }


def generate_trade(instrument: str, current_price: float, tech: dict, opts: dict, confluence: dict, fvg: dict = None) -> dict:
    """Generate entry, SL, targets based on confluence and FVG zones."""
    direction = confluence["direction"]
    spread = current_price * 0.001
    fvg_entry_used = False
    fvg_detail = None

    if direction == "bullish":
        # Use bullish FVG zone as entry if available and nearby
        best_fvg = fvg.get("best_bullish_fvg") if fvg else None
        if best_fvg and abs(best_fvg["mid"] - current_price) / current_price < 0.015:
            entry_low  = best_fvg["gap_low"]
            entry_high = best_fvg["gap_high"]
            sl = round(best_fvg["gap_low"] * 0.998)  # just below FVG low
            fvg_entry_used = True
            fvg_detail = best_fvg
            print(f"[INFO] Using bullish FVG zone as entry: {entry_low}-{entry_high}")
        else:
            entry_low  = round(current_price - spread)
            entry_high = round(current_price + spread)
            sl = round(max(tech.get("s1", current_price * 0.994), current_price * 0.994))
        t1 = round(tech.get("r1", current_price * 1.005))
        t2 = round(tech.get("r2", current_price * 1.01))
        trade_direction = "LONG"

    elif direction == "bearish":
        best_fvg = fvg.get("best_bearish_fvg") if fvg else None
        if best_fvg and abs(best_fvg["mid"] - current_price) / current_price < 0.015:
            entry_low  = best_fvg["gap_low"]
            entry_high = best_fvg["gap_high"]
            sl = round(best_fvg["gap_high"] * 1.002)  # just above FVG high
            fvg_entry_used = True
            fvg_detail = best_fvg
            print(f"[INFO] Using bearish FVG zone as entry: {entry_low}-{entry_high}")
        else:
            entry_low  = round(current_price - spread)
            entry_high = round(current_price + spread)
            sl = round(min(tech.get("r1", current_price * 1.006), current_price * 1.006))
        t1 = round(tech.get("s1", current_price * 0.995))
        t2 = round(tech.get("s2", current_price * 0.99))
        trade_direction = "SHORT"

    else:
        return {"direction": "NO TRADE", "reason": "Insufficient confluence — market neutral"}

    # Risk/reward
    risk = abs(((entry_low + entry_high) / 2) - sl)
    reward_t1 = abs(t1 - ((entry_low + entry_high) / 2))
    rr_ratio = round(reward_t1 / risk, 2) if risk > 0 else 0

    # Lot size
    lot_size = NIFTY_LOT if instrument == "NIFTY" else BANKNIFTY_LOT
    points_to_t1 = reward_t1
    points_to_sl = risk
    pnl_t1 = round(points_to_t1 * lot_size * LOTS)
    pnl_sl = round(-points_to_sl * lot_size * LOTS)

    # Skip if R:R < 1.5
    if rr_ratio < 1.5 and direction != "neutral":
        trade_direction = "NO TRADE"
        return {"direction": "NO TRADE", "reason": f"R:R ratio {rr_ratio} below 1.5 threshold"}

    return {
        "instrument": instrument,
        "direction": trade_direction,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "entry_mid": round((entry_low + entry_high) / 2),
        "stop_loss": sl,
        "target_1": t1,
        "target_2": t2,
        "risk_points": round(risk),
        "reward_points_t1": round(reward_t1),
        "rr_ratio": rr_ratio,
        "lot_size": lot_size,
        "lots": LOTS,
        "pnl_if_t1": pnl_t1,
        "pnl_if_sl": pnl_sl,
        "max_pain": opts.get("max_pain", 0),
        "pcr": opts.get("pcr", 0),
        "confidence": confluence["confidence"],
        "fvg_entry_used": fvg_entry_used,
        "fvg_detail": fvg_detail,
    }


# ══════════════════════════════════════════════════════════════════════════════
# GEMINI SYNTHESIS
# ══════════════════════════════════════════════════════════════════════════════

def synthesize_with_gemini(session_data: dict, api_key: str) -> dict:
    """Use Gemini to reason through the trade and produce final report."""
    print("[INFO] Synthesizing with Gemini...")

    session = session_data["session"]
    nifty = session_data["nifty"]
    banknifty = session_data["banknifty"]
    global_ctx = session_data["global"]
    fii = session_data["fii_dii"]
    news = session_data["news"]
    nifty_trade = session_data["nifty_trade"]
    banknifty_trade = session_data["banknifty_trade"]
    weights = session_data["weights"]

    news_text = "\n".join([f"[{n['source']}] {n['title']}" for n in news[:15]])
    global_text = "\n".join([f"{k}: {v['price']:,.2f} ({v['change_pct']:+.2f}%)" for k, v in global_ctx.items()])

    prompt = f"""You are an expert Indian stock market analyst with 15+ years of experience trading Nifty 50 and Bank Nifty derivatives.

SESSION: {session.upper()} | Date: {get_ist_now().strftime('%B %d, %Y %H:%M IST')}

GLOBAL CONTEXT:
{global_text}

NIFTY 50:
Current: {nifty['quote'].get('current', 0):,.2f} | Change: {nifty['technicals'].get('change_pct', 0):+.2f}%
EMA9: {nifty['technicals'].get('ema9', 0)} | EMA21: {nifty['technicals'].get('ema21', 0)}
RSI: {nifty['technicals'].get('rsi', 0)} ({nifty['technicals'].get('rsi_signal', 'neutral')})
Trend: {nifty['technicals'].get('trend', 'neutral')}
Support: {nifty['technicals'].get('s1', 0)} / {nifty['technicals'].get('s2', 0)}
Resistance: {nifty['technicals'].get('r1', 0)} / {nifty['technicals'].get('r2', 0)}
PCR: {nifty['options'].get('pcr', 0)} ({nifty['options'].get('pcr_signal', 'neutral')})
Max Pain: {nifty['options'].get('max_pain', 0)}
Call Resistance: {nifty['options'].get('call_resistance', 0)}
Put Support: {nifty['options'].get('put_support', 0)}

BANK NIFTY:
Current: {banknifty['quote'].get('current', 0):,.2f} | Change: {banknifty['technicals'].get('change_pct', 0):+.2f}%
EMA9: {banknifty['technicals'].get('ema9', 0)} | EMA21: {banknifty['technicals'].get('ema21', 0)}
RSI: {banknifty['technicals'].get('rsi', 0)} ({banknifty['technicals'].get('rsi_signal', 'neutral')})
Trend: {banknifty['technicals'].get('trend', 'neutral')}
PCR: {banknifty['options'].get('pcr', 0)} ({banknifty['options'].get('pcr_signal', 'neutral')})
Max Pain: {banknifty['options'].get('max_pain', 0)}

FII/DII:
FII Net: ₹{fii.get('fii_net_cr', 0):,.0f} Cr ({fii.get('fii_signal', 'neutral')})
DII Net: ₹{fii.get('dii_net_cr', 0):,.0f} Cr

EVENT CALENDAR:
Risk Level: {session_data.get('event_calendar', {}).get('risk_level', 'normal')}
Events Today: {', '.join(session_data.get('event_calendar', {}).get('events', [])) or 'None'}
Warning: {session_data.get('event_calendar', {}).get('warning', 'Normal trading day')}

MARKET REGIME:
Regime: {session_data.get('market_regime', {}).get('regime', 'unknown')}
ATR%: {session_data.get('market_regime', {}).get('atr_pct', 0)}%
Trending: {session_data.get('market_regime', {}).get('trending', False)}
Signal: {session_data.get('market_regime', {}).get('regime_signal', 'neutral')}

PROPOSED TRADES:
Nifty: {json.dumps(nifty_trade, indent=2)}
Bank Nifty: {json.dumps(banknifty_trade, indent=2)}

CURRENT FACTOR WEIGHTS: {json.dumps(weights)}

MARKET NEWS:
{news_text}

Based on ALL the above data, provide a comprehensive market analysis and validate/adjust the proposed trades.

Return ONLY valid JSON:
{{
  "session": "{session}",
  "date": "{get_ist_now().strftime('%Y-%m-%d')}",
  "timestamp": "{get_ist_now().strftime('%H:%M IST')}",
  "market_bias": "bullish/bearish/neutral",
  "bias_strength": "strong/moderate/weak",
  "reasoning": {{
    "global_analysis": "2-3 sentences on global cues",
    "technical_analysis": "2-3 sentences on Nifty & BankNifty technicals",
    "options_analysis": "2-3 sentences on PCR, max pain, OI",
    "sentiment_analysis": "2-3 sentences on FII/DII and news",
    "confluence_summary": "Final 2-3 sentence reasoning for the trade"
  }},
  "nifty_trade": {{
    "direction": "LONG/SHORT/NO TRADE",
    "entry_zone": "XXXXX - XXXXX",
    "stop_loss": XXXXX,
    "target_1": XXXXX,
    "target_2": XXXXX,
    "confidence_pct": XX,
    "risk_reward": "1:X",
    "key_levels_to_watch": ["level 1", "level 2"],
    "invalidation": "condition that invalidates this trade"
  }},
  "banknifty_trade": {{
    "direction": "LONG/SHORT/NO TRADE",
    "entry_zone": "XXXXX - XXXXX",
    "stop_loss": XXXXX,
    "target_1": XXXXX,
    "target_2": XXXXX,
    "confidence_pct": XX,
    "risk_reward": "1:X",
    "key_levels_to_watch": ["level 1", "level 2"],
    "invalidation": "condition that invalidates this trade"
  }},
  "what_to_watch": ["point 1", "point 2", "point 3"],
  "risk_factors": ["risk 1", "risk 2"]
}}"""

    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    resp = requests.post(url, headers={"Content-Type": "application/json"},
        params={"key": api_key},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"temperature": 0.3, "maxOutputTokens": 8192}},
        timeout=60)
    if not resp.ok:
        print("[DEBUG]", resp.status_code, resp.text[:500])
    resp.raise_for_status()
    raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


# ══════════════════════════════════════════════════════════════════════════════
# SAVE & TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

def save_signal(report: dict):
    """Save latest signal to file."""
    os.makedirs("data", exist_ok=True)
    with open("data/latest_signal.json", "w") as f:
        json.dump(report, f, indent=2)
    date_str = get_ist_now().strftime("%Y-%m-%d")
    session = report.get("session", "unknown")
    with open(f"data/signal_{date_str}_{session}.json", "w") as f:
        json.dump(report, f, indent=2)
    print(f"[INFO] Signal saved.")


def send_telegram(report: dict, bot_token: str, chat_id: str, event_calendar: dict = None, market_regime: dict = None):
    """Send concise trade signal to Telegram."""
    session_labels = {
        "pre_market": "🌅 Pre-Market", "mid_morning": "🌤 Mid-Morning",
        "afternoon": "🌞 Afternoon",   "post_market": "🌆 Post-Market"
    }
    session_label = session_labels.get(report.get("session", ""), "📊")
    bias = report.get("market_bias", "neutral")
    bias_emoji = "🟢" if bias == "bullish" else "🔴" if bias == "bearish" else "🟡"
    event_cal = event_calendar or {}
    regime = market_regime or {}

    def format_trade_block(trade: dict, label: str) -> list:
        lines = []
        direction = trade.get("direction", "NO TRADE")
        dir_emoji = "▲" if direction == "LONG" else "▼" if direction == "SHORT" else "⊘"
        conf = trade.get("confidence_pct", trade.get("confidence", 0))
        fvg_used = trade.get("fvg_entry_used", False)
        fvg_tag = " [FVG]" if fvg_used else ""

        lines.append(f"*{label} — {dir_emoji} {direction}{fvg_tag} | {conf}%*")

        if direction not in ["NO TRADE", None]:
            entry = trade.get("entry_zone", f"{trade.get('entry_low',0):,.0f}-{trade.get('entry_high',0):,.0f}")
            lines.append(f"Entry : `{entry}`")
            lines.append(f"SL    : `{trade.get('stop_loss', 'N/A'):,}` | T1: `{trade.get('target_1', 'N/A'):,}` | T2: `{trade.get('target_2', 'N/A'):,}`")
            lines.append(f"R:R   : {trade.get('risk_reward', 'N/A')}")
            if trade.get("invalidation"):
                lines.append(f"⚠️ _{trade['invalidation']}_")
        else:
            reason = trade.get("reason", "Insufficient confluence")
            lines.append(f"_Skip: {reason[:80]}_")
        return lines

    factors = report.get("reasoning", {})
    nifty = report.get("nifty_trade", {})
    bnf = report.get("banknifty_trade", {})

    # Factor signals (one line)
    factor_line = " | ".join([
        f"🌍 {factors.get('global_analysis', '')[:30]}..." if factors.get('global_analysis') else "🌍 N/A",
    ])

    event_str = event_cal.get("warning", "Normal day")[:40]
    regime_str = regime.get("regime", "unknown").replace("_", " ").title()

    lines = [
        f"📈 *Indian Market | {session_label}*",
        f"{bias_emoji} Bias: *{bias.upper()}* ({report.get('bias_strength', '')})",
        f"",
    ]
    lines += format_trade_block(nifty, "NIFTY 50")
    lines.append("")
    lines += format_trade_block(bnf, "BANK NIFTY")
    lines += [
        f"",
        f"─────────────────────",
        f"📅 {event_str}",
        f"🎯 Regime: {regime_str} | ATR: {regime.get('atr_pct', 0):.1f}%",
        f"📐 NIFTY FVG: {nifty.get('fvg_detail', {}).get('type', 'none').title() if nifty.get('fvg_entry_used') else 'N/A'}",
        f"📐 BNF FVG: {bnf.get('fvg_detail', {}).get('type', 'none').title() if bnf.get('fvg_entry_used') else 'N/A'}",
    ]

    message = "\n".join(lines)
    if len(message) > 4096:
        message = message[:4090] + "\n..."

    resp = requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown", "disable_web_page_preview": True},
        timeout=15)
    if resp.ok:
        print("[INFO] Signal sent to Telegram.")
    else:
        print(f"[WARN] Telegram failed: {resp.status_code} {resp.text[:200]}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def run_pipeline():
    key = os.getenv("GEMINI_API_KEY")
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not key:
        raise ValueError("GEMINI_API_KEY not set.")

    session = get_run_session()
    print(f"[START] Indian Market Agent — {get_ist_now().strftime('%Y-%m-%d %H:%M IST')} | Session: {session}")

    # Load weights
    weights = {"global": 0.25, "technical": 0.35, "options": 0.25, "sentiment": 0.15}
    if os.path.exists("data/weights.json"):
        with open("data/weights.json") as f:
            weights = json.load(f)

    # Fetch all data
    global_ctx = fetch_global_context()
    time.sleep(1)
    vix = fetch_india_vix()
    time.sleep(1)

    nifty_quote = fetch_yahoo_quote("^NSEI")
    time.sleep(0.5)
    banknifty_quote = fetch_yahoo_quote("^NSEBANK")
    time.sleep(1)

    nifty_tech = compute_technicals(nifty_quote)
    banknifty_tech = compute_technicals(banknifty_quote)

    nifty_opts = fetch_nse_options_data("NIFTY")
    time.sleep(2)
    banknifty_opts = fetch_nse_options_data("BANKNIFTY")
    time.sleep(1)

    fii_dii = fetch_fii_dii()
    time.sleep(1)
    news = fetch_news()

    # FVG Analysis
    nifty_fvg = fetch_fvg_analysis("^NSEI", "NIFTY")
    time.sleep(1)
    banknifty_fvg = fetch_fvg_analysis("^NSEBANK", "BANKNIFTY")
    time.sleep(1)

    # Event calendar & market regime checks
    event_calendar = check_event_calendar()
    market_regime = check_market_regime(nifty_quote, banknifty_quote)

    # Confluence
    nifty_confluence = compute_confluence(global_ctx, nifty_tech, banknifty_tech, nifty_opts, banknifty_opts, fii_dii, weights)
    banknifty_confluence = compute_confluence(global_ctx, banknifty_tech, nifty_tech, banknifty_opts, nifty_opts, fii_dii, weights)

    # Trades — now FVG-informed
    nifty_trade = generate_trade("NIFTY", nifty_quote.get("current", 0), nifty_tech, nifty_opts, nifty_confluence, nifty_fvg)
    banknifty_trade = generate_trade("BANKNIFTY", banknifty_quote.get("current", 0), banknifty_tech, banknifty_opts, banknifty_confluence, banknifty_fvg)

    # Apply filters (event calendar, market regime, confidence threshold)
    nifty_trade, banknifty_trade = apply_filters(nifty_trade, banknifty_trade, event_calendar, market_regime)

    # Gemini synthesis
    session_data = {
        "session": session,
        "global": global_ctx,
        "vix": vix,
        "nifty": {"quote": nifty_quote, "technicals": nifty_tech, "options": nifty_opts, "fvg": nifty_fvg},
        "banknifty": {"quote": banknifty_quote, "technicals": banknifty_tech, "options": banknifty_opts, "fvg": banknifty_fvg},
        "fii_dii": fii_dii,
        "news": news,
        "nifty_trade": nifty_trade,
        "banknifty_trade": banknifty_trade,
        "weights": weights,
        "event_calendar": event_calendar,
        "market_regime": market_regime,
    }

    report = synthesize_with_gemini(session_data, key)
    save_signal(report)

    if bot_token and chat_id:
        send_telegram(report, bot_token, chat_id, event_calendar, market_regime)

    print("[DONE] Research pipeline complete.")
    return report


if __name__ == "__main__":
    result = run_pipeline()
    print(json.dumps(result, indent=2))
