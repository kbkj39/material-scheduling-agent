"""Behavioral acceptance tests. Fixtures are synthetic; no model/network is used."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from material_agent.agent import Agent
from material_agent.models import MockModel
from material_agent.tools import ToolBox


AT_18 = "2026-11-12T18:00:00+08:00"
AT_19 = "2026-11-12T19:00:00+08:00"
AT_20 = "2026-11-12T20:00:00+08:00"


class SequenceModel:
    """Deliberately fallible decision source for runtime boundary tests."""

    mode = "mock"

    def __init__(self, decisions):
        self.decisions = copy.deepcopy(decisions)
        self.calls = 0

    def decide(self, messages, tools):
        self.calls += 1
        if self.decisions:
            return self.decisions.pop(0)
        return {
            "action": "finish",
            "arguments": {"status": "completed", "message": "模型声称已完成"},
            "reason": "用于验证运行器不信任未经工具证实的完成声明",
        }


def decision(action, arguments):
    return {"action": action, "arguments": arguments, "reason": "测试外部决策边界"}


class AgentAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.catalog_path = self.root / "catalog.json"
        self.db_path = self.root / "drafts.sqlite3"
        self.catalog = {
            "materials": {
                "M01": {
                    "title": "字幕功能介绍",
                    "platform": "douyin",
                    "content": "展示支持字幕的产品功能。",
                    "rights": "cleared",
                },
                "M02": {
                    "title": "未补齐授权的素材",
                    "platform": "douyin",
                    "content": "一段合成演示素材。",
                },
                "M03": {
                    "title": "高风险文案",
                    "platform": "douyin",
                    "content": "本课程保证收益。",
                    "rights": "cleared",
                },
            },
            "rules": {
                "required_fields": ["title", "platform", "content", "rights"],
                "blocked_terms": [
                    {"id": "R01", "term": "保证收益", "reason": "禁止收益承诺"}
                ],
            },
            "slots": [AT_18, AT_19, AT_20],
            "schedule": [],
        }
        self.write_catalog()

    def write_catalog(self):
        self.catalog_path.write_text(
            json.dumps(self.catalog, ensure_ascii=False), encoding="utf-8"
        )

    def request(self, asset_id="M01", request_id="case-001", **changes):
        item = {"asset_id": asset_id, "required_at": AT_18, "hard_requirement": True}
        item.update(changes)
        return {"request_id": request_id, "materials": [item]}

    def run_agent(self, request=None, *, model=None, faults=None, max_steps=12):
        toolbox = ToolBox(
            request or self.request(),
            self.catalog_path,
            self.db_path,
            faults=faults,
        )
        return Agent(model or MockModel(), toolbox, max_steps=max_steps).run()

    def item(self, result):
        self.assertEqual(len(result["items"]), 1)
        return result["items"][0]

    def assert_not_saved(self, item):
        self.assertNotIn(item["draft"]["status"], {"saved", "existing"})
        self.assertFalse(item["draft"].get("id"))

    def test_normal_schedule_saves_a_draft_at_requested_time(self):
        result = self.run_agent()
        item = self.item(result)
        self.assertEqual(result["mode"], "mock")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(item["requested_at"], AT_18)
        self.assertEqual(item["suggested_at"], AT_18)
        self.assertEqual(item["pending_confirmations"], [])
        self.assertEqual(item["draft"]["status"], "saved")
        self.assertTrue(item["draft"]["id"])
        self.assertFalse(item["risks"])
        self.assertEqual(item["audit_status"], "approved")

    def test_missing_material_information_requires_user_input(self):
        result = self.run_agent(self.request("M02"))
        item = self.item(result)
        self.assertEqual(result["status"], "needs_information")
        self.assert_not_saved(item)
        self.assertNotEqual(item["audit_status"], "approved")
        self.assertIn("rights", json.dumps(result, ensure_ascii=False))

    def test_rule_risk_blocks_saving(self):
        result = self.run_agent(self.request("M03"))
        item = self.item(result)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(item["risks"])
        self.assertIn("R01", json.dumps(item["risks"], ensure_ascii=False))
        self.assert_not_saved(item)

    def test_conflict_proposal_keeps_hard_requirement_pending_confirmation(self):
        self.catalog["schedule"] = [{"asset_id": "OTHER", "at": AT_18}]
        self.write_catalog()
        result = self.run_agent()
        item = self.item(result)
        self.assertEqual(result["status"], "pending_confirmation")
        self.assertEqual(item["requested_at"], AT_18)
        self.assertIn(item["suggested_at"], {AT_19, AT_20})
        self.assertTrue(item["pending_confirmations"])
        self.assertEqual(item["draft"]["status"], "saved")

    def test_confirmed_alternative_can_complete_without_changing_original_request(self):
        self.catalog["schedule"] = [{"asset_id": "OTHER", "at": AT_18}]
        self.write_catalog()
        result = self.run_agent(self.request(confirmed_alternative_at=AT_19))
        item = self.item(result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(item["requested_at"], AT_18)
        self.assertEqual(item["suggested_at"], AT_19)
        self.assertFalse(item["pending_confirmations"])
        self.assertEqual(item["draft"]["status"], "saved")

    def test_duplicate_request_returns_same_draft(self):
        first = self.run_agent()
        second = self.run_agent()
        self.assertEqual(self.item(first)["draft"]["id"], self.item(second)["draft"]["id"])
        self.assertEqual(self.item(second)["draft"]["status"], "existing")

    def test_same_request_id_with_different_business_parameters_is_rejected(self):
        first = self.run_agent()
        result = self.run_agent(self.request(required_at=AT_19))
        item = self.item(result)
        self.assertNotEqual(result["status"], "completed")
        self.assert_not_saved(item)
        original_again = self.run_agent()
        self.assertEqual(self.item(first)["draft"]["id"], self.item(original_again)["draft"]["id"])

    def test_save_failure_is_reported_and_prior_successes_are_preserved(self):
        result = self.run_agent(faults={"save_draft": 1})
        item = self.item(result)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(item["draft"]["status"], "failed")
        self.assertTrue(item["draft"].get("error"))
        self.assertFalse(item["draft"].get("id"))
        self.assertEqual(item["audit_status"], "approved")
        self.assertEqual(item["suggested_at"], AT_18)

    def test_call_limit_returns_incomplete_and_keeps_partial_results(self):
        result = self.run_agent(max_steps=1)
        self.assertEqual(result["status"], "incomplete")
        self.assert_not_saved(self.item(result))
        self.assertLessEqual(
            len([event for event in result["trace"] if event["action"] != "finish"]), 1
        )

    def test_model_cannot_forge_success_by_finishing_before_tools(self):
        result = self.run_agent(model=SequenceModel([]))
        self.assertNotEqual(result["status"], "completed")
        self.assert_not_saved(self.item(result))
        self.assertNotEqual(self.item(result)["audit_status"], "approved")

    def test_model_cannot_bypass_read_and_review_before_saving(self):
        model = SequenceModel([
            decision("save_draft", {"asset_id": "M01", "proposed_at": AT_18})
        ])
        result = self.run_agent(model=model)
        self.assertNotEqual(result["status"], "completed")
        self.assert_not_saved(self.item(result))

    def test_tool_enforces_confirmation_even_when_model_claims_completion(self):
        self.catalog["schedule"] = [{"asset_id": "OTHER", "at": AT_18}]
        self.write_catalog()
        model = SequenceModel([
            decision("read_material", {"asset_id": "M01"}),
            decision("check_rules", {"asset_id": "M01"}),
            decision("query_slots", {"asset_id": "M01", "preferred_at": AT_19}),
            decision("save_draft", {"asset_id": "M01", "proposed_at": AT_19}),
        ])
        result = self.run_agent(model=model)
        item = self.item(result)
        self.assertEqual(result["status"], "pending_confirmation")
        self.assertEqual(item["requested_at"], AT_18)
        self.assertEqual(item["suggested_at"], AT_19)
        self.assertTrue(item["pending_confirmations"])

    def test_mock_matches_required_time_by_instant_across_timezone_offsets(self):
        # 11:00 UTC is the SECOND catalog slot, not the first free alternative.
        required_at = "2026-11-12T11:00:00+00:00"
        result = self.run_agent(self.request(required_at=required_at))
        item = self.item(result)
        self.assertEqual(datetime.fromisoformat(item["suggested_at"]), datetime.fromisoformat(required_at))
        self.assertEqual(result["status"], "completed")
        self.assertFalse(item["pending_confirmations"])

    def test_invalid_save_time_is_reported_without_crashing_result_reconstruction(self):
        result = self.run_agent(model=SequenceModel([
            decision("save_draft", {"asset_id": "M01", "proposed_at": []}),
        ]))
        self.assertEqual(result["status"], "failed")
        self.assert_not_saved(self.item(result))
        error_event = next(event for event in result["trace"] if event["action"] == "save_draft")
        self.assertFalse(error_event["observation"]["ok"])

    def test_invalid_save_after_success_preserves_committed_receipt(self):
        result = self.run_agent(model=SequenceModel([
            decision("read_material", {"asset_id": "M01"}),
            decision("check_rules", {"asset_id": "M01"}),
            decision("query_slots", {"asset_id": "M01", "preferred_at": AT_18}),
            decision("save_draft", {"asset_id": "M01", "proposed_at": AT_18}),
            decision("save_draft", {"asset_id": "M01", "proposed_at": "not-a-time"}),
        ]))
        item = self.item(result)
        saved = next(event["observation"]["draft"] for event in result["trace"]
                     if event["action"] == "save_draft" and event["observation"]["ok"])
        self.assertEqual(item["draft"]["status"], "saved")
        self.assertEqual(item["draft"]["id"], saved["id"])
        self.assertEqual(item["suggested_at"], AT_18)

    def test_success_at_original_time_clears_prior_failed_alternative_confirmation(self):
        result = self.run_agent(faults={"save_draft": 1}, model=SequenceModel([
            decision("read_material", {"asset_id": "M01"}),
            decision("check_rules", {"asset_id": "M01"}),
            decision("query_slots", {"asset_id": "M01", "preferred_at": AT_19}),
            decision("save_draft", {"asset_id": "M01", "proposed_at": AT_19}),
            decision("save_draft", {"asset_id": "M01", "proposed_at": AT_18}),
        ]))
        item = self.item(result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(item["draft"]["status"], "saved")
        self.assertEqual(item["suggested_at"], AT_18)
        self.assertEqual(item["pending_confirmations"], [])

    def test_save_recheck_risk_replaces_obsolete_approved_audit_in_result(self):
        fixture = self

        class ChangingCatalogToolBox(ToolBox):
            def execute(self, action, arguments):
                if action == "save_draft":
                    fixture.catalog["materials"]["M01"]["content"] = "本课程保证收益。"
                    fixture.write_catalog()
                return super().execute(action, arguments)

        tools = ChangingCatalogToolBox(self.request(), self.catalog_path, self.db_path)
        result = Agent(MockModel(), tools, max_steps=12).run()
        item = self.item(result)
        self.assertEqual(item["audit_status"], "blocked")
        self.assertIn("R01", json.dumps(item["risks"], ensure_ascii=False))
        self.assert_not_saved(item)
        save_event = next(event for event in result["trace"] if event["action"] == "save_draft")
        self.assertEqual(save_event["observation"]["error"]["code"], "audit_changed")

    def test_changed_occupancy_changes_observation_and_model_actions(self):
        normal = self.run_agent(self.request(request_id="free-first"))
        self.catalog["schedule"] = [{"asset_id": "OTHER", "at": AT_18}]
        self.write_catalog()
        conflict = self.run_agent(self.request(request_id="occupied-second"))
        self.assertEqual(self.item(normal)["suggested_at"], AT_18)
        self.assertNotEqual(self.item(conflict)["suggested_at"], AT_18)
        normal_queries = [event["arguments"] for event in normal["trace"] if event["action"] == "query_slots"]
        conflict_queries = [event["arguments"] for event in conflict["trace"] if event["action"] == "query_slots"]
        self.assertNotEqual(normal_queries, conflict_queries)

    def test_trace_contains_executed_tool_evidence_and_public_reasons(self):
        result = self.run_agent()
        tool_events = [event for event in result["trace"] if event["action"] != "finish"]
        self.assertTrue(tool_events)
        for event in tool_events:
            self.assertIn("step", event)
            self.assertIsInstance(event["arguments"], dict)
            self.assertIn("observation", event)
            self.assertTrue(event["reason"])

    def test_transient_read_failure_is_observed_then_retried(self):
        result = self.run_agent(faults={"read_material": 1})
        self.assertEqual(result["status"], "completed")
        reads = [event for event in result["trace"] if event["action"] == "read_material"]
        self.assertEqual(len(reads), 2)
        self.assertFalse(reads[0]["observation"]["ok"])
        self.assertTrue(reads[1]["observation"]["ok"])
        self.assertEqual(self.item(result)["draft"]["status"], "saved")

    def test_multiple_materials_preserve_success_when_another_needs_information(self):
        request = self.request()
        request["materials"].append(self.request("M02")["materials"][0])
        result = self.run_agent(request)
        items = {item["asset_id"]: item for item in result["items"]}
        self.assertEqual(result["status"], "needs_information")
        self.assertEqual(items["M01"]["draft"]["status"], "saved")
        self.assertEqual(items["M02"]["status"], "needs_information")
        self.assert_not_saved(items["M02"])

    def test_concurrent_duplicate_requests_create_one_logical_draft(self):
        # Independent ToolBox instances intentionally share only the SQLite database.
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.run_agent(), range(4)))
        drafts = [self.item(result)["draft"] for result in results]
        self.assertTrue(all(draft["status"] in {"saved", "existing"} for draft in drafts))
        self.assertEqual(len({draft["id"] for draft in drafts}), 1)
        self.assertEqual(sum(draft["status"] == "saved" for draft in drafts), 1)


if __name__ == "__main__":
    unittest.main()
