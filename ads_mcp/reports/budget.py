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

"""Budget pacing for the current month."""

import calendar
import datetime
from typing import Any

from mcp.types import ToolAnnotations

from ads_mcp.reports import common, gaql, reports_mcp
from ads_mcp.reports.common import Column

# Google Ads limits monthly charges for a daily budget to daily x 30.4.
AVG_DAYS_PER_MONTH = 30.4
# Pace bands: below 85 % is UNDER, above 115 % is OVER.
_UNDER = 85.0
_OVER = 115.0
_MAX_CAMPAIGNS = 2000

BUDGET_COLUMNS = (
    Column("campaign_id", "campaign.id", "int"),
    Column("campaign", "campaign.name"),
    Column("status", "campaign.status", "enum"),
    Column("budget_rn", "campaign_budget.resource_name"),
    Column("budget_name", "campaign_budget.name"),
    Column("daily_budget", "campaign_budget.amount_micros", "money"),
    Column("period", "campaign_budget.period", "enum"),
    Column("total_budget", "campaign_budget.total_amount_micros", "money"),
    Column("shared", "campaign_budget.explicitly_shared", "bool"),
)


def account_now(time_zone: str) -> tuple[datetime.datetime, bool]:
    """Current time in the account time zone (falls back to UTC)."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.datetime.now(ZoneInfo(time_zone)), True
    except Exception:  # missing tz database (e.g. Windows without tzdata)
        return datetime.datetime.now(datetime.timezone.utc), False


def pace_status(pace_pct: float | None) -> str:
    if pace_pct is None:
        return "NO_DATA"
    if pace_pct < _UNDER:
        return "UNDER"
    if pace_pct > _OVER:
        return "OVER"
    return "ON_TRACK"


@reports_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
@common.google_ads_errors
def budget_pacing(
    customer_id: str | int,
    limit: int = 50,
    offset: int = 0,
    login_customer_id: str | int | None = None,
) -> dict[str, Any]:
    """Month-to-date spend against each campaign budget, with a projection
    for the full month.

    One row per budget (a shared budget lists all its campaigns). For daily
    budgets: `expected_mtd` = daily budget x elapsed days, `pace_pct` =
    spend / expected, `projected_month` = spend / elapsed days x days in the
    month, and `monthly_limit` = daily budget x 30.4 (the most Google
    charges per month). `pace` is UNDER (<85 %), ON_TRACK or OVER (>115 %).
    Uses the account time zone. Rows are sorted by month-to-date spend.

    Args:
        customer_id: Google Ads customer ID (e.g. '1234567890').
        limit: Budgets per page (1-200).
        offset: Row offset for the next page.
        login_customer_id: Optional manager account ID.
    """
    cid = gaql.customer_id(customer_id)
    limit, offset = common.page_args(limit, offset)
    account = common.account_info(cid, login_customer_id)
    notes: list[str] = []

    now, tz_ok = account_now(account.get("time_zone") or "UTC")
    if not tz_ok:
        notes.append(
            "Account time zone unavailable on this server; used UTC for "
            "elapsed days."
        )
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    elapsed = (now.day - 1) + (now.hour * 60 + now.minute) / 1440.0
    elapsed = max(elapsed, 1 / 24)  # avoid division by ~0 just after midnight

    spend_query = gaql.build_query(
        [c.field for c in BUDGET_COLUMNS] + ["metrics.cost_micros"],
        "campaign",
        ["segments.date DURING THIS_MONTH"],
        limit=_MAX_CAMPAIGNS,
    )
    live_query = gaql.build_query(
        [c.field for c in BUDGET_COLUMNS],
        "campaign",
        ["campaign.status = 'ENABLED'"],
        limit=_MAX_CAMPAIGNS,
    )
    spend_rows = common.fetch_rows(cid, spend_query, login_customer_id)
    live_rows = common.fetch_rows(cid, live_query, login_customer_id)
    if len(spend_rows) >= _MAX_CAMPAIGNS or len(live_rows) >= _MAX_CAMPAIGNS:
        notes.append(f"Only the first {_MAX_CAMPAIGNS} campaigns were read.")

    campaigns: dict[int, dict[str, Any]] = {}
    for r in live_rows:
        row = common.shape_row(r, BUDGET_COLUMNS)
        row["spend_micros"] = 0
        campaigns[row["campaign_id"]] = row
    for r in spend_rows:
        row = common.shape_row(r, BUDGET_COLUMNS)
        existing = campaigns.setdefault(
            row["campaign_id"], {**row, "spend_micros": 0}
        )
        existing["spend_micros"] += int(r.metrics.cost_micros)

    budgets: dict[str, dict[str, Any]] = {}
    for c in campaigns.values():
        key = c.get("budget_rn") or f"campaign:{c['campaign_id']}"
        b = budgets.setdefault(
            key,
            {
                "budget_id": int(key.split("/")[-1]) if "/" in key else None,
                "budget_name": c.get("budget_name", ""),
                "period": c.get("period", ""),
                "daily_budget": c.get("daily_budget"),
                "total_budget": c.get("total_budget"),
                "shared": c.get("shared", False),
                "campaigns": [],
                "spend_micros": 0,
                "active": False,
            },
        )
        b["campaigns"].append(
            {
                "id": c["campaign_id"],
                "name": c.get("campaign", ""),
                "status": c.get("status", ""),
            }
        )
        b["spend_micros"] += c["spend_micros"]
        if c.get("status") == "ENABLED":
            b["active"] = True

    rows: list[dict[str, Any]] = []
    non_daily = 0
    for b in budgets.values():
        spend = round(b.pop("spend_micros") / common.MICROS, 2)
        active = b.pop("active")
        row = {k: v for k, v in b.items() if v not in (None, "")}
        if b["budget_id"] is None:
            row.pop("budget_id", None)
        if not b.get("total_budget"):
            row.pop("total_budget", None)
        row["spend_mtd"] = spend
        daily = b.get("daily_budget")
        if b.get("period") == "DAILY" and daily:
            expected = daily * elapsed
            pace = spend / expected * 100 if expected else None
            projected = spend / elapsed * days_in_month
            row["monthly_limit"] = round(daily * AVG_DAYS_PER_MONTH, 2)
            row["expected_mtd"] = round(expected, 2)
            row["pace_pct"] = round(pace, 1) if pace is not None else None
            row["projected_month"] = round(projected, 2)
            row["pace"] = pace_status(pace) if active else "INACTIVE"
        else:
            non_daily += 1
            row["pace"] = "NOT_DAILY"
        if len(row["campaigns"]) > 10:
            more = len(row["campaigns"]) - 10
            row["campaigns"] = row["campaigns"][:10] + [{"more": more}]
        rows.append(row)

    rows.sort(key=lambda r: r["spend_mtd"], reverse=True)
    page = rows[offset : offset + limit]
    next_offset = offset + limit if len(rows) > offset + limit else None
    if non_daily:
        notes.append(
            f"{non_daily} budget(s) are not daily budgets; pacing is not "
            "computed for them."
        )
    notes.append(
        "Enabled campaigns that started or changed budget mid-month make "
        "expected_mtd approximate."
    )

    daily_rows = [r for r in rows if "projected_month" in r]
    cur = account["currency"]
    totals = {
        "daily_budget": round(
            sum(r.get("daily_budget", 0) for r in daily_rows), 2
        ),
        "spend_mtd": round(sum(r["spend_mtd"] for r in rows), 2),
        "projected_month": round(
            sum(r["projected_month"] for r in daily_rows), 2
        ),
        "monthly_limit": round(sum(r["monthly_limit"] for r in daily_rows), 2),
    }
    over = [r for r in rows if r.get("pace") == "OVER"]
    under = [r for r in rows if r.get("pace") == "UNDER"]
    summary = "\n".join(
        line
        for line in [
            f"**Budget pacing** - {cid}, {now:%Y-%m} (day {elapsed:.1f} of "
            f"{days_in_month}), {cur}",
            f"Spend MTD {common.money(totals['spend_mtd'], cur)}; projected "
            f"{common.money(totals['projected_month'], cur)} vs monthly limit "
            f"{common.money(totals['monthly_limit'], cur)}. {len(over)} over "
            f"pace, {len(under)} under pace.",
            common.md_table(
                page,
                [
                    ("budget_name", "Budget"),
                    ("daily_budget", "Daily"),
                    ("spend_mtd", "Spend MTD"),
                    ("projected_month", "Projected"),
                    ("pace", "Pace"),
                ],
            ),
        ]
        if line
    )
    return common.envelope(
        "budget_pacing",
        cid,
        account,
        page,
        summary,
        next_offset=next_offset,
        totals=totals,
        notes=notes,
        extra={
            "month": f"{now:%Y-%m}",
            "days_elapsed": round(elapsed, 2),
            "days_in_month": days_in_month,
        },
    )
