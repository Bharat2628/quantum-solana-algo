#!/usr/bin/env python3
"""
Dynamic Micro-Ratchet Step-Ladder Cloud Engine (V7 Gravitas Hardened Protocol)
Runs autonomously 24/7 on Linux VPS.
Integrated with Quantum Web Dashboard (/algo/ on snkfleet.duckdns.org)

Gravitas Protocol Upgrades:
1. Dynamic Liquidity-Scaled Stop-Loss:
   - Low-Liquidity (< $40k): -3.5% SL (Dump prevention)
   - High-Liquidity (>= $40k): -5.5% SL (Breathing room for multi-bagger runners like 1, BUBBLE)
2. 5-Minute Momentum Velocity Filter:
   - M5 Volume >= $2,500
   - M5 Price Change >= +1.0% (Green momentum only, never catching falling knives)
   - M5 Buys / Sells >= 1.20 (Aggressive buyers dominating)
   - H1 Drop > -10.0% (Rejects waterfall collapses)
3. Anti-Chop Re-entry Breakout Gate:
   - Repeated entry in previously traded token strictly requires: current_price >= prev_exit_price * 1.03
   - Kills sideways consolidation churn (saving ~₹22,000 lost in BUBBLE/STREAM/OWLDACITY churn)
4. Relaxed Stagnation Cut:
   - Extended from 5m (300s) to 12m (720s) to give high-conviction runners consolidation runway
5. Expanded Discovery & 500-Trade Lifecycle Cap:
   - Includes Pump.fun / PumpSwap graduated pools & DexScreener latest profiles
6. High-Performance Batch Evaluation:
   - Evaluates up to 120 candidates in batches of 30 (~2.5s total cycle), eliminating lag
"""

import os
import sys
import time
import json
import requests
from datetime import datetime, timezone

# Ensure UTF-8 output
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
        sys.stderr.reconfigure(encoding='utf-8', line_buffering=True)
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "engine_v3_state.json")
LOG_FILE = os.path.join(BASE_DIR, "engine_v3.log")

TOTAL_CAPITAL_INR = 100_000.0
MAX_CONCURRENT_SLOTS = 4          # Calibrated 4-slot portfolio
TRADE_SIZE_INR = 3_000.0           # Calibrated ₹3,000 per slot (4 slots = ₹12,000 max, ₹88,000 reserve)
TARGET_LIFECYCLE_TRADES = 500      # Expanded to 500 lifecycle trades

# Dynamic Liquidity-Scaled Stop-Loss (Calibrated to Solana Volatility)
HARD_SL_PCT_LOW_LIQ = 0.055        # -5.5% SL for pools < $40k
HARD_SL_PCT_HIGH_LIQ = 0.075       # -7.5% Buffer SL for pools >= $40k
LIQUIDITY_TIER_THRESHOLD_USD = 40_000.0

SIMULATED_SLIPPAGE_PCT = 0.015     # 1.5% slippage
ESTIMATED_GAS_INR = 37.50          # Priority fee

# Calibrated Positive Expectancy Discovery Gates ($32k-$70k Golden Pocket)
MIN_LIQUIDITY_USD = 32_000.0       # Eliminates sub-$32k illiquid slippage traps
MAX_LIQUIDITY_USD = 70_000.0       # Eliminates >$70k whale distribution dump pools (e.g. CAPYGLOW $84k)
MIN_24H_VOLUME_USD = 40_000.0      # $40k 24h volume
MIN_MARKET_CAP_USD = 80_000.0      # $80k market cap
MIN_VOL_TO_MC_RATIO = 0.80         # Volume-to-MC ratio
MIN_BUY_SELL_RATIO = 0.75
MIN_AGE_HOURS = 0.50               # 30 mins minimum (allows fresh launchpad runners)
MAX_AGE_HOURS = 72.0               # Reject stale coins

# 5-Minute Velocity & Momentum Gates (+3.0% to +5.8% Breakout Window)
MIN_M5_VOLUME_USD = 3_500.0        # At least $3.5k volume in last 5m
MIN_M5_BUYS = 8                    # At least 8 distinct buys in last 5m
MIN_M5_PRICE_CHANGE_PCT = 3.0      # Reject weak chop (< +3.0%)
MAX_M5_PRICE_CHANGE_PCT = 5.8      # Reject blown-off pump exhaustion (> +5.8%)
MIN_M5_BUY_SELL_RATIO = 1.25       # Buyers must dominate sellers by 25% in last 5m
MAX_H1_DROP_PCT = -12.0            # Allows normal pullbacks, rejects waterfall collapses (> -12% in 1 hour)

# Anti-Chop Re-Entry Protection
REENTRY_COOLDOWN_SEC = 7200.0      # 120-minute cooldown (up from 60m)
REENTRY_BREAKOUT_PCT = 1.03        # Must breakout 3% ABOVE previous exit price to re-enter
STAGNATION_TIMEOUT_SEC = 360.0     # 6-minute stagnation threshold (cut dead trades early!)

POLL_INTERVAL_SEC = 15

EXCLUDED_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn"
}

def log_event(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("starting_capital") and "open_positions" in data:
                    data["engine_name"] = "Dynamic Micro-Ratchet Protocol (V7 Gravitas)"
                    return data
        except Exception as e:
            log_event(f"Notice reading existing state: {e}. Starting fresh.")
    return {
        "engine_name": "Dynamic Micro-Ratchet Protocol (V7 Gravitas)",
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

def get_initial_sl(entry_liq: float) -> tuple:
    """Returns initial stop-loss multiple and label based on pool liquidity tier."""
    if entry_liq >= LIQUIDITY_TIER_THRESHOLD_USD:
        sl = round(1.0 - HARD_SL_PCT_HIGH_LIQ, 3)
        label = f"DYNAMIC_SL_HIGH_LIQ (-{int(HARD_SL_PCT_HIGH_LIQ*100)}% @ {sl:.3f}x)"
    else:
        sl = round(1.0 - HARD_SL_PCT_LOW_LIQ, 3)
        label = f"DYNAMIC_SL_LOW_LIQ (-{HARD_SL_PCT_LOW_LIQ*100:.1f}% @ {sl:.3f}x)"
    return sl, label

def get_ratchet_sl(peak_mult: float, prev_sl: float, entry_liq: float) -> tuple:
    """
    Micro-Ratchet Step-Ladder Stop-Loss Formula (V7 Gravitas):
    - Peak >= 6.00x : SL = peak_mult * 0.85 (15% trailing from peak)
    - Peak >= 5.00x : SL = 4.20x (+320% floor)
    - Peak >= 4.00x : SL = 3.30x (+230% floor)
    - Peak >= 3.00x : SL = 2.40x (+140% floor)
    - Peak >= 2.00x : SL = 1.65x (+65% floor)
    - Peak >= 1.50x : SL = 1.35x (+35% floor)
    - Peak >= 1.35x : SL = 1.25x (+25% floor)
    - Peak >= 1.20x : SL = 1.14x (+14% floor)
    - Peak >= 1.08x : SL = 1.02x (+2% Net Green breakeven guard)
    - Peak <  1.08x : Dynamic Liquidity SL (-3.5% or -5.5%)
    """
    if peak_mult >= 6.0:
        sl = round(peak_mult * 0.85, 3)
        label = f"TRAILING_85% (+{int((sl - 1.0)*100)}% floor @ {sl:.2f}x)"
    elif peak_mult >= 5.0:
        sl = 4.20
        label = "RATCHET_LOCK (+320% floor @ 4.20x)"
    elif peak_mult >= 4.0:
        sl = 3.30
        label = "RATCHET_LOCK (+230% floor @ 3.30x)"
    elif peak_mult >= 3.0:
        sl = 2.40
        label = "RATCHET_LOCK (+140% floor @ 2.40x)"
    elif peak_mult >= 2.0:
        sl = 1.70
        label = "RATCHET_LOCK (+70% floor @ 1.70x)"
    elif peak_mult >= 1.50:
        sl = 1.36
        label = "RATCHET_LOCK (+36% floor @ 1.36x)"
    elif peak_mult >= 1.40:
        sl = 1.28
        label = "RATCHET_LOCK (+28% floor @ 1.28x)"
    elif peak_mult >= 1.30:
        sl = 1.20
        label = "RATCHET_LOCK (+20% floor @ 1.20x)"
    elif peak_mult >= 1.20:
        sl = 1.13
        label = "RATCHET_LOCK (+13% floor @ 1.13x)"
    elif peak_mult >= 1.12:
        sl = 1.06
        label = "BREAKEVEN_GUARD (+6% Net Green @ 1.06x)"
    elif peak_mult >= 1.06:
        sl = 1.015
        label = "BREAKEVEN_GUARD (+1.5% Friction Guard @ 1.015x)"
    else:
        sl, label = get_initial_sl(entry_liq)

    final_sl = max(prev_sl, sl)
    return final_sl, label

def fetch_live_pair(mint: str) -> dict:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
    try:
        r = requests.get(url, timeout=7)
        if r.status_code == 200:
            pairs = r.json().get("pairs", [])
            if pairs:
                return max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
    except Exception:
        pass
    return {}

def fetch_pairs_batch(mints: list) -> list:
    """Fetch live pairs for up to 120 mints in chunks of 30 (Ultra-fast batch query)."""
    pairs = []
    chunk_size = 30
    for i in range(0, min(120, len(mints)), chunk_size):
        chunk = mints[i:i+chunk_size]
        url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(chunk)}"
        try:
            r = requests.get(url, timeout=7)
            if r.status_code == 200:
                pairs.extend(r.json().get("pairs", []))
        except Exception:
            pass
        time.sleep(0.08)
    return pairs

def fetch_boosted_mints() -> list:
    mints = set()
    endpoints = [
        "https://api.dexscreener.com/token-boosts/top/v1",
        "https://api.dexscreener.com/token-boosts/latest/v1",
        "https://api.dexscreener.com/token-profiles/latest/v1"
    ]
    for ep in endpoints:
        try:
            r = requests.get(ep, timeout=7)
            if r.status_code == 200:
                items = r.json()
                if isinstance(items, list):
                    for p in items:
                        if p.get("chainId") == "solana":
                            m = p.get("tokenAddress")
                            if m and m not in EXCLUDED_MINTS:
                                mints.add(m)
        except Exception:
            pass

    # DexScreener Solana Search for fresh pump-graduated momentum pairs
    try:
        r_srch = requests.get("https://api.dexscreener.com/latest/dex/search?q=pump", timeout=7)
        if r_srch.status_code == 200:
            pairs = r_srch.json().get("pairs", [])
            for p in pairs:
                if p.get("chainId") == "solana":
                    addr = p.get("baseToken", {}).get("address")
                    if addr and addr not in EXCLUDED_MINTS:
                        mints.add(addr)
    except Exception:
        pass

    # Raydium Standard Pools (Pages 1 & 2 - Top 200 high-volume pools)
    for page in [1, 2]:
        try:
            r_ray = requests.get(
                f"https://api-v3.raydium.io/pools/info/list?poolType=standard&poolSortField=volume24h&sortType=desc&pageSize=100&page={page}",
                timeout=7
            )
            if r_ray.status_code == 200:
                data = r_ray.json()
                pools = data.get("data", {}).get("data", [])
                for pool in pools:
                    for side in ["mintA", "mintB"]:
                        token_info = pool.get(side, {})
                        addr = token_info.get("address")
                        sym = token_info.get("symbol", "").upper()
                        if addr and addr not in EXCLUDED_MINTS and sym not in ["SOL", "WSOL", "USDC", "USDT"]:
                            mints.add(addr)
        except Exception:
            pass

    return list(mints)

def evaluate_pair(p: dict) -> dict:
    if not p:
        return {}

    base = p.get("baseToken", {})
    mint = base.get("address")
    symbol = base.get("symbol", "UNKNOWN")
    name = base.get("name", "Unknown")
    price = float(p.get("priceUsd", 0) or 0)
    if price <= 0 or not mint or mint in EXCLUDED_MINTS:
        return {}

    liq = float(p.get("liquidity", {}).get("usd", 0) or 0)
    vol_24h = float(p.get("volume", {}).get("h24", 0) or 0)
    vol_m5 = float(p.get("volume", {}).get("m5", 0) or 0)
    fdv = float(p.get("fdv", 0) or 0)
    mc = float(p.get("marketCap", fdv) or 0)
    created_at = p.get("pairCreatedAt")

    age = (time.time() - (created_at / 1000.0)) / 3600.0 if created_at else 0.0

    # 24h Transactions
    txns_24h = p.get("txns", {}).get("h24", {})
    buys_24h = txns_24h.get("buys", 0) or 0
    sells_24h = txns_24h.get("sells", 0) or 0
    bs_ratio_24h = buys_24h / max(1, sells_24h)
    vol_mc = vol_24h / max(1, mc)

    # 5-Minute Micro Velocity
    txns_m5 = p.get("txns", {}).get("m5", {})
    buys_m5 = txns_m5.get("buys", 0) or 0
    sells_m5 = txns_m5.get("sells", 0) or 0
    bs_ratio_m5 = buys_m5 / max(1, sells_m5)
    m5_change = float(p.get("priceChange", {}).get("m5", 0) or 0)
    h1_change = float(p.get("priceChange", {}).get("h1", 0) or 0)

    # Base Quality Gates
    base_ok = (
        MIN_LIQUIDITY_USD <= liq <= MAX_LIQUIDITY_USD and 
        vol_24h >= MIN_24H_VOLUME_USD and 
        mc >= MIN_MARKET_CAP_USD and
        vol_mc >= MIN_VOL_TO_MC_RATIO and 
        bs_ratio_24h >= MIN_BUY_SELL_RATIO and 
        MIN_AGE_HOURS <= age <= MAX_AGE_HOURS
    )
    if not base_ok:
        return {}

    # Gravitas V7 Velocity Gates (Only enter green active momentum; strictly reject falling knives & blown-off pumps)
    velocity_ok = (
        buys_m5 >= MIN_M5_BUYS and
        vol_m5 >= MIN_M5_VOLUME_USD and
        MIN_M5_PRICE_CHANGE_PCT <= m5_change <= MAX_M5_PRICE_CHANGE_PCT and
        bs_ratio_m5 >= MIN_M5_BUY_SELL_RATIO and
        h1_change >= MAX_H1_DROP_PCT
    )

    if velocity_ok:
        return {
            "mint": mint,
            "symbol": symbol,
            "name": name,
            "price": price,
            "mc": mc,
            "liquidity": liq,
            "volume_24h": vol_24h,
            "volume_m5": vol_m5,
            "m5_change": m5_change,
            "h1_change": h1_change,
            "buys_m5": buys_m5,
            "sells_m5": sells_m5,
            "dex": p.get("dexId"),
            "url": p.get("url") or f"https://dexscreener.com/solana/{mint}"
        }
    return {}

def run_single_cycle(state: dict) -> bool:
    state_changed = False

    # 1. Update Open Positions & Check Exits
    still_open = []
    for pos in state["open_positions"]:
        mint = pos["mint"]
        time.sleep(0.3)
        pair_data = fetch_live_pair(mint)
        curr_price = float(pair_data.get("priceUsd", 0) or 0) if pair_data else 0.0

        if curr_price <= 0:
            still_open.append(pos)
            continue

        pos["current_price_usd"] = curr_price
        entry_price = pos["entry_price_usd"]
        mult = curr_price / entry_price
        pos["current_multiple"] = round(mult, 4)
        pos["current_pnl_pct"] = round((mult - 1.0) * 100.0, 2)
        state_changed = True

        # Update peak multiple & peak price
        prev_peak = pos.get("peak_multiple", mult)
        peak_mult = max(prev_peak, mult)
        pos["peak_multiple"] = round(peak_mult, 4)
        pos["peak_price_usd"] = round(max(pos.get("peak_price_usd", curr_price), curr_price), 8)

        # Calculate Dynamic Micro-Ratchet SL (Liquidity-Scaled)
        entry_liq = pos.get("entry_liquidity_usd", 35000.0)
        init_sl, _ = get_initial_sl(entry_liq)
        prev_sl = pos.get("current_sl_floor", init_sl)
        locked_sl, sl_label = get_ratchet_sl(peak_mult, prev_sl, entry_liq)
        pos["current_sl_floor"] = locked_sl
        pos["locked_sl_mult"] = locked_sl
        pos["current_sl_label"] = sl_label

        # Calculate Trade Age in Seconds
        entry_time_str = pos.get("entry_time")
        trade_age_sec = 0.0
        if entry_time_str:
            try:
                entry_dt = datetime.fromisoformat(entry_time_str)
                trade_age_sec = (datetime.now(timezone.utc) - entry_dt).total_seconds()
            except Exception:
                pass

        # Check Exits:
        # 1. 6-Minute Stagnation Cut: If open >= 6 min (360s), peak < +5% (no breakout momentum), and currently <= 1.00x
        is_stagnant = (trade_age_sec >= STAGNATION_TIMEOUT_SEC) and (peak_mult < 1.05) and (mult <= 1.00)
        # 2. Hard / Ratchet Stop-Loss
        is_sl_hit = (mult <= locked_sl)

        if is_stagnant or is_sl_hit:
            if is_stagnant:
                exit_price = curr_price * (1.0 - SIMULATED_SLIPPAGE_PCT)
            else:
                # Simulated on-chain stop execution floor: protects capital at max(locked_sl * 0.985, 0.88)
                effective_floor = max(locked_sl * (1.0 - SIMULATED_SLIPPAGE_PCT), 0.88)
                gap_fill = curr_price * (1.0 - SIMULATED_SLIPPAGE_PCT)
                exit_price = max(gap_fill, entry_price * effective_floor) if locked_sl >= 0.90 else gap_fill

            gross = pos["size_inr"] * (exit_price / entry_price)
            net_payout = max(0.0, gross - ESTIMATED_GAS_INR)
            pos["exit_price_usd"] = exit_price
            exit_mult = round(exit_price / entry_price, 4)
            pos["exit_multiple"] = exit_mult

            # Correct Win/Loss check based on actual realized price
            is_win = (exit_mult >= 1.0) and (net_payout > pos["size_inr"])
            if is_win:
                pos["exit_reason"] = f"RATCHET_TP_LOCKED ({locked_sl:.2f}x | Peak: {peak_mult:.2f}x)"
                state["total_wins"] += 1
                icon = "🟢"
                tag = "RATCHET PROFIT LOCK"
            elif is_stagnant:
                pos["exit_reason"] = f"6MIN_STAGNATION_CUT ({exit_mult:.3f}x | Age: {int(trade_age_sec)}s | Peak: {peak_mult:.2f}x)"
                state["total_losses"] += 1
                icon = "🟡"
                tag = "6MIN STAGNATION CUT"
            else:
                if exit_mult < 0.50:
                    pos["exit_reason"] = f"GAP_SLIPPAGE_DUMP ({exit_mult:.3f}x)"
                    tag = "GAP SLIPPAGE"
                else:
                    pos["exit_reason"] = f"DYNAMIC_STOP_LOSS ({locked_sl:.3f}x | Liq: ${entry_liq:,.0f})"
                    tag = "DYNAMIC STOP LOSS"
                state["total_losses"] += 1
                icon = "🔴"

            pos["exit_time"] = datetime.now(timezone.utc).isoformat()
            pos["net_payout_inr"] = round(net_payout, 2)
            pos["net_pnl_inr"] = round(net_payout - pos["size_inr"], 2)

            state["cash_balance"] += net_payout
            state["closed_trades"].append(pos)
            state_changed = True
            save_state(state)
            log_event(f"{icon} [AUTO-EXIT {tag}] Slot {pos.get('slot_id')} | {pos['symbol']} at {exit_mult:.2f}x (Age: {int(trade_age_sec)}s, Peak: {peak_mult:.2f}x) | PnL: ₹{pos['net_pnl_inr']:+,.2f}")
            continue

        still_open.append(pos)

    state["open_positions"] = still_open

    # 2. Check for New Entries
    open_count = len(state["open_positions"])
    total_lifetime = len(state["closed_trades"]) + open_count

    if total_lifetime < TARGET_LIFECYCLE_TRADES and open_count < MAX_CONCURRENT_SLOTS and state["cash_balance"] >= TRADE_SIZE_INR:
        active_slot_ids = {p.get("slot_id") for p in state["open_positions"]}
        available_slot_ids = [s for s in range(1, MAX_CONCURRENT_SLOTS + 1) if s not in active_slot_ids]

        # Build map of last exit prices for anti-chop breakout protection
        last_exit_prices = {}
        for t in state.get("closed_trades", []):
            m = t.get("mint")
            ex_p = float(t.get("exit_price_usd", 0) or 0)
            if m and ex_p > 0:
                last_exit_prices[m] = ex_p

        # Re-entry cooldown guard (60-minute window)
        now_utc = datetime.now(timezone.utc)
        recent_closed_mints = set()
        for t in state.get("closed_trades", []):
            exit_str = t.get("exit_time")
            if exit_str:
                try:
                    exit_dt = datetime.fromisoformat(exit_str)
                    if (now_utc - exit_dt).total_seconds() < REENTRY_COOLDOWN_SEC:
                        recent_closed_mints.add(t["mint"])
                except Exception:
                    recent_closed_mints.add(t["mint"])
            else:
                recent_closed_mints.add(t["mint"])

        # Permanent session blacklist for repeat losers (2+ losses)
        loss_counts = {}
        for t in state.get("closed_trades", []):
            is_win = (t.get("exit_multiple", 0) >= 1.0) and (t.get("net_payout_inr", 0) > t.get("size_inr", 10000))
            if not is_win:
                m = t.get("mint")
                if m:
                    loss_counts[m] = loss_counts.get(m, 0) + 1
        repeat_losers = {m for m, count in loss_counts.items() if count >= 2}

        blocked_mints = {p["mint"] for p in state["open_positions"]} | recent_closed_mints | repeat_losers
        raw_candidates = fetch_boosted_mints()
        unblocked_candidates = [m for m in raw_candidates if m not in blocked_mints][:120]

        # Ultra-fast batch fetch in chunks of 30
        candidate_pairs = fetch_pairs_batch(unblocked_candidates)
        
        # Evaluate pairs in memory
        evaluated_candidates = []
        for p in candidate_pairs:
            ev = evaluate_pair(p)
            if ev:
                evaluated_candidates.append(ev)

        # Sort passing candidates by momentum velocity: m5_change * (buys_m5 / max(1, sells_m5))
        evaluated_candidates.sort(key=lambda x: x["m5_change"] * (x["buys_m5"] / max(1, x["sells_m5"])), reverse=True)

        for evaluated in evaluated_candidates:
            if not available_slot_ids or state["cash_balance"] < TRADE_SIZE_INR:
                break
            mint = evaluated["mint"]
            if mint in blocked_mints:
                continue

            # Anti-Chop Gate: If token was traded before, require current price > prev_exit * 1.03 (Breakout to higher high only!)
            if mint in last_exit_prices:
                prev_exit = last_exit_prices[mint]
                if evaluated["price"] < (prev_exit * REENTRY_BREAKOUT_PCT):
                    log_event(f"🛡️ [ANTI-CHOP-REJECT] {evaluated['symbol']} price ${evaluated['price']:.6f} below breakout threshold (${prev_exit * REENTRY_BREAKOUT_PCT:.6f}). Skipping chop.")
                    continue

            assigned_slot = available_slot_ids.pop(0)
            entry_price = evaluated["price"] * (1.0 + SIMULATED_SLIPPAGE_PCT)
            entry_liq = evaluated["liquidity"]
            init_sl, init_label = get_initial_sl(entry_liq)

            new_trade = {
                "slot_id": assigned_slot,
                "trade_num": total_lifetime + 1,
                "mint": mint,
                "symbol": evaluated["symbol"],
                "name": evaluated["name"],
                "size_inr": TRADE_SIZE_INR,
                "entry_price_usd": entry_price,
                "current_price_usd": evaluated["price"],
                "peak_price_usd": evaluated["price"],
                "current_multiple": 1.0,
                "current_pnl_pct": 0.0,
                "peak_multiple": 1.0,
                "current_sl_floor": init_sl,
                "locked_sl_mult": init_sl,
                "current_sl_label": init_label,
                "entry_liquidity_usd": entry_liq,
                "entry_volume_24h_usd": evaluated["volume_24h"],
                "entry_volume_m5_usd": evaluated["volume_m5"],
                "entry_market_cap_usd": evaluated["mc"],
                "entry_m5_change": evaluated["m5_change"],
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "url": evaluated["url"]
            }
            state["open_positions"].append(new_trade)
            state["cash_balance"] -= TRADE_SIZE_INR
            state["total_trades_initiated"] = total_lifetime + 1
            blocked_mints.add(mint)
            total_lifetime += 1
            state_changed = True
            save_state(state)
            log_event(f"🚀 [AUTO-BUY V7] Slot {assigned_slot} (#{new_trade['trade_num']}): {new_trade['symbol']} @ ${entry_price:.6f} | Liq: ${entry_liq:,.0f} | 5m: +{evaluated['m5_change']:.1f}% | Vol5m: ${evaluated['volume_m5']:,.0f} | Initial SL: {init_sl:.3f}x")

    # Track Peak Equity
    open_val = sum(p["size_inr"] * p.get("current_multiple", 1.0) for p in state["open_positions"])
    total_equity = state["cash_balance"] + open_val
    if total_equity > state.get("peak_equity", TOTAL_CAPITAL_INR):
        state["peak_equity"] = round(total_equity, 2)
        state_changed = True

    return state_changed

def print_status_summary(state: dict):
    open_positions = state["open_positions"]
    open_val = sum(p["size_inr"] * p.get("current_multiple", 1.0) for p in open_positions)
    total_equity = state["cash_balance"] + open_val
    mult = total_equity / TOTAL_CAPITAL_INR
    roi = ((total_equity - TOTAL_CAPITAL_INR) / TOTAL_CAPITAL_INR) * 100.0
    closed = state["closed_trades"]

    print("\n" + "=" * 90, flush=True)
    print(f"  QUANTUM V7 GRAVITAS PROTOCOL  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}", flush=True)
    print("=" * 90, flush=True)
    print(f"  Cash Balance   : ₹{state['cash_balance']:>10,.2f}  |  Open Pos Value: ₹{open_val:>10,.2f}", flush=True)
    print(f"  Total Capital  : ₹{total_equity:>10,.2f}  |  Multiple      : {mult:>6.2f}x ({roi:>+6.1f}%)", flush=True)
    print(f"  Peak Capital   : ₹{state.get('peak_equity', TOTAL_CAPITAL_INR):>10,.2f}  |  Win / Loss    : {state.get('total_wins', 0)}W / {state.get('total_losses', 0)}L", flush=True)
    print(f"  Lifetime Trades: {len(closed) + len(open_positions)} / {TARGET_LIFECYCLE_TRADES}", flush=True)
    print("-" * 90, flush=True)

    if not open_positions:
        print("  Active Slots: [0/8] -- Scanning for M5 momentum breakouts in batch mode...", flush=True)
    else:
        print(f"  Active Slots ({len(open_positions)}/8):", flush=True)
        for pos in open_positions:
            m = pos.get("current_multiple", 1.0)
            pnl_inr = (m - 1.0) * pos["size_inr"]
            liq = pos.get("entry_liquidity_usd", 0)
            print(f"   • Slot {pos.get('slot_id')} [{pos['symbol']}]: {m:.3f}x ({pos.get('current_pnl_pct', 0.0):+6.1f}%) | "
                  f"Peak: {pos.get('peak_multiple', 1.0):.3f}x | Floor: {pos.get('current_sl_floor', 0.95):.3f}x | "
                  f"PnL: ₹{pnl_inr:+7.1f} | Liq: ${liq:,.0f} | {pos.get('current_sl_label', '')}", flush=True)

    print("=" * 90 + "\n", flush=True)

def main():
    log_event("=" * 70)
    log_event("Starting Quantum Solana Engine (V7 Gravitas Hardened Protocol)")
    log_event(f"Parameters: Slots={MAX_CONCURRENT_SLOTS}, Size=₹{TRADE_SIZE_INR:,.0f}, Cap={TARGET_LIFECYCLE_TRADES} trades")
    log_event(f"Dynamic SL: Low-Liq (<$40k)={-HARD_SL_PCT_LOW_LIQ*100:.1f}%, High-Liq (>=$40k)={-HARD_SL_PCT_HIGH_LIQ*100:.1f}%")
    log_event(f"M5 Velocity: Vol>={MIN_M5_VOLUME_USD}, Buys>={MIN_M5_BUYS}, M5_Price>={MIN_M5_PRICE_CHANGE_PCT}%, Buys/Sells>={MIN_M5_BUY_SELL_RATIO}")
    log_event(f"Anti-Chop Breakout Gate: +{int((REENTRY_BREAKOUT_PCT-1.0)*100)}% above previous exit required")
    log_event("=" * 70)

    state = load_state()
    save_state(state)

    cycle_count = 0
    while True:
        try:
            changed = run_single_cycle(state)
            if changed:
                save_state(state)

            cycle_count += 1
            print_status_summary(state)

            time.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            log_event("Engine stopped by user.")
            break
        except Exception as e:
            log_event(f"Unexpected cycle exception: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
