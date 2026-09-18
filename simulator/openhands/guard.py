"""Read-only semantic gate. It can reject, never author a user reply."""

import base64
import json
import re
import uuid

from ..api_agent import parse_object
from ..episode import obvious_leak
from .provenance import source_check
from .evidence_links import quoted_observations
from .feedback_projection import authorized_feedback_values, filter_authorized_overlap
from ..state_machine import REQUEST_SEMANTICS

SYSTEM = (
    "审核一次 User 到 Code 的动作。只返回 JSON "
    '{decision:"allow|reject",kind:"message|internal_plan|closing",claims_observation:boolean,state_consistent:boolean,reasons:[string]}。'
    "decision 只判断正文、权限、证据和发送行为是否安全：拒绝角色倒置、越界任务、"
    "无证据观察、参考实现或内部标识和过早完成；宿主已审核绑定的当前任务测试可公开。"
    "state_consistent 只诊断请求类型／推进动作的分类是否贴切；"
    "如果唯一问题是 BUILD、DEBUG 等合法标签的分类差异，decision 必须为 allow，并将 "
    "state_consistent 设为 false。不要把标签分类差异写成安全拒绝。"
    "任务范围内前瞻性编码策略或实现约束可作为 PLAN，不需要运行证据；"
    "补充 Code 已公开方案用 REFINE，明确纠偏用 CORRECT，只有明确要求改实现才是 BUILD。"
    "不得借策略反馈新增业务功能、虚构现状，或无依据扩成新文档/测试任务；"
    "原需求已有的细节或 Code 明确询问的澄清可以保留。"
    "claims_observation 仅在正文实际声称或转述执行观察时为 true；自动附带证据不算正文声称。"
    "转述运行结果时，宿主附加的原始输入与结果必须原样保留；internal_plan 不发送；"
    "公开文字遵循 dialogue_language。只判断，不改写。"
)


def _quotes_distinct_raw_value(text, experiences):
    """Detect exact, non-trivial results even if semantic review misses them."""
    for experience in experiences:
        observation = experience.get("observation", {})
        kind = observation.get("kind")
        result_key = "error" if kind == "runtime_error" else "output"
        if kind in ("runtime_error", "wrong_output"):
            value = observation.get(result_key)
            normalized = value.strip() if isinstance(value, str) else ""
            if len(normalized) >= 24 and normalized in text:
                return True
    return False


class MessageGuard:
    def __init__(self, relay, tasks, dialogue_language="zh-CN"):
        self.relay, self.tasks, self.language = relay, tasks, dialogue_language

    def review(
        self,
        operation,
        payload,
        state,
        requirement,
        history,
        code_sources=(),
        supporting_checks=(),
    ):
        text = payload.get("text", "")
        errors = []
        provenance = source_check(text, code_sources, requirement, history)
        requested_ids = payload.get("evidence_ids", [])
        supporting = {
            item["id"]: item
            for item in supporting_checks
            if (
                isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and item.get("task_id") != state.data.get("task_id")
            )
        }
        inferred = []
        if operation == "send" and requested_ids == []:
            inferred = quoted_observations(text, state.data["checks"])
            identifiers = [item["id"] for item in inferred] or list(supporting)
        else:
            identifiers = requested_ids
        known = {c["id"]: c for c in state.data["checks"]}
        if operation == "send":
            known.update(supporting)
        latest_revision = max(
            (c.get("revision", -1) for c in known.values()), default=-1
        )
        experiences = [
            c["simulated_experience"]
            for c in known.values()
            if c.get("simulated_experience") and c.get("revision") == latest_revision
        ]
        bound = [
            item for item in experiences
            if isinstance(identifiers, list) and item.get("summary_id") in identifiers
        ]
        active_values = [
            value
            for experience in bound
            for value in authorized_feedback_values(experience.get("observation", {}))
        ]
        if operation == "send":
            reason = obvious_leak(
                text,
                self.tasks,
                deferred_reference_test_values=active_values,
            )
            if reason:
                return {"allowed": False, "reasons": [reason]}
            private_identifiers = list(known) + [
                t["id"] for t in state.data["transitions"]
            ]
            if re.search(r"\btask-\d+\b", text, re.I) or any(
                i in text or i[:8] in text for i in private_identifiers
            ):
                return {
                    "allowed": False,
                    "reasons": [
                        "internal task/check/permit identifier in public reply"
                    ],
                }
            provenance = filter_authorized_overlap(provenance, active_values)
            if provenance["matches"]:
                return {
                    "allowed": False,
                    "reasons": ["private test source in message"],
                    "provenance": provenance,
                }
        effective_payload = {**payload, "evidence_ids": identifiers}
        if not isinstance(identifiers, list) or any(
            not isinstance(i, str) or i not in known for i in identifiers
        ):
            errors.append("evidence must reference recorded observations")
        evidence = (
            [known[i] for i in identifiers if isinstance(i, str) and i in known]
            if isinstance(identifiers, list)
            else []
        )
        evidence += [
            c
            for c in state.data["checks"]
            if c["result"] == "failed"
            and not c.get("resolved_by")
            and c not in evidence
        ]
        body = dict(
            model=self.relay.config["model"],
            stream=False,
            temperature=0,
            max_tokens=1024,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(
                        dict(
                            operation=operation,
                            proposal=effective_payload,
                            requirement=requirement,
                            communication=state.communication(),
                            current_action=(
                                state.data.get("permit")
                                if operation == "send"
                                else None
                            ),
                            evidence=evidence,
                            simulated_observations=experiences,
                            dialogue_language=self.language,
                            current_state=state.data.get("state"),
                            request_semantics=REQUEST_SEMANTICS,
                        ),
                        ensure_ascii=False,
                    ),
                },
            ],
        )
        status, raw = self.relay.dispatch(
            dict(
                id="gate-" + uuid.uuid4().hex,
                path="/v1/chat/completions",
                body=base64.b64encode(json.dumps(body).encode()).decode(),
            )
        )
        if status != 200:
            raise RuntimeError("semantic gate unavailable; no message sent")
        result = parse_object(json.loads(raw)["choices"][0]["message"]["content"])
        required = {"decision", "kind", "claims_observation", "state_consistent", "reasons"}
        if (
            set(result) != required
            or result.get("decision") not in ("allow", "reject")
            or result.get("kind") not in ("message", "internal_plan", "closing")
            or type(result.get("claims_observation")) is not bool
            or type(result.get("state_consistent")) is not bool
            or not isinstance(result.get("reasons"), list)
            or not all(isinstance(r, str) for r in result["reasons"])
        ):
            raise ValueError("invalid gate response; no message sent")
        warnings = []
        if not result["state_consistent"]:
            warnings.append(
                "Request type/control classification may not match the proposed action."
            )
        if operation == "send":
            if result["claims_observation"] and not identifiers:
                errors.append(
                    "This reply claims observed results. Attach actual supporting evidence_ids from session_state."
                )
            reports_execution = (
                result["claims_observation"]
                or bool(inferred)
                or _quotes_distinct_raw_value(text, bound)
            )
            if reports_execution:
                for experience in bound:
                    observation = experience.get("observation", {})
                    kind = observation.get("kind")
                    if kind in ("runtime_error", "wrong_output"):
                        result_key = "error" if kind == "runtime_error" else "output"
                        for key in ("input", result_key):
                            value = observation.get(key)
                            if not isinstance(value, str) or value not in text:
                                errors.append(
                                    f"Public feedback must preserve the exact {key} "
                                    "from current execution evidence."
                                )
            accepted = any(
                a["task_id"] == state.data["task_id"] for a in state.data["accepted"]
            )
            if result["kind"] == "closing" and not accepted:
                errors.append(
                    "Only a closing/duplicate success report: handle accept_task or unresolved records before sending."
                )
        normalized = dict(
            allowed=result["decision"] == "allow",
            handoff_kind=(
                "request_or_feedback" if result["kind"] == "message" else result["kind"]
            ),
        )
        return {
            **normalized,
            "allowed": normalized["allowed"] and not errors,
            "reasons": result["reasons"] + errors,
            "warnings": warnings,
            "unresolved": state.pending_checks(),
            "provenance": provenance,
            "evidence_linking": {
                "method": "exact_quote_or_semantic_review",
                "matches": inferred,
                "effective_ids": identifiers,
            },
        }
