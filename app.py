#!/usr/bin/env python3
import http.server
import socketserver
import threading
import time
import os
import sys
import json

# Import the updated V9 Runner and V7 Gravitas engines
import engine_sniper_v9
import engine_v3_nochase_vps_fresh

PORT = int(os.environ.get("PORT", 5000))
DASHBOARD_FILE = "quantum_dashboard_snk.html"
STATE_FILE_V3 = "engine_v3_state.json"
STATE_FILE_SNIPER = "engine_sniper_state.json"
STATE_FILE_POLY = "engine_polymarket_state.json"

class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def do_HEAD(self):
        clean_path = self.path.split('?')[0]
        self.send_response(200)
        if clean_path in ["/", "/index.html", "/algo", "/algo/"] or clean_path.endswith(".html"):
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_GET(self):
        clean_path = self.path.split('?')[0]

        # 1. V7 Gravitas Engine State Endpoint
        if clean_path in ["/api/state", "/algo/api/state", "/api/dashboard", "/algo/api/dashboard"]:
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

        # 2. V9 Sniper Engine State Endpoint
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

        # 3. Polymarket State Endpoint
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
                    "status": "SCANNING",
                    "open_positions": [],
                    "closed_trades": [],
                    "bankroll": 50000,
                    "cash": 50000
                }).encode('utf-8'))
            return

        # 4. SNK Fleet Main Dashboard
        elif clean_path in ["/", "/index.html", "/algo", "/algo/"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            target_file = DASHBOARD_FILE if os.path.exists(DASHBOARD_FILE) else "quantum_dashboard.html"
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

def run_sniper_daemon():
    print("🚀 [DAEMON 1] Starting V9 Runner-Capture Engine (Moonbag & PumpPortal)...", flush=True)
    try:
        engine_sniper_v9.main()
    except Exception as e:
        print(f"V9 Sniper engine exception: {e}", file=sys.stderr, flush=True)

def run_gravitas_daemon():
    print("🚀 [DAEMON 2] Starting V7 Gravitas Protocol Engine (Dynamic SL & Anti-Chop)...", flush=True)
    try:
        engine_v3_nochase_vps_fresh.main()
    except Exception as e:
        print(f"V7 Gravitas engine exception: {e}", file=sys.stderr, flush=True)

if __name__ == "__main__":
    # Start both trading daemons concurrently
    t1 = threading.Thread(target=run_sniper_daemon, daemon=True)
    t1.start()

    t2 = threading.Thread(target=run_gravitas_daemon, daemon=True)
    t2.start()

    print(f"🚀 SNK Quant Fleet Server listening on port {PORT}", flush=True)
    with ThreadedTCPServer(("0.0.0.0", PORT), DashboardHandler) as httpd:
        httpd.serve_forever()
