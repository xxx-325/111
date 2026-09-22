"""Persistent OpenAI-compatible tool conversations; requests stay on the host."""
import json
import os
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from .sandbox import TOOLS, tool_result


class API:
    def __init__(self, config):
        self.config = config
        self.deadline = None
        self.key = os.environ.get(config['key_env'])
        if not self.key:
            raise ValueError('missing environment variable: ' + config['key_env'])

    def complete(self, messages, tools=False):
        payload = dict(model=self.config['model'], messages=messages)
        if tools:
            payload['tools'] = TOOLS
        req = Request(self.config['base_url'].rstrip('/') + '/chat/completions',
                      data=json.dumps(payload).encode(), headers={
                          'Authorization': 'Bearer ' + self.key,
                          'Content-Type': 'application/json',
                          'User-Agent': 'curl/8.0',
                      })
        try:
            timeout = self.config.get('timeout', 180)
            if self.deadline is not None:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('episode deadline reached')
                timeout = min(timeout, remaining)
            with urlopen(req, timeout=timeout) as response:
                data = json.load(response)
        except HTTPError as error:
            raise RuntimeError(f'Provider HTTP {error.code}; response body withheld to avoid credential disclosure') from None
        message = data['choices'][0]['message']
        # Preserve tool protocol, but do not forward provider-private reasoning.
        return {k: message[k] for k in ('role', 'content', 'tool_calls') if k in message}


class ToolAgent:
    def __init__(self, api, system, history=None):
        self.api = api
        self.history = history if history is not None else [{'role': 'system', 'content': system}]

    def turn(self, prompt, sandbox, emit, budget=30):
        self.history.append({'role': 'user', 'content': prompt})
        for _ in range(budget):
            response = self.api.complete(self.history, tools=True)
            self.history.append(response)
            calls = response.get('tool_calls') or []
            if response.get('content'):
                emit('assistant', {'text': response['content'], 'phase': 'commentary' if calls else 'final'})
            if not calls:
                if not response.get('content'):
                    raise ValueError('empty final response')
                return response['content']
            for call in calls:
                if getattr(self.api, 'deadline', None) is not None:
                    remaining = self.api.deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('episode deadline reached')
                    sandbox.timeout = min(sandbox.timeout, remaining)
                emit('tool_call', {'call_id': call['id'], 'function': call['function']})
                result = tool_result(sandbox, call)
                emit('tool_result', {'call_id': call['id'], **result})
                self.history.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(result)})
        self.history.append({'role': 'user', 'content': 'The tool budget for this turn is exhausted. Return your final response now using only observed evidence. Preserve any requested output JSON format. Describe unfinished work honestly.'})
        response = self.api.complete(self.history, tools=False)
        if response.get('tool_calls') or not response.get('content'):
            raise RuntimeError('provider failed to finish at tool budget boundary')
        self.history.append(response)
        emit('assistant', {'text': response['content'], 'phase': 'final'})
        return response['content']


def parse_object(text):
    text = text.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        # Accept a single JSON object preceded by explanatory prose, never eval.
        start = text.find('{')
        if start < 0:
            raise
        value, end = json.JSONDecoder().raw_decode(text[start:])
        if '{' in text[start + end:]:
            raise ValueError('ambiguous multiple JSON objects')
    if not isinstance(value, dict):
        raise ValueError('expected a JSON object')
    return value
