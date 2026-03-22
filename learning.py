"""
Learning Module
Recalibrates factor weights every 10 trades based on what's working
"""

import json
import os
from datetime import datetime, timezone, timedelta
from collections import defaultdict

IST = timezone(timedelta(hours=5, minutes=30))


def analyze_factor_performance(trades: list) -> dict:
    """Analyze which factors are most predictive of winning trades."""
    if len(trades) < 5:
        print("[INFO] Not enough trades to recalibrate (need 5+).")
        return None

    factor_stats = defaultdict(lambda: {"correct": 0, "total": 0})

    for trade in trades:
        if trade.get("result") == "NO TRADE":
            continue

        win = trade.get("win", False)
        direction = trade.get("direction", "")
        trade_bias = "bullish" if direction == "LONG" else "bearish"
        factors = trade.get("factor_signals", {})

        for factor, signal in factors.items():
            factor_stats[factor]["total"] += 1
            # Factor was correct if it agreed with winning trade direction
            if win and signal == trade_bias:
                factor_stats[factor]["correct"] += 1
            elif not win and signal != trade_bias:
                factor_stats[factor]["correct"] += 1  # correctly avoided wrong direction

    return factor_stats


def recalibrate_weights(factor_stats: dict) -> dict:
    """Recalibrate weights based on factor accuracy."""
    factors = ["global", "technical", "options", "sentiment"]

    accuracies = {}
    for factor in factors:
        stats = factor_stats.get(factor, {"correct": 0, "total": 1})
        acc = stats["correct"] / stats["total"] if stats["total"] > 0 else 0.5
        accuracies[factor] = max(acc, 0.1)  # floor at 10%

    # Normalize to sum to 1.0
    total_acc = sum(accuracies.values())
    new_weights = {f: round(acc / total_acc, 3) for f, acc in accuracies.items()}

    print(f"[INFO] New weights: {new_weights}")
    print(f"[INFO] Factor accuracies: {accuracies}")

    return new_weights


def generate_performance_report(trades: list, perf: dict) -> str:
    """Generate a weekly performance summary."""
    completed = [t for t in trades if t.get("result") != "NO TRADE"]
    if not completed:
        return "No completed trades yet."

    last_10 = completed[-10:]
    last_10_wins = len([t for t in last_10 if t.get("win")])
    last_10_pnl = sum(t.get("pnl_inr", 0) for t in last_10)

    lines = [
        f"📊 Performance Report — {datetime.now(IST).strftime('%B %d, %Y')}",
        f"",
        f"Overall: {perf.get('total_trades', 0)} trades | WR: {perf.get('win_rate', 0)}% | P&L: ₹{perf.get('total_pnl_inr', 0):,}",
        f"Last 10: {last_10_wins}/10 wins | P&L: ₹{last_10_pnl:,}",
        f"",
        f"Nifty: {perf.get('nifty_stats', {}).get('trades', 0)} trades | P&L: ₹{perf.get('nifty_stats', {}).get('pnl_inr', 0):,}",
        f"BankNifty: {perf.get('banknifty_stats', {}).get('trades', 0)} trades | P&L: ₹{perf.get('banknifty_stats', {}).get('pnl_inr', 0):,}",
        f"",
        f"Streak: {perf.get('current_streak', 0)} {perf.get('streak_type', '')}s",
    ]

    if perf.get("best_trade"):
        b = perf["best_trade"]
        lines.append(f"Best: {b.get('instrument')} {b.get('direction')} ₹{b.get('pnl_inr', 0):,} ({b.get('date', '')})")
    if perf.get("worst_trade"):
        w = perf["worst_trade"]
        lines.append(f"Worst: {w.get('instrument')} {w.get('direction')} ₹{w.get('pnl_inr', 0):,} ({w.get('date', '')})")

    return "\n".join(lines)


def run_learning():
    """Run the learning and recalibration pipeline."""
    print(f"[START] Learning Module — {datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')}")

    if not os.path.exists("data/trades.json"):
        print("[WARN] No trades file found.")
        return

    with open("data/trades.json") as f:
        trades = json.load(f)

    completed = [t for t in trades if t.get("result") not in ["NO TRADE", None]]
    print(f"[INFO] {len(completed)} completed trades found.")

    if len(completed) < 5:
        print("[INFO] Need at least 5 completed trades to recalibrate.")
        return

    # Load current weights
    weights = {"global": 0.25, "technical": 0.35, "options": 0.25, "sentiment": 0.15}
    if os.path.exists("data/weights.json"):
        with open("data/weights.json") as f:
            weights = json.load(f)

    # Analyze and recalibrate
    factor_stats = analyze_factor_performance(completed)
    if factor_stats:
        new_weights = recalibrate_weights(factor_stats)
        # Blend old and new weights (70% new, 30% old) for stability
        blended = {}
        for f in ["global", "technical", "options", "sentiment"]:
            blended[f] = round(0.7 * new_weights.get(f, 0.25) + 0.3 * weights.get(f, 0.25), 3)
        # Normalize
        total = sum(blended.values())
        blended = {f: round(v / total, 3) for f, v in blended.items()}
        blended["last_updated"] = datetime.now(IST).strftime("%Y-%m-%d")
        blended["trades_since_last_update"] = 0

        with open("data/weights.json", "w") as f:
            json.dump(blended, f, indent=2)
        print(f"[INFO] Weights updated: {blended}")

    # Update performance
    if os.path.exists("data/performance.json"):
        with open("data/performance.json") as f:
            perf = json.load(f)
        report = generate_performance_report(trades, perf)
        print(report)

    print("[DONE] Learning complete.")


if __name__ == "__main__":
    run_learning()
