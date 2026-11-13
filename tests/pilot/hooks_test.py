# Copyright 2026 ads-pilot-mcp contributors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the safety hook inside propose_and_confirm."""

import json
import unittest
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError

from ads_mcp import propose_and_confirm as pac
from ads_mcp.safety import audit_log, hooks
from ads_mcp.safety.changes import FieldChange
from ads_mcp.safety.undo import UndoStore
from ads_mcp.tools import labels

from . import fakes

CID = fakes.CUSTOMER_ID
BUDGET_RN = f"customers/{CID}/campaignBudgets/11"
ITEMS = [pac.ChangeItem(operation="UPDATE", entity_readable="Thing")]


class HookTestBase(fakes.TempStateMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self._previous_hook = pac._safety_hook
        hooks.install(hooks.SafetyHook())

    def tearDown(self):
        pac.set_safety_hook(self._previous_hook)
        if self._previous_hook is None:
            hooks.uninstall()
        super().tearDown()

    def audit_entries(self):
        return list(audit_log.AuditLog(self.audit_path).read())


class TestHookWithUpstreamStyleCalls(HookTestBase):
    """Upstream tools call pac.propose_and_confirm directly."""

    def call(self, validate_only, checksum=None, commit=None, payload=None):
        return pac.propose_and_confirm(
            customer_id=CID,
            validate_only=validate_only,
            checksum=checksum,
            operation_payload=payload or {"tool": "manage_labels", "name": "x"},
            change_items=ITEMS,
            validate_fn=lambda: None,
            commit_fn=commit or (lambda: "Successfully updated label. ID: 1"),
        )

    def test_without_hook_behaviour_is_unchanged(self):
        hooks.uninstall()
        proposal = json.loads(self.call(True))
        result = self.call(False, proposal["checksum"])
        self.assertEqual(result, "Successfully updated label. ID: 1")
        self.assertEqual(self.audit_entries(), [])

    def test_commit_is_audited_and_result_unchanged(self):
        proposal = json.loads(self.call(True))
        self.assertEqual(proposal["status"], "PROPOSED")
        self.assertEqual(self.audit_entries(), [])  # proposals are not logged
        result = self.call(False, proposal["checksum"])
        self.assertEqual(result, "Successfully updated label. ID: 1")
        entries = self.audit_entries()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["event"], audit_log.COMMITTED)
        self.assertEqual(entry["tool"], "manage_labels")
        self.assertEqual(entry["customer_id"], CID)
        self.assertEqual(entry["operation_count"], 1)
        self.assertEqual(entry["operations"][0]["entity_readable"], "Thing")
        self.assertIn("change_id", entry)
        self.assertEqual(entry["result"], "Successfully updated label. ID: 1")

    def test_failed_commit_is_audited(self):
        proposal = json.loads(self.call(True))

        def boom():
            raise RuntimeError("API exploded")

        with self.assertRaises(ToolError):
            self.call(False, proposal["checksum"], commit=boom)
        entry = self.audit_entries()[0]
        self.assertEqual(entry["event"], audit_log.FAILED)
        self.assertIn("API exploded", entry["error"])

    def test_read_only_blocks_both_phases(self):
        self.use_settings(read_only=True)
        commit = MagicMock(return_value="done")
        with self.assertRaisesRegex(ToolError, "read-only"):
            self.call(True)
        with self.assertRaisesRegex(ToolError, "read-only"):
            self.call(False, "0" * 64, commit=commit)
        commit.assert_not_called()
        events = [(e["event"], e["phase"]) for e in self.audit_entries()]
        self.assertEqual(
            events, [("BLOCKED", "propose"), ("BLOCKED", "confirm")]
        )

    def test_allow_list_blocks(self):
        self.use_settings(allowed_customer_ids=["1112223333"])
        with self.assertRaisesRegex(ToolError, "allow-list"):
            self.call(True)

    def test_operation_count_from_payload(self):
        self.use_settings(max_operations_per_mutate=2)
        with self.assertRaisesRegex(ToolError, "max_operations_per_mutate"):
            self.call(True, payload={"tool": "bulk", "operations": [1, 2, 3]})

    def test_audit_disabled(self):
        self.use_settings(audit_log=False)
        proposal = json.loads(self.call(True))
        self.call(False, proposal["checksum"])
        self.assertEqual(self.audit_entries(), [])

    def test_upstream_labels_tool_is_guarded(self):
        """The upstream manage_labels tool is blocked in read-only mode."""
        self.use_settings(read_only=True)
        mock_client = MagicMock()
        mock_client.get_type.side_effect = fakes.real_client().get_type
        label_service = MagicMock()
        mock_client.get_service.return_value = label_service
        with patch(
            "ads_mcp.utils.get_googleads_client", return_value=mock_client
        ):
            with self.assertRaisesRegex(ToolError, "read-only"):
                labels.manage_labels(
                    customer_id=CID, operation="CREATE", name="Promo"
                )
        label_service.mutate_labels.assert_not_called()


class TestGuardedPropose(HookTestBase):
    """ads-pilot-mcp tools use guarded_propose_and_confirm."""

    def budget_change(self, before=10_000_000, after=12_000_000):
        return [
            FieldChange(
                "campaign_budget", BUDGET_RN, "amount_micros", before, after
            )
        ]

    def call(
        self, validate_only, checksum=None, changes=None, commit=None, **kw
    ):
        return hooks.guarded_propose_and_confirm(
            tool="set_budget",
            customer_id=CID,
            validate_only=validate_only,
            checksum=checksum,
            operation_payload={"budget": BUDGET_RN, "amount": 12},
            change_items=ITEMS,
            commit_fn=commit or (lambda: "Budget updated."),
            validate_fn=lambda: None,
            field_changes=(
                changes if changes is not None else self.budget_change()
            ),
            login_customer_id="999-888-7777",
            **kw,
        )

    def test_commit_stores_undo_record(self):
        proposal = json.loads(self.call(True))
        result = self.call(False, proposal["checksum"])
        self.assertIn("Budget updated.", result)
        self.assertIn("can be reverted with undo_change", result)
        change_id = result.split("change_id: ")[1].split(" ")[0]

        record = UndoStore(self.state_dir + "/undo").load(change_id)
        self.assertIsNotNone(record)
        self.assertEqual(record.customer_id, CID)
        self.assertEqual(record.login_customer_id, "9998887777")
        self.assertEqual(record.tool, "set_budget")
        self.assertEqual(record.field_changes[0].before, 10_000_000)

        entry = self.audit_entries()[0]
        self.assertEqual(entry["change_id"], change_id)
        self.assertTrue(entry["reversible"])
        self.assertEqual(entry["field_changes"][0]["after"], 12_000_000)

    def test_before_values_are_bound_to_checksum(self):
        proposal = json.loads(self.call(True))
        # The account changed between propose and confirm.
        with self.assertRaisesRegex(ToolError, "checksum"):
            self.call(
                False,
                proposal["checksum"],
                changes=self.budget_change(before=11_000_000),
            )

    def test_budget_limit_blocks_proposal(self):
        self.use_settings(max_budget_increase_pct=10)
        with self.assertRaisesRegex(ToolError, "max_budget_increase_pct"):
            self.call(True)
        entry = self.audit_entries()[0]
        self.assertEqual(entry["event"], "BLOCKED")
        self.assertEqual(entry["tool"], "set_budget")
        self.assertEqual(entry["field_changes"][0]["before"], 10_000_000)

    def test_non_reversible_change_is_not_stored(self):
        changes = [
            FieldChange(
                "campaign", f"customers/{CID}/campaigns/1", "name", "a", "b"
            )
        ]
        proposal = json.loads(self.call(True, changes=changes))
        result = self.call(False, proposal["checksum"], changes=changes)
        self.assertNotIn("undo_change", result)
        self.assertFalse(self.audit_entries()[0]["reversible"])
        self.assertEqual(UndoStore(self.state_dir + "/undo").list(), [])

    def test_on_committed_callback(self):
        seen = []
        proposal = json.loads(self.call(True, on_committed=seen.append))
        self.call(False, proposal["checksum"], on_committed=seen.append)
        self.assertEqual(len(seen), 1)

    def test_failing_callback_does_not_hide_success(self):
        def broken(change_id):
            raise OSError("disk full")

        proposal = json.loads(self.call(True, on_committed=broken))
        with self.assertLogs(hooks.logger, level="ERROR"):
            result = self.call(False, proposal["checksum"], on_committed=broken)
        self.assertIn("Budget updated.", result)
        self.assertEqual(self.audit_entries()[0]["event"], "COMMITTED")

    def test_context_is_reset_after_call(self):
        self.call(True)
        self.assertIsNone(hooks.current_context())

    def test_installs_hook_if_missing(self):
        hooks.uninstall()
        self.call(True)
        self.assertIsInstance(pac._safety_hook, hooks.SafetyHook)


if __name__ == "__main__":
    unittest.main()
