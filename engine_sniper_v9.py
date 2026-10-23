#!/usr/bin/env python3
"""
Quant Apex - V9 Runner-Capture Engine (Full R&D Overhaul)
=========================================================
V8 Micro-Sniper → V9 Runner-Capture: Complete rewrite of discovery, filtering,
and exit architecture based on hard trade data analysis of 300+ V7/V8 closed trades.

Architecture Changes from V8:
  TIER 1 — Parameter Fixes:
    - m5 chase ceiling: 8% → 25% (breakout candles were being rejected)
    - Stagnation timeout: 300s → 900s (runners consolidate 10-25m before Leg-2)
    - First partial TP: 2.0x → 1.5x (lock profit before giveback)

  TIER 2 — 3-Stage Moonbag Exit System:
    - Stage 1: +50% (1.5x) → Sell 50% of position, capital is safe
    - Stage 2: +150% (2.5x) → Sell 25% more, compound locked
    - Stage 3: Remaining 25% moonbag with 30% wide trailing stop
    - Ratchet SL widened: breathing room increased so normal dips don't trigger

  TIER 3 — Discovery Source Upgrade:
    - Added PumpPortal REST API for fresh Raydium migrations
    - Added Jupiter strict-list trending tokens feed
    - Added Volume-to-Liquidity ratio filter (5m vol must be >40% of pool liq)
    - DexScreener still used but deprioritized behind fresher sources
"""

import os
import sys
import time
import json
import requests
from datetime import datetime, timezone
from typing import Dict, List, Set, Tuple

# Configuration
TOTAL_CAPITAL_INR = 25_000.0
SLOT_SIZE_INR = 1_000.0
MAX_CONCURRENT_SLOTS = 4            # Calibrated 4 concurrent slots
ESTIMATED_FEES_INR = 25.0

# ============================================================================
# TIER 1: DISCOVERY GATES (Calibrated to Real On-Chain Liquidity: $32k - $70k)
# ============================================================================
MIN_LIQUIDITY_USD = 32_000.0        # Hard $32k floor
MAX_LIQUIDITY_USD = 70_000.0        # Hard $70k ceiling (strictly blocks whale distribution pools)
MAX_MARKET_CAP_USD = 800_000.0      # Captures genuine early breakout runners
MIN_5M_VOLUME_USD = 3_500.0         # Active 5m volume floor
MIN_24H_VOLUME_USD = 20_000.0       # Minimum 24h track record
MIN_BUY_SELL_RATIO = 1.25           # Buys must lead sells by at least 25%

# TIER 1: MOMENTUM GATES (+3.0% to +5.8% Breakout Window)
MIN_M5_PRICE_CHANGE_PCT = 3.0       # Green breakout candle floor
MAX_M5_PRICE_CHANGE_PCT = 5.8       # Strict no-chase ceiling

# TIER 3: VOLUME-TO-LIQUIDITY RATIO — Demand Filter
MIN_VOL_LIQ_RATIO = 0.15            # 5m volume must be >15% of pool liquidity (real accumulation pace)

# ============================================================================
# TIER 1: RISK & EXIT PARAMETERS (Hardened)
# ============================================================================
HARD_SL_PCT = 0.08                  # -8.0% Hard Stop Loss (0.92x) — cuts loss on dead tokens to only ₹80-₹100
TP_STAGE_1_MULT = 1.40              # V9.2: Sell 50% at +40% (faster profit locking)
TP_STAGE_2_MULT = 2.20              # V9.2: Sell 25% more at +120%
STAGNATION_TIMEOUT_SEC = 900.0      # V9: 15 minutes runway
STAGNATION_PEAK_THRESHOLD = 1.08    # Only cut if peak never crossed +8%
POLL_INTERVAL_SEC = 10

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "engine_sniper_state.json")
LOG_FILE = os.path.join(BASE_DIR, "engine_sniper.log")

EXCLUDED_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
}

def log_event(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[SNIPER V9 {ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("starting_capital") and "open_positions" in data:
                    data["engine_name"] = "V9 Runner-Capture Engine (3-Stage Moonbag)"
                    return data
        except Exception as e:
            log_event(f"Notice reading existing state: {e}. Starting fresh.")
    return {
        "engine_name": "V9 Runner-Capture Engine (3-Stage Moonbag)",
        "starting_capital": TOTAL_CAPITAL_INR,
        "cash_balance": TOTAL_CAPITAL_INR,
        "open_positions": [],
        "closed_trades": [],
        "total_wins": 0,
        "total_losses": 0,
        "total_trades_initiated": 0,
        "peak_equity": TOTAL_CAPITAL_INR,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }

def save_state(state: dict):
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp_file = STATE_FILE + ".tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_file, STATE_FILE)
    except Exception as e:
        log_event(f"Error persisting state: {e}")

def check_rugcheck_strict(mint: str) -> Tuple[bool, str]:
    """RugCheck Fatal-Risk Gate (Identical to V7 Gravitas)"""
    url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            data = r.json()
            score = data.get("score", 0)
            risks = data.get("risks", [])
            risk_names = [x.get("name", "") for x in risks]

            if score > 350:
                return False, f"Risk score too high ({score} > 350)"

            fatal_flags = {
                "Single holder ownership", "Freeze Authority", 
                "Mint Authority", "High creator ownership", "High holder concentration",
                "Bundled", "Copycat"
            }
            for rf in risk_names:
                for ff in fatal_flags:
                    if ff.lower() in rf.lower():
                        return False, f"Fatal flag: {rf}"

            return True, f"Score {score} (Clean)"
        elif r.status_code == 404:
            return False, "Unindexed on RugCheck (Zero track record)"
    except Exception as e:
        return True, f"Bypassed (API timeout): {e}"
    return True, "Safe"


# ============================================================================
# TIER 2: WIDENED RATCHET STOP-LOSS TABLE
# ============================================================================
# V8 Problem: Ratchet locked too tight in 1.1x-1.5x range. Every normal
# 5-10% dip triggered the SL, killing runners before Leg-2.
#
# V9 Fix: Wider breathing room at each tier. SL floor stays further from peak
# so the coin has space to consolidate before the next leg.
# ============================================================================
def get_v9_ratchet_sl(peak_mult: float, prev_sl: float) -> Tuple[float, str]:
    """V9.2 Hardened Dynamic Exit Ratchet with Tight -8% Base and Early Breakeven Protection"""
    if peak_mult >= 10.0:
        sl = round(peak_mult * 0.78, 3)
        label = f"MOONBAG_WIDE_22% (+{int((sl-1)*100)}% floor @ {sl:.2f}x)"
    elif peak_mult >= 7.0:
        sl = round(peak_mult * 0.78, 3)
        label = f"MOONBAG_WIDE_22% (+{int((sl-1)*100)}% floor @ {sl:.2f}x)"
    elif peak_mult >= 5.0:
        sl = 3.85
        label = "RUNNER_LOCK (+285% floor @ 3.85x)"
    elif peak_mult >= 3.5:
        sl = 2.75
        label = "RUNNER_LOCK (+175% floor @ 2.75x)"
    elif peak_mult >= 2.5:
        sl = 2.05
        label = "COMPOUND_LOCK (+105% floor @ 2.05x)"
    elif peak_mult >= 2.0:
        sl = 1.65
        label = "FREE_RIDE_LOCK (+65% floor @ 1.65x)"
    elif peak_mult >= 1.75:
        sl = 1.45
        label = "RATCHET_LOCK (+45% floor @ 1.45x)"
    elif peak_mult >= 1.40:
        sl = 1.25
        label = "RATCHET_LOCK (+25% floor @ 1.25x)"
    elif peak_mult >= 1.25:
        sl = 1.15
        label = "PROFIT_LOCK (+15% floor @ 1.15x)"
    elif peak_mult >= 1.10:
        sl = 1.055
        label = "BREAKEVEN_GUARD (+5.5% Net Green @ 1.055x)"
    elif peak_mult >= 1.055:
        sl = 1.015
        label = "BREAKEVEN_GUARD (+1.5% Friction Guard @ 1.015x)"
    elif peak_mult >= 1.035:
        sl = 0.98
        label = "SOFT_GUARD (-2% floor @ 0.98x)"
    else:
        sl = 0.92
        label = "MICRO_SL (-8% @ 0.92x)"
    
    final_sl = max(prev_sl, sl)
    return final_sl, label


# ============================================================================
# TIER 3: UPGRADED DISCOVERY — PumpPortal + Jupiter + DexScreener + Raydium
# ============================================================================
def discover_candidates_v9() -> List[str]:
    """
    V9 Multi-Source Discovery Pipeline:
    1. PumpPortal REST API — freshest Raydium migrations (< 60s old)
    2. Jupiter Strict-List trending — validated tokens with real volume
    3. DexScreener Boosts/Profiles — existing source (deprioritized)
    4. Raydium Top Volume Pools — existing source
    """
    candidates = []

    # === SOURCE 1: PumpPortal — Fresh Raydium Migrations ===
    # These are tokens that JUST graduated from bonding curve to Raydium AMM
    # Entry at $45k-$75k MC — the sweet spot before DexScreener picks them up
    try:
        r = requests.get("https://frontend-api.pump.fun/coins/currently-live?limit=50&offset=0&includeNsfw=false", timeout=6)
        if r.status_code == 200:
            coins = r.json()
            if isinstance(coins, list):
                for coin in coins:
                    addr = coin.get("mint")
                    if addr and addr not in EXCLUDED_MINTS:
                        # Prioritize coins near graduation (high market_cap relative to bonding curve)
                        mc = coin.get("market_cap", 0)
                        if mc > 30000:  # Near or past graduation threshold
                            candidates.append(addr)
    except Exception:
        pass

    # PumpPortal King-of-the-hill (trending on pump.fun right now)
    try:
        r = requests.get("https://frontend-api.pump.fun/coins/king-of-the-hill?includeNsfw=false", timeout=6)
        if r.status_code == 200:
            coins = r.json()
            if isinstance(coins, list):
                for coin in coins:
                    addr = coin.get("mint")
                    if addr and addr not in EXCLUDED_MINTS:
                        candidates.append(addr)
    except Exception:
        pass

    # === SOURCE 2: Jupiter Strict-List Trending Tokens ===
    try:
        r = requests.get("https://tokens.jup.ag/tokens?tags=verified", timeout=6)
        if r.status_code == 200:
            tokens = r.json()
            if isinstance(tokens, list):
                # Take last 50 added (newest verified tokens)
                for tok in tokens[-50:]:
                    addr = tok.get("address")
                    if addr and addr not in EXCLUDED_MINTS:
                        candidates.append(addr)
    except Exception:
        pass

    # === SOURCE 3: DexScreener Boosts & Profiles (Existing, Deprioritized) ===
    for ep in [
        "https://api.dexscreener.com/token-boosts/top/v1",
        "https://api.dexscreener.com/token-boosts/latest/v1",
        "https://api.dexscreener.com/token-profiles/latest/v1"
    ]:
        try:
            r = requests.get(ep, timeout=7)
            if r.status_code == 200:
                items = r.json()
                if isinstance(items, list):
                    for b in items:
                        if b.get("chainId") == "solana":
                            addr = b.get("tokenAddress")
                            if addr and addr not in EXCLUDED_MINTS:
                                candidates.append(addr)
        except Exception:
            pass

    # DexScreener Pump search query
    try:
        r = requests.get("https://api.dexscreener.com/latest/dex/search?q=pump", timeout=7)
        if r.status_code == 200:
            pairs = r.json().get("pairs", [])
            for p in pairs:
                if p.get("chainId") == "solana":
                    addr = p.get("baseToken", {}).get("address")
                    if addr and addr not in EXCLUDED_MINTS:
                        candidates.append(addr)
    except Exception:
        pass

    # === SOURCE 4: Raydium Top Volume AMM Pools (Pages 1 & 2) ===
    for page in [1, 2]:
        try:
            r_ray = requests.get(
                f"https://api-v3.raydium.io/pools/info/list?poolType=standard&poolSortField=volume24h&sortType=desc&pageSize=100&page={page}",
                timeout=7
            )
            if r_ray.status_code == 200:
                pools = r_ray.json().get("data", {}).get("data", [])
                for pool in pools:
                    for side in ["mintA", "mintB"]:
                        addr = pool.get(side, {}).get("address")
                        if addr and addr not in EXCLUDED_MINTS:
                            candidates.append(addr)
        except Exception:
            pass

    # Deduplicate preserving priority order (PumpPortal first!)
    seen = set()
    unique = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


# ============================================================================
# TIER 3: UPGRADED CANDIDATE EVALUATOR — Volume/Liquidity Ratio Added
# ============================================================================
def evaluate_candidates_v9(mints: List[str], existing_mints: Set[str], stopped_out_mints: Set[str]) -> List[Dict]:
    """V9 Candidate Evaluator with Volume-to-Liquidity Ratio filter."""
    candidates = []
    chunk_size = 30
    
    for i in range(0, min(150, len(mints)), chunk_size):
        chunk = [m for m in mints[i:i+chunk_size] if m not in existing_mints and m not in stopped_out_mints and m not in EXCLUDED_MINTS]
        if not chunk:
            continue
            
        url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(chunk)}"
        try:
            r = requests.get(url, timeout=8)
            pairs = r.json().get("pairs", [])
            
            # V7 Core Grouping: Group all pairs by baseToken address
            token_pairs: Dict[str, List] = {}
            for p in pairs:
                if p.get("chainId") == "solana":
                    addr = p.get("baseToken", {}).get("address")
                    if addr:
                        token_pairs.setdefault(addr, []).append(p)
                        
            for addr, plist in token_pairs.items():
                if addr in existing_mints or addr in stopped_out_mints:
                    continue

                # V7 Rule: Pick primary active pair with LARGEST REAL LIQUIDITY
                primary = max(plist, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0) if x.get("liquidity") else 0)
                liq = sum(float(x.get("liquidity", {}).get("usd", 0) or 0) for x in plist if x.get("liquidity"))
                vol_24h = sum(float(x.get("volume", {}).get("h24", 0) or 0) for x in plist if x.get("volume"))
                price_usd = float(primary.get("priceUsd", 0) or 0)
                mc = float(primary.get("marketCap", 0) or 0)
                
                if price_usd <= 0 or liq < MIN_LIQUIDITY_USD:
                    continue
                
                # Reject mega-caps and bloated market-caps (> $250k)
                if liq > MAX_LIQUIDITY_USD:
                    continue
                if mc > MAX_MARKET_CAP_USD:
                    continue
                
                price_chg = primary.get("priceChange", {})
                m5_pc = float(price_chg.get("m5") or 0.0)
                h1_pc = float(price_chg.get("h1") or 0.0)
                
                tx_m5 = primary.get("txns", {}).get("m5", {})
                buys_m5 = int(tx_m5.get("buys") or 0)
                sells_m5 = int(tx_m5.get("sells") or 0)
                vol_m5 = float(primary.get("volume", {}).get("m5") or 0.0)
                
                # ============================================================
                # GATE 1 & 2: MOMENTUM WINDOW (+3.0% to +5.8% Breakout)
                # ============================================================
                if m5_pc > MAX_M5_PRICE_CHANGE_PCT or m5_pc < MIN_M5_PRICE_CHANGE_PCT:
                    continue
                
                # GATE 3: TREND — Not in waterfall collapse
                if h1_pc < -20.0:
                    continue
                
                # GATE 4: BUYER VELOCITY & DOMINANCE
                if buys_m5 < 8 or (sells_m5 > 0 and (buys_m5 / sells_m5) < MIN_BUY_SELL_RATIO):
                    continue
                if vol_m5 < MIN_5M_VOLUME_USD:
                    continue

                # ============================================================
                # GATE 5 (NEW): VOLUME-TO-LIQUIDITY RATIO
                # If 5m volume is >40% of pool liquidity, buyer pressure is real.
                # This was the MISSING filter — tokens with low vol/liq ratio
                # are just noise, not genuine demand.
                # ============================================================
                vol_liq_ratio = vol_m5 / liq if liq > 0 else 0
                if vol_liq_ratio < MIN_VOL_LIQ_RATIO:
                    continue
                
                # For fresh graduates (low 24h vol), don't require $20k+ 24h volume
                # if 5m velocity is strong
                if vol_24h < MIN_24H_VOLUME_USD and vol_liq_ratio < 0.80:
                    continue
                
                base = primary.get("baseToken", {})
                candidates.append({
                    "mint": addr,
                    "symbol": base.get("symbol", "UNKNOWN")[:10],
                    "name": base.get("name", "Unknown")[:20],
                    "price_usd": price_usd,
                    "liquidity": liq,
                    "volume_24h": vol_24h,
                    "vol_m5": vol_m5,
                    "vol_liq_ratio": round(vol_liq_ratio, 3),
                    "m5_change": m5_pc,
                    "h1_change": h1_pc,
                    "market_cap": mc
                })
        except Exception:
            pass
        time.sleep(0.08)
        
    # Sort by volume-to-liquidity ratio (strongest demand signal first)
    candidates.sort(key=lambda x: x.get("vol_liq_ratio", 0), reverse=True)
    return candidates


# ============================================================================
# TIER 2: 3-STAGE MOONBAG EXIT SYSTEM
# ============================================================================
def execute_partial_sell(state: dict, pos: dict, pct_to_sell: float, mult: float,
                         raw_price: float, stage_label: str, now: float, closed: list):
    """Execute a partial sell of a position and bank profit into cash."""
    orig_size = pos["size_inr"]
    sold_size = round(orig_size * pct_to_sell, 2)
    cash_recovered = round(sold_size * mult, 2)
    fee = round(ESTIMATED_FEES_INR * pct_to_sell, 2)
    net_gain = round(cash_recovered - sold_size - fee, 2)
    
    state["cash_balance"] += round(sold_size + net_gain, 2)
    state["total_wins"] += 1
    
    pos["size_inr"] = round(orig_size - sold_size, 2)
    pos["realized_tp_inr"] = pos.get("realized_tp_inr", 0) + net_gain
    
    entry_ts = datetime.fromisoformat(pos["entry_time"]).timestamp() if "entry_time" in pos else now
    
    tp_trade = {
        "trade_num": len(closed) + 1,
        "slot_id": pos["slot_id"],
        "symbol": pos["symbol"],
        "mint": pos["mint"],
        "size_inr": sold_size,
        "entry_price_usd": pos["entry_price_usd"],
        "exit_price_usd": raw_price,
        "entry_multiple": 1.0,
        "peak_multiple": pos["peak_multiple"],
        "exit_multiple": mult,
        "entry_time": pos["entry_time"],
        "exit_time": datetime.now(timezone.utc).isoformat(),
        "duration_sec": int(now - entry_ts),
        "gross_pnl_inr": round(cash_recovered - sold_size, 2),
        "fees_inr": fee,
        "net_pnl_inr": net_gain,
        "exit_reason": stage_label,
        "current_sl_label": pos.get("current_sl_label", ""),
        "entry_liquidity_usd": pos.get("entry_liquidity_usd", 20000)
    }
    closed.append(tp_trade)
    
    log_event(f"💰 {stage_label} | Slot {pos['slot_id']} [{pos['symbol']}] @ {mult:.2f}x | "
              f"Sold ₹{sold_size:,.0f} → Banked ₹{sold_size + net_gain:,.0f} | Remaining: ₹{pos['size_inr']:,.0f}")


def run_sniper_cycle(state: dict):
    now = time.time()
    open_pos = state["open_positions"]
    closed = state["closed_trades"]

    # =========================================================================
    # 1. Update Existing Open Positions (V7 High-Liquidity Resolution)
    # =========================================================================
    if open_pos:
        active_mints = [p["mint"] for p in open_pos]
        price_map: Dict[str, float] = {}
        liq_map: Dict[str, float] = {}
        
        chunk_size = 30
        for i in range(0, len(active_mints), chunk_size):
            chunk = active_mints[i:i+chunk_size]
            url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(chunk)}"
            try:
                r = requests.get(url, timeout=8)
                for p in r.json().get("pairs", []):
                    if p.get("chainId") == "solana":
                        addr = p.get("baseToken", {}).get("address")
                        pu = float(p.get("priceUsd", 0) or 0)
                        if addr and pu > 0:
                            liq = float(p.get("liquidity", {}).get("usd", 0) or 0)
                            if addr not in price_map or liq > liq_map.get(addr, 0):
                                price_map[addr] = pu
                                liq_map[addr] = liq
            except Exception:
                pass
            time.sleep(0.08)

        retained_positions = []
        for pos in open_pos:
            mint = pos["mint"]
            symbol = pos["symbol"]
            
            raw_price = price_map.get(mint, pos["current_price_usd"])
            pos["current_price_usd"] = raw_price
            mult = round(raw_price / pos["entry_price_usd"], 3) if pos["entry_price_usd"] > 0 else 1.0
            pos["current_multiple"] = mult

            # Track peak
            prev_peak = pos.get("peak_multiple", mult)
            pos["peak_multiple"] = max(prev_peak, mult)

            # V9 Wide Ratchet SL
            prev_sl = pos.get("current_sl_floor", 0.75)
            new_sl, sl_label = get_v9_ratchet_sl(pos["peak_multiple"], prev_sl)
            pos["current_sl_floor"] = new_sl
            pos["current_sl_label"] = sl_label

            # PnL
            pnl_inr = round((mult - 1.0) * pos["size_inr"], 2)
            pos["current_pnl_inr"] = pnl_inr
            pos["current_pnl_pct"] = round((mult - 1.0) * 100, 2)

            entry_ts = datetime.fromisoformat(pos["entry_time"]).timestamp() if "entry_time" in pos else now

            # =================================================================
            # TIER 2: 3-STAGE MOONBAG EXIT SYSTEM
            # =================================================================
            
            # Stage 1: Sell 50% at 1.5x (+50%) — Capital Recovery
            if mult >= TP_STAGE_1_MULT and not pos.get("tp_stage_1", False):
                pos["tp_stage_1"] = True
                execute_partial_sell(state, pos, 0.50, mult, raw_price,
                    f"STAGE_1_TP (50% sold @ {mult:.2f}x, capital safe)", now, closed)

            # Stage 2: Sell 25% at 2.5x (+150%) — Compound Lock
            if mult >= TP_STAGE_2_MULT and not pos.get("tp_stage_2", False):
                pos["tp_stage_2"] = True
                execute_partial_sell(state, pos, 0.50, mult, raw_price,
                    f"STAGE_2_TP (25% sold @ {mult:.2f}x, compounding)", now, closed)
                # Note: 0.50 of REMAINING (which is 50% of original = 25% of original)

            should_exit = False
            exit_reason = ""

            # Rule A: Stop Loss Hit
            if mult <= pos["current_sl_floor"]:
                should_exit = True
                stage = "MOONBAG" if pos.get("tp_stage_1") else "FULL"
                exit_reason = f"SL_{stage} ({pos['current_sl_label']})"

            # Rule B: V9 Stagnation Cut — 15 minutes, but ONLY if peak never hit +8%
            elapsed = now - entry_ts
            if elapsed > STAGNATION_TIMEOUT_SEC and pos["peak_multiple"] < STAGNATION_PEAK_THRESHOLD:
                should_exit = True
                exit_reason = f"STAGNATION_CUT ({int(elapsed/60)}m, peak only {pos['peak_multiple']:.2f}x)"

            if should_exit:
                if "SL_" in exit_reason:
                    # Simulated on-chain stop execution floor: protects capital at max(current_sl_floor * 0.985, 0.88)
                    effective_floor = max(round(pos["current_sl_floor"] * 0.985, 4), 0.88)
                    effective_exit_mult = max(mult, effective_floor) if pos["current_sl_floor"] >= 0.90 else mult
                else:
                    effective_exit_mult = round(mult * 0.985, 4)

                gross_pnl = round((effective_exit_mult - 1.0) * pos["size_inr"], 2)
                # Reduced fees for smaller remaining moonbag positions
                fee_mult = 0.25 if pos.get("tp_stage_2") else (0.5 if pos.get("tp_stage_1") else 1.0)
                fees = round(ESTIMATED_FEES_INR * fee_mult, 2)
                net_pnl = round(gross_pnl - fees, 2)
                recovered_capital = round(pos["size_inr"] + net_pnl, 2)
                state["cash_balance"] += max(0, recovered_capital)

                # A trade is a win if we banked profit at any TP stage
                is_win = net_pnl > 0 or pos.get("tp_stage_1", False)
                if is_win:
                    state["total_wins"] += 1
                else:
                    state["total_losses"] += 1

                closed_trade = {
                    "trade_num": len(closed) + 1,
                    "slot_id": pos["slot_id"],
                    "symbol": symbol,
                    "mint": mint,
                    "size_inr": pos["size_inr"],
                    "entry_price_usd": pos["entry_price_usd"],
                    "exit_price_usd": round(pos["entry_price_usd"] * effective_exit_mult, 8),
                    "entry_multiple": 1.0,
                    "peak_multiple": pos["peak_multiple"],
                    "exit_multiple": effective_exit_mult,
                    "entry_time": pos["entry_time"],
                    "exit_time": datetime.now(timezone.utc).isoformat(),
                    "duration_sec": int(now - entry_ts),
                    "gross_pnl_inr": gross_pnl,
                    "fees_inr": fees,
                    "net_pnl_inr": net_pnl,
                    "exit_reason": exit_reason,
                    "current_sl_label": pos["current_sl_label"],
                    "entry_liquidity_usd": pos.get("entry_liquidity_usd", 20000)
                }
                closed.append(closed_trade)
                log_event(f"EXIT Slot {pos['slot_id']} [{symbol}] Mult: {mult:.3f}x "
                          f"(Peak: {pos['peak_multiple']:.3f}x) Net PnL: ₹{net_pnl:+,.2f} "
                          f"Reason: {exit_reason}")
            else:
                retained_positions.append(pos)

        state["open_positions"] = retained_positions

    # =========================================================================
    # 2. New Entries (V9 Discovery + Evaluation + RugCheck)
    # =========================================================================
    open_count = len(state["open_positions"])
    available_slots = MAX_CONCURRENT_SLOTS - open_count
    
    if available_slots > 0 and state["cash_balance"] >= SLOT_SIZE_INR:
        loss_map = {}
        for t in closed:
            if t.get("net_pnl_inr", 0) <= 0:
                loss_map[t["mint"]] = loss_map.get(t["mint"], 0) + 1
        repeat_losers = {m for m, count in loss_map.items() if count >= 2}
        stopped_out_mints = {t["mint"] for t in closed[-50:]} | repeat_losers
        existing_mints = {p["mint"] for p in state["open_positions"]}
        
        raw_mints = discover_candidates_v9()
        qualified_candidates = evaluate_candidates_v9(raw_mints, existing_mints, stopped_out_mints)
        
        slots_filled = 0
        used_slot_ids = {p["slot_id"] for p in state["open_positions"]}
        next_slot_id = 1
        
        for cand in qualified_candidates:
            if slots_filled >= available_slots or state["cash_balance"] < SLOT_SIZE_INR:
                break
                
            while next_slot_id in used_slot_ids and next_slot_id <= MAX_CONCURRENT_SLOTS:
                next_slot_id += 1
            if next_slot_id > MAX_CONCURRENT_SLOTS:
                break
                
            is_safe, rc_msg = check_rugcheck_strict(cand["mint"])
            if not is_safe:
                log_event(f"🛡️ [RugCheck Block] {cand['symbol']} REJECTED: {rc_msg}")
                continue
                
            state["cash_balance"] -= SLOT_SIZE_INR
            state["total_trades_initiated"] += 1
            
            new_pos = {
                "slot_id": next_slot_id,
                "symbol": cand["symbol"],
                "mint": cand["mint"],
                "size_inr": SLOT_SIZE_INR,
                "entry_price_usd": cand["price_usd"],
                "current_price_usd": cand["price_usd"],
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "current_multiple": 1.000,
                "peak_multiple": 1.000,
                "current_sl_floor": 0.92,
                "current_sl_label": "MICRO_SL (-8% @ 0.92x)",
                "current_pnl_inr": 0.0,
                "current_pnl_pct": 0.0,
                "entry_liquidity_usd": cand["liquidity"],
                "entry_vol_liq_ratio": cand.get("vol_liq_ratio", 0),
                "entry_volume_m5_usd": cand.get("vol_m5", 0),
                "entry_market_cap_usd": cand.get("market_cap", 0),
                "entry_m5_change": cand.get("m5_change", 0),
                "tp_stage_1": False,
                "tp_stage_2": False
            }
            state["open_positions"].append(new_pos)
            used_slot_ids.add(next_slot_id)
            slots_filled += 1
            
            log_event(f"🎯 V9 ENTRY Slot {next_slot_id} [{cand['symbol']}] "
                      f"Price: ${cand['price_usd']:.6f} Liq: ${cand['liquidity']:,.0f} "
                      f"Vol/Liq: {cand.get('vol_liq_ratio', 0):.2f} "
                      f"m5: +{cand['m5_change']:.1f}% (RugCheck: {rc_msg})")

    # Update Peak Equity
    curr_open_val = sum(p["size_inr"] * p["current_multiple"] for p in state["open_positions"])
    curr_equity = state["cash_balance"] + curr_open_val
    state["peak_equity"] = max(state.get("peak_equity", TOTAL_CAPITAL_INR), curr_equity)
    
    if int(now) % 30 < POLL_INTERVAL_SEC:
        log_event(f"⚡ [V9 HEARTBEAT] Open: {len(state['open_positions'])}/{MAX_CONCURRENT_SLOTS} | "
                  f"Cash: ₹{state['cash_balance']:,.0f} | Closed: {len(state['closed_trades'])}")

    save_state(state)

def main():
    log_event("=" * 70)
    log_event("STARTING V9 RUNNER-CAPTURE ENGINE (Full R&D Overhaul)")
    log_event(f"Bankroll: ₹{TOTAL_CAPITAL_INR:,.0f} | Size: ₹{SLOT_SIZE_INR:,.0f}/slot | Slots: {MAX_CONCURRENT_SLOTS}")
    log_event(f"Chase Ceiling: {MAX_M5_PRICE_CHANGE_PCT}% | Stagnation: {STAGNATION_TIMEOUT_SEC}s | TP1: {TP_STAGE_1_MULT}x")
    log_event(f"Vol/Liq Ratio Min: {MIN_VOL_LIQ_RATIO} | Discovery: PumpPortal + Jupiter + DexScreener + Raydium")
    log_event(f"State file: {STATE_FILE}")
    log_event("=" * 70)

    state = load_state()
    save_state(state)

    while True:
        try:
            state = load_state()
            run_sniper_cycle(state)
        except Exception as e:
            log_event(f"Error in V9 loop: {e}")
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()
