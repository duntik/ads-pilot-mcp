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

"""Tests for ads_mcp.safety.undo and ads_mcp.safety.changes."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from ads_mcp.safety import undo
from ads_mcp.safety.changes import FieldChange

from . import fakes

CID = fakes.CUSTOMER_ID
CAMPAIGN_RN = f"customers/{CID}/campaigns/44"
BUDGET_RN = f"customers/{CID}/campaignBudgets/11"
CRITERION_RN = f"customers/{CID}/adGroupCriteria/22~33"


class TestFieldChange(unittest.TestCase):

    def test_reversible(self):
        self.assertTrue(
            FieldChange(
                "campaign", CAMPAIGN_RN, "status", "ENABLED", "PAUSED"
            ).reversible
        )
        # No "before" value captured.
        self.assertFalse(
            FieldChange(
                "campaign", CAMPAIGN_RN, "status", None, "PAUSED"
            ).reversible
        )
        # Unknown field.
        self.assertFalse(
            FieldChange("campaign", CAMPAIGN_RN, "name", "a", "b").reversible
        )
        # Resource name does not match the resource type.
        self.assertFalse(
            FieldChange(
                "campaign", BUDGET_RN, "status", "ENABLED", "PAUSED"
            ).reversible
        )
        self.assertFalse(
            FieldChange(
                "ad_group_criterion",
                f"customers/{CID}/adGroupCriteria/33",
                "status",
                "ENABLED",
                "PAUSED",
            ).reversible
        )

    def test_round_trip_and_reverse(self):
        fc = FieldChange("campaign_budget", BUDGET_RN, "amount_micros", 1, 2)
        self.assertEqual(FieldChange.from_dict(fc.to_dict()), fc)
        self.assertEqual(fc.reversed().before, 2)
        self.assertEqual(fc.reversed().after, 1)


class TestUndoStore(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = undo.UndoStore(os.path.join(self.dir, "undo"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def record(self, change_id=None, customer_id=CID):
        return undo.ChangeRecord(
            change_id=change_id or undo.new_change_id(),
            created_at="2026-01-01T00:00:00Z",
            customer_id=customer_id,
            tool="test_tool",
            field_changes=[
                FieldChange(
                    "campaign", CAMPAIGN_RN, "status", "ENABLED", "PAUSED"
                )
            ],
        )

    def test_new_change_id_format(self):
        cid = undo.new_change_id()
        self.assertRegex(cid, undo.CHANGE_ID_PATTERN)
        self.assertNotEqual(cid, undo.new_change_id())

    def test_save_load_list(self):
        a = self.record("20260101T000000Z-00000001")
        b = self.record("20260102T000000Z-00000002", customer_id="1112223333")
        self.store.save(a)
        self.store.save(b)
        loaded = self.store.load(a.change_id)
        self.assertEqual(loaded.to_dict(), a.to_dict())
        self.assertTrue(loaded.reversible)
        self.assertEqual(
            [r.change_id for r in self.store.list()],
            [b.change_id, a.change_id],
        )
        self.assertEqual(
            [r.change_id for r in self.store.list(customer_id=CID)],
            [a.change_id],
        )
        self.assertEqual(len(self.store.list(limit=1)), 1)
        self.assertIsNone(self.store.load("20260103T000000Z-00000003"))

    def test_update_in_place(self):
        rec = self.record()
        self.store.save(rec)
        rec.undone_by = "20260109T000000Z-0000000f"
        self.store.save(rec)
        self.assertEqual(
            self.store.load(rec.change_id).undone_by, rec.undone_by
        )
        leftovers = [
            n for n in os.listdir(self.store.directory) if n.endswith(".tmp")
        ]
        self.assertEqual(leftovers, [])

    def test_invalid_ids_rejected(self):
        for bad in ["../../etc/passwd", "", "20260101T000000Z-zzzz", "a/b"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.store.load(bad)

    def test_list_empty_dir(self):
        self.assertEqual(self.store.list(), [])


class TestBuildOperations(unittest.TestCase):

    def setUp(self):
        self.client = fakes.real_client()

    def test_status_budget_and_bid_operations(self):
        ops = undo.build_mutate_operations(
            self.client,
            [
                FieldChange(
                    "campaign", CAMPAIGN_RN, "status", "PAUSED", "ENABLED"
                ),
                FieldChange(
                    "campaign",
                    CAMPAIGN_RN,
                    "target_roas.target_roas",
                    3.0,
                    2.5,
                ),
                FieldChange(
                    "campaign_budget", BUDGET_RN, "amount_micros", 5, 10_000_000
                ),
                FieldChange(
                    "ad_group_criterion",
                    CRITERION_RN,
                    "cpc_bid_micros",
                    2_000_000,
                    1_500_000,
                ),
            ],
        )
        # Two changes to the same campaign merge into one operation.
        self.assertEqual(len(ops), 3)
        campaign_op = ops[0].campaign_operation
        self.assertEqual(campaign_op.update.resource_name, CAMPAIGN_RN)
        self.assertEqual(campaign_op.update.status.name, "ENABLED")
        self.assertAlmostEqual(campaign_op.update.target_roas.target_roas, 2.5)
        self.assertEqual(
            list(campaign_op.update_mask.paths),
            ["status", "target_roas.target_roas"],
        )
        budget_op = ops[1].campaign_budget_operation
        self.assertEqual(budget_op.update.amount_micros, 10_000_000)
        self.assertEqual(list(budget_op.update_mask.paths), ["amount_micros"])
        crit_op = ops[2].ad_group_criterion_operation
        self.assertEqual(crit_op.update.cpc_bid_micros, 1_500_000)

    def test_all_statuses_map_to_enums(self):
        for rtype, rn in [
            ("ad_group", f"customers/{CID}/adGroups/1"),
            ("ad_group_ad", f"customers/{CID}/adGroupAds/1~2"),
            ("ad_group_criterion", CRITERION_RN),
        ]:
            with self.subTest(rtype=rtype):
                ops = undo.build_mutate_operations(
                    self.client,
                    [FieldChange(rtype, rn, "status", "ENABLED", "PAUSED")],
                )
                self.assertEqual(len(ops), 1)

    def test_rejects_unreversible_and_bad_enum(self):
        with self.assertRaises(ValueError):
            undo.build_mutate_operations(
                self.client,
                [FieldChange("campaign", CAMPAIGN_RN, "name", "a", "b")],
            )
        with self.assertRaises(ValueError):
            undo.build_mutate_operations(
                self.client,
                [FieldChange("campaign", CAMPAIGN_RN, "status", "X", "NOPE")],
            )


class TestCurrentValues(unittest.TestCase):

    def test_fetch_and_drift(self):
        service = fakes.FakeAdsService(
            [
                (
                    "FROM campaign ",
                    [
                        fakes.row(
                            campaign={
                                "resource_name": CAMPAIGN_RN,
                                "status": "PAUSED",
                            }
                        )
                    ],
                ),
                (
                    "FROM campaign_budget",
                    [
                        fakes.row(
                            campaign_budget={
                                "resource_name": BUDGET_RN,
                                "amount_micros": 7_000_000,
                            }
                        )
                    ],
                ),
            ]
        )
        changes = [
            FieldChange("campaign", CAMPAIGN_RN, "status", "ENABLED", "PAUSED"),
            FieldChange(
                "campaign_budget",
                BUDGET_RN,
                "amount_micros",
                5_000_000,
                6_000_000,
            ),
            FieldChange(
                "campaign",
                f"customers/{CID}/campaigns/999",
                "status",
                "ENABLED",
                "PAUSED",
            ),
        ]
        with patch("ads_mcp.utils.get_googleads_service", return_value=service):
            current = undo.read_current_values(CID, changes)
        self.assertEqual(current[(CAMPAIGN_RN, "status")], "PAUSED")
        self.assertEqual(current[(BUDGET_RN, "amount_micros")], 7_000_000)
        self.assertIs(
            current[(f"customers/{CID}/campaigns/999", "status")], undo.MISSING
        )
        query = service.query_matching("FROM campaign ")
        self.assertIn("campaign.status", query)
        self.assertIn(f"'{CAMPAIGN_RN}'", query)

        drift = undo.find_drift(changes, current)
        self.assertEqual(
            len(drift), 2
        )  # budget changed again, campaign 999 gone
        self.assertTrue(any("7000000" in d for d in drift))
        self.assertTrue(any("not found" in d for d in drift))

    def test_fetch_validates_inputs(self):
        with self.assertRaises(ValueError):
            undo.fetch_current_values(CID, "campaign", [CAMPAIGN_RN], ["name"])
        with self.assertRaises(ValueError):
            undo.fetch_current_values(
                CID, "campaign", ["customers/1/campaigns/1' OR 1=1"], ["status"]
            )
        with self.assertRaises(ValueError):
            # Belongs to another customer.
            undo.fetch_current_values(
                CID,
                "campaign",
                ["customers/1112223333/campaigns/1"],
                ["status"],
            )
        with self.assertRaises(ValueError):
            undo.fetch_current_values(CID, "bogus", [], [])
        self.assertEqual(
            undo.fetch_current_values(CID, "campaign", [], ["status"]), {}
        )

    def test_values_equal(self):
        self.assertTrue(undo.values_equal(3.0, 3.0000001))
        self.assertTrue(undo.values_equal(5_000_000, 5_000_000))
        self.assertFalse(undo.values_equal(5_000_000, 5_100_000))
        self.assertTrue(undo.values_equal("PAUSED", "PAUSED"))
        self.assertFalse(undo.values_equal(True, 1.5))


if __name__ == "__main__":
    unittest.main()
