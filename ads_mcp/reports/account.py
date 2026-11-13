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

"""Account state reports: change history, conversion actions, recommendations."""

import datetime
import json
import re
from typing import Any

from fastmcp.exceptions import ToolError
from google.ads.googleads.v25.enums.types.change_event_resource_type import (
    ChangeEventResourceTypeEnum,
)
from google.ads.googleads.v25.enums.types.recommendation_type import (
    RecommendationTypeEnum,
)
from mcp.types import ToolAnnotations

from ads_mcp import utils
from ads_mcp.reports import common, gaql, reports_mcp
from ads_mcp.reports.common import Column

_READ_ONLY = ToolAnnotations(readOnlyHint=True)
_CHANGE_RESOURCE_TYPES = frozenset(
    m.name
    for m in ChangeEventResourceTypeEnum.ChangeEventResourceType
    if m.name not in ("UNSPECIFIED", "UNKNOWN")
)
_RECOMMENDATION_TYPES = frozenset(
    m.name
    for m in RecommendationTypeEnum.RecommendationType
    if m.name not in ("UNSPECIFIED", "UNKNOWN")
)
_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")
_MAX_CHANGE_DAYS = 30

CHANGE_COLUMNS = (
    Column("time", "change_event.change_date_time"),
    Column("resource_type", "change_event.change_resource_type", "enum"),
    Column("operation", "change_event.resource_change_operation", "enum"),
    Column("resource", "change_event.change_resource_name"),
    Column("campaign", "change_event.campaign"),
    Column("ad_group", "change_event.ad_group"),
    Column("user", "change_event.user_email"),
    Column("client", "change_event.client_type", "enum"),
)


def _changed_paths(row: Any) -> list[str]:
    paths = list(row.change_event.changed_fields.paths)
    return [_CAMEL.sub("_", p).lower() for p in paths]


def _field_value(resource: Any, path: str) -> Any:
    try:
        value = resource
        for part in path.split("."):
            value = getattr(value, part)
    except AttributeError:
        return None
    formatted = utils.format_output_value(value)
    text = (
        formatted
        if isinstance(formatted, (str, int, float, bool))
        else json.dumps(formatted, default=str, sort_keys=True)
    )
    return common.truncate(str(text), 120) if isinstance(text, str) else text


@reports_mcp.tool(annotations=_READ_ONLY)
@common.google_ads_errors
def change_history(
    customer_id: str | int,
    days: int = 7,
    resource_type: str | None = None,
    include_values: bool = False,
    limit: int = 50,
    offset: int = 0,
    login_customer_id: str | int | None = None,
) -> dict[str, Any]:
    """Recent changes to the account from `change_event`, newest first:
    who changed what, when, with which tool, and which fields.

    Google keeps change events for the last 30 days only.

    Args:
        customer_id: Google Ads customer ID (e.g. '1234567890').
        days: How many days back to look (1-30).
        resource_type: Optional filter such as CAMPAIGN, AD_GROUP, AD_GROUP_AD,
            AD_GROUP_CRITERION, CAMPAIGN_BUDGET, CAMPAIGN_CRITERION or ASSET.
        include_values: Add the old and new value of each changed field.
        limit: Rows per page (1-200).
        offset: Row offset for the next page.
        login_customer_id: Optional manager account ID.
    """
    cid = gaql.customer_id(customer_id)
    limit, offset = common.page_args(limit, offset)
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 0
    if not 1 <= days <= _MAX_CHANGE_DAYS:
        raise ToolError(
            f"Error: 'days' must be between 1 and {_MAX_CHANGE_DAYS}."
        )
    account = common.account_info(cid, login_customer_id)

    today = datetime.date.today()
    # Google rejects start dates older than 30 days; stay one day inside.
    start = today - datetime.timedelta(days=min(days, _MAX_CHANGE_DAYS - 1))
    tomorrow = today + datetime.timedelta(days=1)
    conditions = [
        f"change_event.change_date_time >= '{start.isoformat()}'",
        f"change_event.change_date_time <= '{tomorrow.isoformat()}'",
    ]
    if resource_type:
        rt = gaql.enum_value(
            "resource_type", resource_type, _CHANGE_RESOURCE_TYPES
        )
        conditions.append(f"change_event.change_resource_type = '{rt}'")

    fields = [c.field for c in CHANGE_COLUMNS] + ["change_event.changed_fields"]
    if include_values:
        fields += ["change_event.old_resource", "change_event.new_resource"]
    raw, next_offset = common.fetch_page(
        cid,
        fields,
        "change_event",
        conditions,
        ["change_event.change_date_time DESC"],
        limit,
        offset,
        login_customer_id,
    )

    rows = []
    for r in raw:
        row = common.shape_row(r, CHANGE_COLUMNS)
        paths = _changed_paths(r)
        row["changed_fields"] = paths[:20]
        if include_values and row.get("resource_type"):
            attr = row["resource_type"].lower()
            old = getattr(r.change_event.old_resource, attr, None)
            new = getattr(r.change_event.new_resource, attr, None)
            if old is not None and new is not None:
                row["values"] = {
                    p: {
                        "old": _field_value(old, p),
                        "new": _field_value(new, p),
                    }
                    for p in paths[:10]
                }
        rows.append(row)

    by_type: dict[str, int] = {}
    for row in rows:
        key = row.get("resource_type", "OTHER")
        by_type[key] = by_type.get(key, 0) + 1
    summary = "\n".join(
        line
        for line in [
            f"**Change history** - {cid}, last {days} days: {len(rows)} "
            "changes on this page ("
            + ", ".join(f"{k} {v}" for k, v in sorted(by_type.items()))
            + ")",
            common.md_table(
                rows,
                [
                    ("time", "Time"),
                    ("resource_type", "Type"),
                    ("operation", "Op"),
                    ("user", "User"),
                    ("client", "Client"),
                ],
            ),
        ]
        if line
    )
    return common.envelope(
        "change_history",
        cid,
        account,
        rows,
        summary,
        next_offset=next_offset,
        extra={"days": days},
    )


CONVERSION_COLUMNS = (
    Column("id", "conversion_action.id", "int"),
    Column("name", "conversion_action.name"),
    Column("status", "conversion_action.status", "enum"),
    Column("type", "conversion_action.type", "enum"),
    Column("category", "conversion_action.category", "enum"),
    Column("origin", "conversion_action.origin", "enum"),
    Column("primary", "conversion_action.primary_for_goal", "bool"),
    Column(
        "include_in_conversions",
        "conversion_action.include_in_conversions_metric",
        "bool",
    ),
    Column("counting", "conversion_action.counting_type", "enum"),
    Column(
        "click_lookback_days",
        "conversion_action.click_through_lookback_window_days",
        "int",
    ),
    Column(
        "attribution",
        "conversion_action.attribution_model_settings.attribution_model",
        "enum",
    ),
    Column(
        "default_value",
        "conversion_action.value_settings.default_value",
        "float",
    ),
    Column(
        "always_default_value",
        "conversion_action.value_settings.always_use_default_value",
        "bool",
    ),
    Column(
        "ga4_event",
        "conversion_action.google_analytics_4_settings.event_name",
    ),
)


@reports_mcp.tool(annotations=_READ_ONLY)
@common.google_ads_errors
def conversion_actions(
    customer_id: str | int,
    include_removed: bool = False,
    include_stats: bool = True,
    limit: int = 50,
    offset: int = 0,
    login_customer_id: str | int | None = None,
) -> dict[str, Any]:
    """Lists conversion actions with status, primary/secondary,
    include-in-conversions, counting, attribution and whether the action
    is imported from GA4.

    With `include_stats=True` adds conversions and all conversions for the
    last 30 days, so actions that stopped recording stand out.

    Args:
        customer_id: Google Ads customer ID (e.g. '1234567890').
        include_removed: Also list removed conversion actions.
        include_stats: Add last-30-day conversion counts per action.
        limit: Rows per page (1-200).
        offset: Row offset for the next page.
        login_customer_id: Optional manager account ID.
    """
    cid = gaql.customer_id(customer_id)
    limit, offset = common.page_args(limit, offset)
    account = common.account_info(cid, login_customer_id)

    conditions = []
    if not include_removed:
        conditions.append("conversion_action.status != 'REMOVED'")
    raw, next_offset = common.fetch_page(
        cid,
        [c.field for c in CONVERSION_COLUMNS],
        "conversion_action",
        conditions,
        ["conversion_action.name ASC"],
        limit,
        offset,
        login_customer_id,
    )
    rows = []
    for r in raw:
        row = common.shape_row(r, CONVERSION_COLUMNS)
        row["ga4_imported"] = row.get("type", "").startswith(
            "GOOGLE_ANALYTICS_4"
        )
        rows.append(row)

    notes = []
    if include_stats and rows:
        query = gaql.build_query(
            [
                "segments.conversion_action",
                "metrics.conversions",
                "metrics.all_conversions",
            ],
            "customer",
            ["segments.date DURING LAST_30_DAYS"],
        )
        stats: dict[int, tuple[float, float]] = {}
        for r in common.fetch_rows(cid, query, login_customer_id):
            action_id = utils.extract_id(r.segments.conversion_action)
            if action_id.isdigit():
                prev = stats.get(int(action_id), (0.0, 0.0))
                stats[int(action_id)] = (
                    prev[0] + float(r.metrics.conversions),
                    prev[1] + float(r.metrics.all_conversions),
                )
        for row in rows:
            conv, all_conv = stats.get(row.get("id"), (0.0, 0.0))
            row["conversions_30d"] = round(conv, 2)
            row["all_conversions_30d"] = round(all_conv, 2)
        silent = [
            r["name"]
            for r in rows
            if r.get("primary")
            and r.get("status") == "ENABLED"
            and not r["all_conversions_30d"]
        ]
        if silent:
            notes.append(
                "Primary, enabled actions with no conversions in 30 days: "
                + ", ".join(common.truncate(n, 60) for n in silent[:10])
            )

    primary = sum(1 for r in rows if r.get("primary"))
    ga4 = sum(1 for r in rows if r["ga4_imported"])
    summary = "\n".join(
        line
        for line in [
            f"**Conversion actions** - {cid}: {len(rows)} on this page, "
            f"{primary} primary, {len(rows) - primary} secondary, {ga4} "
            "imported from GA4",
            common.md_table(
                rows,
                [
                    ("name", "Name"),
                    ("status", "Status"),
                    ("primary", "Primary"),
                    ("type", "Type"),
                    ("conversions_30d", "Conv 30d"),
                ],
                max_rows=8,
            ),
        ]
        if line
    )
    return common.envelope(
        "conversion_actions",
        cid,
        account,
        rows,
        summary,
        next_offset=next_offset,
        notes=notes,
    )


RECOMMENDATION_COLUMNS = (
    Column("resource_name", "recommendation.resource_name"),
    Column("type", "recommendation.type", "enum"),
    Column("campaign", "recommendation.campaign"),
    Column("ad_group", "recommendation.ad_group"),
    Column("dismissed", "recommendation.dismissed", "bool"),
)
_IMPACT_FIELDS = (
    ("impressions", "impressions", "int"),
    ("clicks", "clicks", "int"),
    ("cost", "cost_micros", "money"),
    ("conversions", "conversions", "float"),
)
_DETAIL_COLUMNS = (
    Column(
        "current_budget",
        "recommendation.campaign_budget_recommendation.current_budget_amount_micros",
        "money",
    ),
    Column(
        "recommended_budget",
        "recommendation.campaign_budget_recommendation.recommended_budget_amount_micros",
        "money",
    ),
    Column("keyword", "recommendation.keyword_recommendation.keyword.text"),
    Column(
        "keyword_match_type",
        "recommendation.keyword_recommendation.keyword.match_type",
        "enum",
    ),
    Column(
        "recommended_cpc",
        "recommendation.keyword_recommendation.recommended_cpc_bid_micros",
        "money",
    ),
)


@reports_mcp.tool(annotations=_READ_ONLY)
@common.google_ads_errors
def recommendations_list(
    customer_id: str | int,
    types: list[str] | None = None,
    campaign_id: str | int | None = None,
    include_dismissed: bool = False,
    limit: int = 25,
    offset: int = 0,
    login_customer_id: str | int | None = None,
) -> dict[str, Any]:
    """Lists Google's recommendations for the account with their estimated
    impact (potential minus base metrics for the week).

    Read only: this tool does not apply or dismiss anything. Budget and
    keyword recommendations include their details (current vs recommended
    budget; keyword text, match type and suggested CPC).

    Args:
        customer_id: Google Ads customer ID (e.g. '1234567890').
        types: Optional recommendation types, e.g. ['CAMPAIGN_BUDGET', 'KEYWORD',
            'SITELINK_ASSET', 'TARGET_CPA_OPT_IN', 'MAXIMIZE_CONVERSIONS_OPT_IN'].
        campaign_id: Only recommendations for this campaign (numeric ID).
        include_dismissed: Also list dismissed recommendations.
        limit: Rows per page (1-200).
        offset: Row offset for the next page.
        login_customer_id: Optional manager account ID.
    """
    cid = gaql.customer_id(customer_id)
    limit, offset = common.page_args(limit, offset)
    account = common.account_info(cid, login_customer_id)

    conditions = []
    if not include_dismissed:
        conditions.append("recommendation.dismissed = FALSE")
    if types:
        valid = [
            gaql.enum_value("types", t, _RECOMMENDATION_TYPES)
            for t in types[:30]
        ]
        conditions.append(gaql.in_list("recommendation.type", valid))
    if campaign_id:
        campaign_rn = (
            f"customers/{cid}/campaigns/"
            f"{gaql.id_value('campaign_id', campaign_id)}"
        )
        conditions.append(f"recommendation.campaign = '{campaign_rn}'")

    fields = [c.field for c in RECOMMENDATION_COLUMNS + _DETAIL_COLUMNS]
    for _, api, _ in _IMPACT_FIELDS:
        fields.append(f"recommendation.impact.base_metrics.{api}")
        fields.append(f"recommendation.impact.potential_metrics.{api}")
    raw, next_offset = common.fetch_page(
        cid,
        fields,
        "recommendation",
        conditions,
        [],
        limit,
        offset,
        login_customer_id,
    )

    rows = []
    for r in raw:
        row = common.shape_row(r, RECOMMENDATION_COLUMNS)
        row["id"] = utils.extract_id(row.pop("resource_name", ""))
        if "campaign" in row:
            row["campaign_id"] = utils.extract_id(row.pop("campaign"))
        if "ad_group" in row:
            row["ad_group_id"] = utils.extract_id(row.pop("ad_group"))
        impact = {}
        for key, api, kind in _IMPACT_FIELDS:
            base = common.convert(
                getattr(r.recommendation.impact.base_metrics, api), kind
            )
            potential = common.convert(
                getattr(r.recommendation.impact.potential_metrics, api), kind
            )
            delta = round(potential - base, 2)
            if delta:
                impact[key] = delta
        if impact:
            row["impact"] = impact
        details = {
            k: v
            for k, v in common.shape_row(r, _DETAIL_COLUMNS).items()
            if v not in (0, 0.0)
        }
        if details:
            row["details"] = details
        rows.append(row)

    by_type: dict[str, int] = {}
    for row in rows:
        by_type[row.get("type", "OTHER")] = (
            by_type.get(row.get("type", "OTHER"), 0) + 1
        )
    summary = "\n".join(
        line
        for line in [
            f"**Recommendations** - {cid}: {len(rows)} on this page ("
            + ", ".join(f"{k} {v}" for k, v in sorted(by_type.items()))
            + "). Read only: nothing is applied.",
            common.md_table(
                rows,
                [
                    ("type", "Type"),
                    ("campaign_id", "Campaign"),
                    ("impact", "Impact"),
                ],
            ),
        ]
        if line
    )
    return common.envelope(
        "recommendations_list",
        cid,
        account,
        rows,
        summary,
        next_offset=next_offset,
    )
