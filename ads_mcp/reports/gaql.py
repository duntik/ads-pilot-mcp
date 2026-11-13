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

"""Safe GAQL construction for the report tools.

Report tools never put caller text into a query. Field and resource names
come from constants in the code; every caller value is validated and turned
into a literal here: IDs must be digits, dates must be real ISO dates, enum
values must match ``^[A-Z][A-Z0-9_]*$`` (and usually a fixed allow-list),
numbers must be finite. Anything else raises ``ToolError``.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import datetime
import math
import re
from typing import Literal, get_args

from fastmcp.exceptions import ToolError

from ads_mcp import utils

DateRangePreset = Literal[
    "TODAY",
    "YESTERDAY",
    "LAST_7_DAYS",
    "LAST_14_DAYS",
    "LAST_30_DAYS",
    "THIS_MONTH",
    "LAST_MONTH",
    "CUSTOM",
]
DATE_PRESETS = frozenset(get_args(DateRangePreset)) - {"CUSTOM"}

_FIELD = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_RESOURCE = re.compile(r"^[a-z][a-z0-9_]*$")
_ENUM = re.compile(r"^[A-Z][A-Z0-9_]*$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_CUSTOM_RANGE_DAYS = 3 * 366


def customer_id(value: str | int) -> str:
    cleaned = utils.clean_customer_id(value)
    if not cleaned or len(cleaned) > 12:
        raise ToolError(
            "Error: 'customer_id' must be a Google Ads customer ID such as "
            "'1234567890' (hyphens are allowed)."
        )
    return cleaned


def id_value(name: str, value: str | int) -> str:
    """Validates a numeric entity ID (campaign, ad group, ...)."""
    text = str(value).strip()
    if not text.isdigit() or len(text) > 20:
        raise ToolError(f"Error: '{name}' must be a numeric ID, got {value!r}.")
    return text


def enum_value(
    name: str, value: str, allowed: Iterable[str] | None = None
) -> str:
    text = str(value).strip().upper()
    if not _ENUM.match(text):
        raise ToolError(f"Error: '{name}' has an invalid value {value!r}.")
    if allowed is not None:
        allowed = sorted(set(allowed))
        if text not in allowed:
            raise ToolError(
                f"Error: '{name}' must be one of: {', '.join(allowed)}."
            )
    return text


def non_negative(name: str, value: float | int) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"Error: '{name}' must be a number.") from exc
    if not math.isfinite(number) or number < 0:
        raise ToolError(f"Error: '{name}' must be a non-negative number.")
    return number


def to_micros(name: str, amount: float | int) -> int:
    return int(round(non_negative(name, amount) * 1_000_000))


def _parse_date(name: str, value: str) -> datetime.date:
    if not isinstance(value, str) or not _DATE.match(value.strip()):
        raise ToolError(f"Error: '{name}' must be a date in YYYY-MM-DD form.")
    try:
        return datetime.date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ToolError(
            f"Error: '{name}' is not a valid date: {value}."
        ) from exc


@dataclass(frozen=True)
class DateRange:
    """A validated date range, rendered as a GAQL condition."""

    preset: str | None = None
    start: datetime.date | None = None
    end: datetime.date | None = None

    def condition(self, field: str = "segments.date") -> str:
        if self.preset:
            return f"{field} DURING {self.preset}"
        return f"{field} BETWEEN '{self.start.isoformat()}' AND '{self.end.isoformat()}'"

    def to_dict(self) -> dict[str, str]:
        if self.preset:
            return {"preset": self.preset}
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}

    def label(self) -> str:
        if self.preset:
            return self.preset
        return f"{self.start.isoformat()} to {self.end.isoformat()}"


def date_range(
    preset: str | None = "LAST_30_DAYS",
    start_date: str | None = None,
    end_date: str | None = None,
) -> DateRange:
    """Validates a preset or a custom ``start_date``/``end_date`` pair.

    Giving ``start_date`` and ``end_date`` implies ``CUSTOM``.
    """
    if start_date or end_date:
        if not (start_date and end_date):
            raise ToolError(
                "Error: provide both 'start_date' and 'end_date' "
                "(YYYY-MM-DD) for a custom date range."
            )
        start = _parse_date("start_date", start_date)
        end = _parse_date("end_date", end_date)
        if start > end:
            raise ToolError("Error: 'start_date' must not be after 'end_date'.")
        if (end - start).days > _MAX_CUSTOM_RANGE_DAYS:
            raise ToolError("Error: custom date ranges are limited to 3 years.")
        return DateRange(start=start, end=end)
    value = enum_value("date_range", preset or "LAST_30_DAYS")
    if value == "CUSTOM":
        raise ToolError(
            "Error: date_range 'CUSTOM' needs 'start_date' and 'end_date'."
        )
    if value not in DATE_PRESETS:
        raise ToolError(
            "Error: 'date_range' must be one of: "
            + ", ".join(sorted(DATE_PRESETS))
            + ", or CUSTOM with start_date and end_date."
        )
    return DateRange(preset=value)


def in_list(field: str, values: Sequence[str], quoted: bool = True) -> str:
    """``field IN (...)`` for values that were already validated."""
    if not values:
        raise ValueError("in_list needs at least one value")
    if quoted:
        items = ", ".join(f"'{v}'" for v in values)
    else:
        items = ", ".join(values)
    return f"{field} IN ({items})"


def build_query(
    fields: Sequence[str],
    resource: str,
    conditions: Sequence[str] = (),
    order_by: Sequence[str] = (),
    limit: int | None = None,
) -> str:
    """Assembles a GAQL query from code constants and validated literals."""
    if not _RESOURCE.match(resource):
        raise ValueError(f"invalid resource {resource!r}")
    for f in fields:
        if not _FIELD.match(f):
            raise ValueError(f"invalid field {f!r}")
    query = f"SELECT {', '.join(fields)} FROM {resource}"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    if order_by:
        query += " ORDER BY " + ", ".join(order_by)
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return query + " PARAMETERS omit_unselected_resource_names=true"
