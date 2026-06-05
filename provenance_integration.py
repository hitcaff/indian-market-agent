"""
provenance_integration.py
--------------------------
Drop-in provenance logging for the Indian Market Agent.

Wraps two points:
  1. save_signal()     → logs the trade signal as a provenance record
  2. monitor_trade()   → logs the outcome (SL_HIT / T1_HIT / T2_HIT)

Usage — add to research.py after save_signal():
    from provenance_integration import log_signal
    log_signal(report)

Usage — add to live_monitor.py after monitor_trade() returns a final status:
    from provenance_integration import log_outcome
    log_outcome(instrument, signal, status, price)

The provenance DB lives at data/provenance.db
Chain root is anchored to Polygon Amoy every 10 decisions (configurable).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

# Add parent directory to path if decision_provenance not installed
try:
    from decision_provenance import ProvenanceLogger
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from decision_provenance import ProvenanceLogger


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH          = os.getenv("PROVENANCE_DB_PATH", "data/provenance.db")
CONTRACT_ADDRESS = os.getenv("CONTRACT_ADDRESS", "")
POKT_RPC_URL     = os.getenv("POKT_RPC_URL", "")
SIGNER_KEY       = os.getenv("PROVENANCE_SIGNER_KEY", "")
EVM_ANCHOR_EVERY = int(os.getenv("EVM_ANCHOR_EVERY", "10"))

# Agent version — bump this when you retrain or significantly change logic
AGENT_VERSION = "2.0.0"

# Confidence threshold used in research.py
CONFIDENCE_THRESHOLD = 65


# ---------------------------------------------------------------------------
# Singleton logger — one instance per process
# ---------------------------------------------------------------------------

_logger: Optional[ProvenanceLogger] = None


def _get_logger() -> ProvenanceLogger:
    global _logger
    if _logger is not None:
        return _logger

    os.makedirs("data", exist_ok=True)

    evm_config = {}
    if all([SIGNER_KEY, POKT_RPC_URL, CONTRACT_ADDRESS]):
        evm_config = {
            "private_key":      SIGNER_KEY,
            "rpc_url":          POKT_RPC_URL,
            "contract_address": CONTRACT_ADDRESS,
        }

    _logger = ProvenanceLogger(
        model_id="indian-market-agent",
        model_version=AGENT_VERSION,
        db_path=DB_PATH,
        input_schema_version="1.0",
        evm_anchor_every=EVM_ANCHOR_EVERY if evm_config else 0,
        evm_config=evm_config,
    )

    # Register configs for both instruments if not already present
    for instrument, threshold in [("NIFTY", CONFIDENCE_THRESHOLD),
                                   ("BANKNIFTY", CONFIDENCE_THRESHOLD)]:
        existing = _logger.configs.current_config(_logger.model_id)
        if not existing:
            _logger.set_config(
                threshold=threshold / 100.0,   # normalise to 0-1
                above_label="TRADE",
                below_label="NO TRADE",
                changed_by="system",
                change_reason=f"initial deployment v{AGENT_VERSION}",
            )
            break

    return _logger


# ---------------------------------------------------------------------------
# 1. Log signal (call from research.py after save_signal)
# ---------------------------------------------------------------------------

def log_signal(report: dict) -> dict:
    """
    Log a trade signal as a provenance record.

    Args:
        report: The full Gemini synthesis output saved by save_signal()

    Returns:
        provenance summary dict with record_id and chain_root
    """
    lg = _get_logger()

    date       = report.get("date", "")
    session    = report.get("session", "unknown")
    bias       = report.get("market_bias", "neutral")
    bias_str   = report.get("bias_strength", "")
    reasoning  = report.get("reasoning", {})

    results = {}

    for instrument, trade_key in [("NIFTY", "nifty_trade"), ("BANKNIFTY", "banknifty_trade")]:
        trade = report.get(trade_key, {})
        direction  = trade.get("direction", "NO TRADE")
        confidence = trade.get("confidence_pct", trade.get("confidence", 0))

        # Normalise confidence to 0-1 for the logger
        score = confidence / 100.0

        # Input features — everything the model "saw"
        input_features = {
            "instrument":    instrument,
            "date":          date,
            "session":       session,
            "market_bias":   bias,
            "bias_strength": bias_str,
            "direction":     direction,
            "entry_zone":    trade.get("entry_zone", ""),
            "stop_loss":     trade.get("stop_loss", 0),
            "target_1":      trade.get("target_1", 0),
            "target_2":      trade.get("target_2", 0),
            "rr_ratio":      trade.get("risk_reward", ""),
            "fvg_entry":     trade.get("fvg_entry_used", False),
            "global_analysis":    reasoning.get("global_analysis", "")[:200],
            "technical_analysis": reasoning.get("technical_analysis", "")[:200],
            "options_analysis":   reasoning.get("options_analysis", "")[:200],
        }

        # Output — what the agent decided
        output = {
            "direction":      direction,
            "confidence_pct": confidence,
            "entry_zone":     trade.get("entry_zone", ""),
            "stop_loss":      trade.get("stop_loss", 0),
            "target_1":       trade.get("target_1", 0),
            "target_2":       trade.get("target_2", 0),
            "risk_reward":    trade.get("risk_reward", ""),
            "invalidation":   trade.get("invalidation", ""),
            "key_levels":     trade.get("key_levels_to_watch", []),
        }

        result = lg.record(
            input_features=input_features,
            output=output,
            score=score,
            session_id=f"{date}_{session}_{instrument}",
        )

        results[instrument] = result
        print(
            f"[PROVENANCE] {instrument} signal logged | "
            f"record_id={result['record_id'][:8]}... | "
            f"chain_root={result['chain_root'][:16]}... | "
            f"records={result['record_count']}"
        )

        # Log EVM anchor receipt if present
        if result.get("evm_receipt") and not result["evm_receipt"].get("error"):
            print(f"[PROVENANCE] Anchored on-chain: tx={result['evm_receipt']['tx_hash'][:16]}...")

    return results


# ---------------------------------------------------------------------------
# 2. Log outcome (call from live_monitor.py when trade closes)
# ---------------------------------------------------------------------------

def log_outcome(
    instrument: str,
    trade: dict,
    status: str,            # SL_HIT / T1_HIT / T2_HIT
    exit_price: float,
) -> dict:
    """
    Log a trade outcome as a separate provenance record.

    Args:
        instrument:  "NIFTY" or "BANKNIFTY"
        trade:       The parsed trade dict from live_monitor
        status:      Final outcome string
        exit_price:  Price at which the outcome was triggered

    Returns:
        provenance summary dict
    """
    lg = _get_logger()

    direction  = trade.get("direction", "NO TRADE")
    entry_mid  = trade.get("entry_mid", 0)
    sl         = trade.get("stop_loss", 0)
    t1         = trade.get("target_1", 0)
    t2         = trade.get("target_2", 0)

    # PnL in points
    if direction == "LONG":
        pnl_points = round(exit_price - entry_mid, 2)
    elif direction == "SHORT":
        pnl_points = round(entry_mid - exit_price, 2)
    else:
        pnl_points = 0

    # Outcome score: 1.0 = T2 hit, 0.5 = T1 hit, 0.0 = SL hit
    score_map = {"T2_HIT": 1.0, "T1_HIT": 0.5, "SL_HIT": 0.0, "ACTIVE": 0.5}
    score = score_map.get(status, 0.5)

    input_features = {
        "instrument":  instrument,
        "direction":   direction,
        "entry_mid":   entry_mid,
        "stop_loss":   sl,
        "target_1":    t1,
        "target_2":    t2,
        "exit_price":  exit_price,
        "record_type": "outcome",
    }

    output = {
        "status":     status,
        "exit_price": exit_price,
        "pnl_points": pnl_points,
        "outcome":    status,
    }

    result = lg.record(
        input_features=input_features,
        output=output,
        score=score,
        session_id=f"outcome_{instrument}_{status}",
    )

    print(
        f"[PROVENANCE] {instrument} outcome logged | "
        f"status={status} | pnl={pnl_points:+.0f}pts | "
        f"record_id={result['record_id'][:8]}..."
    )

    return result


# ---------------------------------------------------------------------------
# 3. Verify and export (call anytime)
# ---------------------------------------------------------------------------

def verify_chain() -> tuple[bool, str]:
    """Verify the full provenance chain integrity."""
    lg = _get_logger()
    return lg.verify()


def export_audit(path: str = "data/audit_log.jsonl") -> int:
    """Export full audit log as JSONL."""
    lg = _get_logger()
    return lg.export_audit_log(path)


def export_compliance(path: str = "data/eu_ai_act_report.json") -> dict:
    """Export EU AI Act Article 13 compliance report."""
    lg = _get_logger()
    return lg.export_eu_ai_act(path)


# ---------------------------------------------------------------------------
# CLI — verify chain from command line
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "verify"

    if cmd == "verify":
        ok, msg = verify_chain()
        print(f"{'✅' if ok else '❌'} {msg}")

    elif cmd == "export":
        n = export_audit()
        print(f"Exported {n} records to data/audit_log.jsonl")
        report = export_compliance()
        dist = report["audit_summary"]["decision_distribution"]
        print(f"Compliance report: {dist}")

    elif cmd == "stats":
        lg = _get_logger()
        print(f"Records:    {lg.chain.record_count}")
        print(f"Chain root: {lg.chain.current_root}")
        print(f"Labels:     {lg.labels.all_labels()}")
        print(f"Configs:    {len(lg.configs.all_configs(lg.model_id))}")
        ok, msg = lg.verify()
        print(f"Integrity:  {'✅' if ok else '❌'} {msg}")
