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

"""Tests for the safety namespace tools (undo_change, list_changes, ...)."""

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError

from ads_mcp import propose_and_confirm as pac
from ads_mcp.safety import hooks
from ads_mcp.safety.changes import FieldChange
from ads_mcp.safety.undo import ChangeRecord, UndoStore
from ads_mcp.tools.pilot import safety_tools

from . import fakes

CID = fakes.CUSTOMER_ID
CAMPAIGN_RN = f"customers/{CID}/campaigns/44"
BUDGET_RN = f"customers/{CID}/campaignBudgets/11"
CHANGE_ID = "20260101T120000Z-0a0b0c0d"


class TestSafetyTools(fakes.TempStateMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self._previous_hook = pac._safety_hook
        hooks.install(hooks.SafetyHook())
        self.store = UndoStore(os.path.join(self.state_dir, "undo"))
        self.store.save(
            ChangeRecord(
                change_id=CHANGE_ID,
                created_at="2026-01-01T12:00:00Z",
                customer_id=CID,
                login_customer_id="9998887777",
                tool="set_campaign_status",
                field_changes=[
                    FieldChange(
                        "campaign", CAMPAIGN_RN, "status", "ENABLED", "PAUSED"
                    ),
                    FieldChange(
                        "campaign_budget",
                        BUDGET_RN,
                        "amount_micros",
                        10_000_000,
                        12_000_000,
                    ),
                ],
            )
        )
        self.current_status = "PAUSED"
        self.current_budget = 12_000_000
        self.ads_service = fakes.FakeAdsService(
            [
                ("FROM campaign ", self._campaign_rows),
                ("FROM campaign_budget", self._budget_rows),
            ]
        )
        self.client = MagicMock()
        self.client.get_type.side_effect = fakes.real_client().get_type
        self.client.get_service.return_value = self.ads_service
        self.patches = [
            patch(
                "ads_mcp.utils.get_googleads_service",
                return_value=self.ads_service,
            ),
            patch(
                "ads_mcp.utils.get_googleads_client", return_value=self.client
            ),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        pac.set_safety_hook(self._previous_hook)
        super().tearDown()

    def _campaign_rows(self, query):
        return [
            fakes.row(
                campaign={
                    "resource_name": CAMPAIGN_RN,
                    "status": self.current_status,
                }
            )
        ]

    def _budget_rows(self, query):
        return [
            fakes.row(
                campaign_budget={
                    "resource_name": BUDGET_RN,
                    "amount_micros": self.current_budget,
                }
            )
        ]

    def undo(self, **kw):
        kw.setdefault("change_id", CHANGE_ID)
        return safety_tools.undo_change(**kw)

    def test_propose_then_confirm(self):
        proposal = json.loads(self.undo())
        self.assertEqual(proposal["status"], "PROPOSED")
        fields = {i["field_readable"]: i for i in proposal["change_items"]}
        self.assertEqual(fields["status"]["value_readable"], "ENABLED")
        self.assertEqual(fields["status"]["original_value_readable"], "PAUSED")
        self.assertEqual(fields["amount_micros"]["value_readable"], "10000000")

        # The proposal validated the reverse operations with the API.
        request = self.ads_service.mutate.call_args.kwargs["request"]
        self.assertEqual(request.customer_id, CID)
        self.assertTrue(request.validate_only)
        ops = list(request.mutate_operations)
        self.assertEqual(len(ops), 2)
        self.assertEqual(
            ops[0].campaign_operation.update.status.name, "ENABLED"
        )
        self.assertEqual(
            ops[1].campaign_budget_operation.update.amount_micros, 10_000_000
        )
        # The login customer ID of the original change is reused.
        from ads_mcp import utils

        utils.get_googleads_client.assert_called_with(
            login_customer_id="9998887777"
        )
        self.assertEqual(self.store.load(CHANGE_ID).undone_by, None)

        self.ads_service.mutate.return_value = MagicMock(
            mutate_operation_responses=[1, 2]
        )
        result = safety_tools.undo_change(
            change_id=CHANGE_ID,
            validate_only=False,
            checksum=proposal["checksum"],
        )
        self.assertIn("Reverted change", result)
        self.assertIn("2 resource(s)", result)
        self.assertFalse(
            self.ads_service.mutate.call_args.kwargs["request"].validate_only
        )
        new_id = result.split("change_id: ")[1].split(" ")[0]
        self.assertEqual(self.store.load(CHANGE_ID).undone_by, new_id)
        # The undo is itself recorded, so it can be undone ("redo").
        redo = self.store.load(new_id)
        self.assertEqual(redo.undo_of, CHANGE_ID)
        self.assertEqual(redo.field_changes[0].after, "ENABLED")

        with self.assertRaisesRegex(ToolError, "already reverted"):
            self.undo()

    def test_drift_is_refused_unless_forced(self):
        self.current_budget = 15_000_000  # someone changed it again
        with self.assertRaisesRegex(ToolError, "changed after this change"):
            self.undo()
        self.ads_service.mutate.assert_not_called()
        proposal = json.loads(self.undo(force=True))
        budget_item = [
            i
            for i in proposal["change_items"]
            if i["field_readable"] == "amount_micros"
        ][0]
        self.assertEqual(budget_item["original_value_readable"], "15000000")

    def test_missing_resource(self):
        self.ads_service.responders = [
            ("FROM campaign_budget", self._budget_rows)
        ]
        with self.assertRaisesRegex(ToolError, "no longer exist"):
            self.undo(force=True)

    def test_policy_applies_to_undo(self):
        self.use_settings(max_bid_change_pct=1, max_daily_budget=5)
        with self.assertRaisesRegex(ToolError, "max_daily_budget"):
            self.undo()

    def test_read_only_blocks_undo(self):
        self.use_settings(read_only=True)
        with self.assertRaisesRegex(ToolError, "read-only"):
            self.undo()
        self.ads_service.mutate.assert_not_called()

    def test_unknown_and_invalid_ids(self):
        with self.assertRaisesRegex(ToolError, "No reversible change"):
            self.undo(change_id="20260101T120000Z-ffffffff")
        with self.assertRaisesRegex(ToolError, "Invalid change_id"):
            self.undo(change_id="../../secrets")

    def test_list_changes(self):
        changes = safety_tools.list_changes()
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["change_id"], CHANGE_ID)
        self.assertIn("'ENABLED' -> 'PAUSED'", changes[0]["changes"][0])
        self.assertEqual(
            safety_tools.list_changes(customer_id="1112223333"), []
        )

    def test_get_safety_settings(self):
        self.use_settings(max_cpc=3)
        data = safety_tools.get_safety_settings()
        self.assertEqual(data["limits"]["max_cpc"], 3.0)
        self.assertIn("audit_log_path", data)
        with patch("ads_mcp.utils.is_remote_oauth_mode", return_value=True):
            remote = safety_tools.get_safety_settings()
        self.assertNotIn("audit_log_path", remote)
        self.assertNotIn("undo_dir", remote)


if __name__ == "__main__":
    unittest.main()
