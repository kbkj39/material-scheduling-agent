"""Bounded model/tool loop. Final status is reconstructed from tool evidence."""

from __future__ import annotations

import json
from datetime import datetime
from time import monotonic

from .contracts import SYSTEM_PROMPT, TOOL_SCHEMAS, validate_request
from .models import ModelError


class Agent:
    def __init__(self, model, toolbox, max_steps: int = 20):
        if not isinstance(max_steps, int) or isinstance(max_steps, bool) or not 1 <= max_steps <= 100:
            raise ValueError("max_steps must be between 1 and 100")
        self.model = model
        self.toolbox = toolbox
        self.request = validate_request(toolbox.request)
        self.max_steps = max_steps

    def run(self) -> dict:
        started = monotonic()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": json.dumps(self.request, ensure_ascii=False)}]
        trace = []
        stop_reason = "step_limit"
        model_error = None
        for step in range(1, self.max_steps + 1):
            try:
                decision = self.model.decide(messages, TOOL_SCHEMAS)
                if not isinstance(decision, dict) or not isinstance(decision.get("action"), str) or not isinstance(decision.get("arguments"), dict):
                    raise ModelError("model must return one action with an argument object")
                action = decision["action"]
                arguments = decision["arguments"]
                # Enforce JSON-serializable arguments before invoking any tool.
                json.dumps(arguments, allow_nan=False)
            except (ModelError, ValueError, TypeError) as exc:
                model_error = str(exc)[:200] if isinstance(exc, ModelError) else "invalid model decision"
                trace.append({"step": step, "action": "model_error", "arguments": {}, "reason": "模型调用未产生有效动作。", "observation": {"ok": False, "error": {"code": "model_error", "message": model_error, "retryable": False}}})
                stop_reason = "model_error"
                break
            except Exception as exc:
                model_error = "Model execution failed: " + type(exc).__name__
                trace.append({"step": step, "action": "model_error", "arguments": {}, "reason": "模型调用异常。", "observation": {"ok": False, "error": {"code": "model_error", "message": model_error, "retryable": False}}})
                stop_reason = "model_error"
                break
            event = {"step": step, "action": action, "arguments": arguments, "reason": str(decision.get("reason", ""))[:200]}
            if decision.get("metadata"):
                event["model_metadata"] = decision["metadata"]
            if action == "finish":
                event["observation"] = {"ok": True, "note": "Final task status is derived from tool evidence."}
                trace.append(event)
                stop_reason = "model_finished"
                break
            try:
                observation = self.toolbox.execute(action, arguments)
                if not isinstance(observation, dict) or not isinstance(observation.get("ok"), bool):
                    raise TypeError("invalid tool return")
            except Exception as exc:
                observation = {"ok": False, "error": {"code": "internal_tool_error", "message": "Tool execution failed: " + type(exc).__name__, "retryable": False}}
            event["observation"] = observation
            trace.append(event)
            call_id = f"call_{step}"
            messages.append({"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {"name": action, "arguments": json.dumps(arguments, ensure_ascii=False)}}]})
            messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(observation, ensure_ascii=False)})
        items = [self._item_result(item, trace) for item in self.request["materials"]]
        statuses = {item["status"] for item in items}
        status = next((s for s in ["failed", "incomplete", "needs_information", "blocked", "pending_confirmation"] if s in statuses), "completed")
        if stop_reason == "step_limit":
            status = "incomplete"
        elif model_error:
            status = "failed"
        return {"request_id": self.request["request_id"], "mode": getattr(self.model, "mode", "custom"),
                "status": status, "stop_reason": stop_reason, "published": False,
                "message": self._status_message(status), "items": items, "trace": trace,
                "limits": {"max_steps": self.max_steps, "model_turns": len(trace), "tool_calls": sum(e["action"] not in {"finish", "model_error"} for e in trace)},
                "duration_seconds": round(monotonic() - started, 3),
                **({"error": {"code": "model_error", "message": model_error}} if model_error else {})}

    @staticmethod
    def _status_message(status: str) -> str:
        return {"completed": "审核与草稿保存完成；尚未发布。", "pending_confirmation": "已保存待确认草稿，替代时段须用户确认；尚未发布。", "needs_information": "素材信息不足，请补充后重试。", "blocked": "审核风险阻断，请修改素材后重试。", "failed": "任务失败，请查看工具或模型错误；未发布。", "incomplete": "任务未完成，请查看调用上限或缺少的执行步骤。"}[status]

    @staticmethod
    def _item_result(item: dict, trace: list[dict]) -> dict:
        events = [e for e in trace if e["arguments"].get("asset_id") == item["asset_id"]]
        result = {"asset_id": item["asset_id"], "requested_at": item["required_at"], "suggested_at": None,
                  "audit_status": "unknown", "risks": [], "pending_confirmations": [], "missing_fields": [],
                  "draft": {"status": "not_attempted"}, "status": "incomplete"}
        errors = {}
        available_slots = set()
        for event in events:
            action, observation = event["action"], event["observation"]
            proposed_value = event["arguments"].get("proposed_at")
            has_saved = result["draft"]["status"] in {"saved", "existing"}
            proposed = None
            if isinstance(proposed_value, str):
                try:
                    proposed = datetime.fromisoformat(proposed_value)
                except ValueError:
                    pass
            if action == "save_draft" and not has_saved and proposed and any(proposed == datetime.fromisoformat(slot) for slot in available_slots):
                result["suggested_at"] = proposed_value
                result["pending_confirmations"] = []
                original = datetime.fromisoformat(item["required_at"])
                confirmed = item.get("confirmed_alternative_at")
                if item.get("hard_requirement", True) and proposed != original and (not confirmed or proposed != datetime.fromisoformat(confirmed)):
                    result["pending_confirmations"] = ["原硬性发布时间保持不变；请确认建议的替代时段 " + result["suggested_at"]]
            if not observation.get("ok"):
                errors[action] = observation.get("error", {})
                if action == "save_draft" and not has_saved:
                    result["draft"] = {"status": "failed", "error": errors[action]}
                    audit = errors[action].get("details", {}).get("audit")
                    if audit:
                        result["audit_status"] = audit["audit_status"]
                        result["risks"] = audit.get("risks", [])
                        result["missing_fields"] = audit.get("missing_fields", [])
                    if errors[action].get("code") in {"slot_conflict", "slot_unavailable", "audit_changed"}:
                        result["suggested_at"] = None
                        result["pending_confirmations"] = []
                continue
            errors.pop(action, None)
            if action == "read_material":
                result["missing_fields"] = observation.get("missing_fields", [])
                if result["missing_fields"]:
                    result["audit_status"] = "needs_information"
            elif action == "check_rules":
                result["audit_status"] = observation["audit_status"]
                result["risks"] = observation.get("risks", [])
                result["missing_fields"] = observation.get("missing_fields", [])
            elif action == "query_slots":
                available_slots = set(observation.get("available_slots", []))
            elif action == "save_draft":
                receipt = observation["draft"]
                result["draft"] = {"status": "existing" if observation.get("duplicate") else "saved", **receipt}
                # Receipt's domain status is always draft; expose persistence state separately.
                result["draft"]["record_status"] = receipt.get("status", "draft")
                result["draft"]["status"] = "existing" if observation.get("duplicate") else "saved"
                if observation.get("notice"):
                    result["draft"]["notice"] = observation["notice"]
                result["suggested_at"] = receipt["proposed_at"]
                result["pending_confirmations"] = []
                if receipt["confirmation_status"] == "pending_confirmation":
                    result["pending_confirmations"] = ["原硬性发布时间保持不变；请确认建议的替代时段 " + receipt["proposed_at"]]
        if result["draft"]["status"] in {"saved", "existing"}:
            result["status"] = "pending_confirmation" if result["pending_confirmations"] else "completed"
        elif result["audit_status"] == "needs_information":
            result["status"] = "needs_information"
        elif result["audit_status"] == "blocked":
            result["status"] = "blocked"
        elif errors:
            result["status"] = "failed"
        result["errors"] = list(errors.values())
        return result
