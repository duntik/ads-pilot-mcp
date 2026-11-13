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

"""Search term and keyword reports, including wasted spend."""

from typing import Any, Literal

from mcp.types import ToolAnnotations

from ads_mcp.reports import common, gaql, reports_mcp
from ads_mcp.reports.common import METRIC_COLUMNS, Column

_READ_ONLY = ToolAnnotations(readOnlyHint=True)
# Search terms already excluded need no new negative keyword.
_EXCLUDED_STATUSES = frozenset({"EXCLUDED", "ADDED_EXCLUDED"})

SEARCH_TERM_COLUMNS = (
    Column("search_term", "search_term_view.search_term"),
    Column("status", "search_term_view.status", "enum"),
    Column("match_type", "segments.search_term_match_type", "enum"),
    Column("campaign_id", "campaign.id", "int"),
    Column("campaign", "campaign.name"),
    Column("ad_group_id", "ad_group.id", "int"),
    Column("ad_group", "ad_group.name"),
    *METRIC_COLUMNS,
)

KEYWORD_COLUMNS = (
    Column("criterion_id", "ad_group_criterion.criterion_id", "int"),
    Column("keyword", "ad_group_criterion.keyword.text"),
    Column("match_type", "ad_group_criterion.keyword.match_type", "enum"),
    Column("status", "ad_group_criterion.status", "enum"),
    Column("campaign_id", "campaign.id", "int"),
    Column("campaign", "campaign.name"),
    Column("ad_group_id", "ad_group.id", "int"),
    Column("ad_group", "ad_group.name"),
    *METRIC_COLUMNS,
)

QUALITY_COLUMNS = (
    Column(
        "quality_score", "ad_group_criterion.quality_info.quality_score", "int"
    ),
    Column(
        "expected_ctr",
        "ad_group_criterion.quality_info.search_predicted_ctr",
        "enum",
    ),
    Column(
        "ad_relevance",
        "ad_group_criterion.quality_info.creative_quality_score",
        "enum",
    ),
    Column(
        "landing_page_experience",
        "ad_group_criterion.quality_info.post_click_quality_score",
        "enum",
    ),
    Column("cpc_bid", "ad_group_criterion.effective_cpc_bid_micros", "money"),
    Column("search_impr_share", "metrics.search_impression_share", "pct"),
)


def _scope(campaign_id: Any, ad_group_id: Any) -> list[str]:
    conditions = []
    if campaign_id:
        conditions.append(
            f"campaign.id = {gaql.id_value('campaign_id', campaign_id)}"
        )
    if ad_group_id:
        conditions.append(
            f"ad_group.id = {gaql.id_value('ad_group_id', ad_group_id)}"
        )
    return conditions


@reports_mcp.tool(annotations=_READ_ONLY)
@common.google_ads_errors
def wasted_spend(
    customer_id: str | int,
    min_cost: float = 0.0,
    date_range: gaql.DateRangePreset = "LAST_30_DAYS",
    start_date: str | None = None,
    end_date: str | None = None,
    source: Literal["both", "search_terms", "keywords"] = "both",
    limit: int = 25,
    offset: int = 0,
    login_customer_id: str | int | None = None,
) -> dict[str, Any]:
    """Search terms and keywords that cost more than `min_cost` and brought
    zero conversions in the date range, sorted by cost (highest first).

    Use it to find negative keyword and pausing candidates. "Zero
    conversions" uses the `conversions` metric, so it depends on which
    conversion actions count as primary.

    Args:
        customer_id: Google Ads customer ID (e.g. '1234567890').
        min_cost: Only rows that cost more than this, in account currency (not micros).
        date_range: A preset range, or CUSTOM together with start_date and end_date.
        start_date: Custom range start, YYYY-MM-DD.
        end_date: Custom range end, YYYY-MM-DD.
        source: 'search_terms', 'keywords' or 'both'.
        limit: Rows per page for each list (1-200).
        offset: Row offset for the next page.
        login_customer_id: Optional manager account ID.
    """
    cid = gaql.customer_id(customer_id)
    dr = gaql.date_range(date_range, start_date, end_date)
    limit, offset = common.page_args(limit, offset)
    source = gaql.enum_value(
        "source", source, {"BOTH", "SEARCH_TERMS", "KEYWORDS"}
    ).lower()
    min_micros = gaql.to_micros("min_cost", min_cost)
    account = common.account_info(cid, login_customer_id)
    conditions = [
        dr.condition(),
        "metrics.conversions = 0",
        f"metrics.cost_micros > {min_micros}",
    ]
    order = ["metrics.cost_micros DESC"]

    sections: dict[str, list[dict[str, Any]]] = {}
    next_offsets = []
    if source in ("both", "search_terms"):
        raw, nxt = common.fetch_page(
            cid,
            [c.field for c in SEARCH_TERM_COLUMNS],
            "search_term_view",
            conditions,
            order,
            limit,
            offset,
            login_customer_id,
        )
        sections["search_terms"] = [
            common.add_ratios(common.shape_row(r, SEARCH_TERM_COLUMNS))
            for r in raw
        ]
        next_offsets.append(nxt)
    if source in ("both", "keywords"):
        raw, nxt = common.fetch_page(
            cid,
            [c.field for c in KEYWORD_COLUMNS],
            "keyword_view",
            conditions,
            order,
            limit,
            offset,
            login_customer_id,
        )
        sections["keywords"] = [
            common.add_ratios(common.shape_row(r, KEYWORD_COLUMNS)) for r in raw
        ]
        next_offsets.append(nxt)

    cur = account["currency"]
    totals = {}
    lines = [
        f"**Wasted spend** (cost > {common.money(min_cost, cur)}, 0 "
        f"conversions) - {cid}, {dr.label()}"
    ]
    for name, rows in sections.items():
        cost = round(sum(r.get("cost", 0) for r in rows), 2)
        totals[name] = {"rows": len(rows), "cost": cost}
        lines.append(
            f"{name.replace('_', ' ').capitalize()}: {len(rows)} rows on this "
            f"page, {common.money(cost, cur)}"
        )
        label_key = "search_term" if name == "search_terms" else "keyword"
        table = common.md_table(
            rows,
            [
                (label_key, label_key.replace("_", " ").capitalize()),
                ("match_type", "Match"),
                ("campaign", "Campaign"),
                ("cost", "Cost"),
                ("clicks", "Clicks"),
            ],
        )
        if table:
            lines.append(table)

    present = [n for n in next_offsets if n is not None]
    out = common.envelope(
        "wasted_spend",
        cid,
        account,
        [],
        "\n".join(lines),
        date_range=dr,
        next_offset=max(present) if present else None,
        totals=totals,
        notes=["Totals are for the rows on this page only."],
        extra={"min_cost": min_cost},
    )
    del out["rows"]
    out["row_count"] = sum(len(v) for v in sections.values())
    for name, rows in sections.items():
        out[name] = rows
    # Keep the summary last for readability.
    out["summary"] = out.pop("summary")
    return out


@reports_mcp.tool(annotations=_READ_ONLY)
@common.google_ads_errors
def search_terms_report(
    customer_id: str | int,
    date_range: gaql.DateRangePreset = "LAST_30_DAYS",
    start_date: str | None = None,
    end_date: str | None = None,
    campaign_id: str | int | None = None,
    ad_group_id: str | int | None = None,
    min_impressions: int = 1,
    negative_min_clicks: int = 5,
    negative_min_cost: float | None = None,
    only_suggested_negatives: bool = False,
    limit: int = 25,
    offset: int = 0,
    login_customer_id: str | int | None = None,
) -> dict[str, Any]:
    """Search terms that triggered ads, with match type and a
    `suggested_negative` flag, sorted by cost.

    `suggested_negative` is a simple heuristic, not a decision: a term is
    flagged when it has 0 conversions, at least `negative_min_clicks`
    clicks, costs at least `negative_min_cost` (when given) and is not
    already excluded. Review the terms before adding negatives.

    Args:
        customer_id: Google Ads customer ID (e.g. '1234567890').
        date_range: A preset range, or CUSTOM together with start_date and end_date.
        start_date: Custom range start, YYYY-MM-DD.
        end_date: Custom range end, YYYY-MM-DD.
        campaign_id: Only this campaign (numeric ID).
        ad_group_id: Only this ad group (numeric ID).
        min_impressions: Minimum impressions for a term to be listed.
        negative_min_clicks: Clicks needed before a non-converting term is flagged.
        negative_min_cost: Optional minimum cost (account currency) before flagging.
        only_suggested_negatives: Return only the flagged terms.
        limit: Rows per page (1-200).
        offset: Row offset for the next page.
        login_customer_id: Optional manager account ID.
    """
    cid = gaql.customer_id(customer_id)
    dr = gaql.date_range(date_range, start_date, end_date)
    limit, offset = common.page_args(limit, offset)
    min_impr = int(gaql.non_negative("min_impressions", min_impressions))
    neg_clicks = int(
        gaql.non_negative("negative_min_clicks", negative_min_clicks)
    )
    neg_cost_micros = (
        gaql.to_micros("negative_min_cost", negative_min_cost)
        if negative_min_cost is not None
        else None
    )
    account = common.account_info(cid, login_customer_id)

    conditions = [dr.condition(), f"metrics.impressions >= {min_impr}"]
    conditions += _scope(campaign_id, ad_group_id)
    if only_suggested_negatives:
        conditions.append("metrics.conversions = 0")
        conditions.append(f"metrics.clicks >= {neg_clicks}")
        if neg_cost_micros is not None:
            conditions.append(f"metrics.cost_micros >= {neg_cost_micros}")
        conditions.append(
            "search_term_view.status NOT IN ('EXCLUDED', 'ADDED_EXCLUDED')"
        )

    raw, next_offset = common.fetch_page(
        cid,
        [c.field for c in SEARCH_TERM_COLUMNS],
        "search_term_view",
        conditions,
        ["metrics.cost_micros DESC"],
        limit,
        offset,
        login_customer_id,
    )
    rows = []
    for r in raw:
        row = common.add_ratios(common.shape_row(r, SEARCH_TERM_COLUMNS))
        cost_micros = int(r.metrics.cost_micros)
        row["suggested_negative"] = bool(
            not row.get("conversions")
            and row.get("clicks", 0) >= neg_clicks
            and (neg_cost_micros is None or cost_micros >= neg_cost_micros)
            and row.get("status") not in _EXCLUDED_STATUSES
        )
        rows.append(row)

    flagged = [r for r in rows if r["suggested_negative"]]
    cur = account["currency"]
    summary = "\n".join(
        line
        for line in [
            f"**Search terms** - {cid}, {dr.label()}, {cur}: {len(rows)} terms "
            f"on this page, {len(flagged)} suggested negatives "
            f"({common.money(round(sum(r.get('cost', 0) for r in flagged), 2), cur)})",
            common.md_table(
                flagged or rows,
                [
                    ("search_term", "Search term"),
                    ("match_type", "Match"),
                    ("cost", "Cost"),
                    ("clicks", "Clicks"),
                    ("conversions", "Conv"),
                ],
            ),
        ]
        if line
    )
    return common.envelope(
        "search_terms_report",
        cid,
        account,
        rows,
        summary,
        date_range=dr,
        next_offset=next_offset,
        notes=[
            "suggested_negative is a heuristic; check intent before adding "
            "negative keywords."
        ],
    )


@reports_mcp.tool(annotations=_READ_ONLY)
@common.google_ads_errors
def keyword_performance(
    customer_id: str | int,
    date_range: gaql.DateRangePreset = "LAST_30_DAYS",
    start_date: str | None = None,
    end_date: str | None = None,
    campaign_id: str | int | None = None,
    ad_group_id: str | int | None = None,
    include_zero_impressions: bool = False,
    limit: int = 25,
    offset: int = 0,
    login_customer_id: str | int | None = None,
) -> dict[str, Any]:
    """Keyword performance with Quality Score and its three components,
    sorted by cost.

    Quality Score components are BELOW_AVERAGE, AVERAGE or ABOVE_AVERAGE:
    `expected_ctr`, `ad_relevance` and `landing_page_experience`. Quality
    Score is the current value, not a value for the date range, and is left
    out when Google has too little data. `cpc_bid` is the effective max CPC.

    Args:
        customer_id: Google Ads customer ID (e.g. '1234567890').
        date_range: A preset range, or CUSTOM together with start_date and end_date.
        start_date: Custom range start, YYYY-MM-DD.
        end_date: Custom range end, YYYY-MM-DD.
        campaign_id: Only this campaign (numeric ID).
        ad_group_id: Only this ad group (numeric ID).
        include_zero_impressions: Also list keywords with no impressions.
        limit: Rows per page (1-200).
        offset: Row offset for the next page.
        login_customer_id: Optional manager account ID.
    """
    cid = gaql.customer_id(customer_id)
    dr = gaql.date_range(date_range, start_date, end_date)
    limit, offset = common.page_args(limit, offset)
    account = common.account_info(cid, login_customer_id)

    columns = KEYWORD_COLUMNS + QUALITY_COLUMNS
    conditions = [dr.condition(), "ad_group_criterion.negative = FALSE"]
    if not include_zero_impressions:
        conditions.append("metrics.impressions > 0")
    conditions += _scope(campaign_id, ad_group_id)

    raw, next_offset = common.fetch_page(
        cid,
        [c.field for c in columns],
        "keyword_view",
        conditions,
        ["metrics.cost_micros DESC"],
        limit,
        offset,
        login_customer_id,
    )
    rows = []
    for r in raw:
        row = common.add_ratios(common.shape_row(r, columns))
        if not row.get("quality_score"):
            row.pop("quality_score", None)
        rows.append(row)

    scored = [r["quality_score"] for r in rows if "quality_score" in r]
    low = [r for r in rows if r.get("quality_score", 10) <= 4]
    summary = "\n".join(
        line
        for line in [
            f"**Keyword performance** - {cid}, {dr.label()}, "
            f"{account['currency']}: {len(rows)} keywords on this page"
            + (
                f", average QS {sum(scored) / len(scored):.1f}, "
                f"{len(low)} with QS <= 4"
                if scored
                else ""
            ),
            common.md_table(
                rows,
                [
                    ("keyword", "Keyword"),
                    ("match_type", "Match"),
                    ("quality_score", "QS"),
                    ("cost", "Cost"),
                    ("conversions", "Conv"),
                ],
            ),
        ]
        if line
    )
    return common.envelope(
        "keyword_performance",
        cid,
        account,
        rows,
        summary,
        date_range=dr,
        next_offset=next_offset,
    )
