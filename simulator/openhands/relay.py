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
from .config import model_request_limits


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


class RelayAuditError(RuntimeError):
    """The relay could not persist a required private audit record."""


class RelayProviderError(RuntimeError):
    """The upstream provider rejected a request without exposing its body."""

    def __init__(self, status):
        super().__init__("provider rejected request")
        self.status = status


class RelayOutputLimitError(RuntimeError):
    """A completed provider request was truncated before a usable response."""


def _error_code(error):
    if isinstance(error, RelayOutputLimitError):
        return "PROVIDER_OUTPUT_TRUNCATED"
    if isinstance(error, TimeoutError):
        return "REQUEST_DEADLINE"
    if isinstance(error, RelayAuditError):
        return "RELAY_AUDIT_UNAVAILABLE"
    if isinstance(error, RelayProviderError):
        return "PROVIDER_REJECTED"
    if isinstance(error, ValueError):
        return "RELAY_BAD_REQUEST"
    return "RELAY_INTERNAL"


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
        self.limits = model_request_limits(config)
        self.control, self.deadline = control, deadline
        self.budget, self.role = budget, role
        self.stop = threading.Event()
        self.health = self.mailbox / "relay-health.json"
        self.healthy = True
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=2)
        if self.thread.is_alive() and self.budget:
            self.budget.uncertain()

    def _audit(self, value):
        try:
            record = dict(value)
            record.setdefault("request_id", record.get("id"))
            record.setdefault(
                "operation",
                "/v1/chat/completions" if record.get("kind") != "relay_failure" else None,
            )
            record.setdefault(
                "stage",
                {
                    "request": "request",
                    "response": "response",
                    "provider_failure": "provider",
                    "relay_failure": "relay",
                }.get(record.get("kind"), "relay"),
            )
            append(self.audit, record)
        except Exception as error:
            self._mark_unhealthy("RELAY_AUDIT_UNAVAILABLE", value.get("request_id", value.get("id")))
            raise RelayAuditError("private audit is unavailable") from error

    def _mark_unhealthy(self, code, request_id):
        self.healthy = False
        self.stop.set()
        try:
            self.health.write_text(json.dumps({
                "status": "unhealthy",
                "error_code": code,
                "request_id": request_id,
            }, ensure_ascii=False))
        except Exception:
            pass

    @staticmethod
    def _failure_response(error, request_id):
        code = _error_code(error)
        status = 504 if code == "REQUEST_DEADLINE" else 502
        body = {
            "error": {
                "error_code": code,
                "request_id": request_id,
                "retryable": False,
                "message": "Relay request failed; inspect private audit.",
            }
        }
        return status, json.dumps(body, ensure_ascii=False).encode()

    def context(self, packet):
        inherited = _CURRENT_CONTEXT.get()
        deadlines = [time.time() + self.limits["request_timeout"]]
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
        if not self.healthy:
            raise RelayAuditError("relay is unavailable")
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
        requested_output = body.pop("max_completion_tokens", body.get("max_tokens"))
        configured_output = self.limits["max_output_tokens"]
        if configured_output is None:
            # SDK model metadata may insert a default even when our config is null.
            body.pop("max_tokens", None)
        elif requested_output is not None:
            if type(requested_output) is not int or requested_output <= 0:
                raise ValueError("output token request must be a positive integer")
            if requested_output > configured_output:
                raise ValueError("output token request exceeds the configured gateway bound")
            body["max_tokens"] = requested_output
        else:
            body["max_tokens"] = configured_output
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
            # Some OpenAI-compatible gateways reject Python's default client
            # identity at the edge. Keep the provider request compatible with
            # the curl-based smoke check without exposing credentials.
            "User-Agent": "curl/8.0",
        }
        started = time.monotonic()
        # Request contents can contain private user checks; this audit is host-private.
        self._audit(
            {
                "kind": "request",
                "id": packet["id"],
                "request_id": packet["id"],
                "operation": packet["path"],
                "stage": "request",
                "input": body,
            }
        )
        try:
            reservation = (
                self.budget.before(self.role, body, packet["id"]) if self.budget else None
            )
        except Exception as error:
            self._audit({"kind": "budget_failure", "id": packet["id"],
                         "stage": "budget_reservation", "error_type": type(error).__name__,
                         "error_code": "BUDGET_UNAVAILABLE"})
            raise
        metrics = {}
        stage = "provider"
        provider_failure_audited = False
        provider_accounted = False
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
                self._audit(
                    {
                        "kind": "provider_failure",
                        "id": packet["id"],
                        "status": status,
                        "timing": timing,
                    },
                )
                provider_failure_audited = True
                raise RelayProviderError(status)
            context.check()
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                raise ValueError("provider returned invalid JSON") from None
            if self.budget:
                stage = "budget_accounting"
                self.budget.after(self.role, result.get("usage", {}), reservation)
            provider_accounted = True
            stage = "response_audit"
            self._audit(
                {
                    "kind": "response",
                    "id": packet["id"],
                    "output": result,
                    "seconds": elapsed,
                    "timing": timing,
                    "usage": result.get("usage", {}),
                },
            )
            stage = "response_validation"
            if any(choice.get("finish_reason") == "length" for choice in result.get("choices", [])):
                raise RelayOutputLimitError("provider output token limit reached; no reply or tool call forwarded")
            return status, raw
        except Exception as error:
            if self.budget and not provider_failure_audited and not provider_accounted:
                self.budget.uncertain(reservation)
            if not provider_failure_audited:
                self._audit(
                    {
                        "kind": "provider_failure",
                        "id": packet["id"],
                        "error_type": type(error).__name__,
                        "stage": stage,
                        "error_code": "BUDGET_UNAVAILABLE" if stage == "budget_accounting" else _error_code(error),
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
                packet = None
                operation = None
                stage = "read_request"
                try:
                    if path.stat().st_size > 16 * 1024 * 1024:
                        raise ValueError("request too large")
                    with os.fdopen(
                        os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    ) as stream:
                        packet = json.load(stream)
                    packet["id"] = path.stem
                    operation = packet.get("path")
                    if operation == "/control":
                        try:
                            control_body = json.loads(
                                base64.b64decode(packet["body"], validate=True)
                            )
                            operation = control_body.get("operation", "control")
                        except Exception:
                            operation = "control"
                    stage = "dispatch"
                    status, raw = self.dispatch(packet)
                except Exception as exc:
                    audit_record = {
                        "kind": "relay_failure",
                        "id": path.stem,
                        "request_id": path.stem,
                        "path": packet.get("path") if isinstance(packet, dict) else None,
                        "operation": operation,
                        "stage": stage,
                        "error_type": type(exc).__name__,
                        "error_code": _error_code(exc),
                    }
                    try:
                        self._audit(audit_record)
                    except Exception:
                        self._mark_unhealthy("RELAY_AUDIT_UNAVAILABLE", path.stem)
                    status, raw = self._failure_response(exc, path.stem)
                value = {
                    "status": status,
                    "content_type": "application/json",
                    "body": base64.b64encode(raw).decode(),
                }
                temporary = target.with_suffix(".out")
                try:
                    stage = "write_response"
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
                except Exception as exc:
                    self._mark_unhealthy("RELAY_RESPONSE_UNAVAILABLE", path.stem)
                    try:
                        self._audit(
                            {
                                "kind": "relay_failure",
                                "id": path.stem,
                                "request_id": path.stem,
                                "path": packet.get("path") if isinstance(packet, dict) else None,
                                "operation": operation,
                                "stage": stage,
                                "error_type": type(exc).__name__,
                                "error_code": "RELAY_RESPONSE_UNAVAILABLE",
                            }
                        )
                    except RelayAuditError:
                        self._mark_unhealthy("RELAY_AUDIT_UNAVAILABLE", path.stem)
                    break
                if not self.healthy:
                    break
            self.stop.wait(0.05)
