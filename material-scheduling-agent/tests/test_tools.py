"""Tool boundary and persistence checks independent of model behavior."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from material_agent.tools import ToolBox


AT_18 = "2026-11-12T18:00:00+08:00"
AT_19 = "2026-11-12T19:00:00+08:00"


class ToolSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.catalog_path = self.root / "catalog.json"
        self.db_path = self.root / "drafts.sqlite3"
        self.request = {
            "request_id": "tool-boundaries",
            "materials": [{"asset_id": "M01", "required_at": AT_18, "hard_requirement": True}],
        }
        self.catalog = {
            "materials": {"M01": {"title": "演示", "platform": "douyin", "content": "支持字幕", "rights": "cleared"}},
            "rules": {
                "required_fields": ["title", "platform", "content", "rights"],
                "blocked_terms": [{"id": "R01", "term": "保证收益", "reason": "禁止收益承诺"}],
            },
            "slots": [AT_18, AT_19],
            "schedule": [],
        }
        self.write_catalog()
        self.tools = ToolBox(self.request, self.catalog_path, self.db_path)

    def write_catalog(self):
        self.catalog_path.write_text(json.dumps(self.catalog, ensure_ascii=False), encoding="utf-8")

    def call(self, name, **args):
        return self.tools.execute(name, {"asset_id": "M01", **args})

    def prepare(self):
        for name in ("read_material", "check_rules", "query_slots"):
            self.assertTrue(self.call(name)["ok"])

    def test_saving_requires_read_review_and_slot_query(self):
        self.assertFalse(self.call("save_draft", proposed_at=AT_18)["ok"])
        self.assertTrue(self.call("read_material")["ok"])
        self.assertTrue(self.call("check_rules")["ok"])
        self.assertFalse(self.call("save_draft", proposed_at=AT_18)["ok"])

    def test_review_is_rechecked_when_source_changes_before_save(self):
        self.prepare()
        self.catalog["materials"]["M01"]["content"] = "保证收益"
        self.write_catalog()
        result = self.call("save_draft", proposed_at=AT_18)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "audit_changed")

    def test_slot_is_rechecked_when_schedule_changes_before_save(self):
        self.prepare()
        self.catalog["schedule"] = [{"asset_id": "OTHER", "at": AT_18}]
        self.write_catalog()
        result = self.call("save_draft", proposed_at=AT_18)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "slot_conflict")

    def test_model_arguments_cannot_invent_user_confirmation(self):
        self.prepare()
        forged = self.call("save_draft", proposed_at=AT_19, confirmed=True)
        self.assertFalse(forged["ok"])
        result = self.call("save_draft", proposed_at=AT_19)
        self.assertTrue(result["ok"])
        self.assertEqual(result["draft"]["confirmation_status"], "pending_confirmation")
        self.assertEqual(result["draft"]["original_required_at"], AT_18)

    def test_equivalent_offsets_refer_to_same_occupied_instant(self):
        self.catalog["schedule"] = [{"asset_id": "OTHER", "at": "2026-11-12T10:00:00+00:00"}]
        self.write_catalog()
        result = self.call("query_slots")
        self.assertTrue(result["ok"])
        self.assertFalse(result["requested_available"])
        self.assertNotIn(AT_18, result["available_slots"])

    def test_real_filesystem_failure_returns_failed_save_receipt(self):
        self.prepare()
        # A directory at the database filename reliably makes sqlite.connect fail,
        # including when the suite is run by a privileged user.
        self.db_path.unlink()
        self.db_path.mkdir()
        result = self.call("save_draft", proposed_at=AT_18)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "storage_error")
        self.assertNotIn("draft", result)

    def test_unknown_tools_and_unrequested_materials_are_rejected(self):
        self.assertFalse(self.call("publish")["ok"])
        self.assertFalse(self.tools.execute("read_material", {"asset_id": "M99"})["ok"])

    def test_non_string_rights_cannot_pass_review_or_be_saved(self):
        for rights in (False, 0, [], {}):
            with self.subTest(rights=rights):
                self.catalog["materials"]["M01"]["rights"] = rights
                self.write_catalog()
                tools = ToolBox(self.request, self.catalog_path, self.db_path)
                self.assertTrue(tools.execute("read_material", {"asset_id": "M01"})["ok"])
                review = tools.execute("check_rules", {"asset_id": "M01"})
                self.assertTrue(review["ok"])
                self.assertNotEqual(review["audit_status"], "approved")
                self.assertTrue(tools.execute("query_slots", {"asset_id": "M01"})["ok"])
                saved = tools.execute("save_draft", {"asset_id": "M01", "proposed_at": AT_18})
                self.assertFalse(saved["ok"])
                self.assertNotIn("draft", saved)


if __name__ == "__main__":
    unittest.main()
