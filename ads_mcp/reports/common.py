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

"""Shared helpers for report tools: querying, row shaping, paging, output.

Every report returns the same compact envelope::

    {
      "report": "account_overview",
      "customer_id": "1234567890",
      "currency": "USD",
      "date_range": {"preset": "LAST_30_DAYS"},
      "rows": [...],             # short keys, money in currency units
      "row_count": 25,
      "next_offset": 25,         # null when there are no more rows
      "totals": {...},           # when the report has totals
      "notes": [...],            # caveats, only when relevant
      "summary": "...markdown..."
    }
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
import enum
import functools
import threading
from typing import Any

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException

from ads_mcp import utils
from ads_mcp.reports import gaql

DEFAULT_LIMIT = 25
MAX_LIMIT = 200
MAX_OFFSET = 5000
MAX_STRING = 200
MICROS = 1_000_000
_EMPTY_ENUMS = frozenset({"UNSPECIFIED", "UNKNOWN"})


@dataclass(frozen=True)
class Column:
    """Maps one GAQL field to one output key.

    kind: ``str``, ``int``, ``float``, ``money`` (micros -> currency),
    ``enum`` (enum name), ``pct`` (ratio 0-1 -> percent), ``bool``,
    ``list`` (repeated scalar field).
    """

    key: str
    field: str
    kind: str = "str"


METRIC_COLUMNS = (
    Column("impressions", "metrics.impressions", "int"),
    Column("clicks", "metrics.clicks", "int"),
    Column("cost", "metrics.cost_micros", "money"),
    Column("conversions", "metrics.conversions", "float"),
    Column("conv_value", "metrics.conversions_value", "float"),
)


def _get(obj: Any, path: str) -> Any:
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def convert(value: Any, kind: str) -> Any:
    if kind == "money":
        return round(float(value) / MICROS, 2)
    if kind == "int":
        return int(value)
    if kind == "float":
        return round(float(value), 2)
    if kind == "pct":
        return round(float(value) * 100, 2)
    if kind == "bool":
        return bool(value)
    if kind == "enum":
        name = value.name if isinstance(value, enum.Enum) else str(value)
        # UNSPECIFIED / UNKNOWN carry no information; drop them.
        return None if name in _EMPTY_ENUMS else name
    if kind == "list":
        return [truncate(str(v)) for v in list(value)[:20]]
    if isinstance(value, enum.Enum):
        return value.name
    return truncate(str(value))


def truncate(text: str, limit: int = MAX_STRING) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def shape_row(row: Any, columns: Sequence[Column]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in columns:
        value = convert(_get(row, col.field), col.kind)
        if value in ("", None, []):
            continue
        out[col.key] = value
    return out


def add_ratios(row: dict[str, Any]) -> dict[str, Any]:
    """Adds ctr %, avg_cpc and cost_per_conv from the base metrics."""
    clicks = row.get("clicks", 0) or 0
    impressions = row.get("impressions", 0) or 0
    cost = row.get("cost", 0.0) or 0.0
    conversions = row.get("conversions", 0.0) or 0.0
    if impressions:
        row["ctr"] = round(clicks / impressions * 100, 2)
    if clicks:
        row["avg_cpc"] = round(cost / clicks, 2)
    if conversions:
        row["cost_per_conv"] = round(cost / conversions, 2)
    return row


def page_args(limit: int, offset: int) -> tuple[int, int]:
    try:
        limit = int(limit)
        offset = int(offset)
    except (TypeError, ValueError) as exc:
        raise ToolError(
            "Error: 'limit' and 'offset' must be integers."
        ) from exc
    if not 1 <= limit <= MAX_LIMIT:
        raise ToolError(f"Error: 'limit' must be between 1 and {MAX_LIMIT}.")
    if not 0 <= offset <= MAX_OFFSET:
        raise ToolError(f"Error: 'offset' must be between 0 and {MAX_OFFSET}.")
    return limit, offset


def fetch_rows(
    customer_id: str,
    query: str,
    login_customer_id: str | int | None = None,
) -> list[Any]:
    """Runs ``query`` with search_stream and returns the raw rows."""
    service = utils.get_googleads_service(
        "GoogleAdsService", login_customer_id=login_customer_id
    )
    utils.logger.info("ads_mcp.reports query %s", query)
    rows: list[Any] = []
    for batch in service.search_stream(customer_id=customer_id, query=query):
        rows.extend(batch.results)
    return rows


def fetch_page(
    customer_id: str,
    fields: Sequence[str],
    resource: str,
    conditions: Sequence[str],
    order_by: Sequence[str],
    limit: int,
    offset: int,
    login_customer_id: str | int | None = None,
) -> tuple[list[Any], int | None]:
    """Fetches one page. Returns (rows, next_offset or None).

    GAQL has no OFFSET, so the query asks for ``offset + limit + 1`` rows
    and the page is sliced here; the extra row tells whether more exist.
    """
    query = gaql.build_query(
        fields, resource, conditions, order_by, limit=offset + limit + 1
    )
    rows = fetch_rows(customer_id, query, login_customer_id)
    page = rows[offset : offset + limit]
    next_offset = offset + limit if len(rows) > offset + limit else None
    return page, next_offset


_account_cache: dict[tuple[str, str | None], dict[str, str]] = {}
_account_lock = threading.Lock()


def account_info(
    customer_id: str, login_customer_id: str | int | None = None
) -> dict[str, str]:
    """Currency, time zone and name of the account.

    Cached per process in local (stdio) mode. In remote OAuth mode several
    users share the process, so every call asks the API with the caller's
    own credentials.
    """
    key = (customer_id, str(login_customer_id) if login_customer_id else None)
    use_cache = not utils.is_remote_oauth_mode()
    with _account_lock:
        if use_cache and key in _account_cache:
            return _account_cache[key]
    query = gaql.build_query(
        [
            "customer.descriptive_name",
            "customer.currency_code",
            "customer.time_zone",
        ],
        "customer",
        limit=1,
    )
    rows = fetch_rows(customer_id, query, login_customer_id)
    info = {"name": "", "currency": "", "time_zone": ""}
    if rows:
        customer = rows[0].customer
        info = {
            "name": truncate(customer.descriptive_name or ""),
            "currency": customer.currency_code or "",
            "time_zone": customer.time_zone or "",
        }
    if use_cache:
        with _account_lock:
            _account_cache[key] = info
    return info


def clear_account_cache() -> None:
    with _account_lock:
        _account_cache.clear()


def envelope(
    report: str,
    customer_id: str,
    account: Mapping[str, str],
    rows: list[dict[str, Any]],
    summary: str,
    *,
    date_range: gaql.DateRange | None = None,
    next_offset: int | None = None,
    totals: Mapping[str, Any] | None = None,
    notes: Iterable[str] = (),
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "report": report,
        "customer_id": customer_id,
        "currency": account.get("currency", ""),
    }
    if date_range is not None:
        out["date_range"] = date_range.to_dict()
    if extra:
        out.update(extra)
    out["rows"] = rows
    out["row_count"] = len(rows)
    out["next_offset"] = next_offset
    if totals is not None:
        out["totals"] = dict(totals)
    notes = [n for n in notes if n]
    if notes:
        out["notes"] = notes
    out["summary"] = summary
    return out


def money(value: float | int | None, currency: str) -> str:
    if value is None:
        return "-"
    return f"{value:,.2f} {currency}".strip()


def md_table(
    rows: Sequence[Mapping[str, Any]],
    columns: Sequence[tuple[str, str]],
    max_rows: int = 5,
) -> str:
    """A small markdown table of the first ``max_rows`` rows."""
    if not rows:
        return ""
    header = "| " + " | ".join(label for _, label in columns) + " |"
    sep = "|" + "|".join("---" for _ in columns) + "|"
    lines = [header, sep]
    for row in rows[:max_rows]:
        cells = []
        for key, _ in columns:
            value = row.get(key, "")
            if isinstance(value, float):
                value = f"{value:,.2f}"
            cells.append(str(value).replace("|", "\\|")[:60])
        lines.append("| " + " | ".join(cells) + " |")
    if len(rows) > max_rows:
        lines.append(f"_...and {len(rows) - max_rows} more rows in `rows`._")
    return "\n".join(lines)


def google_ads_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Turns ``GoogleAdsException`` into a readable ``ToolError``."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except GoogleAdsException as ex:
            raise ToolError(utils.format_google_ads_error(ex)) from ex

    return wrapper
