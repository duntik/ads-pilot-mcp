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

"""Tests for the safe GAQL builder used by the report tools."""

import unittest

from fastmcp.exceptions import ToolError

from ads_mcp.reports import gaql

INJECTIONS = [
    "1 OR 1=1",
    "1' OR '1'='1",
    "LAST_30_DAYS OR campaign.id > 0",
    "x; DROP",
    "LAST_30_DAYS'",
]


class TestGaql(unittest.TestCase):

    def test_customer_id(self):
        self.assertEqual(gaql.customer_id("123-456-7890"), "1234567890")
        self.assertEqual(gaql.customer_id(1234567890), "1234567890")
        for bad in ["", "abc", "1" * 13]:
            with self.subTest(bad=bad):
                with self.assertRaises(ToolError):
                    gaql.customer_id(bad)

    def test_id_value(self):
        self.assertEqual(gaql.id_value("campaign_id", " 42 "), "42")
        self.assertEqual(gaql.id_value("campaign_id", 42), "42")
        for bad in INJECTIONS + ["-1", "1.5", ""]:
            with self.subTest(bad=bad):
                with self.assertRaises(ToolError):
                    gaql.id_value("campaign_id", bad)

    def test_enum_value(self):
        self.assertEqual(gaql.enum_value("t", "keyword"), "KEYWORD")
        self.assertEqual(
            gaql.enum_value("t", "KEYWORD", {"KEYWORD"}), "KEYWORD"
        )
        with self.assertRaises(ToolError):
            gaql.enum_value("t", "SITELINK", {"KEYWORD"})
        for bad in INJECTIONS:
            with self.subTest(bad=bad):
                with self.assertRaises(ToolError):
                    gaql.enum_value("t", bad)

    def test_numbers(self):
        self.assertEqual(gaql.to_micros("min_cost", 12.345678), 12_345_678)
        self.assertEqual(gaql.to_micros("min_cost", 0), 0)
        for bad in [-1, float("nan"), float("inf"), "abc", None]:
            with self.subTest(bad=bad):
                with self.assertRaises(ToolError):
                    gaql.non_negative("min_cost", bad)

    def test_date_range_presets(self):
        self.assertEqual(
            gaql.date_range().condition(), "segments.date DURING LAST_30_DAYS"
        )
        self.assertEqual(
            gaql.date_range("last_7_days").condition("segments.date"),
            "segments.date DURING LAST_7_DAYS",
        )
        for bad in INJECTIONS + ["ALL_TIME", "CUSTOM"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ToolError):
                    gaql.date_range(bad)

    def test_date_range_custom(self):
        dr = gaql.date_range("CUSTOM", "2026-01-01", "2026-01-31")
        self.assertEqual(
            dr.condition(),
            "segments.date BETWEEN '2026-01-01' AND '2026-01-31'",
        )
        self.assertEqual(
            dr.to_dict(), {"start": "2026-01-01", "end": "2026-01-31"}
        )
        # Dates imply CUSTOM even if the preset was left at its default.
        self.assertIsNone(
            gaql.date_range("LAST_30_DAYS", "2026-01-01", "2026-01-02").preset
        )
        bad_pairs = [
            ("2026-01-01", None),
            ("2026-02-30", "2026-03-01"),
            ("2026-03-01", "2026-02-01"),
            ("2026-01-01' OR '1'='1", "2026-01-02"),
            ("20260101", "20260102"),
            ("2020-01-01", "2026-01-01"),
        ]
        for start, end in bad_pairs:
            with self.subTest(start=start, end=end):
                with self.assertRaises(ToolError):
                    gaql.date_range("CUSTOM", start, end)

    def test_build_query(self):
        q = gaql.build_query(
            ["campaign.id", "metrics.cost_micros"],
            "campaign",
            ["segments.date DURING LAST_7_DAYS", "metrics.impressions > 0"],
            ["metrics.cost_micros DESC"],
            limit=26,
        )
        self.assertEqual(
            q,
            "SELECT campaign.id, metrics.cost_micros FROM campaign WHERE "
            "segments.date DURING LAST_7_DAYS AND metrics.impressions > 0 "
            "ORDER BY metrics.cost_micros DESC LIMIT 26 "
            "PARAMETERS omit_unselected_resource_names=true",
        )
        with self.assertRaises(ValueError):
            gaql.build_query(["campaign.id, x"], "campaign")
        with self.assertRaises(ValueError):
            gaql.build_query(["campaign.id"], "campaign WHERE 1=1")

    def test_in_list(self):
        self.assertEqual(
            gaql.in_list("recommendation.type", ["A", "B"]),
            "recommendation.type IN ('A', 'B')",
        )
        with self.assertRaises(ValueError):
            gaql.in_list("x.y", [])


if __name__ == "__main__":
    unittest.main()
