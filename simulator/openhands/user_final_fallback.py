"""Recovery of unsent ordinary User Agent final text."""

import copy

from .events import latest_user_final
from .state import TransitionError


class UserFinalFallbackMixin:
    """Retain one exact User final and publish it through normal controls."""

    def _fallback_send_request_id(self, record):
        identifiers = [
            item["id"]
            for item in record.get("accepted_context", {}).get("evidence", [])
        ]
        suffix = "-" + "-".join(identifiers) if identifiers else ""
        return "user-final-fallback-" + record["event_id"] + suffix

    def user_final_supporting_checks(self, packet):
        """Return old-task checks only for their bound fallback send audit."""
        record = self.saved.get("user_final_fallback")
        if (
            not isinstance(record, dict)
            or packet.get("operation") != "send"
            or packet.get("request_id") != self._fallback_send_request_id(record)
            or packet.get("_public_message_id") != record.get("event_id")
            or record.get("status") != "sending"
        ):
            return []
        context = record.get("accepted_context", {})
        if (
            context.get("to_task_id") != self.state.data.get("task_id")
            or context.get("from_task_id") == context.get("to_task_id")
        ):
            return []
        return copy.deepcopy(context.get("evidence", []))

    def deliver_user_final_fallback(self):
        """Send one retained final through the normal send control path."""
        record = self.saved.get("user_final_fallback")
        if not isinstance(record, dict) or record.get("status") not in (
            "awaiting_transition", "sending"
        ):
            return False
        request_id = self._fallback_send_request_id(record)
        prior = self.saved.get("control_results", {}).get(request_id)
        if prior is not None:
            record["status"] = (
                "delivered" if prior.get("accepted") else
                "retained_private" if prior.get("retained_private") else "rejected"
            )
            record["result"] = copy.deepcopy(prior)
            self.persist()
            return prior.get("accepted") is True
        permit = self.state.data.get("permit")
        if (
            not permit
            or permit.get("task_id") != record.get("task_id")
            or self.state.data.get("task_id") != record.get("task_id")
        ):
            return False
        record["status"] = "sending"
        self.persist()
        result = self._control({
            "request_id": request_id,
            "operation": "send",
            "payload": {
                "task_id": record["task_id"],
                "permit_id": permit["id"],
                "text": record["text"],
                "evidence_ids": [],
            },
            "_public_message_id": record["event_id"],
        })
        record["status"] = (
            "delivered" if result.get("accepted") else
            "retained_private" if result.get("retained_private") else "rejected"
        )
        record["result"] = copy.deepcopy(result)
        self.persist()
        return result.get("accepted") is True

    def retain_user_final(self, events):
        """Retain one ordinary final without treating it as a public action."""
        accepted_context = self.saved.get("accepted_final_context")
        in_flight = self.saved.get("in_flight")
        if (
            isinstance(accepted_context, dict)
            and isinstance(in_flight, dict)
            and accepted_context.get("command_id") == in_flight.get("id")
        ):
            self.saved.pop("accepted_final_context")
            self.persist()
        else:
            accepted_context = None
        if (
            self.state.data.get("status") != "running"
            or self.state.data.get("phase") != "user"
        ):
            return None
        if isinstance(in_flight, dict) and in_flight.get("role") == "user":
            start = in_flight.get("public_start", len(self.saved["public"]))
            if any(
                item.get("kind") == "user"
                for item in self.saved["public"][start:]
            ):
                return None
        draft = latest_user_final(events)
        if not draft:
            return None
        existing = self.saved.get("user_final_fallback")
        if isinstance(existing, dict):
            if existing.get("event_id") == draft["event_id"]:
                return existing
            if existing.get("status") in ("awaiting_transition", "sending"):
                return existing
        record = {
            "schema": "user-final-fallback-v1",
            "event_id": draft["event_id"],
            "task_id": self.state.data["task_id"],
            "text": draft["text"],
            "status": "captured",
        }
        if (
            accepted_context
            and accepted_context.get("to_task_id") == record["task_id"]
            and accepted_context.get("evidence")
        ):
            record["accepted_context"] = accepted_context
        self.saved["user_final_fallback"] = record
        self.persist()
        permit = self.state.data.get("permit")
        if permit and permit.get("task_id") == record["task_id"]:
            record["status"] = "awaiting_transition"
            self.persist()
            self.deliver_user_final_fallback()
            return record
        try:
            review_payload, _ = self.prepare_send_payload({
                "task_id": record["task_id"],
                "text": record["text"],
                "evidence_ids": [],
            })
        except TransitionError as exc:
            record.update(status="rejected", result={
                "accepted": False,
                "reason": str(exc),
            })
            self.persist()
            return record
        review = self.guard.review(
            "send",
            review_payload,
            self.state,
            self.requirement(),
            [p for p in self.saved["public"] if p["kind"] in ("user", "assistant")],
            self.saved["code_sources"],
        )
        record["review"] = review
        record["status"] = (
            "awaiting_transition" if review.get("allowed")
            and review.get("handoff_kind") == "request_or_feedback"
            else "retained_private" if review.get("handoff_kind") in (
                "internal_plan", "closing"
            ) else "rejected"
        )
        self.persist()
        return record
