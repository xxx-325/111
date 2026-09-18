import base64
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from simulator.openhands.relay import Relay, current_context


class SlowProvider(BaseHTTPRequestHandler):
    mode = "keepalive"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.mode == "headers_late":
            time.sleep(1)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if self.mode == "keepalive":
            for _ in range(20):
                try:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                time.sleep(0.05)
        try:
            self.wfile.write(
                b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'
            )
        except (BrokenPipeError, ConnectionResetError):
            pass


class BudgetFixture:
    maximum = None

    def __init__(self):
        self.before_calls = []
        self.uncertain_calls = []

    def before(self, _role, _body, call_id):
        self.before_calls.append(call_id)
        return call_id

    def after(self, *_args):
        raise AssertionError("timed out provider must not be recorded as successful")

    def uncertain(self, call_id=None):
        self.uncertain_calls.append(call_id)


def packet(identifier, seconds):
    body = {"messages": [], "tools": [], "max_tokens": 1}
    return {
        "id": identifier,
        "path": "/v1/chat/completions",
        "body": base64.b64encode(json.dumps(body).encode()).decode(),
        "deadline": time.time() + seconds,
    }


class RelayDeadlineTests(unittest.TestCase):
    def setUp(self):
        os.environ["RELAY_TEST_KEY"] = "placeholder"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SlowProvider)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        os.environ.pop("RELAY_TEST_KEY", None)

    def relay(self, directory, budget=None, control=None):
        config = {
            "base_url": f"http://127.0.0.1:{self.server.server_port}/v1",
            "model": "fake",
            "key_env": "RELAY_TEST_KEY",
        }
        return Relay(
            config,
            Path(directory) / "mailbox",
            Path(directory) / "audit.jsonl",
            control=control,
            budget=budget,
        )

    def test_keepalive_bytes_do_not_extend_total_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = BudgetFixture()
            relay = self.relay(directory, budget)
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                relay.dispatch(packet("a" * 32, 0.3))
            self.assertLess(time.monotonic() - started, 0.8)
            self.assertEqual(budget.uncertain_calls, ["a" * 32])

    def test_expired_request_fails_before_budget_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = BudgetFixture()
            relay = self.relay(directory, budget)
            expired = packet("e" * 32, -1)
            with self.assertRaises(TimeoutError):
                relay.dispatch(expired)
            self.assertEqual(budget.before_calls, [])
            self.assertEqual(budget.uncertain_calls, [])

    def test_current_context_is_noop_outside_and_active_inside_control(self):
        current_context().check()
        with tempfile.TemporaryDirectory() as directory:
            observed = []

            def control(_body):
                observed.append(current_context().deadline)
                return {"accepted": True}

            relay = self.relay(directory, control=control)
            request = {
                "id": "d" * 32,
                "path": "/control",
                "body": base64.b64encode(b"{}").decode(),
                "deadline": time.time() + 0.5,
            }
            relay.dispatch(request)
            self.assertEqual(len(observed), 1)
            self.assertLessEqual(observed[0], request["deadline"])
            self.assertIsNone(current_context().deadline)

    def test_waiting_for_headers_is_inside_total_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            SlowProvider.mode = "headers_late"
            relay = self.relay(directory)
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                relay.dispatch(packet("b" * 32, 0.3))
            self.assertLess(time.monotonic() - started, 0.8)
            SlowProvider.mode = "keepalive"

    def test_timed_out_provider_releases_single_relay_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            mailbox = Path(directory) / "mailbox"
            mailbox.mkdir()
            control_called = threading.Event()
            relay = self.relay(
                directory,
                control=lambda _body: control_called.set() or {"accepted": True},
            )
            first = packet("0" * 32, 0.3)
            first_path = mailbox / ("0" * 32 + ".request")
            temporary = first_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(first))
            temporary.replace(first_path)
            relay.start()
            time.sleep(0.05)
            control_id = "f" * 32
            control = {
                "id": control_id,
                "path": "/control",
                "body": base64.b64encode(b"{}").decode(),
                "deadline": time.time() + 1,
            }
            control_path = mailbox / (control_id + ".request")
            temporary = control_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(control))
            temporary.replace(control_path)
            self.assertTrue(control_called.wait(0.8))
            relay.close()
            self.assertTrue(control_path.with_suffix(".response").exists())


if __name__ == "__main__":
    unittest.main()
