"""A real function-calling model adapter and an explicitly offline simulator."""

from __future__ import annotations

import json
import os
from datetime import datetime
from urllib import error, parse, request


class ModelError(RuntimeError):
    pass


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LiveModel:
    mode = "live"

    def __init__(self, *, api_key: str | None = None, model: str | None = None, base_url: str | None = None, timeout: float = 45):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model or os.getenv("OPENAI_MODEL", "")
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.timeout = timeout
        if not self.api_key or not self.model:
            raise ModelError("live mode requires OPENAI_API_KEY and OPENAI_MODEL; no fallback to mock")
        url = parse.urlsplit(self.base_url)
        if url.username or url.password or url.query or url.fragment:
            raise ModelError("base URL must not contain credentials, query, or fragment")
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ModelError("use HTTPS, or HTTP on loopback for a local model server")
        self.opener = request.build_opener(_NoRedirect)

    def decide(self, messages: list[dict], tools: list[dict]) -> dict:
        payload = {"model": self.model, "messages": messages, "tools": tools, "tool_choice": "required", "parallel_tool_calls": False}
        req = request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw = response.read(1_048_577)
        except error.HTTPError as exc:
            # Response bodies may include private input; do not copy them into traces.
            raise ModelError(f"model HTTP error {exc.code}") from None
        except (error.URLError, TimeoutError, OSError):
            raise ModelError("model connection failed or timed out") from None
        if len(raw) > 1_048_576:
            raise ModelError("model response exceeded 1 MiB")
        try:
            response = json.loads(raw)
            calls = response["choices"][0]["message"]["tool_calls"]
            if len(calls) != 1:
                raise ValueError("expected exactly one function call")
            function = calls[0]["function"]
            arguments = json.loads(function["arguments"])
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
            reason = arguments.pop("reason", "Model selected the next function from tool observations.")
            usage = response.get("usage") or {}
            if not isinstance(usage, dict):
                raise ValueError("invalid usage metadata")
            return {"action": function["name"], "arguments": arguments, "reason": str(reason)[:200],
                    "metadata": {"model": self.model, "usage": {k: v for k, v in usage.items() if isinstance(v, int)}}}
        except (KeyError, IndexError, TypeError, ValueError, UnicodeError):
            raise ModelError("invalid function-calling response") from None


class MockModel:
    """Deterministic observation-driven simulator. This is NOT an LLM or ReAct proof."""

    mode = "mock"

    @staticmethod
    def _decision(action: str, arguments: dict, reason: str) -> dict:
        return {"action": action, "arguments": arguments, "reason": "[MOCK] " + reason}

    def decide(self, messages: list[dict], tools: list[dict]) -> dict:
        user_request = json.loads(next(m["content"] for m in messages if m["role"] == "user"))
        events = []
        pending = {}
        for message in messages:
            if message["role"] == "assistant":
                for call in message.get("tool_calls", []):
                    pending[call["id"]] = call["function"]
            elif message["role"] == "tool":
                function = pending.get(message["tool_call_id"])
                if function:
                    events.append((function["name"], json.loads(function["arguments"]), json.loads(message["content"])))
        outcomes = []
        for item in user_request["materials"]:
            asset = item["asset_id"]
            history = [e for e in events if e[1].get("asset_id") == asset]
            if history and not history[-1][2].get("ok"):
                action, arguments, observation = history[-1]
                previous_errors = sum(e[0] == action and not e[2].get("ok") for e in history)
                if observation.get("error", {}).get("retryable") and action != "save_draft" and previous_errors < 2:
                    return self._decision(action, arguments, "工具返回可重试错误；保留已有结果并重试一次。")
                outcomes.append("failed")
                continue
            successful = {name: observation for name, _, observation in history if observation.get("ok")}
            if "save_draft" in successful:
                draft = successful["save_draft"]["draft"]
                outcomes.append("pending_confirmation" if draft["confirmation_status"] == "pending_confirmation" else "completed")
                continue
            if "read_material" not in successful:
                return self._decision("read_material", {"asset_id": asset}, "先读取素材，核对可用信息。")
            if successful["read_material"].get("missing_fields"):
                outcomes.append("needs_information")
                continue
            if "check_rules" not in successful:
                return self._decision("check_rules", {"asset_id": asset}, "素材已读取，检查审核规则。")
            audit = successful["check_rules"]
            if audit["audit_status"] != "approved":
                outcomes.append("needs_information" if audit["audit_status"] == "needs_information" else "blocked")
                continue
            if "query_slots" not in successful:
                return self._decision("query_slots", {"asset_id": asset, "preferred_at": item["required_at"]}, "审核通过，查询原要求时段的占用情况。")
            query = successful["query_slots"]
            available = query["available_slots"]
            confirmed = item.get("confirmed_alternative_at")
            def available_equivalent(value):
                if not value:
                    return None
                instant = datetime.fromisoformat(value)
                return next((slot for slot in available if datetime.fromisoformat(slot) == instant), None)
            preferred = available_equivalent(confirmed) or available_equivalent(item["required_at"])
            if preferred is None:
                if not available:
                    outcomes.append("incomplete")
                    continue
                preferred = available[0]
            queried = [args.get("preferred_at") for name, args, obs in history if name == "query_slots" and obs.get("ok")]
            if not any(value and datetime.fromisoformat(value) == datetime.fromisoformat(preferred) for value in queried):
                return self._decision("query_slots", {"asset_id": asset, "preferred_at": preferred}, "原时段不可用或有已确认替代要求，补查候选时段。")
            return self._decision("save_draft", {"asset_id": asset, "proposed_at": preferred}, "保存草稿；硬要求的未确认替代时间交由工具标记待确认。")
        status = next((s for s in ["failed", "incomplete", "needs_information", "blocked", "pending_confirmation"] if s in outcomes), "completed")
        return self._decision("finish", {"status": status, "message": "模拟决策结束；最终事实以工具回执为准。"}, "已处理可执行步骤，返回草稿与待处理事项。")
