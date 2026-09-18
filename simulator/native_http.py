"""Container-side loopback HTTP bridge. Transport is a private file mailbox."""

import base64
import json
import math
import select
import socket
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path("/mailbox")
REQUEST_DEADLINE_SECONDS = 175
RESPONSE_GRACE_SECONDS = 2


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def client_gone(self):
        readable, _, _ = select.select([self.connection], [], [], 0)
        if not readable:
            return False
        try:
            return self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
        except BlockingIOError:
            return False
        except OSError:
            return True

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if not 0 < length <= 16 * 1024 * 1024:
            self.send_error(413)
            return
        identifier = uuid.uuid4().hex
        request = ROOT / (identifier + ".request")
        temporary = ROOT / (identifier + ".tmp")
        requested = self.headers.get("X-Request-Timeout")
        try:
            duration = (
                min(REQUEST_DEADLINE_SECONDS, float(requested))
                if requested
                else REQUEST_DEADLINE_SECONDS
            )
        except ValueError:
            self.send_error(400)
            return
        if not math.isfinite(duration) or duration <= 0:
            self.send_error(400)
            return
        deadline = time.time() + duration
        temporary.write_text(
            json.dumps(
                {
                    "path": self.path,
                    "body": base64.b64encode(self.rfile.read(length)).decode(),
                    "deadline": deadline,
                }
            )
        )
        temporary.replace(request)
        response = ROOT / (identifier + ".response")
        cancel = ROOT / (identifier + ".cancel")
        until = time.monotonic() + duration + RESPONSE_GRACE_SECONDS
        while not response.exists():
            if self.client_gone():
                cancel.touch(exist_ok=True)
                return
            if time.monotonic() >= until:
                cancel.touch(exist_ok=True)
                self.send_error(504)
                return
            time.sleep(0.05)
        data = json.loads(response.read_text())
        body = base64.b64decode(data["body"])
        self.send_response(data["status"])
        self.send_header("Content-Type", data["content_type"])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            cancel.touch(exist_ok=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8789), Handler).serve_forever()
