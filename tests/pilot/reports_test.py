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

"""Tests for the report tools, using real GoogleAdsRow objects."""

import datetime
import json
import unittest
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException

from ads_mcp.reports import account, budget, common, performance, search_terms

from . import fakes

CID = fakes.CUSTOMER_ID
M = 1_000_000


def metrics(cost, clicks=0, impressions=0, conversions=0.0, value=0.0):
    return {
        "cost_micros": int(cost * M),
        "clicks": clicks,
        "impressions": impressions,
        "conversions": conversions,
        "conversions_value": value,
    }


class ReportTestBase(unittest.TestCase):

    responders: list = []

    def setUp(self):
        common.clear_account_cache()
        self.service = fakes.FakeAdsService(self.responders)
        patcher = patch(
            "ads_mcp.utils.get_googleads_service", return_value=self.service
        )
        self.get_service = patcher.start()
        self.addCleanup(patcher.stop)

    def assert_envelope(self, out, report):
        self.assertEqual(out["report"], report)
        self.assertEqual(out["customer_id"], CID)
        self.assertEqual(out["currency"], "USD")
        self.assertIn("summary", out)
        self.assertTrue(out["summary"].startswith("**"))
        json.dumps(out)  # must be JSON serialisable


class TestAccountOverview(ReportTestBase):

    def setUp(self):
        self.responders = [
            (
                "FROM campaign ",
                [
                    fakes.row(
                        campaign={
                            "id": 1,
                            "name": "Brand",
                            "status": "ENABLED",
                            "advertising_channel_type": "SEARCH",
                        },
                        metrics=metrics(120.5, 100, 2000, 10),
                    ),
                    fakes.row(
                        campaign={
                            "id": 2,
                            "name": "Generic",
                            "status": "PAUSED",
                        },
                        metrics=metrics(80, 40, 4000, 0),
                    ),
                    fakes.row(campaign={"id": 3}, metrics=metrics(1)),
                ],
            ),
            (
                "FROM customer ",
                [fakes.row(metrics=metrics(300, 150, 7000, 12))],
            ),
        ]
        super().setUp()

    def test_rows_totals_and_paging(self):
        out = performance.account_overview(
            customer_id="123-456-7890", limit=2, login_customer_id="999"
        )
        self.assert_envelope(out, "account_overview")
        self.get_service.assert_called_with(
            "GoogleAdsService", login_customer_id="999"
        )
        self.assertEqual(out["date_range"], {"preset": "LAST_30_DAYS"})
        self.assertEqual(out["row_count"], 2)
        self.assertEqual(out["next_offset"], 2)
        brand = out["rows"][0]
        self.assertEqual(brand["campaign"], "Brand")
        self.assertEqual(brand["status"], "ENABLED")
        self.assertEqual(brand["channel"], "SEARCH")
        self.assertEqual(brand["cost"], 120.5)  # micros converted
        self.assertEqual(brand["cost_per_conv"], 12.05)
        self.assertEqual(brand["ctr"], 5.0)
        self.assertEqual(brand["avg_cpc"], 1.21)
        self.assertNotIn("cost_per_conv", out["rows"][1])
        self.assertEqual(out["totals"]["cost"], 300.0)
        self.assertEqual(out["totals"]["cost_per_conv"], 25.0)
        self.assertIn("300.00 USD", out["summary"])

        q = self.service.query_matching("FROM campaign ")
        self.assertIn("segments.date DURING LAST_30_DAYS", q)
        self.assertIn("metrics.impressions > 0", q)
        self.assertIn("ORDER BY metrics.cost_micros DESC", q)
        self.assertIn("LIMIT 3", q)  # offset + limit + 1
        self.assertNotIn(
            "campaign.status =", q
        )  # historical totals stay complete

    def test_custom_range_and_last_page(self):
        out = performance.account_overview(
            customer_id=CID,
            start_date="2026-01-01",
            end_date="2026-01-31",
            include_zero_impressions=True,
            offset=1,
        )
        self.assertIsNone(out["next_offset"])
        self.assertEqual(out["row_count"], 2)
        q = self.service.query_matching("FROM campaign ")
        self.assertIn("BETWEEN '2026-01-01' AND '2026-01-31'", q)
        self.assertNotIn("metrics.impressions > 0", q)

    def test_validation_errors(self):
        with self.assertRaises(ToolError):
            performance.account_overview(customer_id=CID, limit=0)
        with self.assertRaises(ToolError):
            performance.account_overview(customer_id=CID, limit=1000)
        with self.assertRaises(ToolError):
            performance.account_overview(customer_id=CID, date_range="FOREVER")
        with self.assertRaises(ToolError):
            performance.account_overview(customer_id="not-an-id")

    def test_google_ads_exception_becomes_tool_error(self):
        error = MagicMock()
        error.message = "Bad field"
        ex = GoogleAdsException(
            MagicMock(), MagicMock(), MagicMock(), MagicMock()
        )
        ex.failure = MagicMock(errors=[error])
        ex.request_id = "req-1"
        self.service.search_stream = MagicMock(side_effect=ex)
        with self.assertRaisesRegex(ToolError, "Bad field"):
            performance.account_overview(customer_id=CID)


class TestSearchTermReports(ReportTestBase):

    def setUp(self):
        self.responders = [
            (
                "FROM search_term_view",
                [
                    fakes.row(
                        search_term_view={
                            "search_term": "free stuff",
                            "status": "NONE",
                        },
                        segments={"search_term_match_type": "BROAD"},
                        campaign={"id": 1, "name": "C"},
                        ad_group={"id": 2, "name": "AG"},
                        metrics=metrics(30, 12, 100, 0),
                    ),
                    fakes.row(
                        search_term_view={
                            "search_term": "excluded",
                            "status": "EXCLUDED",
                        },
                        metrics=metrics(20, 20, 100, 0),
                    ),
                    fakes.row(
                        search_term_view={
                            "search_term": "buyer",
                            "status": "ADDED",
                        },
                        metrics=metrics(10, 10, 50, 2),
                    ),
                    fakes.row(
                        search_term_view={
                            "search_term": "few clicks",
                            "status": "NONE",
                        },
                        metrics=metrics(5, 2, 50, 0),
                    ),
                ],
            ),
            (
                "FROM keyword_view",
                [
                    fakes.row(
                        ad_group_criterion={
                            "criterion_id": 9,
                            "keyword": {
                                "text": "cheap",
                                "match_type": "PHRASE",
                            },
                            "status": "ENABLED",
                            "quality_info": {
                                "quality_score": 3,
                                "search_predicted_ctr": "BELOW_AVERAGE",
                                "creative_quality_score": "AVERAGE",
                                "post_click_quality_score": "ABOVE_AVERAGE",
                            },
                            "effective_cpc_bid_micros": 1_500_000,
                        },
                        metrics={
                            **metrics(40, 20, 400, 0),
                            "search_impression_share": 0.25,
                        },
                    ),
                    fakes.row(
                        ad_group_criterion={
                            "criterion_id": 10,
                            "keyword": {"text": "new"},
                        },
                        metrics=metrics(1, 1, 10, 0),
                    ),
                ],
            ),
        ]
        super().setUp()

    def test_wasted_spend(self):
        out = search_terms.wasted_spend(
            customer_id=CID, min_cost=4.5, date_range="LAST_7_DAYS"
        )
        self.assert_envelope(out, "wasted_spend")
        self.assertNotIn("rows", out)
        self.assertEqual(len(out["search_terms"]), 4)
        self.assertEqual(len(out["keywords"]), 2)
        self.assertEqual(out["row_count"], 6)
        self.assertEqual(out["totals"]["keywords"]["cost"], 41.0)
        q = self.service.query_matching("FROM search_term_view")
        self.assertIn("metrics.conversions = 0", q)
        self.assertIn("metrics.cost_micros > 4500000", q)
        self.assertIn("segments.date DURING LAST_7_DAYS", q)
        self.assertEqual(list(out)[-1], "summary")

    def test_wasted_spend_single_source(self):
        out = search_terms.wasted_spend(customer_id=CID, source="keywords")
        self.assertNotIn("search_terms", out)
        self.assertEqual(len(self.service.queries), 2)  # account + keywords
        with self.assertRaises(ToolError):
            search_terms.wasted_spend(customer_id=CID, source="everything")
        with self.assertRaises(ToolError):
            search_terms.wasted_spend(customer_id=CID, min_cost=-1)

    def test_search_terms_suggested_negative(self):
        out = search_terms.search_terms_report(
            customer_id=CID, campaign_id="1", negative_min_clicks=5
        )
        self.assert_envelope(out, "search_terms_report")
        flags = {r["search_term"]: r["suggested_negative"] for r in out["rows"]}
        self.assertEqual(
            flags,
            {
                "free stuff": True,  # 12 clicks, 0 conv
                "excluded": False,  # already excluded
                "buyer": False,  # converts
                "few clicks": False,  # below click threshold
            },
        )
        row = out["rows"][0]
        self.assertEqual(row["match_type"], "BROAD")
        self.assertEqual(row["campaign"], "C")
        q = self.service.query_matching("FROM search_term_view")
        self.assertIn("campaign.id = 1", q)
        self.assertIn("metrics.impressions >= 1", q)

    def test_search_terms_cost_threshold_and_filter(self):
        out = search_terms.search_terms_report(
            customer_id=CID, negative_min_cost=50, only_suggested_negatives=True
        )
        self.assertFalse(any(r["suggested_negative"] for r in out["rows"]))
        q = self.service.query_matching("FROM search_term_view")
        self.assertIn("metrics.cost_micros >= 50000000", q)
        self.assertIn("NOT IN ('EXCLUDED', 'ADDED_EXCLUDED')", q)
        with self.assertRaises(ToolError):
            search_terms.search_terms_report(
                customer_id=CID, ad_group_id="1 OR 1=1"
            )

    def test_keyword_performance_quality_score(self):
        out = search_terms.keyword_performance(customer_id=CID, ad_group_id=7)
        self.assert_envelope(out, "keyword_performance")
        kw = out["rows"][0]
        self.assertEqual(kw["keyword"], "cheap")
        self.assertEqual(kw["match_type"], "PHRASE")
        self.assertEqual(kw["quality_score"], 3)
        self.assertEqual(kw["expected_ctr"], "BELOW_AVERAGE")
        self.assertEqual(kw["ad_relevance"], "AVERAGE")
        self.assertEqual(kw["landing_page_experience"], "ABOVE_AVERAGE")
        self.assertEqual(kw["cpc_bid"], 1.5)
        self.assertEqual(kw["search_impr_share"], 25.0)
        # No quality data: the keys are left out instead of 0 / UNSPECIFIED.
        self.assertNotIn("quality_score", out["rows"][1])
        self.assertNotIn("expected_ctr", out["rows"][1])
        self.assertIn("1 with QS <= 4", out["summary"])
        q = self.service.query_matching("FROM keyword_view")
        self.assertIn("ad_group_criterion.negative = FALSE", q)
        self.assertIn("ad_group.id = 7", q)


class TestLandingAndGeo(ReportTestBase):

    def setUp(self):
        self.responders = [
            (
                "FROM landing_page_view",
                [
                    fakes.row(
                        landing_page_view={
                            "unexpanded_final_url": "https://example.com/a"
                        },
                        metrics={
                            **metrics(10, 5, 100, 1),
                            "mobile_friendly_clicks_percentage": 0.8,
                            "speed_score": 7,
                        },
                    )
                ],
            ),
            (
                "FROM expanded_landing_page_view",
                [
                    fakes.row(
                        expanded_landing_page_view={
                            "expanded_final_url": "https://example.com/a?x=1"
                        },
                        metrics=metrics(10, 5, 100, 1),
                    )
                ],
            ),
            (
                "FROM geographic_view",
                [
                    fakes.row(
                        geographic_view={
                            "country_criterion_id": 2840,
                            "location_type": "LOCATION_OF_PRESENCE",
                        },
                        segments={
                            "geo_target_region": "geoTargetConstants/21167"
                        },
                        metrics=metrics(50, 20, 400, 4),
                    )
                ],
            ),
            (
                "FROM user_location_view",
                [
                    fakes.row(
                        user_location_view={
                            "country_criterion_id": 2840,
                            "targeting_location": True,
                        },
                        metrics=metrics(5, 2, 40, 0),
                    )
                ],
            ),
            (
                "FROM geo_target_constant",
                [
                    fakes.row(
                        geo_target_constant={
                            "resource_name": "geoTargetConstants/21167",
                            "canonical_name": "Example Region,Country",
                        }
                    ),
                    fakes.row(
                        geo_target_constant={
                            "resource_name": "geoTargetConstants/2840",
                            "canonical_name": "Country",
                        }
                    ),
                ],
            ),
        ]
        super().setUp()

    def test_landing_pages(self):
        out = performance.landing_page_performance(customer_id=CID)
        self.assert_envelope(out, "landing_page_performance")
        row = out["rows"][0]
        self.assertEqual(row["url"], "https://example.com/a")
        self.assertEqual(row["mobile_friendly_clicks_pct"], 80.0)
        self.assertEqual(row["speed_score"], 7)
        out = performance.landing_page_performance(
            customer_id=CID, expanded=True
        )
        self.assertEqual(out["rows"][0]["url"], "https://example.com/a?x=1")
        self.assertNotIn("speed_score", out["rows"][0])

    def test_geographic_view_with_names(self):
        out = performance.geo_performance(customer_id=CID, level="region")
        self.assert_envelope(out, "geo_performance")
        row = out["rows"][0]
        self.assertEqual(row["location"], "Example Region,Country")
        self.assertEqual(row["location_id"], 21167)
        self.assertEqual(row["location_type"], "LOCATION_OF_PRESENCE")
        self.assertNotIn("location_rn", row)
        self.assertIn("notes", out)
        q = self.service.query_matching("FROM geographic_view")
        self.assertIn("segments.geo_target_region", q)
        q = self.service.query_matching("FROM geo_target_constant")
        self.assertIn("'geoTargetConstants/21167'", q)

    def test_user_location_country(self):
        out = performance.geo_performance(
            customer_id=CID, view="user_location", level="country"
        )
        row = out["rows"][0]
        self.assertTrue(row["targeted"])
        self.assertEqual(row["location"], "Country")
        q = self.service.query_matching("FROM user_location_view")
        self.assertNotIn("segments.geo_target", q)
        with self.assertRaises(ToolError):
            performance.geo_performance(customer_id=CID, level="street")


class TestAdPerformance(ReportTestBase):

    def setUp(self):
        self.responders = [
            (
                "FROM ad_group_ad_asset_view",
                [
                    fakes.row(
                        ad_group_ad_asset_view={
                            "ad_group_ad": f"customers/{CID}/adGroupAds/5~6",
                            "field_type": "HEADLINE",
                            "performance_label": "BEST",
                            "pinned_field": "HEADLINE_1",
                        },
                        asset={"text_asset": {"text": "Great offer"}},
                    ),
                    fakes.row(
                        ad_group_ad_asset_view={
                            "ad_group_ad": f"customers/{CID}/adGroupAds/5~6",
                            "field_type": "DESCRIPTION",
                            "performance_label": "UNSPECIFIED",
                        },
                        asset={"text_asset": {"text": "Learn more today"}},
                    ),
                ],
            ),
            (
                "FROM ad_group_ad ",
                [
                    fakes.row(
                        campaign={"name": "C"},
                        ad_group={"id": 5, "name": "AG"},
                        ad_group_ad={
                            "ad": {
                                "id": 6,
                                "type_": "RESPONSIVE_SEARCH_AD",
                                "final_urls": ["https://example.com"],
                            },
                            "status": "ENABLED",
                            "ad_strength": "GOOD",
                            "policy_summary": {"approval_status": "APPROVED"},
                        },
                        metrics=metrics(10, 5, 100, 1),
                    ),
                    fakes.row(
                        ad_group={"id": 5},
                        ad_group_ad={
                            "ad": {"id": 7, "type_": "EXPANDED_TEXT_AD"}
                        },
                        metrics=metrics(1, 1, 10, 0),
                    ),
                ],
            ),
        ]
        super().setUp()

    def test_ads_with_asset_labels(self):
        out = performance.ad_performance(customer_id=CID, campaign_id="3")
        self.assert_envelope(out, "ad_performance")
        rsa = out["rows"][0]
        self.assertEqual(rsa["ad_strength"], "GOOD")
        self.assertEqual(rsa["approval"], "APPROVED")
        self.assertEqual(rsa["final_urls"], ["https://example.com"])
        self.assertEqual(len(rsa["assets"]), 2)
        self.assertEqual(rsa["assets"][0]["label"], "BEST")
        self.assertEqual(rsa["assets"][0]["pinned"], "HEADLINE_1")
        self.assertNotIn("label", rsa["assets"][1])  # UNSPECIFIED dropped
        self.assertEqual(rsa["asset_labels"], {"BEST": 1})
        self.assertNotIn("assets", out["rows"][1])
        q = self.service.query_matching("FROM ad_group_ad_asset_view")
        self.assertIn(f"'customers/{CID}/adGroupAds/5~6'", q)
        self.assertNotIn("5~7", q)  # non-RSA ads are not looked up
        self.assertIn(
            "campaign.id = 3", self.service.query_matching("FROM ad_group_ad ")
        )

    def test_without_assets(self):
        performance.ad_performance(customer_id=CID, include_assets=False)
        with self.assertRaises(AssertionError):
            self.service.query_matching("FROM ad_group_ad_asset_view")


class TestBudgetPacing(ReportTestBase):

    def setUp(self):
        budget_a = {
            "resource_name": f"customers/{CID}/campaignBudgets/100",
            "name": "Shared A",
            "amount_micros": 10 * M,
            "period": "DAILY",
            "explicitly_shared": True,
        }
        budget_b = {
            "resource_name": f"customers/{CID}/campaignBudgets/200",
            "name": "B",
            "amount_micros": 20 * M,
            "period": "DAILY",
        }
        budget_c = {
            "resource_name": f"customers/{CID}/campaignBudgets/300",
            "name": "Total",
            "period": "CUSTOM_PERIOD",
            "total_amount_micros": 500 * M,
        }
        self.responders = [
            (
                "segments.date DURING THIS_MONTH",
                [
                    fakes.row(
                        campaign={"id": 1, "name": "One", "status": "ENABLED"},
                        campaign_budget=budget_a,
                        metrics={"cost_micros": 100 * M},
                    ),
                    fakes.row(
                        campaign={"id": 2, "name": "Two", "status": "PAUSED"},
                        campaign_budget=budget_a,
                        metrics={"cost_micros": 50 * M},
                    ),
                    fakes.row(
                        campaign={"id": 4, "name": "Four", "status": "ENABLED"},
                        campaign_budget=budget_c,
                        metrics={"cost_micros": 10 * M},
                    ),
                ],
            ),
            (
                "campaign.status = 'ENABLED'",
                [
                    fakes.row(
                        campaign={"id": 1, "name": "One", "status": "ENABLED"},
                        campaign_budget=budget_a,
                    ),
                    fakes.row(
                        campaign={
                            "id": 3,
                            "name": "Three",
                            "status": "ENABLED",
                        },
                        campaign_budget=budget_b,
                    ),
                ],
            ),
        ]
        super().setUp()

    def test_pacing(self):
        fixed_now = datetime.datetime(2026, 4, 11, 0, 0)  # 10 full days elapsed
        with patch.object(
            budget, "account_now", return_value=(fixed_now, True)
        ):
            out = budget.budget_pacing(customer_id=CID)
        self.assert_envelope(out, "budget_pacing")
        self.assertEqual(out["days_in_month"], 30)
        self.assertEqual(out["days_elapsed"], 10.0)
        rows = {r["budget_name"]: r for r in out["rows"]}

        shared = rows["Shared A"]
        self.assertEqual(shared["spend_mtd"], 150.0)
        self.assertEqual(len(shared["campaigns"]), 2)
        self.assertEqual(shared["expected_mtd"], 100.0)
        self.assertEqual(shared["pace_pct"], 150.0)
        self.assertEqual(shared["pace"], "OVER")
        self.assertEqual(shared["projected_month"], 450.0)
        self.assertEqual(shared["monthly_limit"], 304.0)

        idle = rows["B"]  # enabled but no spend this month
        self.assertEqual(idle["spend_mtd"], 0.0)
        self.assertEqual(idle["pace"], "UNDER")

        total = rows["Total"]
        self.assertEqual(total["pace"], "NOT_DAILY")
        self.assertEqual(total["total_budget"], 500.0)
        self.assertTrue(any("not daily" in n for n in out["notes"]))

        self.assertEqual(out["rows"][0]["budget_name"], "Shared A")  # by spend
        self.assertEqual(out["totals"]["spend_mtd"], 160.0)
        self.assertEqual(out["totals"]["daily_budget"], 30.0)

    def test_timezone_fallback_note(self):
        fixed_now = datetime.datetime(
            2026, 4, 1, 12, 0, tzinfo=datetime.timezone.utc
        )
        with patch.object(
            budget, "account_now", return_value=(fixed_now, False)
        ):
            out = budget.budget_pacing(customer_id=CID, limit=1)
        self.assertEqual(out["next_offset"], 1)
        self.assertTrue(any("UTC" in n for n in out["notes"]))

    def test_account_now(self):
        now, ok = budget.account_now("Not/AZone")
        self.assertFalse(ok)
        self.assertIsNotNone(now.tzinfo)


class TestAccountReports(ReportTestBase):

    def setUp(self):
        self.responders = [
            (
                "FROM change_event",
                [
                    fakes.row(
                        change_event={
                            "change_date_time": "2026-01-05 10:00:00.000000",
                            "change_resource_type": "CAMPAIGN",
                            "resource_change_operation": "UPDATE",
                            "change_resource_name": f"customers/{CID}/campaigns/1",
                            "campaign": f"customers/{CID}/campaigns/1",
                            "user_email": "user@example.com",
                            "client_type": "GOOGLE_ADS_WEB_CLIENT",
                            "changed_fields": {
                                "paths": ["status", "targetCpa.targetCpaMicros"]
                            },
                            "old_resource": {"campaign": {"status": "ENABLED"}},
                            "new_resource": {"campaign": {"status": "PAUSED"}},
                        }
                    )
                ],
            ),
            (
                "segments.conversion_action",
                [
                    fakes.row(
                        segments={
                            "conversion_action": f"customers/{CID}/conversionActions/11"
                        },
                        metrics={"conversions": 4.0, "all_conversions": 6.0},
                    )
                ],
            ),
            (
                "FROM conversion_action",
                [
                    fakes.row(
                        conversion_action={
                            "id": 11,
                            "name": "Lead form",
                            "status": "ENABLED",
                            "type_": "GOOGLE_ANALYTICS_4_CUSTOM",
                            "primary_for_goal": True,
                            "include_in_conversions_metric": True,
                            "google_analytics_4_settings": {
                                "event_name": "generate_lead"
                            },
                        }
                    ),
                    fakes.row(
                        conversion_action={
                            "id": 12,
                            "name": "Old pixel",
                            "status": "ENABLED",
                            "type_": "WEBPAGE",
                            "primary_for_goal": True,
                        }
                    ),
                    fakes.row(
                        conversion_action={
                            "id": 13,
                            "name": "Page view",
                            "status": "ENABLED",
                            "type_": "WEBPAGE",
                            "primary_for_goal": False,
                        }
                    ),
                ],
            ),
            (
                "FROM recommendation",
                [
                    fakes.row(
                        recommendation={
                            "resource_name": f"customers/{CID}/recommendations/abc",
                            "type_": "CAMPAIGN_BUDGET",
                            "campaign": f"customers/{CID}/campaigns/1",
                            "impact": {
                                "base_metrics": {
                                    "clicks": 10,
                                    "cost_micros": 5 * M,
                                },
                                "potential_metrics": {
                                    "clicks": 15,
                                    "cost_micros": 8 * M,
                                },
                            },
                            "campaign_budget_recommendation": {
                                "current_budget_amount_micros": 10 * M,
                                "recommended_budget_amount_micros": 15 * M,
                            },
                        }
                    )
                ],
            ),
        ]
        super().setUp()

    def test_change_history(self):
        out = account.change_history(
            customer_id=CID,
            days=3,
            resource_type="campaign",
            include_values=True,
        )
        self.assert_envelope(out, "change_history")
        row = out["rows"][0]
        self.assertEqual(row["resource_type"], "CAMPAIGN")
        self.assertEqual(row["operation"], "UPDATE")
        self.assertEqual(
            row["changed_fields"], ["status", "target_cpa.target_cpa_micros"]
        )
        self.assertEqual(
            row["values"]["status"], {"old": "ENABLED", "new": "PAUSED"}
        )
        q = self.service.query_matching("FROM change_event")
        self.assertIn("change_event.change_resource_type = 'CAMPAIGN'", q)
        self.assertIn("change_event.change_date_time >= '", q)
        self.assertIn("ORDER BY change_event.change_date_time DESC", q)
        self.assertIn("change_event.old_resource", q)
        for bad in [0, 31, "x"]:
            with self.assertRaises(ToolError):
                account.change_history(customer_id=CID, days=bad)
        with self.assertRaises(ToolError):
            account.change_history(
                customer_id=CID, resource_type="CAMPAIGN' OR 1=1"
            )

    def test_change_history_without_values(self):
        account.change_history(customer_id=CID)
        q = self.service.query_matching("FROM change_event")
        self.assertNotIn("old_resource", q)

    def test_conversion_actions(self):
        out = account.conversion_actions(customer_id=CID)
        self.assert_envelope(out, "conversion_actions")
        rows = {r["name"]: r for r in out["rows"]}
        lead = rows["Lead form"]
        self.assertTrue(lead["ga4_imported"])
        self.assertTrue(lead["primary"])
        self.assertEqual(lead["ga4_event"], "generate_lead")
        self.assertEqual(lead["conversions_30d"], 4.0)
        self.assertEqual(lead["all_conversions_30d"], 6.0)
        self.assertFalse(rows["Old pixel"]["ga4_imported"])
        self.assertFalse(rows["Page view"]["primary"])
        self.assertTrue(any("Old pixel" in n for n in out["notes"]))
        self.assertIn("2 primary", out["summary"])
        self.assertIn(
            "conversion_action.status != 'REMOVED'",
            self.service.query_matching("FROM conversion_action"),
        )

    def test_conversion_actions_without_stats(self):
        out = account.conversion_actions(
            customer_id=CID, include_removed=True, include_stats=False
        )
        self.assertNotIn("conversions_30d", out["rows"][0])
        self.assertNotIn(
            "REMOVED", self.service.query_matching("FROM conversion_action")
        )

    def test_recommendations(self):
        out = account.recommendations_list(
            customer_id=CID, types=["campaign_budget", "KEYWORD"], campaign_id=1
        )
        self.assert_envelope(out, "recommendations_list")
        rec = out["rows"][0]
        self.assertEqual(rec["id"], "abc")
        self.assertEqual(rec["type"], "CAMPAIGN_BUDGET")
        self.assertEqual(rec["campaign_id"], "1")
        self.assertEqual(rec["impact"], {"clicks": 5, "cost": 3.0})
        self.assertEqual(
            rec["details"], {"current_budget": 10.0, "recommended_budget": 15.0}
        )
        q = self.service.query_matching("FROM recommendation")
        self.assertIn(
            "recommendation.type IN ('CAMPAIGN_BUDGET', 'KEYWORD')", q
        )
        self.assertIn("recommendation.dismissed = FALSE", q)
        self.assertIn(
            f"recommendation.campaign = 'customers/{CID}/campaigns/1'", q
        )
        with self.assertRaises(ToolError):
            account.recommendations_list(customer_id=CID, types=["NOT_A_TYPE"])


class TestAccountInfoCache(ReportTestBase):

    def test_account_info_is_cached(self):
        common.account_info(CID)
        common.account_info(CID)
        self.assertEqual(len(self.service.queries), 1)
        self.assertEqual(common.account_info(CID)["time_zone"], "America/New_York")

    def test_account_info_not_cached_in_remote_mode(self):
        with patch("ads_mcp.utils.is_remote_oauth_mode", return_value=True):
            common.account_info(CID)
            common.account_info(CID)
        self.assertEqual(len(self.service.queries), 2)


if __name__ == "__main__":
    unittest.main()
