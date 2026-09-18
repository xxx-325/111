"""Host-only provider relay: credentials never enter execution containers."""

import asyncio
import base64
import contextvars
from dataclasses import dataclass
import json
import math
import os
import re
import threading
import time

import httpx


@dataclass(frozen=True)
class RequestContext:
    """One request's authoritative absolute deadline and cancellation marker."""

    deadline: float = None
    cancel: object = None

    def check(self):
        if self.cancel is not None and self.cancel.exists():
            raise TimeoutError("request cancelled by caller")
        if self.deadline is not None and time.time() >= self.deadline:
            raise TimeoutError("request deadline reached")

    def remaining(self):
        self.check()
        return self.deadline - time.time() if self.deadline is not None else 175


_CURRENT_CONTEXT = contextvars.ContextVar("relay_request_context", default=None)


def current_context():
    """Return the active request fence, or a no-op fence outside Relay dispatch."""
    return _CURRENT_CONTEXT.get() or RequestContext()


def append(path, value):
    with path.open("a") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


class Relay:
    def __init__(
        self,
        config,
        mailbox,
        audit,
        control=None,
        deadline=None,
        budget=None,
        role="user",
    ):
        self.config, self.mailbox, self.audit = config, mailbox, audit
        self.control, self.deadline = control, deadline
        self.budget, self.role = budget, role
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=2)
        if self.thread.is_alive() and self.budget:
            self.budget.uncertain()

    def context(self, packet):
        inherited = _CURRENT_CONTEXT.get()
        deadlines = [time.time() + 175]
        if self.deadline:
            deadlines.append(time.time() + max(0, self.deadline - time.monotonic()))
        if packet.get("deadline"):
            deadline = packet["deadline"]
            if type(deadline) not in (int, float) or not math.isfinite(deadline):
                raise ValueError("invalid request deadline")
            deadlines.append(deadline)
        if inherited and inherited.deadline is not None:
            deadlines.append(inherited.deadline)
        cancel = (
            inherited.cancel
            if inherited and inherited.cancel is not None
            else self.mailbox / (packet["id"] + ".cancel") if packet.get("id") else None
        )
        return RequestContext(min(deadlines), cancel)

    async def _provider_request(self, url, body, headers, context, metrics):
        async with asyncio.timeout(context.remaining()):
            async with httpx.AsyncClient(timeout=None, trust_env=True) as client:
                async with client.stream(
                    "POST", url, content=json.dumps(body).encode(), headers=headers
                ) as response:
                    metrics["headers_seconds"] = time.monotonic()
                    chunks = []
                    async for chunk in response.aiter_bytes():
                        context.check()
                        if chunk and "first_byte_seconds" not in metrics:
                            metrics["first_byte_seconds"] = time.monotonic()
                        chunks.append(chunk)
                    context.check()
                    return response.status_code, b"".join(chunks), metrics

    def dispatch(self, packet):
        context = self.context(packet)
        context.check()
        token = _CURRENT_CONTEXT.set(context)
        try:
            return self._dispatch(packet, context)
        finally:
            _CURRENT_CONTEXT.reset(token)

    def _dispatch(self, packet, context):
        body = json.loads(base64.b64decode(packet["body"], validate=True))
        if packet["path"] == "/control" and self.control:
            return (
                200,
                json.dumps(
                    self.control(
                        {
                            **body,
                            "request_id": packet["id"],
                            "_request_context": context,
                        }
                    ),
                    ensure_ascii=False,
                ).encode(),
            )
        if packet["path"] != "/v1/chat/completions":
            raise ValueError("endpoint denied")
        if self.deadline and time.monotonic() >= self.deadline:
            raise TimeoutError("run budget exhausted")
        if body.get("stream") or any(
            t.get("type") != "function" for t in body.get("tools", [])
        ):
            raise ValueError("only non-streaming local function calls are supported")
        requested_output = body.pop(
            "max_completion_tokens", body.get("max_tokens", 4096)
        )
        if not isinstance(requested_output, int) or not 0 < requested_output <= 4096:
            raise ValueError(
                "output token request exceeds the configured gateway bound"
            )
        body["max_tokens"] = requested_output
        if self.budget:
            if (
                self.budget.maximum is not None
                and len(json.dumps(body).encode()) > 60000
            ):
                raise ValueError(
                    "cost-limited request exceeds conservative input-size bound; no truncation performed"
                )
        body["model"] = self.config["model"]
        url = self.config["base_url"].rstrip("/") + "/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + os.environ[self.config["key_env"]],
        }
        started = time.monotonic()
        # Request contents can contain private user checks; this audit is host-private.
        append(self.audit, {"kind": "request", "id": packet["id"], "input": body})
        reservation = (
            self.budget.before(self.role, body, packet["id"]) if self.budget else None
        )
        metrics = {}
        try:
            status, raw, metrics = asyncio.run(
                self._provider_request(url, body, headers, context, metrics)
            )
            elapsed = time.monotonic() - started
            timing = {
                "headers_seconds": round(metrics["headers_seconds"] - started, 3),
                "first_byte_seconds": (
                    round(metrics["first_byte_seconds"] - started, 3)
                    if "first_byte_seconds" in metrics
                    else None
                ),
                "total_seconds": round(elapsed, 3),
            }
            if status >= 400:
                if self.budget:
                    self.budget.uncertain(reservation)
                append(
                    self.audit,
                    {
                        "kind": "provider_failure",
                        "id": packet["id"],
                        "status": status,
                        "timing": timing,
                    },
                )
                return (
                    status,
                    b'{"error":{"message":"Provider rejected request; see private status","type":"provider_error"}}',
                )
            context.check()
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                raise ValueError("provider returned invalid JSON") from None
            if self.budget:
                self.budget.after(self.role, result.get("usage", {}), reservation)
            append(
                self.audit,
                {
                    "kind": "response",
                    "id": packet["id"],
                    "output": result,
                    "seconds": elapsed,
                    "timing": timing,
                    "usage": result.get("usage", {}),
                },
            )
            return status, raw
        except Exception as error:
            if self.budget:
                self.budget.uncertain(reservation)
            append(
                self.audit,
                {
                    "kind": "provider_failure",
                    "id": packet["id"],
                    "error_type": type(error).__name__,
                    "timing": {
                        "headers_seconds": (
                            round(metrics["headers_seconds"] - started, 3)
                            if "headers_seconds" in metrics
                            else None
                        ),
                        "first_byte_seconds": (
                            round(metrics["first_byte_seconds"] - started, 3)
                            if "first_byte_seconds" in metrics
                            else None
                        ),
                        "total_seconds": round(time.monotonic() - started, 3),
                    },
                },
            )
            raise

    def run(self):
        while not self.stop.is_set():
            for path in self.mailbox.glob("*.request"):
                if not re.fullmatch(r"[a-f0-9]{32}\.request", path.name):
                    continue
                target = path.with_suffix(".response")
                if target.exists():
                    continue
                try:
                    if path.stat().st_size > 16 * 1024 * 1024:
                        raise ValueError("request too large")
                    with os.fdopen(
                        os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    ) as stream:
                        packet = json.load(stream)
                    packet["id"] = path.stem
                    status, raw = self.dispatch(packet)
                except Exception as exc:
                    append(
                        self.audit,
                        {
                            "kind": "relay_failure",
                            "id": path.stem,
                            "error_type": type(exc).__name__,
                        },
                    )
                    status, raw = (
                        502,
                        b'{"error":{"message":"Relay denied or failed request"}}',
                    )
                value = {
                    "status": status,
                    "content_type": "application/json",
                    "body": base64.b64encode(raw).decode(),
                }
                temporary = target.with_suffix(".out")
                with os.fdopen(
                    os.open(
                        temporary,
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                        0o600,
                    ),
                    "w",
                ) as stream:
                    json.dump(value, stream)
                temporary.replace(target)
            self.stop.wait(0.05)
