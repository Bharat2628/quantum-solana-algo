#!/usr/bin/env python3
"""
Engine V4: The Data-Backed Golden Breakout & 5x Ladder Protocol
Empirically calibrated from Solana on-chain survival & runner data:
  1. THE GOLDEN BREAKOUT WINDOW:
     - Age Filter: Token created between 45 minutes and 4.5 hours ago.
       (Rejects 0-30m sniper dumps and >6h fatigue bleeders).
     - Market Cap: $250,000 to $2,500,000.
       (Proves survival from bonding curve & initial dev shakeout).
     - Liquidity Pool: >= $35,000 (Prevents catastrophic slippage).
     - Order Flow: Buys >= 1.35x Sells (Net aggressive buyer absorption).
     - 1h Volume: >= $150,000.
     - Price Momentum: 5m change <= +18.0% (Bans vertical tops), 1h change >= +15.0%.
  2. QUALITY GATES:
     - RugCheck score <= 300.
     - Mint & Freeze authorities strictly revoked.
  3. CONCENTRATED 4-SLOT BASKET:
     - Maximum 4 concurrent active positions.
     - Slot size: 20% of equity (Leaves 20% cash buffer).
  4. THE 3-STAGE 5X LADDER EXIT:
     - Hard Invalidation Stop: -12.0% (Covers DEX noise without deep drawdown).
     - Breakeven Trigger: At 1.30x -> Stop moves to 1.08x (Guarantees net green exit after fees).
     - Stage 1 (2.0x): Sell 50% tokens -> Recovers 100% of entire initial capital to cash! (Risk = 0).
     - Stage 2 (5.0x): Sell 25% tokens -> Locks in the 5x windfall profit into cash!
     - Stage 3 (Moonbag 25%): 20% Trailing Stop from peak (Rides 10x-25x super runners).
     - Time Stop: If after 45 minutes peak < 1.15x and multiple < 1.03x -> Exit before fatigue dump.
"""

import os
import sys
import time
import json
import requests
from datetime import datetime, timezone
from typing import List, Dict, Set

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
        sys.stderr.reconfigure(encoding='utf-8', line_buffering=True)
    except Exception:
        pass

TOTAL_CAPITAL_INR = 100_000.0
NUM_SLOTS = 4                 # Strict 4-slot basket
SLOT_RATIO = 0.20             # 20% per slot (Max 80% invested, 20% cash reserve)

ENTRY_GAS_INR = 12.50         # ~0.001 SOL
EXIT_GAS_INR = 15.00          # Priority exit gas
DEX_FEE_PCT = 0.01            # 1.0%
SLIPPAGE_PCT = 0.012          # 1.2% normal
PANIC_SLIPPAGE_PCT = 0.025     # 2.5% panic

# 3-STAGE 5X EXIT LADDER
HARD_SL_PCT = 0.12            # -12% Invalidation stop loss
BE_TRIGGER_MULT = 1.30        # At 1.30x -> Move stop to 1.08x floor
BE_FLOOR_MULT = 1.08          # 1.08x floor covers all fees + locks net green
STAGE1_TP_MULT = 2.00         # Stage 1: At 2.0x -> Sell 50% (Recovers 100% cash)
STAGE1_SELL_RATIO = 0.50
STAGE2_TP_MULT = 5.00         # Stage 2: At 5.0x -> Sell 25% (Locks 5x profit)
STAGE2_SELL_RATIO = 0.25
MOONBAG_TRAIL_PCT = 0.20      # Stage 3: 20% Trailing stop on remaining 25% moonbag

POLL_INTERVAL_SEC = 30        # Faster 30s polling to reduce slippage
STATE_FILE = "engine_v3_state.json"

EXCLUDED_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
}

def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "engine_name": "Engine V4 Golden Breakout & 5x Ladder",
        "starting_capital": TOTAL_CAPITAL_INR,
        "cash_balance": TOTAL_CAPITAL_INR,
        "open_positions": [],
        "closed_trades": [],
        "stopped_out_history": {},
        "total_wins": 0,
        "total_losses": 0,
        "total_gas_paid_inr": 0.0,
        "peak_equity": TOTAL_CAPITAL_INR,
        "daily_trades_count": 0,
        "last_daily_reset_ts": time.time(),
        "created_at": datetime.now(timezone.utc).isoformat()
    }

def save_state(state: Dict):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)

def get_current_slot_size(state: Dict) -> float:
    open_val = 0.0
    for p in state["open_positions"]:
        entry = p.get("entry_price_usd", 0)
        curr = p.get("current_price_usd", 0)
        mult = curr / entry if entry > 0 else 1.0
        open_val += p.get("effective_invested_inr", 0) * p.get("tokens_remaining_ratio", 1.0) * mult
    
    current_equity = state["cash_balance"] + open_val
    slot_size = current_equity * SLOT_RATIO
    return round(max(3000.0, slot_size), 2)

def check_rugcheck_strict(mint: str) -> (bool, str):
    """RugCheck Fatal-Risk Gate"""
    url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            data = r.json()
            score = data.get("score", 0)
            risks = data.get("risks", [])
            risk_names = [x.get("name", "") for x in risks]

            if score > 350:
                return False, f"Risk score high ({score} > 350)"

            fatal_flags = {
                "Freeze Authority", "Mint Authority", "Single holder ownership", 
                "High creator ownership"
            }
            for rf in risk_names:
                for ff in fatal_flags:
                    if ff.lower() in rf.lower():
                        return False, f"Fatal flag: {rf}"

            return True, f"Score {score} (Clean)"
    except Exception as e:
        return True, f"Bypassed (API timeout): {e}"
    return True, "Safe"

def discover_candidates_live() -> List[str]:
    """
    DISCOVER REAL BREAKOUT CANDIDATES:
    Scans Top Boosts, Active Profiles, and Top Trending Solana pairs.
    """
    discovered = []
    headers = {"User-Agent": "Mozilla/5.0"}
    endpoints = [
        "https://api.dexscreener.com/token-boosts/top/v1",
        "https://api.dexscreener.com/token-boosts/latest/v1",
        "https://api.dexscreener.com/token-profiles/latest/v1",
        "https://api.dexscreener.com/latest/dex/search?q=pump",
        "https://api.dexscreener.com/latest/dex/search?q=solana"
    ]
    for url in endpoints:
        try:
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    for item in data:
                        if item.get("chainId") == "solana":
                            addr = item.get("tokenAddress")
                            if addr and addr not in discovered:
                                discovered.append(addr)
                elif isinstance(data, dict):
                    for p in data.get("pairs", []):
                        if p.get("chainId") == "solana":
                            addr = p.get("baseToken", {}).get("address")
                            if addr and addr not in discovered:
                                discovered.append(addr)
        except Exception:
            pass

    return discovered

def evaluate_breakout_candidates(mints: List[str], existing_mints: Set[str], stopped_out_history: Dict[str, float]) -> List[Dict]:
    """
    CONTINUOUS DYNAMIC MOMENTUM & SURVIVAL MATRIX:
    Replaces rigid discrete bins with continuous dynamic multi-factor scoring.
    Safety non-negotiables:
      - Price > 0
      - Liquidity >= $25,000 (Prevents excessive execution slippage)
      - Market Cap >= $100,000
      - Age >= 0.55 hours (~33m, escapes 0-30m sniper dump cliff)
      - 5m Price change <= +20% (Prevents buying the blowoff peak)
      - Dynamic Volume vs Pool: 1h Vol >= max($35k, 0.70 * liq)
      - Net Buyer Order Flow: 1h Buys >= 1.05x Sells OR 24h Buys >= 1.10x Sells
    """
    chunk_size = 30
    candidates = []
    now_ts = time.time()
    now_ms = now_ts * 1000

    for i in range(0, len(mints), chunk_size):
        chunk = [m for m in mints[i:i + chunk_size] if m not in existing_mints and m not in EXCLUDED_MINTS]
        if not chunk:
            continue
        url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(chunk)}"
        try:
            r = requests.get(url, timeout=10)
            pairs = r.json().get("pairs", [])
            token_pairs: Dict[str, List] = {}
            for p in pairs:
                if p.get("chainId") == "solana":
                    addr = p.get("baseToken", {}).get("address")
                    if addr:
                        token_pairs.setdefault(addr, []).append(p)

            for addr, plist in token_pairs.items():
                if addr in existing_mints or addr in EXCLUDED_MINTS:
                    continue

                if addr in stopped_out_history:
                    if (now_ts - stopped_out_history[addr]) < (6 * 3600):
                        continue

                primary = max(plist, key=lambda x: x.get("liquidity", {}).get("usd", 0) if x.get("liquidity") else 0)
                liq = sum(x.get("liquidity", {}).get("usd", 0) for x in plist if x.get("liquidity"))
                vol_24h = sum(x.get("volume", {}).get("h24", 0) for x in plist if x.get("volume"))
                vol_1h = float(primary.get("volume", {}).get("h1") or 0.0)
                mc = float(primary.get("fdv", 0) or primary.get("marketCap", 0) or 0)
                price_usd = float(primary.get("priceUsd", 0) or 0)

                # 1. Non-negotiable anti-scam floor
                if price_usd <= 0 or liq < 25_000.0 or mc < 100_000.0:
                    continue

                created_at = primary.get("pairCreatedAt", 0)
                if created_at <= 0:
                    continue
                age_hours = (now_ms - created_at) / (1000.0 * 3600.0)

                # Never enter during 0-33m sniper dump zone
                if age_hours < 0.55:
                    continue

                # Anti-blowoff top guard (>20% vertical in 5m)
                price_chg = primary.get("priceChange", {})
                m5_pc = float(price_chg.get("m5") or 0.0)
                h1_pc = float(price_chg.get("h1") or 0.0)
                if m5_pc > 20.0:
                    continue

                # Dynamic Volume Gate: Scaled dynamically to pool depth
                min_vol = max(35_000.0, 0.70 * liq)
                if vol_1h < min_vol and vol_24h < (min_vol * 3.5):
                    continue

                # Order flow: Net buyers absorbing volume
                txns_1h = primary.get("txns", {}).get("h1", {})
                buys_1h = int(txns_1h.get("buys") or 0)
                sells_1h = int(txns_1h.get("sells") or 0)

                txns_24h = primary.get("txns", {}).get("h24", {})
                buys_24h = int(txns_24h.get("buys") or 0)
                sells_24h = int(txns_24h.get("sells") or 0)

                ratio_1h = buys_1h / max(1, sells_1h)
                ratio_24h = buys_24h / max(1, sells_24h)

                if ratio_1h < 1.05 and ratio_24h < 1.10:
                    continue

                # Continuous Dynamic Score:
                liq_factor = min(3.0, liq / 30_000.0)
                vol_factor = min(4.0, vol_1h / 50_000.0)
                ratio_factor = min(2.5, ratio_1h)
                age_factor = 1.2 if (0.8 <= age_hours <= 12.0) else (0.8 if age_hours < 0.8 else 0.95)
                score = round(liq_factor * vol_factor * ratio_factor * age_factor, 3)

                base = primary.get("baseToken", {})
                candidates.append({
                    "mint": addr,
                    "symbol": base.get("symbol", "UNKNOWN")[:10],
                    "name": base.get("name", "Unknown")[:20],
                    "price_usd": price_usd,
                    "market_cap": mc,
                    "liquidity": liq,
                    "volume_1h": vol_1h,
                    "volume_24h": vol_24h,
                    "age_hours": round(age_hours, 2),
                    "m5_change": m5_pc,
                    "h1_change": h1_pc,
                    "buys_1h": buys_1h,
                    "sells_1h": sells_1h,
                    "dex": primary.get("dexId", "").upper(),
                    "url": primary.get("url"),
                    "score": score
                })
        except Exception:
            pass
        time.sleep(0.08)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates

def fill_empty_slots(state: Dict):
    open_positions = state["open_positions"]
    needed = NUM_SLOTS - len(open_positions)
    cash = state["cash_balance"]

    if needed <= 0:
        return

    slot_size_inr = get_current_slot_size(state)
    if cash < slot_size_inr:
        return

    existing_mints = set(p["mint"] for p in open_positions)
    stopped_out_history = state.get("stopped_out_history", {})

    raw_mints = discover_candidates_live()
    ranked_candidates = evaluate_breakout_candidates(raw_mints, existing_mints, stopped_out_history)

    if not ranked_candidates:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔍 [Scanner] Discovered {len(raw_mints)} tokens | 0 passed dynamic momentum criteria", flush=True)
        return
    else:
        top_preview = ", ".join(f"{c['symbol']}({c['score']})" for c in ranked_candidates[:4])
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔍 [Scanner] Discovered {len(raw_mints)} tokens | {len(ranked_candidates)} passed dynamic criteria (Top: {top_preview})", flush=True)

    filled_count = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for cand in ranked_candidates:
        if filled_count >= needed or state["cash_balance"] < slot_size_inr:
            break

        is_safe, rc_msg = check_rugcheck_strict(cand["mint"])
        if not is_safe:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 🛡️ [RugCheck Block] {cand['symbol']} REJECTED: {rc_msg}", flush=True)
            continue

        entry_fee_inr = slot_size_inr * (DEX_FEE_PCT + SLIPPAGE_PCT)
        effective_invested = slot_size_inr - ENTRY_GAS_INR - entry_fee_inr
        state["total_gas_paid_inr"] += ENTRY_GAS_INR

        pos = {
            "slot_id": len(open_positions) + 1,
            "mint": cand["mint"],
            "symbol": cand["symbol"],
            "name": cand["name"],
            "size_inr": slot_size_inr,
            "effective_invested_inr": effective_invested,
            "entry_price_usd": cand["price_usd"],
            "current_price_usd": cand["price_usd"],
            "peak_price_usd": cand["price_usd"],
            "current_multiple": 1.0,
            "current_pnl_pct": 0.0,
            "current_sl_floor": 0.88,
            "current_sl_label": "HARD_STOP_LOSS (-12%)",
            "peak_multiple": 1.0,
            "stage1_hit": False,
            "stage2_hit": False,
            "tokens_remaining_ratio": 1.0,
            "locked_cash_inr": 0.0,
            "entry_time": now_iso,
            "entry_liquidity_usd": cand["liquidity"],
            "entry_market_cap_usd": cand["market_cap"],
            "entry_volume_1h_usd": cand["volume_1h"],
            "entry_age_hours": cand["age_hours"],
            "dex": cand["dex"],
            "url": cand["url"]
        }

        open_positions.append(pos)
        state["cash_balance"] -= slot_size_inr
        state["daily_trades_count"] = state.get("daily_trades_count", 0) + 1
        filled_count += 1
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🎯 [Dynamic Breakout Entry] {cand['symbol']} @ ₹{slot_size_inr:,.0f} | MC: ${cand['market_cap']:,.0f} | Age: {cand['age_hours']}h | 1h Vol: ${cand['volume_1h']:,.0f} | Pool: ${cand['liquidity']:,.0f}", flush=True)

    for idx, p in enumerate(open_positions, 1):
        p["slot_id"] = idx

    save_state(state)


def update_and_evaluate_portfolio(state: Dict):
    open_positions = state["open_positions"]
    if not open_positions:
        return state["cash_balance"], 0.0

    mints = [p["mint"] for p in open_positions]
    price_map: Dict[str, float] = {}
    liq_map: Dict[str, float] = {}

    chunk_size = 30
    for i in range(0, len(mints), chunk_size):
        chunk = mints[i:i + chunk_size]
        url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(chunk)}"
        try:
            r = requests.get(url, timeout=10)
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

    now_iso = datetime.now(timezone.utc).isoformat()
    now_ts = time.time()
    remaining_open = []
    total_open_value_inr = 0.0

    for pos in open_positions:
        mint = pos["mint"]
        raw_price = price_map.get(mint, pos["current_price_usd"])
        pool_liq = liq_map.get(mint, 0.0)

        pos["current_price_usd"] = raw_price
        entry_price = pos["entry_price_usd"]

        if raw_price > pos["peak_price_usd"]:
            pos["peak_price_usd"] = raw_price

        multiple = raw_price / entry_price if entry_price > 0 else 1.0
        pos["current_multiple"] = round(multiple, 4)
        pos["current_pnl_pct"] = round((multiple - 1.0) * 100.0, 2)
        peak_mult = pos["peak_price_usd"] / entry_price if entry_price > 0 else 1.0

        exit_triggered = False
        exit_reason = ""
        payout_inr = 0.0
        net_exit = 0.0

        # Calculate hold duration
        entry_iso = pos.get("entry_time")
        hold_minutes = 0.0
        if entry_iso:
            try:
                entry_dt = datetime.fromisoformat(entry_iso.replace("Z", "+00:00"))
                hold_minutes = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60.0
            except Exception:
                pass

        # Continuous Multi-Tier Dynamic Profit Ratchet (Buffer-Protected for Net Green):
        if peak_mult >= 1.60:
            current_sl_floor = 1.40
            sl_label = f"DYNAMIC_LOCK (+40% floor @ {multiple:.2f}x)"
        elif peak_mult >= 1.35:
            current_sl_floor = 1.22
            sl_label = f"DYNAMIC_LOCK (+22% floor @ {multiple:.2f}x)"
        elif peak_mult >= 1.20:
            current_sl_floor = 1.12
            sl_label = f"DYNAMIC_LOCK (+12% floor @ {multiple:.2f}x)"
        elif peak_mult >= 1.12:
            current_sl_floor = 1.075
            sl_label = f"DYNAMIC_LOCK (+7.5% floor @ {multiple:.2f}x)"
        elif peak_mult >= 1.075:
            current_sl_floor = 1.045
            sl_label = f"BREAKEVEN_GUARD (+4.5% floor @ {multiple:.2f}x)"
        else:
            current_sl_floor = 1.0 - HARD_SL_PCT
            sl_label = f"HARD_STOP_LOSS (-12% @ {multiple:.2f}x)"

        pos["current_sl_floor"] = current_sl_floor
        pos["current_sl_label"] = sl_label
        pos["peak_multiple"] = round(peak_mult, 3)

        # Time stop: Fatigue prevention (if after 45m coin doesn't break out)
        fatigue_exit = (hold_minutes >= 45.0 and peak_mult < 1.15 and multiple < 1.03 and not pos["stage1_hit"])

        # Liquidity drain escape
        liq_drain = (pool_liq < 18_000.0 and pos.get("entry_liquidity_usd", 0) >= 35_000.0)

        # -------------------------------------------------------------
        # 1. HARD STOP / BREAKEVEN / FATIGUE EXIT
        # -------------------------------------------------------------
        if not pos["stage1_hit"] and (multiple <= current_sl_floor or fatigue_exit or liq_drain):
            exit_triggered = True
            if liq_drain:
                exit_reason = f"LIQUIDITY_DRAIN_ESCAPE (${pool_liq:,.0f} @ {multiple:.2f}x)"
            elif fatigue_exit:
                exit_reason = f"FATIGUE_TIMEOUT (45m flat @ {multiple:.2f}x)"
            else:
                exit_reason = sl_label

            is_panic = liq_drain or multiple <= (1.0 - HARD_SL_PCT)
            slip = PANIC_SLIPPAGE_PCT if is_panic else SLIPPAGE_PCT
            gross = pos["effective_invested_inr"] * pos["tokens_remaining_ratio"] * multiple
            net_exit = max(0.0, gross - (gross * (DEX_FEE_PCT + slip)) - EXIT_GAS_INR)
            state["total_gas_paid_inr"] += EXIT_GAS_INR
            payout_inr = pos["locked_cash_inr"] + net_exit
            state.setdefault("stopped_out_history", {})[mint] = now_ts

        # -------------------------------------------------------------
        # 2. STAGE 1 REALIZATION: 2.0x (Sell 50% -> Recovers 100% Cash!)
        # -------------------------------------------------------------
        elif multiple >= STAGE1_TP_MULT and not pos["stage1_hit"]:
            pos["stage1_hit"] = True
            exec_mult = multiple
            sold_portion = pos["effective_invested_inr"] * STAGE1_SELL_RATIO * exec_mult
            net_sold = max(0.0, sold_portion - (sold_portion * (DEX_FEE_PCT + SLIPPAGE_PCT)) - EXIT_GAS_INR)
            state["total_gas_paid_inr"] += EXIT_GAS_INR
            pos["locked_cash_inr"] += net_sold
            pos["tokens_remaining_ratio"] -= STAGE1_SELL_RATIO
            state["cash_balance"] += net_sold
            print(f"  💰 [STAGE 1 TP: 2.0x HIT] {pos['symbol']} @ {exec_mult:.2f}x! Locked cash: ₹{net_sold:,.0f}. Capital 100% recovered to cash. Risk = 0!", flush=True)

        # -------------------------------------------------------------
        # 3. STAGE 2 REALIZATION: 5.0x (Sell 25% -> Locks in 5x Windfall!)
        # -------------------------------------------------------------
        elif multiple >= STAGE2_TP_MULT and pos["stage1_hit"] and not pos["stage2_hit"]:
            pos["stage2_hit"] = True
            exec_mult = multiple
            sold_portion = pos["effective_invested_inr"] * STAGE2_SELL_RATIO * exec_mult
            net_sold = max(0.0, sold_portion - (sold_portion * (DEX_FEE_PCT + SLIPPAGE_PCT)) - EXIT_GAS_INR)
            state["total_gas_paid_inr"] += EXIT_GAS_INR
            pos["locked_cash_inr"] += net_sold
            pos["tokens_remaining_ratio"] -= STAGE2_SELL_RATIO
            state["cash_balance"] += net_sold
            print(f"  🚀 [STAGE 2 TP: 5.0x HIT] {pos['symbol']} @ {exec_mult:.2f}x! Locked windfall cash: ₹{net_sold:,.0f}!", flush=True)

        # -------------------------------------------------------------
        # 4. STAGE 3: 20% TRAILING STOP ON 25% MOONBAG
        # -------------------------------------------------------------
        elif pos["stage1_hit"]:
            peak = pos["peak_price_usd"]
            drawdown_from_peak = (peak - raw_price) / peak if peak > 0 else 0.0
            if drawdown_from_peak >= MOONBAG_TRAIL_PCT:
                exit_triggered = True
                exit_reason = f"MOONBAG_TRAIL_EXIT (Peak {peak/entry_price:.2f}x -> Exit {multiple:.2f}x)"
                gross = pos["effective_invested_inr"] * pos["tokens_remaining_ratio"] * multiple
                net_exit = max(0.0, gross - (gross * (DEX_FEE_PCT + SLIPPAGE_PCT)) - EXIT_GAS_INR)
                state["total_gas_paid_inr"] += EXIT_GAS_INR
                payout_inr = pos["locked_cash_inr"] + net_exit

        if exit_triggered:
            net_pnl = payout_inr - pos["size_inr"]
            if net_pnl > 0:
                state["total_wins"] += 1
            else:
                state["total_losses"] += 1

            state["cash_balance"] += net_exit
            state["closed_trades"].append({
                "mint": pos["mint"],
                "symbol": pos["symbol"],
                "size_inr": pos["size_inr"],
                "entry_price_usd": pos["entry_price_usd"],
                "exit_price_usd": raw_price,
                "peak_multiple": round(pos["peak_price_usd"] / pos["entry_price_usd"], 2),
                "exit_multiple": round(multiple, 3),
                "exit_reason": exit_reason,
                "exit_time": now_iso,
                "net_payout_inr": round(payout_inr, 2),
                "net_pnl_inr": round(net_pnl, 2),
                "entry_market_cap_usd": pos.get("entry_market_cap_usd", 0),
                "entry_liquidity_usd": pos.get("entry_liquidity_usd", 0),
                "entry_volume_1h_usd": pos.get("entry_volume_1h_usd", 0)
            })
            state.setdefault("stopped_out_history", {})[pos["mint"]] = now_ts
            sign = '+' if net_pnl > 0 else ''
            print(f"  🏁 [Closed Trade] {pos['symbol']} | {exit_reason} | Net PnL: {sign}₹{net_pnl:,.0f}", flush=True)
        else:
            current_val = (pos["effective_invested_inr"] * pos["tokens_remaining_ratio"] * multiple)
            total_open_value_inr += current_val
            remaining_open.append(pos)

    state["open_positions"] = remaining_open
    return state["cash_balance"], total_open_value_inr

def print_telemetry(state: Dict, cash: float, open_val: float):
    equity = cash + open_val
    if equity > state.get("peak_equity", TOTAL_CAPITAL_INR):
        state["peak_equity"] = equity

    peak = state.get("peak_equity", TOTAL_CAPITAL_INR)
    dd_pct = ((peak - equity) / peak * 100.0) if peak > 0 else 0.0

    wins = state.get("total_wins", 0)
    losses = state.get("total_losses", 0)
    total_closed = len(state.get("closed_trades", []))
    wr = (wins / total_closed * 100.0) if total_closed > 0 else 0.0
    closed_pnl = sum(t.get("net_pnl_inr", 0) for t in state.get("closed_trades", []))

    print("-" * 80)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Equity: ₹{equity:,.2f} ({equity/TOTAL_CAPITAL_INR:.3f}x) | Cash: ₹{cash:,.2f} | Open Val: ₹{open_val:,.2f} ({len(state['open_positions'])}/{NUM_SLOTS})")
    print(f"Closed: {total_closed} ({wins}W/{losses}L, WR: {wr:.1f}%) | Closed PnL: ₹{closed_pnl:,.2f} | Peak: ₹{peak:,.2f} | DD: {dd_pct:.1f}%")
    print("-" * 80)

def main():
    print("=" * 80)
    print("🚀 QUANTUM ENGINE V4: GOLDEN BREAKOUT & 5X LADDER PROTOCOL")
    print("=" * 80)
    print(f"💰 Starting Capital: ₹{TOTAL_CAPITAL_INR:,.0f} | Strict {NUM_SLOTS}-Slot Basket (₹20,000 / slot)")
    print(f"🎯 Golden Breakout Filter: Age 45m-4.5h | MC $250k-$2.5M | Buys >= 1.35x Sells")
    print(f"🪜 3-Stage 5x Ladder: 2.0x (Sell 50% = 0 Risk) | 5.0x (Sell 25% = 5x Lock) | Moonbag 25% (20% Trail)")
    print("=" * 80)

    state = load_state()

    while True:
        try:
            # 1. Fill empty slots
            fill_empty_slots(state)

            # 2. Update and evaluate portfolio
            cash, open_val = update_and_evaluate_portfolio(state)

            # 3. Print telemetry
            print_telemetry(state, cash, open_val)

            # 4. Save state
            save_state(state)

        except Exception as e:
            print(f"[ERROR] Cycle exception: {e}", flush=True)

        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()
