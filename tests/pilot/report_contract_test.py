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

"""Contract tests for the reports namespace.

* every GAQL field used by a report exists in the Google Ads API protos of
  the client library version in use;
* every report tool is annotated read-only and documents its parameters;
* a full page of output stays small enough for an LLM context (the offline
  counterpart of the upstream token-usage smoke test).
"""

import inspect
import json
import unittest
from unittest.mock import patch

from google.ads.googleads.v25.services.types.google_ads_service import (
    GoogleAdsRow,
)

from ads_mcp.reports import (
    account,
    budget,
    common,
    performance,
    reports_mcp,
    search_terms,
)
from ads_mcp.safety import changes

from . import fakes

# Roughly 4 characters per token; a 25-row page must stay under ~6k tokens.
MAX_PAGE_CHARS = 24_000


def field_exists(path: str) -> bool:
    descriptor = GoogleAdsRow.pb().DESCRIPTOR
    for part in path.split("."):
        field = descriptor.fields_by_name.get(
            part
        ) or descriptor.fields_by_name.get(part + "_")
        if field is None:
            return False
        descriptor = field.message_type
        if descriptor is None:
            return True
    return True


def all_columns():
    for module in (account, budget, common, performance, search_terms):
        for name, value in vars(module).items():
            if (
                isinstance(value, tuple)
                and value
                and isinstance(value[0], common.Column)
            ):
                for col in value:
                    yield f"{module.__name__}.{name}", col.field


class TestReportContract(unittest.IsolatedAsyncioTestCase):

    def test_all_report_fields_exist_in_api(self):
        missing = [
            f"{where}: {field}"
            for where, field in all_columns()
            if not field_exists(field)
        ]
        extra_fields = [
            "landing_page_view.unexpanded_final_url",
            "expanded_landing_page_view.expanded_final_url",
            "segments.geo_target_region",
            "segments.geo_target_city",
            "segments.geo_target_most_specific_location",
            "geographic_view.location_type",
            "user_location_view.targeting_location",
            "metrics.mobile_friendly_clicks_percentage",
            "metrics.speed_score",
            "geo_target_constant.canonical_name",
            "segments.conversion_action",
            "metrics.all_conversions",
            "change_event.old_resource",
            "change_event.changed_fields",
            "recommendation.impact.base_metrics.cost_micros",
            "recommendation.impact.potential_metrics.conversions",
            "customer.currency_code",
            "customer.time_zone",
        ]
        missing += [f for f in extra_fields if not field_exists(f)]
        self.assertEqual(missing, [])

    def test_undo_fields_exist_in_api(self):
        for spec in changes.RESOURCE_SPECS.values():
            for path in spec.fields:
                with self.subTest(field=f"{spec.resource_type}.{path}"):
                    self.assertTrue(
                        field_exists(f"{spec.resource_type}.{path}")
                    )

    async def test_tools_are_read_only_and_documented(self):
        tools = await reports_mcp.list_tools()
        names = sorted(t.name for t in tools)
        self.assertEqual(
            names,
            [
                "account_overview",
                "ad_performance",
                "budget_pacing",
                "change_history",
                "conversion_actions",
                "geo_performance",
                "keyword_performance",
                "landing_page_performance",
                "recommendations_list",
                "search_terms_report",
                "wasted_spend",
            ],
        )
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertTrue(tool.annotations.readOnlyHint)
                self.assertTrue(tool.description)
                self.assertNotIn("Args:", tool.description)
                for param, schema in tool.parameters["properties"].items():
                    self.assertIn(
                        "description",
                        schema,
                        f"{tool.name}.{param} has no description",
                    )

    def test_full_page_is_token_friendly(self):
        rows = [
            fakes.row(
                search_term_view={
                    "search_term": f"some fairly long search term number {i}",
                    "status": "NONE",
                },
                segments={"search_term_match_type": "PHRASE"},
                campaign={"id": 1000 + i, "name": f"Campaign name {i}"},
                ad_group={"id": 2000 + i, "name": f"Ad group name {i}"},
                metrics={
                    "cost_micros": 12_345_678,
                    "clicks": 12,
                    "impressions": 345,
                    "conversions": 0.0,
                },
            )
            for i in range(30)
        ]
        service = fakes.FakeAdsService([("FROM search_term_view", rows)])
        common.clear_account_cache()
        with patch("ads_mcp.utils.get_googleads_service", return_value=service):
            out = search_terms.search_terms_report(
                customer_id=fakes.CUSTOMER_ID
            )
        self.assertEqual(out["row_count"], 25)
        self.assertEqual(out["next_offset"], 25)
        size = len(json.dumps(out))
        self.assertLess(size, MAX_PAGE_CHARS, f"page is {size} chars")

    def test_long_strings_are_truncated(self):
        self.assertEqual(len(common.truncate("x" * 500)), common.MAX_STRING)
        self.assertEqual(common.convert(["a"] * 50, "list"), ["a"] * 20)

    def test_tools_are_wrapped_for_api_errors(self):
        for fn in (
            performance.account_overview,
            performance.ad_performance,
            budget.budget_pacing,
            account.change_history,
            search_terms.wasted_spend,
        ):
            self.assertTrue(hasattr(fn, "__wrapped__"))
            self.assertIn("customer_id", inspect.signature(fn).parameters)


if __name__ == "__main__":
    unittest.main()
