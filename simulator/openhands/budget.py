"""Shared model-call accounting, including SDK condensation and semantic gates."""
import copy
import json
import math
import os
import threading
import time
import uuid
from pathlib import Path


class Budget:
    def __init__(self, config, data=None, journal=None):
        self.configured_input_tokens = config.get('context_window_tokens', 262144)
        if type(self.configured_input_tokens) is not int or self.configured_input_tokens <= 0:
            raise ValueError('context_window_tokens must be a positive integer')
        self.maximum = config.get('max_cost')
        self.prices = config.get('pricing')
        if self.maximum is not None:
            if type(self.maximum) not in (int, float) or not math.isfinite(self.maximum) or self.maximum <= 0:
                raise ValueError('max_cost must be positive')
            if not self.prices or not self.prices.get('source') or not self.prices.get('currency'):
                raise ValueError('cost limit requires explicit pricing, currency and a verifiable source')
            roles = ('user', 'code', 'decomposer', 'judge') if config.get('progressive_issues') else ('user', 'code')
            for role in roles:
                row = self.prices.get(role, {})
                model_config = config.get(role, config['user'])
                if row.get('model') != model_config['model'] or any(type(row.get(k)) not in (int, float) or not math.isfinite(row[k]) or row[k] < 0 for k in ('input_per_million', 'output_per_million')):
                    raise ValueError('cost limit requires model-matched per-role input/output pricing')
        self.journal = Path(journal) if journal else None
        if self.journal and self.journal.exists():
            data = json.loads(self.journal.read_text())
        elif self.maximum is not None and data and data.get('attempts'):
            raise RuntimeError('cost-limited resume requires the per-call budget journal')
        self.data = copy.deepcopy(data) if data is not None else dict(attempts=0, calls=0, prompt_tokens=0, completion_tokens=0,
                                cost=0.0 if self.prices else None, usage_missing=False)
        self.data.setdefault('pending', {})
        if self.data['pending']:
            self.data['usage_missing'] = True
        self.deadline = time.monotonic() + config.get('max_seconds', 1200)
        self.lock = threading.RLock()

    def _save(self):
        if self.journal:
            temporary = self.journal.with_suffix('.tmp')
            with temporary.open('w') as stream:
                json.dump(self.data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.journal)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def before(self, role, body, call_id=None):
        with self.lock:
            if time.monotonic() >= self.deadline:
                raise TimeoutError('time budget exhausted')
            reserve = 0
            if self.maximum is not None:
                if self.data['usage_missing']:
                    raise RuntimeError('provider usage missing; cost-limited run paused')
                price = self.prices[role]
                # Hold the configured context reservation across concurrent calls.
                input_tokens = self.configured_input_tokens
                output_tokens = body.get('max_tokens', 0)
                reserve = (input_tokens*price['input_per_million'] + output_tokens*price['output_per_million'])/1e6
                if self.data['cost'] + sum(self.data['pending'].values()) + reserve > self.maximum:
                    raise TimeoutError('insufficient remaining cost budget for conservative call reservation')
            identifier = call_id or uuid.uuid4().hex
            if identifier in self.data['pending']:
                raise RuntimeError('uncertain model call cannot be replayed')
            self.data['pending'][identifier] = reserve
            self.data['attempts'] = self.data.get('attempts', 0)+1
            counts = self.data.setdefault('roles', {}).setdefault(role, dict(attempts=0, calls=0, prompt_tokens=0, completion_tokens=0))
            counts['attempts'] += 1
            self._save()
            return identifier

    def uncertain(self, call_id=None):
        with self.lock:
            self.data['usage_missing'] = True
            self._save()

    def after(self, role, usage, call_id=None):
        with self.lock:
            self.data['calls'] += 1
            counts = self.data.setdefault('roles', {}).setdefault(role, dict(attempts=0, calls=0, prompt_tokens=0, completion_tokens=0))
            counts['calls'] += 1
            if call_id:
                self.data['pending'].pop(call_id, None)
            if not isinstance(usage, dict) or not all(type(usage.get(k)) is int and usage[k] >= 0 for k in ('prompt_tokens', 'completion_tokens')):
                self.data['usage_missing'] = True
            else:
                self.data['prompt_tokens'] += usage['prompt_tokens']
                self.data['completion_tokens'] += usage['completion_tokens']
                counts['prompt_tokens'] += usage['prompt_tokens']
                counts['completion_tokens'] += usage['completion_tokens']
                if self.prices and role in self.prices:
                    price = self.prices[role]
                    self.data['cost'] += (usage['prompt_tokens']*price['input_per_million'] + usage['completion_tokens']*price['output_per_million'])/1e6
            self._save()
