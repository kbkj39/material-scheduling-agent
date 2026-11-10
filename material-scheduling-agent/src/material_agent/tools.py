"""Constrained local business tools.  No tool publishes content."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .storage import DraftStore, StorageError


class ToolError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False, details: dict | None = None):
        self.code, self.message, self.retryable = code, message, retryable
        self.details = details
        super().__init__(message)


def _instant(value: Any) -> str:
    if not isinstance(value, str):
        raise ToolError("invalid_time", "时间必须为带时区偏移的 ISO 8601 字符串。")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ToolError("invalid_time", "时间必须为带时区偏移的 ISO 8601 字符串。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ToolError("invalid_time", "时间缺少时区偏移。")
    return parsed.astimezone(timezone.utc).isoformat()


def _error(code: str, message: str, retryable: bool = False, details: dict | None = None) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "retryable": retryable, **({"details": details} if details else {})}}


class ToolBox:
    """Tools bind immutable user requirements; model arguments cannot grant consent.

    ``faults`` maps a tool name to the number of upcoming injected failures.
    Every call, including injected failures, returns an explicit receipt.
    """

    _ARGUMENTS = {
        "read_material": ({"asset_id"}, {"asset_id"}),
        "check_rules": ({"asset_id"}, {"asset_id"}),
        "query_slots": ({"asset_id", "preferred_at"}, {"asset_id"}),
        "save_draft": ({"asset_id", "proposed_at"}, {"asset_id", "proposed_at"}),
    }

    def __init__(self, request: dict, catalog_path: str | Path, db_path: str | Path,
                 faults: dict[str, int] | None = None):
        self.request = copy.deepcopy(request)
        self.catalog_path, self.db_path = Path(catalog_path), Path(db_path)
        self.faults = dict(faults or {})
        self._read: set[str] = set()
        self._approved: set[str] = set()
        self._queried: dict[str, set[str]] = {}
        self._draft_store: DraftStore | None = None

    def _store(self) -> DraftStore:
        if self._draft_store is None:
            self._draft_store = DraftStore(self.db_path)
        return self._draft_store

    def _item(self, asset_id: Any) -> dict:
        if not isinstance(asset_id, str) or not asset_id:
            raise ToolError("invalid_arguments", "asset_id 必须为非空字符串。")
        if not isinstance(self.request, dict) or not isinstance(self.request.get("request_id"), str) or not self.request["request_id"].strip():
            raise ToolError("invalid_request", "request_id 必须为非空字符串。")
        items = self.request.get("materials")
        if not isinstance(items, list):
            raise ToolError("invalid_request", "materials 必须为列表。")
        matches = [item for item in items if isinstance(item, dict) and item.get("asset_id") == asset_id]
        if len(matches) != 1:
            raise ToolError("asset_not_requested", "素材必须在用户请求中恰好出现一次。")
        item = matches[0]
        _instant(item.get("required_at"))
        if "hard_requirement" in item and not isinstance(item["hard_requirement"], bool):
            raise ToolError("invalid_request", "hard_requirement 必须为布尔值。")
        if item.get("confirmed_alternative_at") is not None:
            _instant(item["confirmed_alternative_at"])
        return item

    def _fingerprint(self) -> str:
        normalized = copy.deepcopy(self.request)
        for item in normalized.get("materials", []):
            if not isinstance(item, dict):
                raise ToolError("invalid_request", "每项素材必须为对象。")
            item["required_at"] = _instant(item.get("required_at"))
            item["hard_requirement"] = item.get("hard_requirement", True)
            item["confirmed_alternative_at"] = (
                _instant(item["confirmed_alternative_at"])
                if item.get("confirmed_alternative_at") is not None else None
            )
        normalized["materials"].sort(key=lambda item: item.get("asset_id", ""))
        return hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode()).hexdigest()

    def _catalog(self) -> dict:
        try:
            value = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ToolError("catalog_unavailable", "素材与规则资料暂不可读取。", True) from exc
        if not isinstance(value, dict) or not isinstance(value.get("materials"), dict) or not isinstance(value.get("rules"), dict):
            raise ToolError("invalid_catalog", "资料格式错误：需要 materials 与 rules 对象。")
        return value

    @staticmethod
    def _audit(catalog: dict, asset_id: str) -> dict:
        material = catalog["materials"].get(asset_id)
        if not isinstance(material, dict):
            raise ToolError("material_not_found", f"未找到素材 {asset_id}。")
        fields = catalog["rules"].get("required_fields", ["title", "platform", "content", "rights"])
        if not isinstance(fields, list) or not all(isinstance(field, str) for field in fields):
            raise ToolError("invalid_catalog", "required_fields 必须为字符串列表。")
        missing = [field for field in fields if not isinstance(material.get(field), str)
                   or not material[field].strip()]
        risks = []
        terms = catalog["rules"].get("blocked_terms", [])
        if not isinstance(terms, list):
            raise ToolError("invalid_catalog", "blocked_terms 必须为列表。")
        text = str(material.get("title", "")) + "\n" + str(material.get("content", ""))
        for rule in terms:
            if not isinstance(rule, dict) or not isinstance(rule.get("term"), str) or not rule["term"]:
                raise ToolError("invalid_catalog", "每条风险规则需要非空 term。")
            if rule["term"].casefold() in text.casefold():
                risks.append({"rule_id": rule.get("id", "blocked_term"), "reason": rule.get("reason", "命中阻断词"), "evidence": rule["term"]})
        if material.get("rights") is not None and material["rights"] != "cleared":
            risks.append({"rule_id": "RIGHTS_NOT_CLEARED", "reason": "版权状态未获授权。", "evidence": material["rights"]})
        return {"audit_status": "needs_information" if missing else "blocked" if risks else "approved",
                "missing_fields": missing, "risks": risks}

    def execute(self, action: str, arguments: dict) -> dict:
        try:
            if not isinstance(action, str) or action not in self._ARGUMENTS:
                raise ToolError("unknown_tool", "不允许调用该工具。")
            if not isinstance(arguments, dict):
                raise ToolError("invalid_arguments", "工具参数必须为对象。")
            allowed, required = self._ARGUMENTS[action]
            if not required.issubset(arguments) or not set(arguments).issubset(allowed):
                raise ToolError("invalid_arguments", "工具参数缺失或含未声明字段。")
            item = self._item(arguments.get("asset_id"))
            asset_id = item["asset_id"]
            if self.faults.get(action, 0) > 0:
                self.faults[action] -= 1
                return _error("injected_failure", f"已注入 {action} 工具失败。", True)
            result = getattr(self, "_" + action)(item, arguments)
            return {"ok": True, "asset_id": asset_id, **result}
        except (ToolError, StorageError) as exc:
            return _error(exc.code, exc.message, exc.retryable, getattr(exc, "details", None))
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            return _error("invalid_data", "工具输入或资料格式无效。")
        except OSError:
            return _error("storage_error", "草稿存储暂不可用。", True)

    def _read_material(self, item: dict, arguments: dict) -> dict:
        catalog = self._catalog()
        audit = self._audit(catalog, item["asset_id"])
        self._read.add(item["asset_id"])
        return {"material": copy.deepcopy(catalog["materials"][item["asset_id"]]),
                "missing_fields": audit["missing_fields"]}

    def _check_rules(self, item: dict, arguments: dict) -> dict:
        audit = self._audit(self._catalog(), item["asset_id"])
        if audit["audit_status"] == "approved":
            self._approved.add(item["asset_id"])
        else:
            self._approved.discard(item["asset_id"])
        return audit

    def _slots(self, catalog: dict, item: dict) -> tuple[list[str], list[str]]:
        slots = catalog.get("slots", [])
        schedule = catalog.get("schedule", [])
        if not isinstance(slots, list) or not isinstance(schedule, list):
            raise ToolError("invalid_catalog", "slots 与 schedule 必须为列表。")
        unique: dict[str, str] = {}
        for slot in slots:
            unique.setdefault(_instant(slot), slot)
        occupied = [_instant(entry["at"]) for entry in schedule]
        occupied += [_instant(slot) for slot in self._store().occupied(
            exclude_request_id=self.request["request_id"], exclude_asset_id=item["asset_id"])]
        return [unique[key] for key in sorted(unique)], occupied

    def _query_slots(self, item: dict, arguments: dict) -> dict:
        catalog = self._catalog()
        slots, occupied = self._slots(catalog, item)
        preferred_at = arguments.get("preferred_at", item["required_at"])
        _instant(preferred_at)
        available = [slot for slot in slots if _instant(slot) not in occupied]
        self._queried[item["asset_id"]] = {_instant(slot) for slot in available}
        return {"requested_at": item["required_at"],
                "requested_available": _instant(item["required_at"]) in {_instant(slot) for slot in available},
                "preferred_at": preferred_at,
                "preferred_available": _instant(preferred_at) in {_instant(slot) for slot in available},
                "available_slots": available,
                "occupied_slots": [slot for slot in slots if _instant(slot) in occupied]}

    def _save_draft(self, item: dict, arguments: dict) -> dict:
        proposed_at = arguments["proposed_at"]
        proposed = _instant(proposed_at)
        fingerprint = self._fingerprint()
        store = self._store()
        existing = store.get(self.request["request_id"], item["asset_id"], fingerprint)
        if existing is not None:
            return {"draft": existing, "duplicate": True,
                    "notice": "返回已存草稿；该回执不证明当前素材或时段仍可用。"}
        asset_id = item["asset_id"]
        if asset_id not in self._read or asset_id not in self._approved or asset_id not in self._queried:
            raise ToolError("prerequisite_missing", "保存前必须读取素材、通过审核并查询时段。")
        if proposed not in self._queried[asset_id]:
            raise ToolError("slot_not_queried", "建议时段必须来自已查询的可用候选。")
        catalog = self._catalog()
        audit = self._audit(catalog, asset_id)
        if audit["audit_status"] != "approved":
            self._approved.discard(asset_id)
            raise ToolError("audit_changed", "保存前复核未通过，需重新检查素材与规则。", details={"audit": audit})
        slots, occupied = self._slots(catalog, item)
        if proposed not in {_instant(slot) for slot in slots}:
            raise ToolError("slot_unavailable", "建议时段已不在当前候选范围。")
        if proposed in occupied:
            raise ToolError("slot_conflict", "保存前复检发现建议时段已被占用。", True)
        confirmed_alternative = item.get("confirmed_alternative_at")
        confirmed = (not item.get("hard_requirement", True)
                     or proposed == _instant(item["required_at"])
                     or (confirmed_alternative is not None and proposed == _instant(confirmed_alternative)))
        draft = {"id": "draft_" + hashlib.sha256((self.request["request_id"] + "\0" + asset_id).encode()).hexdigest()[:20],
                 "request_id": self.request["request_id"], "asset_id": asset_id,
                 "original_required_at": item["required_at"], "proposed_at": proposed_at,
                 "confirmation_status": "confirmed" if confirmed else "pending_confirmation", "status": "draft"}
        # The transaction checks other drafts again, closing the query/save race.
        external = [entry["at"] for entry in catalog.get("schedule", [])]
        saved, duplicate = store.save(self.request["request_id"], asset_id, fingerprint, draft, external)
        return {"draft": saved, "duplicate": duplicate}
