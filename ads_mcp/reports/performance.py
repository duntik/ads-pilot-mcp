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

"""Performance reports: account overview, landing pages, geo, ads."""

import re
from typing import Any, Literal

from mcp.types import ToolAnnotations

from ads_mcp.reports import common, gaql, reports_mcp
from ads_mcp.reports.common import METRIC_COLUMNS, Column

_READ_ONLY = ToolAnnotations(readOnlyHint=True)
_GEO_RN = re.compile(r"^geoTargetConstants/\d+$")

CAMPAIGN_COLUMNS = (
    Column("campaign_id", "campaign.id", "int"),
    Column("campaign", "campaign.name"),
    Column("status", "campaign.status", "enum"),
    Column("channel", "campaign.advertising_channel_type", "enum"),
    *METRIC_COLUMNS,
)
TOTAL_COLUMNS = METRIC_COLUMNS


def _date_filters(
    dr: gaql.DateRange, include_zero_impressions: bool
) -> list[str]:
    conditions = [dr.condition()]
    if not include_zero_impressions:
        conditions.append("metrics.impressions > 0")
    return conditions


@reports_mcp.tool(annotations=_READ_ONLY)
@common.google_ads_errors
def account_overview(
    customer_id: str | int,
    date_range: gaql.DateRangePreset = "LAST_30_DAYS",
    start_date: str | None = None,
    end_date: str | None = None,
    include_zero_impressions: bool = False,
    limit: int = 25,
    offset: int = 0,
    login_customer_id: str | int | None = None,
) -> dict[str, Any]:
    """Spend, clicks, impressions, conversions and cost per conversion by
    campaign for a date range, plus account totals.

    Campaigns are sorted by cost (highest first). Totals cover the whole
    account for the range, not only the rows on this page. Money is in the
    account currency (not micros). Campaigns paused or removed during the
    range are included, so totals are complete.

    Args:
        customer_id: Google Ads customer ID (e.g. '1234567890' or '123-456-7890').
        date_range: A preset range, or CUSTOM together with start_date and end_date.
        start_date: Custom range start, YYYY-MM-DD (implies CUSTOM).
        end_date: Custom range end, YYYY-MM-DD (implies CUSTOM).
        include_zero_impressions: Also list campaigns with no impressions.
        limit: Rows per page (1-200).
        offset: Row offset for the next page (use `next_offset` from the previous call).
        login_customer_id: Optional manager account ID for the login-customer-id header.
    """
    cid = gaql.customer_id(customer_id)
    dr = gaql.date_range(date_range, start_date, end_date)
    limit, offset = common.page_args(limit, offset)
    account = common.account_info(cid, login_customer_id)

    raw, next_offset = common.fetch_page(
        cid,
        [c.field for c in CAMPAIGN_COLUMNS],
        "campaign",
        _date_filters(dr, include_zero_impressions),
        ["metrics.cost_micros DESC"],
        limit,
        offset,
        login_customer_id,
    )
    rows = [
        common.add_ratios(common.shape_row(r, CAMPAIGN_COLUMNS)) for r in raw
    ]

    totals_raw = common.fetch_rows(
        cid,
        gaql.build_query(
            [c.field for c in TOTAL_COLUMNS], "customer", [dr.condition()]
        ),
        login_customer_id,
    )
    totals: dict[str, Any] = {}
    for r in totals_raw:
        totals = common.add_ratios(common.shape_row(r, TOTAL_COLUMNS))
    for key in ("impressions", "clicks", "cost", "conversions"):
        totals.setdefault(key, 0)

    cur = account["currency"]
    summary = "\n".join(
        line
        for line in [
            f"**Account overview** - {account['name'] or cid} ({cid}), "
            f"{dr.label()}, {cur}",
            f"Spend {common.money(totals['cost'], cur)} | clicks "
            f"{totals['clicks']:,} | impressions {totals['impressions']:,} | "
            f"conversions {totals['conversions']:,.2f} | cost/conv "
            f"{common.money(totals.get('cost_per_conv'), cur)}",
            common.md_table(
                rows,
                [
                    ("campaign", "Campaign"),
                    ("cost", "Cost"),
                    ("clicks", "Clicks"),
                    ("conversions", "Conv"),
                    ("cost_per_conv", "Cost/conv"),
                ],
            ),
        ]
        if line
    )
    return common.envelope(
        "account_overview",
        cid,
        account,
        rows,
        summary,
        date_range=dr,
        next_offset=next_offset,
        totals=totals,
    )


@reports_mcp.tool(annotations=_READ_ONLY)
@common.google_ads_errors
def landing_page_performance(
    customer_id: str | int,
    date_range: gaql.DateRangePreset = "LAST_30_DAYS",
    start_date: str | None = None,
    end_date: str | None = None,
    expanded: bool = False,
    limit: int = 25,
    offset: int = 0,
    login_customer_id: str | int | None = None,
) -> dict[str, Any]:
    """Performance by landing page, sorted by cost.

    By default groups by the final URL set on the ad (`landing_page_view`).
    With `expanded=True` groups by the URL users actually landed on, after
    URL expansion and tracking parameters (`expanded_landing_page_view`).
    Includes the mobile-friendly click rate and the landing page speed score
    (1-10) where Google reports them.

    Args:
        customer_id: Google Ads customer ID (e.g. '1234567890').
        date_range: A preset range, or CUSTOM together with start_date and end_date.
        start_date: Custom range start, YYYY-MM-DD.
        end_date: Custom range end, YYYY-MM-DD.
        expanded: Group by the expanded (actual) landing page URL.
        limit: Rows per page (1-200).
        offset: Row offset for the next page.
        login_customer_id: Optional manager account ID.
    """
    cid = gaql.customer_id(customer_id)
    dr = gaql.date_range(date_range, start_date, end_date)
    limit, offset = common.page_args(limit, offset)
    account = common.account_info(cid, login_customer_id)

    if expanded:
        resource = "expanded_landing_page_view"
        url_field = "expanded_landing_page_view.expanded_final_url"
    else:
        resource = "landing_page_view"
        url_field = "landing_page_view.unexpanded_final_url"
    columns = (
        Column("url", url_field),
        *METRIC_COLUMNS,
        Column(
            "mobile_friendly_clicks_pct",
            "metrics.mobile_friendly_clicks_percentage",
            "pct",
        ),
        Column("speed_score", "metrics.speed_score", "int"),
    )
    raw, next_offset = common.fetch_page(
        cid,
        [c.field for c in columns],
        resource,
        _date_filters(dr, False),
        ["metrics.cost_micros DESC"],
        limit,
        offset,
        login_customer_id,
    )
    rows = []
    for r in raw:
        row = common.add_ratios(common.shape_row(r, columns))
        if not row.get("speed_score"):
            row.pop("speed_score", None)
        rows.append(row)

    summary = "\n".join(
        line
        for line in [
            f"**Landing pages** ({'expanded' if expanded else 'final'} URLs) - "
            f"{cid}, {dr.label()}, {account['currency']}",
            common.md_table(
                rows,
                [
                    ("url", "URL"),
                    ("cost", "Cost"),
                    ("clicks", "Clicks"),
                    ("conversions", "Conv"),
                    ("speed_score", "Speed"),
                ],
            ),
        ]
        if line
    )
    return common.envelope(
        "landing_page_performance",
        cid,
        account,
        rows,
        summary,
        date_range=dr,
        next_offset=next_offset,
    )


_GEO_LEVEL_SEGMENT = {
    "region": "segments.geo_target_region",
    "city": "segments.geo_target_city",
    "most_specific": "segments.geo_target_most_specific_location",
}


def _resolve_geo_names(
    cid: str, resource_names: list[str], login_customer_id: Any
) -> dict[str, str]:
    names = [rn for rn in dict.fromkeys(resource_names) if _GEO_RN.match(rn)]
    if not names:
        return {}
    query = gaql.build_query(
        [
            "geo_target_constant.resource_name",
            "geo_target_constant.canonical_name",
        ],
        "geo_target_constant",
        [gaql.in_list("geo_target_constant.resource_name", names)],
    )
    out = {}
    for row in common.fetch_rows(cid, query, login_customer_id):
        gtc = row.geo_target_constant
        out[gtc.resource_name] = gtc.canonical_name
    return out


@reports_mcp.tool(annotations=_READ_ONLY)
@common.google_ads_errors
def geo_performance(
    customer_id: str | int,
    date_range: gaql.DateRangePreset = "LAST_30_DAYS",
    start_date: str | None = None,
    end_date: str | None = None,
    view: Literal["geographic", "user_location"] = "geographic",
    level: Literal["country", "region", "city", "most_specific"] = "region",
    limit: int = 25,
    offset: int = 0,
    login_customer_id: str | int | None = None,
) -> dict[str, Any]:
    """Performance by location, sorted by cost, with location names.

    `view="geographic"` (`geographic_view`) reports the location that
    triggered the ad, split by location type: LOCATION_OF_PRESENCE (people
    in the area) or AREA_OF_INTEREST (people searching for it), so a place
    can appear twice. `view="user_location"` (`user_location_view`) reports
    where users physically were, with `targeted` telling whether that
    location was targeted.

    Args:
        customer_id: Google Ads customer ID (e.g. '1234567890').
        date_range: A preset range, or CUSTOM together with start_date and end_date.
        start_date: Custom range start, YYYY-MM-DD.
        end_date: Custom range end, YYYY-MM-DD.
        view: 'geographic' or 'user_location'.
        level: Location granularity: country, region, city or most_specific.
        limit: Rows per page (1-200).
        offset: Row offset for the next page.
        login_customer_id: Optional manager account ID.
    """
    cid = gaql.customer_id(customer_id)
    dr = gaql.date_range(date_range, start_date, end_date)
    limit, offset = common.page_args(limit, offset)
    view = gaql.enum_value(
        "view", view, {"GEOGRAPHIC", "USER_LOCATION"}
    ).lower()
    level = gaql.enum_value(
        "level", level, {"COUNTRY", "REGION", "CITY", "MOST_SPECIFIC"}
    ).lower()
    account = common.account_info(cid, login_customer_id)

    resource = (
        "geographic_view" if view == "geographic" else "user_location_view"
    )
    columns = [Column("country_id", f"{resource}.country_criterion_id", "int")]
    if view == "geographic":
        columns.append(
            Column("location_type", "geographic_view.location_type", "enum")
        )
    else:
        columns.append(
            Column("targeted", "user_location_view.targeting_location", "bool")
        )
    if level != "country":
        columns.append(Column("location_rn", _GEO_LEVEL_SEGMENT[level]))
    columns.extend(METRIC_COLUMNS)

    raw, next_offset = common.fetch_page(
        cid,
        [c.field for c in columns],
        resource,
        _date_filters(dr, False),
        ["metrics.cost_micros DESC"],
        limit,
        offset,
        login_customer_id,
    )
    rows = [common.add_ratios(common.shape_row(r, columns)) for r in raw]

    lookup = []
    for row in rows:
        rn = row.get("location_rn") or (
            f"geoTargetConstants/{row['country_id']}"
            if "country_id" in row
            else None
        )
        row["_rn"] = rn
        if rn:
            lookup.append(rn)
    names = _resolve_geo_names(cid, lookup, login_customer_id)
    for row in rows:
        rn = row.pop("_rn")
        row.pop("location_rn", None)
        if rn:
            row["location_id"] = int(rn.split("/")[-1])
            row["location"] = names.get(rn, rn)

    summary = "\n".join(
        line
        for line in [
            f"**Geo performance** ({view}, {level}) - {cid}, {dr.label()}, "
            f"{account['currency']}",
            common.md_table(
                rows,
                [
                    ("location", "Location"),
                    ("cost", "Cost"),
                    ("clicks", "Clicks"),
                    ("conversions", "Conv"),
                    ("cost_per_conv", "Cost/conv"),
                ],
            ),
        ]
        if line
    )
    notes = []
    if view == "geographic":
        notes.append(
            "Rows are split by location_type, so one place can appear twice."
        )
    return common.envelope(
        "geo_performance",
        cid,
        account,
        rows,
        summary,
        date_range=dr,
        next_offset=next_offset,
        notes=notes,
    )


AD_COLUMNS = (
    Column("campaign", "campaign.name"),
    Column("ad_group_id", "ad_group.id", "int"),
    Column("ad_group", "ad_group.name"),
    Column("ad_id", "ad_group_ad.ad.id", "int"),
    Column("type", "ad_group_ad.ad.type", "enum"),
    Column("status", "ad_group_ad.status", "enum"),
    Column("ad_strength", "ad_group_ad.ad_strength", "enum"),
    Column("approval", "ad_group_ad.policy_summary.approval_status", "enum"),
    Column("final_urls", "ad_group_ad.ad.final_urls", "list"),
    *METRIC_COLUMNS,
)
ASSET_COLUMNS = (
    Column("ad_group_ad", "ad_group_ad_asset_view.ad_group_ad"),
    Column("field", "ad_group_ad_asset_view.field_type", "enum"),
    Column("label", "ad_group_ad_asset_view.performance_label", "enum"),
    Column("pinned", "ad_group_ad_asset_view.pinned_field", "enum"),
    Column("text", "asset.text_asset.text"),
)


@reports_mcp.tool(annotations=_READ_ONLY)
@common.google_ads_errors
def ad_performance(
    customer_id: str | int,
    date_range: gaql.DateRangePreset = "LAST_30_DAYS",
    start_date: str | None = None,
    end_date: str | None = None,
    campaign_id: str | None = None,
    ad_group_id: str | None = None,
    include_assets: bool = True,
    limit: int = 10,
    offset: int = 0,
    login_customer_id: str | int | None = None,
) -> dict[str, Any]:
    """Ad performance with ad strength and approval status, sorted by cost.

    For responsive search ads, `include_assets=True` adds each headline and
    description with its performance label (BEST, GOOD, LOW, LEARNING,
    PENDING) where Google reports one, and its pin position.

    Args:
        customer_id: Google Ads customer ID (e.g. '1234567890').
        date_range: A preset range, or CUSTOM together with start_date and end_date.
        start_date: Custom range start, YYYY-MM-DD.
        end_date: Custom range end, YYYY-MM-DD.
        campaign_id: Only ads in this campaign (numeric ID).
        ad_group_id: Only ads in this ad group (numeric ID).
        include_assets: Add RSA headline/description performance labels.
        limit: Ads per page (1-200; keep it small when include_assets is on).
        offset: Row offset for the next page.
        login_customer_id: Optional manager account ID.
    """
    cid = gaql.customer_id(customer_id)
    dr = gaql.date_range(date_range, start_date, end_date)
    limit, offset = common.page_args(limit, offset)
    account = common.account_info(cid, login_customer_id)

    conditions = _date_filters(dr, False)
    if campaign_id:
        conditions.append(
            f"campaign.id = {gaql.id_value('campaign_id', campaign_id)}"
        )
    if ad_group_id:
        conditions.append(
            f"ad_group.id = {gaql.id_value('ad_group_id', ad_group_id)}"
        )

    raw, next_offset = common.fetch_page(
        cid,
        [c.field for c in AD_COLUMNS],
        "ad_group_ad",
        conditions,
        ["metrics.cost_micros DESC"],
        limit,
        offset,
        login_customer_id,
    )
    rows = [common.add_ratios(common.shape_row(r, AD_COLUMNS)) for r in raw]

    rsa_keys = [
        f"customers/{cid}/adGroupAds/{row['ad_group_id']}~{row['ad_id']}"
        for row in rows
        if row.get("type") == "RESPONSIVE_SEARCH_AD"
        and "ad_group_id" in row
        and "ad_id" in row
    ]
    if include_assets and rsa_keys:
        query = gaql.build_query(
            [c.field for c in ASSET_COLUMNS],
            "ad_group_ad_asset_view",
            [
                gaql.in_list("ad_group_ad_asset_view.ad_group_ad", rsa_keys),
                gaql.in_list(
                    "ad_group_ad_asset_view.field_type",
                    ["HEADLINE", "DESCRIPTION"],
                ),
                "ad_group_ad_asset_view.enabled = TRUE",
            ],
        )
        assets: dict[str, list[dict[str, Any]]] = {}
        for r in common.fetch_rows(cid, query, login_customer_id):
            asset = common.shape_row(r, ASSET_COLUMNS)
            key = asset.pop("ad_group_ad", "")
            if asset.get("pinned") == "UNSPECIFIED":
                asset.pop("pinned")
            assets.setdefault(key, []).append(asset)
        for row in rows:
            key = f"customers/{cid}/adGroupAds/{row.get('ad_group_id')}~{row.get('ad_id')}"
            if key in assets:
                row["assets"] = assets[key]
                labels: dict[str, int] = {}
                for a in assets[key]:
                    if "label" in a:
                        labels[a["label"]] = labels.get(a["label"], 0) + 1
                if labels:
                    row["asset_labels"] = labels

    summary = "\n".join(
        line
        for line in [
            f"**Ad performance** - {cid}, {dr.label()}, {account['currency']}",
            common.md_table(
                rows,
                [
                    ("ad_id", "Ad"),
                    ("type", "Type"),
                    ("ad_strength", "Strength"),
                    ("cost", "Cost"),
                    ("conversions", "Conv"),
                ],
            ),
        ]
        if line
    )
    return common.envelope(
        "ad_performance",
        cid,
        account,
        rows,
        summary,
        date_range=dr,
        next_offset=next_offset,
    )
