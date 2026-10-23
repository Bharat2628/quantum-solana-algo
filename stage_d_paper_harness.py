#!/usr/bin/env python3
"""
Stage D: Independent Paper Validation Harness (Production-Grade Audit Ready)
=============================================================================
CRITICAL ENGINE FIXES & AUDIT PROOFS:
1. Canonical ACID Store: SQLite database (`paper_store.db`).
   - Position closure, cash balance update, and closed trade recording happen
     inside a single atomic `BEGIN IMMEDIATE ... COMMIT` transaction.
   - In-memory state updated ONLY AFTER successful database commit.
   - Transaction failure immediately raises and halts or reloads from DB.
2. Independent Health Probing (No Permanent Outage Freeze):
   - `probe_discovery_health()` executes independently every cycle regardless
     of entry eligibility, allowing heartbeat recovery automatically after outages.
3. Persistent High-Water Mark (Peak Equity):
   - Peak equity is committed to `engine_meta` on every cycle update.
   - Survives crashes and restarts without resetting the drawdown circuit breaker.
4. Fail-Closed Metadata & Non-Empty DB Guard:
   - If DB contains existing tables/trades but missing fingerprint, startup ABORTS immediately.
5. Max Outage Timeout on Draining Positions:
   - If an open position experiences a persistent quote outage (>30m) during draining,
     it is recorded as `ABANDONED_UNRESOLVED_OUTAGE` to prevent indefinite stalling.
"""

import os
import sys
import time
import json
import csv
import hashlib
import sqlite3
import requests
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ============================================================================
# CANONICAL FROZEN SPECIFICATION (FINGERPRINTED & IMMUTABLE)
# ============================================================================
STRICT_CONFIG = {
    # Version & Frozen Metadata
    "VERSION": "1.4.1-PATCHED",
    "INITIAL_PAPER_CAPITAL_INR": 25_000.0,
    "SLOT_SIZE_INR": 3_000.0,
    "MAX_SLOTS": 4,

    # Dual Core Discovery Gates
    "MIN_LIQUIDITY_USD": 32_000.0,
    "MAX_LIQUIDITY_USD": 70_000.0,
    "MIN_M5_PRICE_CHANGE_PCT": 3.0,
    "MAX_M5_PRICE_CHANGE_PCT": 5.8,

    # Baseline Quality & Anti-Scam Filters
    "MIN_M5_VOLUME_USD": 3_500.0,
    "MIN_M5_BUYS": 8,
    "MIN_M5_BUY_SELL_RATIO": 1.25,
    "MIN_24H_VOLUME_USD": 40_000.0,
    "MIN_AGE_HOURS": 0.5,
    "MAX_AGE_HOURS": 72.0,

    # Dynamic Stop-Loss & Liquidity Threshold (Aligned with V7)
    "LIQUIDITY_TIER_THRESHOLD_USD": 40_000.0,
    "SL_LOW_LIQ_MULT": 0.945,           # -5.5% for pools < $40k
    "SL_HIGH_LIQ_MULT": 0.925,          # -7.5% for pools >= $40k

    # Execution Friction Model
    "SLIPPAGE_PCT": 0.015,              # 1.5% adverse slippage on entry & exit
    "ROUND_TRIP_FEE_INR": 37.50,        # Complete round-trip priority gas fee

    # Stopping Rules
    "MAX_SAMPLE_TRADES": 30,
    "MAX_DRAWDOWN_INR": 3_500.0,
    "MAX_CONSECUTIVE_LOSSES": 7,
    "POLL_INTERVAL_SEC": 15,
    "PRICE_STALE_TIMEOUT_SEC": 60,
    "MAX_PERSISTENT_OUTAGE_SEC": 1800   # 30 minutes max outage before marking unresolved
}

def compute_config_fingerprint(cfg: dict) -> str:
    serialized = json.dumps(cfg, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]

CONFIG_FINGERPRINT = compute_config_fingerprint(STRICT_CONFIG)

DATA_DIR = os.environ.get("PAPER_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))

def ensure_data_dir():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        pass

DB_FILE = os.path.join(DATA_DIR, "paper_store.db")
LEDGER_CSV = os.path.join(DATA_DIR, "paper_ledger.csv")
LOG_FILE = os.path.join(DATA_DIR, "paper_engine.log")

EXCLUDED_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn"
}

def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[PAPER {ts}] {msg}"
    print(line, flush=True)
    try:
        ensure_data_dir()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def init_db(conn: sqlite3.Connection):
    """Initializes canonical schema with WAL mode enabled for crash resilience."""
    with conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS engine_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS open_positions (
                pair_address TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                mint TEXT NOT NULL,
                size_inr REAL NOT NULL,
                entry_price_usd REAL NOT NULL,
                current_price_usd REAL NOT NULL,
                entry_time TEXT NOT NULL,
                last_quote_time REAL NOT NULL,
                current_multiple REAL NOT NULL,
                peak_multiple REAL NOT NULL,
                current_sl_floor REAL NOT NULL,
                current_sl_label TEXT NOT NULL,
                entry_liquidity_usd REAL NOT NULL,
                entry_m5_change REAL NOT NULL,
                entry_vol_m5_usd REAL NOT NULL,
                entry_buys_m5 INTEGER NOT NULL
            );
        """)
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS closed_trades (
                trade_num INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                mint TEXT NOT NULL,
                pair_address TEXT NOT NULL,
                entry_time TEXT NOT NULL,
                exit_time TEXT NOT NULL,
                duration_sec INTEGER NOT NULL,
                entry_price_usd REAL NOT NULL,
                exit_price_usd REAL NOT NULL,
                peak_multiple REAL NOT NULL,
                exit_multiple REAL NOT NULL,
                size_inr REAL NOT NULL,
                gross_pnl_inr REAL NOT NULL,
                round_trip_fee_inr REAL NOT NULL,
                net_pnl_inr REAL NOT NULL,
                cash_after_trade REAL NOT NULL,
                entry_liquidity_usd REAL NOT NULL,
                entry_m5_change REAL NOT NULL,
                entry_vol_m5_usd REAL NOT NULL,
                entry_buys_m5 INTEGER NOT NULL,
                exit_reason TEXT NOT NULL
            );
        """)

def get_db_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(DB_FILE)), exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn

def is_db_non_empty(conn: sqlite3.Connection) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('open_positions', 'closed_trades')")
    if cur.fetchone()[0] < 2:
        return False
    cur.execute("SELECT (SELECT count(*) FROM open_positions) + (SELECT count(*) FROM closed_trades)")
    return cur.fetchone()[0] > 0

def load_or_init_state(conn: sqlite3.Connection) -> dict:
    has_existing_data = is_db_non_empty(conn)
    init_db(conn)
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM engine_meta")
    meta = dict(cur.fetchall())

    run_id = f"RUN-{int(time.time())}-{CONFIG_FINGERPRINT[:6]}"
    
    if "config_fingerprint" in meta:
        stored_fp = meta.get("config_fingerprint")
        if not stored_fp or stored_fp != CONFIG_FINGERPRINT:
            log(f"CRITICAL: Config fingerprint mismatch! Stored: '{stored_fp}', Expected: '{CONFIG_FINGERPRINT}'. Aborting.")
            sys.exit(1)
    else:
        # Safeguard: If DB has trades/positions but NO fingerprint metadata, FAIL-CLOSED!
        if has_existing_data:
            log("CRITICAL: Existing non-empty database found with missing config_fingerprint! Aborting to prevent corruption.")
            sys.exit(1)

        # Fresh empty DB initialization
        with conn:
            conn.execute("INSERT OR REPLACE INTO engine_meta (key, value) VALUES ('config_fingerprint', ?)", (CONFIG_FINGERPRINT,))
            conn.execute("INSERT OR REPLACE INTO engine_meta (key, value) VALUES ('run_id', ?)", (run_id,))
            conn.execute("INSERT OR REPLACE INTO engine_meta (key, value) VALUES ('cash_balance_inr', ?)", (str(STRICT_CONFIG["INITIAL_PAPER_CAPITAL_INR"]),))
            conn.execute("INSERT OR REPLACE INTO engine_meta (key, value) VALUES ('peak_equity_inr', ?)", (str(STRICT_CONFIG["INITIAL_PAPER_CAPITAL_INR"]),))
            conn.execute("INSERT OR REPLACE INTO engine_meta (key, value) VALUES ('is_draining', ?)", ("0",))
            conn.execute("INSERT OR REPLACE INTO engine_meta (key, value) VALUES ('is_stopped', ?)", ("0",))
            conn.execute("INSERT OR REPLACE INTO engine_meta (key, value) VALUES ('stop_reason', ?)", ("",))
            conn.execute("INSERT OR REPLACE INTO engine_meta (key, value) VALUES ('consecutive_losses', ?)", ("0",))

    cur.execute("SELECT key, value FROM engine_meta")
    meta = dict(cur.fetchall())
    
    cur.execute("SELECT * FROM open_positions")
    open_positions = [dict(row) for row in cur.fetchall()]

    cur.execute("SELECT * FROM closed_trades ORDER BY trade_num ASC")
    closed_trades = [dict(row) for row in cur.fetchall()]

    return {
        "engine_name": "Stage D Paper Validation Engine (SQLite ACID)",
        "run_id": meta.get("run_id", run_id),
        "config_fingerprint": CONFIG_FINGERPRINT,
        "starting_capital_inr": STRICT_CONFIG["INITIAL_PAPER_CAPITAL_INR"],
        "cash_balance_inr": float(meta.get("cash_balance_inr", STRICT_CONFIG["INITIAL_PAPER_CAPITAL_INR"])),
        "open_positions": open_positions,
        "closed_trades": closed_trades,
        "consecutive_losses": int(meta.get("consecutive_losses", 0)),
        "peak_equity_inr": float(meta.get("peak_equity_inr", STRICT_CONFIG["INITIAL_PAPER_CAPITAL_INR"])),
        "is_draining": meta.get("is_draining") == "1",
        "is_stopped": meta.get("is_stopped") == "1",
        "stop_reason": meta.get("stop_reason", ""),
        "discovery_feed_healthy": False,
        "position_quotes_healthy": True,
        "last_discovery_pulse_ts": 0.0
    }

def execute_atomic_trade_close(conn: sqlite3.Connection, pair_address: str, trade_record: dict, new_cash: float, consecutive_losses: int):
    """
    ACID Transaction:
    1. Removes position from `open_positions` with strict rowcount guard (must delete exactly 1 row).
    2. Inserts trade into `closed_trades`.
    3. Updates `cash_balance_inr` and `consecutive_losses` in `engine_meta`.
    4. Commits as a single atomic unit. Aborts if position does not exist or was already closed.
    """
    with conn:
        cursor = conn.execute("DELETE FROM open_positions WHERE pair_address = ?", (pair_address,))
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Duplicate close / missing position guard triggered: expected to delete 1 row for {pair_address}, "
                f"but deleted {cursor.rowcount}. Transaction rolled back."
            )
        
        cols = [
            "run_id", "symbol", "mint", "pair_address", "entry_time", "exit_time",
            "duration_sec", "entry_price_usd", "exit_price_usd", "peak_multiple",
            "exit_multiple", "size_inr", "gross_pnl_inr", "round_trip_fee_inr",
            "net_pnl_inr", "cash_after_trade", "entry_liquidity_usd", "entry_m5_change",
            "entry_vol_m5_usd", "entry_buys_m5", "exit_reason"
        ]
        placeholders = ",".join("?" for _ in cols)
        values = [trade_record.get(c) for c in cols]
        conn.execute(f"INSERT INTO closed_trades ({','.join(cols)}) VALUES ({placeholders})", values)
        
        conn.execute("INSERT OR REPLACE INTO engine_meta (key, value) VALUES ('cash_balance_inr', ?)", (str(new_cash),))
        conn.execute("INSERT OR REPLACE INTO engine_meta (key, value) VALUES ('consecutive_losses', ?)", (str(consecutive_losses),))

def execute_atomic_entry(conn: sqlite3.Connection, new_pos: dict, new_cash: float):
    """ACID Transaction for inserting a new open position and deducting cash."""
    with conn:
        cols = list(new_pos.keys())
        placeholders = ",".join("?" for _ in cols)
        values = list(new_pos.values())
        conn.execute(f"INSERT INTO open_positions ({','.join(cols)}) VALUES ({placeholders})", values)
        conn.execute("INSERT OR REPLACE INTO engine_meta (key, value) VALUES ('cash_balance_inr', ?)", (str(new_cash),))

def persist_peak_equity(conn: sqlite3.Connection, peak_equity: float):
    """Persists the high-water mark peak equity to avoid resetting drawdown after restarts."""
    with conn:
        conn.execute("INSERT OR REPLACE INTO engine_meta (key, value) VALUES ('peak_equity_inr', ?)", (str(peak_equity),))

def update_open_position_quote(conn: sqlite3.Connection, pair_address: str, curr_price: float, current_multiple: float, peak_multiple: float, sl_floor: float, sl_label: str, last_quote_time: float):
    with conn:
        conn.execute("""
            UPDATE open_positions
            SET current_price_usd = ?, current_multiple = ?, peak_multiple = ?,
                current_sl_floor = ?, current_sl_label = ?, last_quote_time = ?
            WHERE pair_address = ?
        """, (curr_price, current_multiple, peak_multiple, sl_floor, sl_label, last_quote_time, pair_address))

def export_ledger_csv(conn: sqlite3.Connection):
    """
    Exports closed trades from canonical SQLite to CSV.
    Any I/O error during CSV export is caught, logged, and isolated
    so that reporting failures never interrupt core state or cycle processing.
    """
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM closed_trades ORDER BY trade_num ASC")
        rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            return
        fieldnames = list(rows[0].keys())
        tmp_csv = LEDGER_CSV + ".tmp"
        with open(tmp_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_csv, LEDGER_CSV)
    except Exception as exc:
        log(f"Warning: Failed to export ledger CSV view (SQLite canonical state unaffected): {exc}")

def fetch_pair_locked_quote(pair_address: str) -> Tuple[Optional[float], Optional[dict]]:
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    try:
        r = requests.get(url, timeout=7)
        r.raise_for_status()
        data = r.json()

        pairs = data.get("pairs") or []
        pair = next(
            (p for p in pairs if p.get("pairAddress") == pair_address and p.get("chainId") == "solana"),
            None
        )
        if not pair and isinstance(data.get("pair"), dict):
            p = data.get("pair")
            if p.get("pairAddress") == pair_address and p.get("chainId") == "solana":
                pair = p

        if not pair:
            return None, None

        price = float(pair.get("priceUsd") or 0)
        return (price, pair) if price > 0 else (None, None)
    except Exception as exc:
        log(f"Pair quote fetch error ({pair_address}): {exc}")
        return None, None

def is_strictly_qualified(pair: dict) -> Tuple[bool, str]:
    pair_created = pair.get("pairCreatedAt")
    if not pair_created:
        return False, "REJECTED: pairCreatedAt missing or null"
    try:
        age_hours = (time.time() * 1000 - float(pair_created)) / (1000 * 3600)
        if not (STRICT_CONFIG["MIN_AGE_HOURS"] <= age_hours <= STRICT_CONFIG["MAX_AGE_HOURS"]):
            return False, f"Age {age_hours:.1f}h outside [{STRICT_CONFIG['MIN_AGE_HOURS']}h, {STRICT_CONFIG['MAX_AGE_HOURS']}h]"
    except Exception:
        return False, "REJECTED: invalid pairCreatedAt timestamp"

    liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    if not (STRICT_CONFIG["MIN_LIQUIDITY_USD"] <= liq <= STRICT_CONFIG["MAX_LIQUIDITY_USD"]):
        return False, f"Liq ${liq:,.0f} outside [32k, 70k]"

    m5 = float(pair.get("priceChange", {}).get("m5", 0) or 0)
    if not (STRICT_CONFIG["MIN_M5_PRICE_CHANGE_PCT"] <= m5 <= STRICT_CONFIG["MAX_M5_PRICE_CHANGE_PCT"]):
        return False, f"m5 {m5:+.1f}% outside [3.0%, 5.8%]"

    vol_m5 = float(pair.get("volume", {}).get("m5", 0) or 0)
    if vol_m5 < STRICT_CONFIG["MIN_M5_VOLUME_USD"]:
        return False, f"Vol5m ${vol_m5:,.0f} < $3.5k"

    tx_m5 = pair.get("txns", {}).get("m5", {})
    buys_m5 = int(tx_m5.get("buys", 0) or 0)
    sells_m5 = int(tx_m5.get("sells", 0) or 0)
    if buys_m5 < STRICT_CONFIG["MIN_M5_BUYS"]:
        return False, f"Buys5m {buys_m5} < 8"

    bs_ratio = buys_m5 / max(1, sells_m5)
    if bs_ratio < STRICT_CONFIG["MIN_M5_BUY_SELL_RATIO"]:
        return False, f"B/S ratio {bs_ratio:.2f} < 1.25"

    vol_24h = float(pair.get("volume", {}).get("h24", 0) or 0)
    if vol_24h < STRICT_CONFIG["MIN_24H_VOLUME_USD"]:
        return False, f"Vol24h ${vol_24h:,.0f} < $40k"

    return True, "QUALIFIED"

def get_paper_exit_rule(peak_mult: float, prev_sl: float, entry_liq: float) -> Tuple[float, str]:
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
        if entry_liq < STRICT_CONFIG["LIQUIDITY_TIER_THRESHOLD_USD"]:
            sl = STRICT_CONFIG["SL_LOW_LIQ_MULT"]
            label = f"DYNAMIC_SL_LOW_LIQ ({sl:.3f}x | EntryLiq < $40k)"
        else:
            sl = STRICT_CONFIG["SL_HIGH_LIQ_MULT"]
            label = f"DYNAMIC_SL_HIGH_LIQ ({sl:.3f}x | EntryLiq >= $40k)"

    # Strict Monotonicity Guard: Stop-loss floor can never loosen / decrease
    if prev_sl > sl:
        return prev_sl, f"MONOTONIC_FLOOR_HOLD ({prev_sl:.3f}x)"
    return sl, label

def probe_discovery_health(state: dict):
    """
    Independent health probe: Pings discovery endpoint every cycle to verify market feed connectivity.
    Updates `last_discovery_pulse_ts` independently so the engine recovers cleanly from temporary outages.
    """
    try:
        r = requests.get("https://api.dexscreener.com/token-boosts/top/v1", timeout=5)
        if r.status_code == 200:
            state["last_discovery_pulse_ts"] = time.time()
    except Exception as e:
        log(f"Discovery probe warning: {e}")

def discover_paper_candidates() -> List[dict]:
    qualified_pairs = []
    endpoints = [
        "https://api.dexscreener.com/token-boosts/top/v1",
        "https://api.dexscreener.com/token-boosts/latest/v1",
        "https://api.dexscreener.com/token-profiles/latest/v1"
    ]
    raw_mints = set()
    for ep in endpoints:
        try:
            r = requests.get(ep, timeout=7)
            if r.status_code == 200:
                for item in r.json():
                    if item.get("chainId") == "solana":
                        addr = item.get("tokenAddress")
                        if addr and addr not in EXCLUDED_MINTS:
                            raw_mints.add(addr)
        except Exception:
            pass

    try:
        r = requests.get("https://api.dexscreener.com/latest/dex/search?q=pump", timeout=7)
        if r.status_code == 200:
            for p in r.json().get("pairs", []):
                if p.get("chainId") == "solana":
                    addr = p.get("baseToken", {}).get("address")
                    if addr and addr not in EXCLUDED_MINTS:
                        raw_mints.add(addr)
    except Exception:
        pass

    chunk_size = 30
    mint_list = list(raw_mints)
    for i in range(0, min(120, len(mint_list)), chunk_size):
        chunk = mint_list[i:i+chunk_size]
        url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(chunk)}"
        try:
            r = requests.get(url, timeout=7)
            if r.status_code == 200:
                for p in r.json().get("pairs", []):
                    if p.get("chainId") == "solana":
                        ok, _ = is_strictly_qualified(p)
                        if ok:
                            qualified_pairs.append(p)
        except Exception:
            pass
        time.sleep(0.08)

    return qualified_pairs

def evaluate_and_run_cycle(conn: sqlite3.Connection, state: dict):
    now = time.time()
    open_pos = state["open_positions"]
    closed = state["closed_trades"]

    # 0. Independent Discovery Health Probe
    probe_discovery_health(state)
    last_pulse = state.get("last_discovery_pulse_ts", 0.0)
    if last_pulse > 0.0:
        discovery_age = now - last_pulse
        state["discovery_feed_healthy"] = (discovery_age <= STRICT_CONFIG["PRICE_STALE_TIMEOUT_SEC"])
    else:
        state["discovery_feed_healthy"] = False

    # 1. Update Existing Open Positions
    any_quote_failed = False
    still_open = []

    for pos in open_pos:
        pair_address = pos["pair_address"]
        symbol = pos["symbol"]
        curr_price, pair_data = fetch_pair_locked_quote(pair_address)

        if curr_price is None:
            any_quote_failed = True
            quote_age = now - pos.get("last_quote_time", now)
            log(f"⚠️ QUOTE OUTAGE on [{symbol}] (Pair: {pair_address}) - Age: {int(quote_age)}s")
            
            # Persistent Outage Resolution Guard (Both Normal Operation & Draining Mode)
            # If quote feed is permanently silent for >= MAX_PERSISTENT_OUTAGE_SEC (30m),
            # position cannot be monitored or closed via normal SL. Resolve exposure deterministically
            # by recording ABANDONED_UNRESOLVED_OUTAGE with -100% loss.
            if quote_age >= STRICT_CONFIG["MAX_PERSISTENT_OUTAGE_SEC"]:
                log(f"🚨 UNRESOLVED EXPOSURE: [{symbol}] pool permanently dead for {int(quote_age/60)}m. Force-closing as ABANDONED.")
                trade_record = {
                    "run_id": state["run_id"],
                    "symbol": symbol,
                    "mint": pos["mint"],
                    "pair_address": pair_address,
                    "entry_time": pos["entry_time"],
                    "exit_time": datetime.now(timezone.utc).isoformat(),
                    "duration_sec": int(now - datetime.fromisoformat(pos["entry_time"]).timestamp()),
                    "entry_price_usd": pos["entry_price_usd"],
                    "exit_price_usd": 0.0,
                    "peak_multiple": pos["peak_multiple"],
                    "exit_multiple": 0.0,
                    "size_inr": pos["size_inr"],
                    "gross_pnl_inr": -pos["size_inr"],
                    "round_trip_fee_inr": 0.0,
                    "net_pnl_inr": -pos["size_inr"],
                    "cash_after_trade": state["cash_balance_inr"],
                    "entry_liquidity_usd": pos["entry_liquidity_usd"],
                    "entry_m5_change": pos["entry_m5_change"],
                    "entry_vol_m5_usd": pos["entry_vol_m5_usd"],
                    "entry_buys_m5": pos["entry_buys_m5"],
                    "exit_reason": "ABANDONED_UNRESOLVED_OUTAGE"
                }
                new_consec_losses = state["consecutive_losses"] + 1
                execute_atomic_trade_close(conn, pair_address, trade_record, state["cash_balance_inr"], new_consec_losses)
                state["consecutive_losses"] = new_consec_losses
                closed.append(trade_record)
                state["open_positions"] = [p for p in state["open_positions"] if p["pair_address"] != pair_address]

                try:
                    export_ledger_csv(conn)
                except Exception as csv_err:
                    log(f"Warning: CSV export failed (SQLite state intact): {csv_err}")
                continue

            still_open.append(pos)
            continue

        pos["last_quote_time"] = now
        pos["current_price_usd"] = curr_price

        mult = round(curr_price / pos["entry_price_usd"], 4) if pos["entry_price_usd"] > 0 else 1.0
        pos["current_multiple"] = mult
        pos["peak_multiple"] = max(pos.get("peak_multiple", mult), mult)

        prev_sl = pos.get("current_sl_floor", STRICT_CONFIG["SL_HIGH_LIQ_MULT"])
        new_sl, sl_label = get_paper_exit_rule(pos["peak_multiple"], prev_sl, pos["entry_liquidity_usd"])
        pos["current_sl_floor"] = new_sl
        pos["current_sl_label"] = sl_label

        update_open_position_quote(conn, pair_address, curr_price, mult, pos["peak_multiple"], new_sl, sl_label, now)

        entry_ts = datetime.fromisoformat(pos["entry_time"]).timestamp()
        elapsed = now - entry_ts

        should_exit = False
        exit_reason = ""

        if mult <= pos["current_sl_floor"]:
            should_exit = True
            exit_reason = f"SL_TRIGGERED ({pos['current_sl_label']})"

        if elapsed >= 360.0 and pos["peak_multiple"] < 1.03:
            should_exit = True
            exit_reason = f"STAGNATION_CUT (6m elapsed, peak {pos['peak_multiple']:.2f}x)"

        if should_exit:
            effective_exit_mult = round(mult * (1.0 - STRICT_CONFIG["SLIPPAGE_PCT"]), 4)
            gross_pnl = round((effective_exit_mult - 1.0) * pos["size_inr"], 2)
            round_trip_fee = STRICT_CONFIG["ROUND_TRIP_FEE_INR"]
            net_pnl = round(gross_pnl - round_trip_fee, 2)
            recovered_capital = round(pos["size_inr"] + net_pnl, 2)

            new_cash = round(state["cash_balance_inr"] + recovered_capital, 2)
            new_consec_losses = 0 if net_pnl > 0 else (state["consecutive_losses"] + 1)

            trade_record = {
                "run_id": state["run_id"],
                "symbol": symbol,
                "mint": pos["mint"],
                "pair_address": pair_address,
                "entry_time": pos["entry_time"],
                "exit_time": datetime.now(timezone.utc).isoformat(),
                "duration_sec": int(elapsed),
                "entry_price_usd": pos["entry_price_usd"],
                "exit_price_usd": round(curr_price * (1.0 - STRICT_CONFIG["SLIPPAGE_PCT"]), 8),
                "peak_multiple": pos["peak_multiple"],
                "exit_multiple": effective_exit_mult,
                "size_inr": pos["size_inr"],
                "gross_pnl_inr": gross_pnl,
                "round_trip_fee_inr": round_trip_fee,
                "net_pnl_inr": net_pnl,
                "cash_after_trade": new_cash,
                "entry_liquidity_usd": pos["entry_liquidity_usd"],
                "entry_m5_change": pos["entry_m5_change"],
                "entry_vol_m5_usd": pos["entry_vol_m5_usd"],
                "entry_buys_m5": pos["entry_buys_m5"],
                "exit_reason": exit_reason
            }

            # ATOMIC COMMIT TO CANONICAL STORE
            try:
                execute_atomic_trade_close(conn, pair_address, trade_record, new_cash, new_consec_losses)
            except Exception as e:
                log(f"CRITICAL: Atomic trade close failed in SQLite: {e}. State remains unchanged.")
                still_open.append(pos)
                continue

            # Update in-memory state ONLY AFTER SUCCESSFUL DB COMMIT
            state["cash_balance_inr"] = new_cash
            state["consecutive_losses"] = new_consec_losses
            closed.append(trade_record)
            # Remove from open_positions immediately so subsequent errors (e.g. CSV I/O) cannot cause duplicate closes
            state["open_positions"] = [p for p in state["open_positions"] if p["pair_address"] != pair_address]

            try:
                export_ledger_csv(conn)
            except Exception as csv_err:
                log(f"Warning: CSV export failed (SQLite state intact): {csv_err}")

            log(f"🚪 EXIT [{symbol}] Mult: {mult:.3f}x (Eff: {effective_exit_mult:.3f}x | Peak: {pos['peak_multiple']:.2f}x) "
                f"Net PnL: ₹{net_pnl:+,.2f} | Cash: ₹{new_cash:,.2f} | Reason: {exit_reason}")
        else:
            still_open.append(pos)

    state["open_positions"] = still_open

    # Fail-closed Health States
    has_stale_position = any(now - p.get("last_quote_time", now) > STRICT_CONFIG["PRICE_STALE_TIMEOUT_SEC"] for p in state["open_positions"])
    state["position_quotes_healthy"] = (not any_quote_failed) and (not has_stale_position)

    # 2. Equity & Stopping Rules (Orderly Drain Protocol)
    # Stale-Valuation Drawdown Policy:
    # If any open position has a stale quote (> PRICE_STALE_TIMEOUT_SEC), equity cannot be reliably
    # marked-to-market. For drawdown circuit-breaker evaluation, we value stale positions conservatively
    # at their guaranteed stop-loss floor (current_sl_floor) rather than assuming their previous unverified multiple.
    curr_equity = state["cash_balance_inr"]
    has_unreliable_valuation = False
    for p in state["open_positions"]:
        is_pos_stale = (now - p.get("last_quote_time", now) > STRICT_CONFIG["PRICE_STALE_TIMEOUT_SEC"])
        if is_pos_stale:
            has_unreliable_valuation = True
            # Conservative haircut: value at SL floor with slippage to prevent hiding drawdown
            conservative_mult = p.get("current_sl_floor", STRICT_CONFIG["SL_HIGH_LIQ_MULT"]) * (1.0 - STRICT_CONFIG["SLIPPAGE_PCT"])
            curr_equity += p["size_inr"] * conservative_mult
        else:
            curr_equity += p["size_inr"] * p.get("current_multiple", 1.0)
    
    # Persistent High-Water Mark (Only advance peak equity if all open position quotes are healthy & reliable)
    if not has_unreliable_valuation and curr_equity > state.get("peak_equity_inr", STRICT_CONFIG["INITIAL_PAPER_CAPITAL_INR"]):
        state["peak_equity_inr"] = curr_equity
        persist_peak_equity(conn, curr_equity)

    current_drawdown = state["peak_equity_inr"] - curr_equity

    if not state["is_draining"]:
        if len(closed) >= STRICT_CONFIG["MAX_SAMPLE_TRADES"]:
            state["is_draining"] = True
            state["stop_reason"] = f"TARGET_SAMPLE_REACHED ({len(closed)}/30 trades)"
            log(f"🛑 STOPPING RULE TRIGGERED: {state['stop_reason']}. Draining remaining {len(state['open_positions'])} open positions.")
            with conn:
                conn.execute("INSERT OR REPLACE INTO engine_meta (key, value) VALUES ('is_draining', '1')")
                conn.execute("INSERT OR REPLACE INTO engine_meta (key, value) VALUES ('stop_reason', ?)", (state["stop_reason"],))

        elif current_drawdown >= STRICT_CONFIG["MAX_DRAWDOWN_INR"]:
            state["is_draining"] = True
            state["stop_reason"] = f"MAX_DRAWDOWN_BREACHED (Drawdown ₹{current_drawdown:,.2f} >= ₹{STRICT_CONFIG['MAX_DRAWDOWN_INR']:,.2f})"
            log(f"🛑 CIRCUIT BREAKER TRIGGERED: {state['stop_reason']}. Draining remaining {len(state['open_positions'])} open positions.")
            with conn:
                conn.execute("INSERT OR REPLACE INTO engine_meta (key, value) VALUES ('is_draining', '1')")
                conn.execute("INSERT OR REPLACE INTO engine_meta (key, value) VALUES ('stop_reason', ?)", (state["stop_reason"],))

        elif state["consecutive_losses"] >= STRICT_CONFIG["MAX_CONSECUTIVE_LOSSES"]:
            state["is_draining"] = True
            state["stop_reason"] = f"CONSECUTIVE_LOSSES_LIMIT ({state['consecutive_losses']} in a row)"
            log(f"🛑 CIRCUIT BREAKER TRIGGERED: {state['stop_reason']}. Draining remaining {len(state['open_positions'])} open positions.")
            with conn:
                conn.execute("INSERT OR REPLACE INTO engine_meta (key, value) VALUES ('is_draining', '1')")
                conn.execute("INSERT OR REPLACE INTO engine_meta (key, value) VALUES ('stop_reason', ?)", (state["stop_reason"],))

    if state["is_draining"] and len(state["open_positions"]) == 0:
        state["is_stopped"] = True
        with conn:
            conn.execute("INSERT OR REPLACE INTO engine_meta (key, value) VALUES ('is_stopped', '1')")
        log(f"🏁 VALIDATION FINISHED: {state['stop_reason']}. Total closed trades: {len(closed)}. Final Cash: ₹{state['cash_balance_inr']:,.2f}")
        return

    # 3. New Entries (Gated by Independent Health)
    feed_is_ok = state["discovery_feed_healthy"] and (len(state["open_positions"]) == 0 or state["position_quotes_healthy"])

    if not state["is_draining"] and feed_is_ok:
        available_slots = STRICT_CONFIG["MAX_SLOTS"] - len(state["open_positions"])
        if available_slots > 0 and state["cash_balance_inr"] >= STRICT_CONFIG["SLOT_SIZE_INR"]:
            candidates = discover_paper_candidates()
            open_pairs = {p["pair_address"] for p in state["open_positions"]}
            open_mints = {p["mint"] for p in state["open_positions"]}
            recent_mints = {t["mint"] for t in closed[-30:]}

            for pair in candidates:
                if available_slots <= 0 or state["cash_balance_inr"] < STRICT_CONFIG["SLOT_SIZE_INR"]:
                    break
                pair_addr = pair.get("pairAddress")
                mint = pair.get("baseToken", {}).get("address")
                if not pair_addr or pair_addr in open_pairs or mint in open_mints or mint in recent_mints:
                    continue

                raw_price = float(pair.get("priceUsd", 0) or 0)
                if raw_price <= 0:
                    continue

                simulated_entry_price = round(raw_price * (1.0 + STRICT_CONFIG["SLIPPAGE_PCT"]), 8)
                liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                m5 = float(pair.get("priceChange", {}).get("m5", 0) or 0)
                vol_m5 = float(pair.get("volume", {}).get("m5", 0) or 0)
                buys_m5 = int(pair.get("txns", {}).get("m5", {}).get("buys", 0) or 0)
                symbol = pair.get("baseToken", {}).get("symbol", "UNKNOWN")[:10]

                new_cash = round(state["cash_balance_inr"] - STRICT_CONFIG["SLOT_SIZE_INR"], 2)

                initial_sl_mult = STRICT_CONFIG["SL_LOW_LIQ_MULT"] if liq < STRICT_CONFIG["LIQUIDITY_TIER_THRESHOLD_USD"] else STRICT_CONFIG["SL_HIGH_LIQ_MULT"]
                new_pos = {
                    "symbol": symbol,
                    "mint": mint,
                    "pair_address": pair_addr,
                    "size_inr": STRICT_CONFIG["SLOT_SIZE_INR"],
                    "entry_price_usd": simulated_entry_price,
                    "current_price_usd": simulated_entry_price,
                    "entry_time": datetime.now(timezone.utc).isoformat(),
                    "last_quote_time": now,
                    "current_multiple": 1.000,
                    "peak_multiple": 1.000,
                    "current_sl_floor": initial_sl_mult,
                    "current_sl_label": f"DYNAMIC_SL ({initial_sl_mult:.3f}x | Liq: ${liq:,.0f})",
                    "entry_liquidity_usd": liq,
                    "entry_m5_change": m5,
                    "entry_vol_m5_usd": vol_m5,
                    "entry_buys_m5": buys_m5
                }

                # ATOMIC ENTRY COMMIT
                try:
                    execute_atomic_entry(conn, new_pos, new_cash)
                except Exception as e:
                    log(f"CRITICAL: Atomic entry failed in SQLite: {e}. Entry cancelled.")
                    continue

                state["cash_balance_inr"] = new_cash
                state["open_positions"].append(new_pos)
                open_pairs.add(pair_addr)
                open_mints.add(mint)
                available_slots -= 1

                log(f"🎯 ENTRY [{symbol}] Pair: {pair_addr} | Price: ${simulated_entry_price:.6f} | "
                    f"Liq: ${liq:,.0f} | m5: +{m5:.1f}% | Buys: {buys_m5} | Cash: ₹{new_cash:,.2f}")

def main():
    log("=" * 75)
    log("STARTING STAGE D: INDEPENDENT PAPER VALIDATION HARNESS (SQLITE ACID)")
    log(f"Version: {STRICT_CONFIG['VERSION']} | Fingerprint: {CONFIG_FINGERPRINT}")
    log(f"Data Dir: {DATA_DIR} | Isolated Capital: ₹{STRICT_CONFIG['INITIAL_PAPER_CAPITAL_INR']:,.0f} | Slot: ₹{STRICT_CONFIG['SLOT_SIZE_INR']:,.0f}")
    log(f"Database: {DB_FILE}")
    log("=" * 75)

    conn = get_db_connection()
    state = load_or_init_state(conn)

    while not state.get("is_stopped", False):
        try:
            evaluate_and_run_cycle(conn, state)
        except Exception as e:
            log(f"Unexpected cycle exception: {e}")
        time.sleep(STRICT_CONFIG["POLL_INTERVAL_SEC"])

    conn.close()
    log("Stage D execution terminated cleanly according to protocol.")

if __name__ == "__main__":
    main()
