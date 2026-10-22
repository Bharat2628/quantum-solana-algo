import http.server
import socketserver
import threading
import time
import os
import sys

# Import our engine runner
import engine_v4_nochase

PORT = int(os.environ.get("PORT", 5000))
DASHBOARD_FILE = "quantum_dashboard.html"
STATE_FILE = "engine_v3_state.json"

class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def do_HEAD(self):
        clean_path = self.path.split('?')[0]
        if clean_path in ["/api/state", "/algo-api/state", "/", "/index.html", "/algo", "/algo/"]:
            self.send_response(200)
            if clean_path.endswith(".html") or clean_path in ["/", "/algo", "/algo/"]:
                self.send_header("Content-Type", "text/html; charset=utf-8")
            else:
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return
        return super().do_HEAD()

    def do_GET(self):
        clean_path = self.path.split('?')[0]
        if clean_path in ["/api/state", "/algo-api/state"]:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.wfile.write(b"{}")
            return

        elif clean_path in ["/", "/index.html", "/algo", "/algo/"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if os.path.exists(DASHBOARD_FILE):
                with open(DASHBOARD_FILE, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.wfile.write(b"Dashboard file not found.")
            return

        return super().do_GET()

class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

def run_trading_daemon():
    print("🚀 Starting Quantum Trading Engine daemon thread...")
    try:
        engine_v4_nochase.main()
    except Exception as e:
        print(f"Engine daemon exception: {e}", file=sys.stderr)

if __name__ == "__main__":
    # Start engine in background thread
    t = threading.Thread(target=run_trading_daemon, daemon=True)
    t.start()

    print(f"🚀 Quantum Dashboard Web Server listening on port {PORT}")
    with ThreadedTCPServer(("0.0.0.0", PORT), DashboardHandler) as httpd:
        httpd.serve_forever()
