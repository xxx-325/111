import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen as stdlib_urlopen

from simulator import native_http
from simulator.openhands import control_tools
from simulator.openhands.control_tools import ControlRequestError, HostExecutor, StateAction
from simulator.openhands.relay import Relay


class KeepaliveProvider(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(200)
        self.end_headers()
        for _ in range(20):
            try:
                self.wfile.write(b" ")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            time.sleep(0.05)


class NativeDeadlineTests(unittest.TestCase):
    def test_control_error_body_is_bounded_structured_and_never_echoed(self):
        for detail in ({'error_code': 'PROVIDER_REJECTED', 'request_id': 'a' * 32,
                        'retryable': False, 'message': 'private provider text'},
                       [], 'private detail'):
            with self.subTest(detail=detail):
                error = HTTPError('http://localhost/control', 502, 'private', {}, None)
                raw = json.dumps({'error': detail}).encode()
                with patch.object(error, 'read', return_value=raw) as read:
                    with patch.object(control_tools, 'urlopen', side_effect=error):
                        with self.assertRaises(ControlRequestError) as raised:
                            control_tools.request_control('read_state', {})
                read.assert_called_once_with(256 * 1024)
                result = raised.exception
                self.assertNotIn('private', str(result))
                self.assertFalse(result.retryable)
                self.assertEqual(result.error_code,
                                 'PROVIDER_REJECTED' if isinstance(detail, dict) else 'CONTROL_HTTP_ERROR')
                self.assertEqual(result.request_id, 'a' * 32 if isinstance(detail, dict) else None)

    def test_shared_deadline_returns_failure_and_forced_disconnect_marks_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mailbox = root / "mailbox"
            mailbox.mkdir()
            provider_server = ThreadingHTTPServer(("127.0.0.1", 0), KeepaliveProvider)
            provider_thread = threading.Thread(
                target=provider_server.serve_forever, daemon=True
            )
            provider_thread.start()
            bridge = ThreadingHTTPServer(("127.0.0.1", 0), native_http.Handler)
            bridge_thread = threading.Thread(target=bridge.serve_forever, daemon=True)
            bridge_thread.start()
            old_root = native_http.ROOT
            old_native_deadline = native_http.REQUEST_DEADLINE_SECONDS
            old_native_grace = native_http.RESPONSE_GRACE_SECONDS
            old_deadline = control_tools.CONTROL_DEADLINE_SECONDS
            old_grace = control_tools.CONTROL_RESPONSE_GRACE_SECONDS
            old_urlopen = control_tools.urlopen
            native_http.ROOT = mailbox
            native_http.REQUEST_DEADLINE_SECONDS = 0.3
            native_http.RESPONSE_GRACE_SECONDS = 0.4
            control_tools.CONTROL_DEADLINE_SECONDS = 0.3
            control_tools.CONTROL_RESPONSE_GRACE_SECONDS = 0.4
            os.environ["NATIVE_DEADLINE_KEY"] = "placeholder"
            config = {
                "base_url": f"http://127.0.0.1:{provider_server.server_port}/v1",
                "model": "fake",
                "key_env": "NATIVE_DEADLINE_KEY",
            }
            nested = Relay(config, root / "unused", root / "provider.jsonl")

            def control(_body):
                body = {"messages": [], "tools": [], "max_tokens": 1}
                import base64

                packet = {
                    "id": "a" * 32,
                    "path": "/v1/chat/completions",
                    "body": base64.b64encode(json.dumps(body).encode()).decode(),
                }
                nested.dispatch(packet)

            relay = Relay(config, mailbox, root / "relay.jsonl", control=control)
            relay.start()

            def redirect(request, timeout):
                request = Request(
                    f"http://127.0.0.1:{bridge.server_port}/control",
                    data=request.data,
                    headers=dict(request.header_items()),
                )
                return stdlib_urlopen(request, timeout=timeout)

            control_tools.urlopen = redirect
            try:
                started = time.monotonic()
                with self.assertRaises(ControlRequestError) as raised:
                    HostExecutor("review")(StateAction())
                self.assertEqual(raised.exception.error_code, "REQUEST_DEADLINE")
                self.assertIsNotNone(raised.exception.request_id)
                self.assertLess(time.monotonic() - started, 0.8)
                responses = list(mailbox.glob("*.response"))
                self.assertEqual(len(responses), 1)
                response = json.loads(responses[0].read_text())
                self.assertEqual(response["status"], 504)

                control_tools.urlopen = lambda request, timeout: redirect(request, 0.05)
                with self.assertRaises(ControlRequestError) as raised:
                    HostExecutor("review")(StateAction())
                self.assertEqual(raised.exception.error_code, "CONTROL_UNAVAILABLE")
                until = time.monotonic() + 1
                while time.monotonic() < until and not list(mailbox.glob("*.cancel")):
                    time.sleep(0.01)
                self.assertTrue(list(mailbox.glob("*.cancel")))
            finally:
                control_tools.urlopen = old_urlopen
                control_tools.CONTROL_DEADLINE_SECONDS = old_deadline
                control_tools.CONTROL_RESPONSE_GRACE_SECONDS = old_grace
                native_http.ROOT = old_root
                native_http.REQUEST_DEADLINE_SECONDS = old_native_deadline
                native_http.RESPONSE_GRACE_SECONDS = old_native_grace
                relay.close()
                bridge.shutdown()
                bridge.server_close()
                provider_server.shutdown()
                provider_server.server_close()
                bridge_thread.join(timeout=2)
                provider_thread.join(timeout=2)
                os.environ.pop("NATIVE_DEADLINE_KEY", None)


if __name__ == "__main__":
    unittest.main()
