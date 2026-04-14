#!/usr/bin/env python3
"""
Test server to check how git clone displays different error responses.
Runs two tests sequentially on port 9999.
"""

import http.server
import threading
import subprocess
import tempfile
import os


def pkt_line(s: str) -> bytes:
    data = s.encode()
    length = len(data) + 4
    return f"{length:04x}".encode() + data


def run_git_clone(url: str, label: str):
    with tempfile.TemporaryDirectory() as tmpdir:
        result = subprocess.run(
            ["git", "clone", url, tmpdir + "/repo"],
            capture_output=True,
            text=True,
        )
        print(f"\n=== {label} ===")
        print(f"exit code: {result.returncode}")
        if result.stdout:
            print(f"stdout: {result.stdout.strip()}")
        if result.stderr:
            print(f"stderr: {result.stderr.strip()}")


class Test1Handler(http.server.BaseHTTPRequestHandler):
    """Test 1: plain HTTP 503"""

    def do_GET(self):
        self.send_response(503)
        self.end_headers()

    def log_message(self, format, *args):
        pass


class Test2Handler(http.server.BaseHTTPRequestHandler):
    """Test 2: git pkt-line ERR message"""

    def do_GET(self):
        body = (
            pkt_line("# service=git-upload-pack\n")
            + b"0000"
            + pkt_line("ERR mirror is syncing, retry in a few minutes")
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/x-git-upload-pack-advertisement")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def serve_once(handler_class, port: int, label: str):
    server = http.server.HTTPServer(("127.0.0.1", port), handler_class)
    t = threading.Thread(target=server.handle_request)
    t.start()
    run_git_clone(f"http://127.0.0.1:{port}/test/repo", label)
    t.join()
    server.server_close()


if __name__ == "__main__":
    serve_once(Test1Handler, 9998, "HTTP 503 (plain)")
    serve_once(Test2Handler, 9999, "git pkt-line ERR")
