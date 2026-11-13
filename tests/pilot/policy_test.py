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

"""Tests for ads_mcp.safety.policy."""

import unittest

from ads_mcp.safety import policy
from ads_mcp.safety.changes import FieldChange
from ads_mcp.safety.settings import load_settings

CID = "1234567890"
BUDGET_RN = f"customers/{CID}/campaignBudgets/11"
AD_GROUP_RN = f"customers/{CID}/adGroups/22"
CRITERION_RN = f"customers/{CID}/adGroupCriteria/22~33"
CAMPAIGN_RN = f"customers/{CID}/campaigns/44"


def budget(before, after):
    return FieldChange(
        "campaign_budget", BUDGET_RN, "amount_micros", before, after
    )


class TestPolicy(unittest.TestCase):

    def settings(self, **section):
        return load_settings(section=section, env={})

    def test_allow_list(self):
        cfg = self.settings(allowed_customer_ids=["1112223333"])
        self.assertEqual(policy.evaluate(cfg, "111-222-3333", 1), [])
        violations = policy.evaluate(cfg, CID, 1)
        self.assertEqual(len(violations), 1)
        self.assertIn("allow-list", violations[0])

    def test_operation_count(self):
        cfg = self.settings(max_operations_per_mutate=3)
        self.assertEqual(policy.evaluate(cfg, CID, 3), [])
        self.assertIn(
            "max_operations_per_mutate", policy.evaluate(cfg, CID, 4)[0]
        )
        unlimited = self.settings(max_operations_per_mutate=None)
        self.assertEqual(policy.evaluate(unlimited, CID, 10_000), [])

    def test_budget_increase_pct(self):
        cfg = self.settings(max_budget_increase_pct=50)
        # +50 % is allowed, +60 % is not; decreases are always allowed.
        self.assertEqual(
            policy.evaluate(cfg, CID, 1, [budget(10_000_000, 15_000_000)]), []
        )
        violations = policy.evaluate(
            cfg, CID, 1, [budget(10_000_000, 16_000_000)]
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("60.0%", violations[0])
        self.assertIn("max_budget_increase_pct", violations[0])
        self.assertEqual(
            policy.evaluate(cfg, CID, 1, [budget(10_000_000, 1_000_000)]), []
        )

    def test_max_daily_budget(self):
        cfg = self.settings(max_daily_budget=100, max_budget_increase_pct=None)
        self.assertEqual(
            policy.evaluate(cfg, CID, 1, [budget(50_000_000, 100_000_000)]), []
        )
        violations = policy.evaluate(
            cfg, CID, 1, [budget(50_000_000, 100_010_000)]
        )
        self.assertIn("max_daily_budget", violations[0])
        # A new budget (no "before") is still capped by the absolute limit.
        self.assertTrue(
            policy.evaluate(cfg, CID, 1, [budget(None, 200_000_000)])
        )

    def test_cpc_limits(self):
        cfg = self.settings(max_cpc=2, max_bid_change_pct=25)
        ok = FieldChange(
            "ad_group_criterion",
            CRITERION_RN,
            "cpc_bid_micros",
            1_000_000,
            1_250_000,
        )
        self.assertEqual(policy.evaluate(cfg, CID, 1, [ok]), [])
        too_high = FieldChange(
            "ad_group", AD_GROUP_RN, "cpc_bid_micros", 1_900_000, 2_100_000
        )
        self.assertIn("max_cpc", policy.evaluate(cfg, CID, 1, [too_high])[0])
        big_cut = FieldChange(
            "ad_group", AD_GROUP_RN, "cpc_bid_micros", 2_000_000, 1_000_000
        )
        violations = policy.evaluate(cfg, CID, 1, [big_cut])
        self.assertIn("-50.0%", violations[0])
        self.assertIn("max_bid_change_pct", violations[0])

    def test_bidding_targets(self):
        cfg = self.settings(max_bid_change_pct=20)
        tcpa = FieldChange(
            "campaign",
            CAMPAIGN_RN,
            "maximize_conversions.target_cpa_micros",
            10_000_000,
            13_000_000,
        )
        self.assertTrue(policy.evaluate(cfg, CID, 1, [tcpa]))
        troas = FieldChange(
            "campaign", CAMPAIGN_RN, "target_roas.target_roas", 4.0, 4.4
        )
        self.assertEqual(policy.evaluate(cfg, CID, 1, [troas]), [])
        troas_big = FieldChange(
            "campaign", CAMPAIGN_RN, "target_roas.target_roas", 4.0, 2.0
        )
        self.assertIn("4 -> 2", policy.evaluate(cfg, CID, 1, [troas_big])[0])

    def test_status_and_unknown_fields_ignored(self):
        cfg = self.settings(max_bid_change_pct=1, max_budget_increase_pct=1)
        status = FieldChange(
            "campaign", CAMPAIGN_RN, "status", "ENABLED", "PAUSED"
        )
        unknown = FieldChange("campaign", CAMPAIGN_RN, "name", "a", "b")
        self.assertEqual(policy.evaluate(cfg, CID, 1, [status, unknown]), [])

    def test_customer_override(self):
        cfg = self.settings(
            max_cpc=1,
            customer_overrides={"1112223333": {"max_cpc": 10}},
        )
        bid = FieldChange(
            "ad_group",
            "customers/1112223333/adGroups/1",
            "cpc_bid_micros",
            4_000_000,
            5_000_000,
        )
        self.assertEqual(policy.evaluate(cfg, "1112223333", 1, [bid]), [])
        self.assertTrue(policy.evaluate(cfg, CID, 1, [bid]))

    def test_multiple_violations_are_all_reported(self):
        cfg = self.settings(
            allowed_customer_ids=["1112223333"],
            max_operations_per_mutate=1,
            max_budget_increase_pct=10,
        )
        violations = policy.evaluate(
            cfg, CID, 2, [budget(10_000_000, 20_000_000)]
        )
        self.assertEqual(len(violations), 3)


if __name__ == "__main__":
    unittest.main()
