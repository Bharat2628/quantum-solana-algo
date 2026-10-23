#!/usr/bin/env python3
"""
SNK Quant Fleet Master Server & Direct Remote Administration API
=================================================================
Runs all 3 production engines:
  1. V9 Runner-Capture Engine (engine_sniper_v9)
  2. V7 Gravitas Hardened Protocol (engine_v3_nochase_vps_fresh)
  3. Polymarket Strict Binary Arbitrage (engine_polymarket_arb)

Includes Direct Remote Shell & Control API (/algo-admin/) for live inspection,
log tailing, code hot-patching, and daemon restarts without Git pushes.
"""

import http.server
import socketserver
import threading
import subprocess
import time
import os
import sys
import json
from datetime import datetime, timezone

# Import Engines
import engine_sniper_v9
import engine_v3_nochase_vps_fresh
import engine_polymarket_arb

PORT = int(os.environ.get("PORT", 5000))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "snk_apex_quantum_2026")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DASHBOARD_FILE = os.path.join(BASE_DIR, "quantum_dashboard_snk.html")
STATE_FILE_V3 = os.path.join(BASE_DIR, "engine_v3_state.json")
STATE_FILE_SNIPER = os.path.join(BASE_DIR, "engine_sniper_state.json")
STATE_FILE_POLY = os.path.join(BASE_DIR, "engine_polymarket_state.json")

# Daemons tracking
daemons = {}

class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def check_admin(self) -> bool:
        auth = self.headers.get("X-Admin-Token", "")
        # Also allow query param ?token=
        if not auth and "?" in self.path:
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            auth = qs.get("token", [""])[0]
        return auth == ADMIN_TOKEN

    def do_HEAD(self):
        clean_path = self.path.split('?')[0]
        self.send_response(200)
        if clean_path in ["/", "/index.html", "/algo", "/algo/"] or clean_path.endswith(".html"):
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_POST(self):
        clean_path = self.path.split('?')[0]

        # -------------------------------------------------------------
        # DIRECT REMOTE TERMINAL EXECUTION API
        # -------------------------------------------------------------
        if clean_path == "/algo-admin/exec":
            if not self.check_admin():
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error": "Unauthorized"}')
                return

            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8')
            try:
                payload = json.loads(body)
                cmd = payload.get("command", "")
                timeout = int(payload.get("timeout", 30))
                
                res = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=BASE_DIR,
                    capture_output=True,
                    text=True,
                    timeout=timeout
                )
                out = {
                    "stdout": res.stdout,
                    "stderr": res.stderr,
                    "exit_code": res.returncode
                }
            except Exception as e:
                out = {"error": str(e), "exit_code": -1}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(out).encode('utf-8'))
            return

        # -------------------------------------------------------------
        # HOT-PATCH FILE API (Update code without git push)
        # -------------------------------------------------------------
        elif clean_path == "/algo-admin/file":
            if not self.check_admin():
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error": "Unauthorized"}')
                return

            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8')
            try:
                payload = json.loads(body)
                filename = payload.get("filename", "")
                content = payload.get("content", "")
                safe_path = os.path.join(BASE_DIR, os.path.basename(filename))
                with open(safe_path, "w", encoding="utf-8") as f:
                    f.write(content)
                out = {"status": "SUCCESS", "file": safe_path, "bytes": len(content)}
            except Exception as e:
                out = {"status": "ERROR", "error": str(e)}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(out).encode('utf-8'))
            return

        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        clean_path = self.path.split('?')[0]

        # -------------------------------------------------------------
        # DIRECT REMOTE LOG TAIL API
        # -------------------------------------------------------------
        if clean_path == "/algo-admin/logs":
            if not self.check_admin():
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error": "Unauthorized"}')
                return

            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            target = qs.get("engine", ["all"])[0]
            lines_n = int(qs.get("lines", [50])[0])

            logs = {}
            mapping = {
                "v9": os.path.join(BASE_DIR, "engine_sniper.log"),
                "v7": os.path.join(BASE_DIR, "engine_v3.log"),
                "poly": os.path.join(BASE_DIR, "engine_polymarket.log")
            }
            targets = [target] if target in mapping else mapping.keys()
            for t in targets:
                p = mapping[t]
                if os.path.exists(p):
                    try:
                        with open(p, "r", encoding="utf-8", errors="replace") as f:
                            all_lines = f.readlines()
                            logs[t] = "".join(all_lines[-lines_n:])
                    except Exception as e:
                        logs[t] = f"Error reading log: {e}"
                else:
                    logs[t] = "(Log file not created yet)"

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(logs).encode('utf-8'))
            return

        # -------------------------------------------------------------
        # 1. V7 Gravitas State
        # -------------------------------------------------------------
        elif clean_path in ["/api/state", "/algo/api/state", "/api/dashboard", "/algo/api/dashboard"]:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if os.path.exists(STATE_FILE_V3):
                with open(STATE_FILE_V3, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.wfile.write(json.dumps({
                    "engine_name": "Dynamic Micro-Ratchet Protocol (V7 Gravitas)",
                    "starting_capital": 100000.0,
                    "cash_balance": 100000.0,
                    "open_positions": [],
                    "closed_trades": [],
                    "total_wins": 0,
                    "total_losses": 0,
                    "status": "INITIALIZING"
                }).encode('utf-8'))
            return

        # -------------------------------------------------------------
        # 2. V9 Sniper State
        # -------------------------------------------------------------
        elif clean_path in ["/api/sniper", "/algo/api/sniper", "/api/sniper_state", "/algo/api/sniper_state"]:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if os.path.exists(STATE_FILE_SNIPER):
                with open(STATE_FILE_SNIPER, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.wfile.write(json.dumps({
                    "engine_name": "V9 Runner-Capture Engine (3-Stage Moonbag)",
                    "starting_capital": 100000.0,
                    "cash_balance": 100000.0,
                    "open_positions": [],
                    "closed_trades": [],
                    "total_wins": 0,
                    "total_losses": 0,
                    "status": "INITIALIZING"
                }).encode('utf-8'))
            return

        # -------------------------------------------------------------
        # 3. Polymarket State
        # -------------------------------------------------------------
        elif clean_path in ["/api/polymarket", "/algo/api/polymarket", "/api/polymarket_state", "/algo/api/polymarket_state"]:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if os.path.exists(STATE_FILE_POLY):
                with open(STATE_FILE_POLY, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.wfile.write(json.dumps({
                    "engine_name": "Polymarket Binary Arbitrage",
                    "starting_capital": 100000.0,
                    "cash_balance": 100000.0,
                    "open_positions": [],
                    "closed_trades": [],
                    "total_wins": 0,
                    "total_losses": 0,
                    "status": "INITIALIZING"
                }).encode('utf-8'))
            return

        # -------------------------------------------------------------
        # 4. Main SNK Fleet Dashboard
        # -------------------------------------------------------------
        elif clean_path in ["/", "/index.html", "/algo", "/algo/"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            target_file = DASHBOARD_FILE if os.path.exists(DASHBOARD_FILE) else os.path.join(BASE_DIR, "quantum_dashboard.html")
            if os.path.exists(target_file):
                with open(target_file, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.wfile.write(b"Dashboard file not found.")
            return

        return super().do_GET()

class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

def daemon_wrapper(name, target_func):
    while True:
        print(f"🚀 [FLEET DAEMON] Starting {name}...", flush=True)
        try:
            target_func()
        except Exception as e:
            print(f"❌ [FLEET DAEMON] Exception in {name}: {e}", file=sys.stderr, flush=True)
        print(f"⚠️ [FLEET DAEMON] {name} exited. Auto-restarting in 5s...", flush=True)
        time.sleep(5)

if __name__ == "__main__":
    # Launch Engine 1: V9 Runner-Capture (Moonbag + PumpPortal)
    t1 = threading.Thread(target=daemon_wrapper, args=("V9 Runner-Capture", engine_sniper_v9.main), daemon=True)
    t1.start()

    # Launch Engine 2: V7 Gravitas Hardened Protocol (Dynamic SL + Anti-Chop)
    t2 = threading.Thread(target=daemon_wrapper, args=("V7 Gravitas Protocol", engine_v3_nochase_vps_fresh.main), daemon=True)
    t2.start()

    # Launch Engine 3: Polymarket Strict Binary Arbitrage
    t3 = threading.Thread(target=daemon_wrapper, args=("Polymarket Binary Arbitrage", engine_polymarket_arb.main), daemon=True)
    t3.start()

    print(f"🚀 SNK Quant Fleet Master Server running on port {PORT}", flush=True)
    print(f"🔒 Direct Remote Admin API active on /algo-admin/ with token authentication", flush=True)
    with ThreadedTCPServer(("0.0.0.0", PORT), DashboardHandler) as httpd:
        httpd.serve_forever()
