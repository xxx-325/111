import base64
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from simulator.openhands.relay import Relay, RelayAuditError, current_context


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


class RejectingProvider(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(429)
        self.end_headers()
        self.wfile.write(b'{"message":"provider private detail"}')


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
    def test_one_audit_failure_disables_future_dispatch_even_if_disk_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            relay = self.relay(directory)
            relay.mailbox.mkdir()
            with patch('simulator.openhands.relay.append', side_effect=OSError('disk')):
                with self.assertRaises(RelayAuditError):
                    relay._audit({'kind': 'request', 'id': 'a' * 32})
            self.assertFalse(relay.healthy)
            with self.assertRaises(RelayAuditError):
                relay.dispatch(packet('b' * 32, 1))

    def test_budget_rejection_is_distinct_in_private_audit_without_provider_call(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = BudgetFixture()
            relay = self.relay(directory, budget=budget)
            with patch.object(budget, 'before', side_effect=TimeoutError('private budget detail')):
                with patch.object(relay, '_provider_request') as provider:
                    with self.assertRaises(TimeoutError):
                        relay.dispatch(packet('b' * 32, 1))
                    provider.assert_not_called()
            records = [json.loads(line) for line in relay.audit.read_text().splitlines()]
            failure = records[-1]
            self.assertEqual(failure['stage'], 'budget_reservation')
            self.assertEqual(failure['error_code'], 'BUDGET_UNAVAILABLE')
            self.assertNotIn('private budget detail', json.dumps(failure))

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

    def _queue(self, mailbox, identifier, value):
        request = mailbox / (identifier + ".request")
        temporary = request.with_suffix(".tmp")
        temporary.write_text(json.dumps(value))
        temporary.replace(request)
        return request

    def test_audit_failure_returns_safe_error_and_stops_relay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mailbox = root / "mailbox"
            mailbox.mkdir()
            relay = self.relay(directory, control=lambda _body: {"accepted": True})
            identifier = "1" * 32
            self._queue(
                mailbox,
                identifier,
                {
                    "path": "/v1/chat/completions",
                    "body": base64.b64encode(json.dumps({"messages": [], "tools": [], "max_tokens": 1}).encode()).decode(),
                },
            )
            response = mailbox / (identifier + ".response")
            with patch("simulator.openhands.relay.append", side_effect=OSError("private audit disk failed")):
                relay.start()
                until = time.monotonic() + 1
                while time.monotonic() < until and not response.exists():
                    time.sleep(0.01)
                relay.close()
            self.assertTrue(response.exists())
            value = json.loads(response.read_text())
            body = json.loads(base64.b64decode(value["body"]))
            self.assertEqual(value["status"], 502)
            self.assertEqual(body["error"]["error_code"], "RELAY_AUDIT_UNAVAILABLE")
            self.assertEqual(body["error"]["request_id"], identifier)
            self.assertNotIn("private audit disk failed", body["error"]["message"])
            self.assertFalse(relay.healthy)
            self.assertEqual(json.loads((mailbox / "relay-health.json").read_text())["error_code"], "RELAY_AUDIT_UNAVAILABLE")

    def test_response_write_failure_marks_relay_unhealthy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mailbox = root / "mailbox"
            mailbox.mkdir()
            relay = self.relay(directory, control=lambda _body: {"accepted": True})
            identifier = "2" * 32
            request = self._queue(
                mailbox,
                identifier,
                {
                    "path": "/control",
                    "body": base64.b64encode(json.dumps({"operation": "read_state"}).encode()).decode(),
                },
            )
            original_replace = Path.replace

            def fail_response_replace(path, target):
                if path.parent == mailbox and path.name.endswith(".out"):
                    raise OSError("response mailbox unavailable")
                return original_replace(path, target)

            with patch.object(Path, "replace", fail_response_replace):
                relay.start()
                until = time.monotonic() + 1
                while time.monotonic() < until and relay.healthy:
                    time.sleep(0.01)
                relay.close()
            self.assertFalse(relay.healthy)
            health = json.loads((mailbox / "relay-health.json").read_text())
            self.assertEqual(health["error_code"], "RELAY_RESPONSE_UNAVAILABLE")
            self.assertFalse((mailbox / (identifier + ".response")).exists())

    def test_control_callback_failure_is_safe_and_audited_with_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mailbox = root / "mailbox"
            mailbox.mkdir()

            def fail(_body):
                raise RuntimeError("private callback detail")

            relay = self.relay(directory, control=fail)
            identifier = "3" * 32
            self._queue(
                mailbox,
                identifier,
                {
                    "path": "/control",
                    "body": base64.b64encode(json.dumps({"operation": "read_state"}).encode()).decode(),
                },
            )
            relay.start()
            response = mailbox / (identifier + ".response")
            until = time.monotonic() + 1
            while time.monotonic() < until and not response.exists():
                time.sleep(0.01)
            relay.close()
            value = json.loads(response.read_text())
            body = json.loads(base64.b64decode(value["body"]))
            self.assertEqual(value["status"], 502)
            self.assertEqual(body["error"]["error_code"], "RELAY_INTERNAL")
            self.assertNotIn("private callback detail", body["error"]["message"])
            records = [json.loads(line) for line in (root / "audit.jsonl").read_text().splitlines()]
            failure = records[-1]
            self.assertEqual(failure["request_id"], identifier)
            self.assertEqual(failure["operation"], "read_state")
            self.assertEqual(failure["stage"], "dispatch")
            self.assertEqual(failure["error_type"], "RuntimeError")

    def test_provider_rejection_is_distinct_and_does_not_expose_body(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), RejectingProvider)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = {
                    "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    "model": "fake",
                    "key_env": "RELAY_TEST_KEY",
                }
                relay = Relay(config, root / "mailbox", root / "audit.jsonl")
                with self.assertRaises(Exception) as raised:
                    relay.dispatch(packet("4" * 32, 1))
                self.assertEqual(type(raised.exception).__name__, "RelayProviderError")
                records = [json.loads(line) for line in (root / "audit.jsonl").read_text().splitlines()]
                failure = records[-1]
                self.assertEqual(failure["kind"], "provider_failure")
                self.assertEqual(failure["status"], 429)
                self.assertEqual(failure["operation"], "/v1/chat/completions")
                self.assertEqual(failure["stage"], "provider")
                self.assertNotIn("provider private detail", json.dumps(records))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
