"""Reasoning budget and truncation regressions; no provider/model calls."""
import base64
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from simulator.openhands.budget import Budget
from simulator.openhands.config import model_request_limits, validate_example
from simulator.openhands.container import SDKContainer
from simulator.openhands.relay import Relay, RelayOutputLimitError


class ModelRequestLimitTests(unittest.TestCase):
    def packet(self, body=None):
        return dict(id='a' * 32, path='/v1/chat/completions',
                    body=base64.b64encode(json.dumps(body or {}).encode()).decode())

    def relay(self, root, **limits):
        return Relay(dict(model='fixture', key_env='PROBE_KEY', **limits,
                          base_url='https://provider.invalid/v1'),
                     Path(root), Path(root) / 'provider.jsonl')

    def test_invalid_limits_rejected_without_provider_call(self):
        self.assertEqual(model_request_limits({}),
                         dict(max_input_tokens=262144, max_output_tokens=None, request_timeout=175))
        for field in ('max_input_tokens', 'request_timeout'):
            for invalid in (True, 0, -1, '16384', 1.5, None):
                with self.subTest(field=field, value=invalid), self.assertRaises(ValueError):
                    model_request_limits({field: invalid})
        for invalid in (True, 0, -1, '16384', 1.5):
            with self.subTest(max_output_tokens=invalid), self.assertRaises(ValueError):
                model_request_limits({'max_output_tokens': invalid})
        errors = validate_example({'judge': {'model': 'fixture', 'request_timeout': 0}})
        self.assertTrue(any('judge.request_timeout' in error for error in errors))

    def test_configured_limits_reach_sdk_runtime_and_resume_must_match(self):
        with tempfile.TemporaryDirectory() as root:
            config = dict(model='fixture', execution_backend='shared_diagnostic',
                          max_input_tokens=262144, max_output_tokens=None, request_timeout=600)
            worker = SDKContainer(Path(root) / 'worker', Path(root) / 'workspace',
                                  config, 'unused', 'code', None, time.monotonic() + 100)
            saved = json.loads((worker.directory / 'inbox/config.json').read_text())
            self.assertEqual(model_request_limits(saved), model_request_limits(config))
            self.assertTrue((worker.directory / 'runtime/simulator/openhands/config.py').is_file())
            with self.assertRaisesRegex(ValueError, 'limits differ'):
                SDKContainer(worker.directory, worker.workspace,
                             dict(config, max_input_tokens=65536), 'unused', 'code', None, 100)

    def test_worker_uses_limits_for_llm_bridge_and_control(self):
        from simulator.openhands import worker, control_tools
        config = dict(model='fixture', role='code', max_input_tokens=262144,
                      max_output_tokens=None, request_timeout=600)
        with patch.object(worker.Path, 'read_text', return_value=json.dumps(config)), \
                patch.object(worker.subprocess, 'Popen') as popen, \
                patch.object(control_tools, 'CONTROL_DEADLINE_SECONDS', 175), \
                patch.object(worker, 'LLM', side_effect=RuntimeError('captured')) as llm:
            with self.assertRaisesRegex(RuntimeError, 'captured'):
                worker.main()
            self.assertEqual(llm.call_args.kwargs['max_input_tokens'], 262144)
            self.assertIsNone(llm.call_args.kwargs['max_output_tokens'])
            self.assertEqual(llm.call_args.kwargs['timeout'], 605)
            self.assertEqual(control_tools.CONTROL_DEADLINE_SECONDS, 600)
            self.assertEqual(popen.call_args.args[0][-2:], ['--request-timeout', '600'])

    def test_extended_deadline_still_obeys_caller_and_episode(self):
        with tempfile.TemporaryDirectory() as root:
            relay = self.relay(root, request_timeout=600)
            self.assertGreater(relay.context({}).remaining(), 590)
            self.assertLessEqual(relay.context({'deadline': time.time() + 2}).remaining(), 2)
            relay.deadline = time.monotonic() + 1
            self.assertLessEqual(relay.context({}).remaining(), 1)

    def test_semantic_and_preparation_calls_share_explicit_output_budget(self):
        from simulator.openhands.guard import MessageGuard
        from simulator.openhands.issue_stages import call_json
        from simulator.openhands.source import project_commit
        from simulator.openhands.state import TaskState

        relay = MagicMock(config={'model': 'fixture', 'max_output_tokens': 16384})
        replies = iter([
            dict(decision='allow', kind='message', claims_observation=False,
                 state_consistent=True, reasons=[]),
            {},
            dict(title='Public requirement', body='A supported behavior', supported=True),
            dict(allowed=True, reason='grounded'),
        ])

        def dispatch(packet):
            body = json.loads(base64.b64decode(packet['body']))
            self.assertEqual(body['max_tokens'], 16384)
            reply = next(replies)
            return 200, json.dumps({'choices': [{'message': {'content': json.dumps(reply)}}]}).encode()

        relay.dispatch.side_effect = dispatch
        MessageGuard(relay, []).review('send', {'text': 'Explain the mechanism'}, TaskState(), {}, [])
        call_json(relay, 'fixture', {})
        project_commit({'identifier': 'private-fixture-commit', 'title': 'fixture', 'patch': ''}, relay)
        self.assertEqual(relay.dispatch.call_count, 4)

    def test_larger_request_is_budgeted_and_excess_is_rejected(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, PROBE_KEY='fixture'):
            relay = self.relay(root, max_output_tokens=16384)
            response = {'choices': [{'finish_reason': 'stop', 'message': {'content': 'OK'}}],
                        'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
            provider = AsyncMock(return_value=(200, json.dumps(response).encode(),
                                               {'headers_seconds': time.monotonic()}))
            relay.budget = MagicMock(maximum=None)
            with patch.object(relay, '_provider_request', provider):
                relay.dispatch(self.packet({'max_completion_tokens': 16384}))
                self.assertEqual(provider.call_args.args[1]['max_tokens'], 16384)
                self.assertEqual(relay.budget.before.call_args.args[1]['max_tokens'], 16384)
                with self.assertRaises(ValueError):
                    relay.dispatch(self.packet({'max_tokens': 16385}))
                self.assertEqual(provider.call_count, 1)

    def test_unbounded_output_omits_gateway_cap(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, PROBE_KEY='fixture'):
            relay = self.relay(root)
            response = {'choices': [{'finish_reason': 'stop', 'message': {'content': 'OK'}}],
                        'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
            provider = AsyncMock(return_value=(200, json.dumps(response).encode(),
                                               {'headers_seconds': time.monotonic()}))
            with patch.object(relay, '_provider_request', provider):
                for body in ({}, {'max_tokens': 8192}, {'max_completion_tokens': 8192},
                             {'max_tokens': 8192, 'max_completion_tokens': 4096}):
                    with self.subTest(body=body):
                        relay.dispatch(self.packet(body))
                        self.assertNotIn('max_tokens', provider.call_args.args[1])
                        self.assertNotIn('max_completion_tokens', provider.call_args.args[1])

    def test_length_response_stops_once_with_raw_audit_and_known_usage(self):
        for message in ({'content': '', 'reasoning_content': 'PRIVATE reasoning'},
                        {'content': 'partial text'},
                        {'tool_calls': [{'function': {'name': 'terminal', 'arguments': '{'}}]}):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as root, \
                    patch.dict(os.environ, PROBE_KEY='fixture'):
                relay = self.relay(root, max_output_tokens=None)
                relay.budget = Budget({})
                response = {'choices': [{'finish_reason': 'length', 'message': message}],
                            'usage': {'prompt_tokens': 100, 'completion_tokens': 4096}}
                provider = AsyncMock(return_value=(200, json.dumps(response).encode(),
                                                   {'headers_seconds': time.monotonic()}))
                with patch.object(relay, '_provider_request', provider):
                    with self.assertRaises(RelayOutputLimitError) as raised:
                        relay.dispatch(self.packet())
                provider.assert_awaited_once()
                self.assertEqual(relay.budget.data['calls'], 1)
                self.assertEqual(relay.budget.data['completion_tokens'], 4096)
                self.assertFalse(relay.budget.data['usage_missing'])
                self.assertEqual(relay.budget.data['pending'], {})
                records = [json.loads(line) for line in relay.audit.read_text().splitlines()]
                self.assertEqual(records[1]['output'], response)
                self.assertEqual(records[-1]['error_code'], 'PROVIDER_OUTPUT_TRUNCATED')
                status, raw = relay._failure_response(raised.exception, 'a' * 32)
                self.assertEqual(status, 502)
                self.assertFalse(json.loads(raw)['error']['retryable'])
                self.assertNotIn('PRIVATE', raw.decode())

    def test_sdk_does_not_retry_relay_failures_at_the_http_layer(self):
        from litellm.exceptions import BadGatewayError
        from openhands.sdk import LLM, Message, TextContent
        from simulator import native_http

        truncated = {'choices': [{'finish_reason': 'length', 'message': {'content': ''}}],
                     'usage': {'prompt_tokens': 1, 'completion_tokens': 4096}}
        for status, body in ((524, b'gateway timeout'),
                             (200, json.dumps(truncated).encode())):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as root, \
                    patch.dict(os.environ, PROBE_KEY='fixture'), \
                    patch.object(native_http, 'ROOT', Path(root)):
                relay = self.relay(root)
                provider = AsyncMock(return_value=(status, body,
                                     {'headers_seconds': time.monotonic()}))
                server = ThreadingHTTPServer(('127.0.0.1', 0), native_http.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                with patch.object(relay, '_provider_request', provider):
                    relay.start()
                    thread.start()
                    try:
                        llm = LLM(model='openai/test', api_key='fixture',
                                  base_url=f'http://127.0.0.1:{server.server_port}/v1',
                                  stream=False, num_retries=0, timeout=5,
                                  max_input_tokens=262144, max_output_tokens=None,
                                  usage_id='retry-fixture')
                        with self.assertRaises(BadGatewayError):
                            llm.completion([Message(role='user', content=[TextContent(text='test')])])
                        provider.assert_awaited_once()
                        self.assertEqual(len(list(Path(root).glob('*.request'))), 1)
                    finally:
                        server.shutdown()
                        server.server_close()
                        thread.join(timeout=2)
                        relay.close()


if __name__ == '__main__':
    unittest.main()
