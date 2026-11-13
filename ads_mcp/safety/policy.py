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

"""Policy checks for proposed mutations.

Every function here is pure: it takes settings and a description of the
change and returns a list of human-readable violations (empty = allowed).
"""

from collections.abc import Iterable

from ads_mcp import utils
from ads_mcp.safety.changes import FieldChange, FieldKind, field_kind
from ads_mcp.safety.settings import PolicyLimits, SafetySettings

_MICROS = 1_000_000
# Tolerance for float rounding when comparing percentages.
_EPS = 1e-9


def _pct_change(before: float, after: float) -> float:
    return (after - before) / before * 100.0


def _money(micros: float) -> str:
    return f"{micros / _MICROS:,.2f}"


def _as_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def check_customer(settings: SafetySettings, customer_id: str) -> list[str]:
    cid = utils.clean_customer_id(customer_id)
    if not settings.is_customer_allowed(cid):
        return [
            f"Customer {cid} is not in the allow-list "
            "(safety.allowed_customer_ids / ADS_PILOT_ALLOWED_CUSTOMER_IDS)."
        ]
    return []


def check_operation_count(
    limits: PolicyLimits, operation_count: int
) -> list[str]:
    limit = limits.max_operations_per_mutate
    if limit is not None and operation_count > limit:
        return [
            f"{operation_count} operations in one mutate exceeds the limit of "
            f"{limit} (max_operations_per_mutate). Split the change into "
            "smaller batches."
        ]
    return []


def check_field_change(limits: PolicyLimits, change: FieldChange) -> list[str]:
    """Checks one field change against the budget and bid limits."""
    kind = field_kind(change.resource_type, change.field)
    if kind is None or kind == FieldKind.STATUS:
        return []

    before = _as_number(change.before)
    after = _as_number(change.after)
    if after is None:
        return []
    where = f"{change.resource_name} {change.field}"
    violations: list[str] = []

    if kind == FieldKind.BUDGET:
        if (
            limits.max_daily_budget is not None
            and after > limits.max_daily_budget * _MICROS + _EPS
        ):
            violations.append(
                f"{where}: new daily budget {_money(after)} is above the "
                f"maximum of {limits.max_daily_budget:,.2f} (max_daily_budget)."
            )
        if (
            limits.max_budget_increase_pct is not None
            and before is not None
            and before > 0
            and after > before
        ):
            pct = _pct_change(before, after)
            if pct > limits.max_budget_increase_pct + _EPS:
                violations.append(
                    f"{where}: budget increase of {pct:.1f}% "
                    f"({_money(before)} -> {_money(after)}) is above the "
                    f"maximum of {limits.max_budget_increase_pct:g}% "
                    "(max_budget_increase_pct)."
                )
        return violations

    # Bids and bidding targets.
    if (
        kind == FieldKind.CPC_BID
        and limits.max_cpc is not None
        and after > limits.max_cpc * _MICROS + _EPS
    ):
        violations.append(
            f"{where}: new CPC bid {_money(after)} is above the maximum of "
            f"{limits.max_cpc:,.2f} (max_cpc)."
        )
    if (
        limits.max_bid_change_pct is not None
        and before is not None
        and before > 0
    ):
        pct = _pct_change(before, after)
        if abs(pct) > limits.max_bid_change_pct + _EPS:
            if kind == FieldKind.TARGET_ROAS:
                shown = f"{before:g} -> {after:g}"
            else:
                shown = f"{_money(before)} -> {_money(after)}"
            violations.append(
                f"{where}: change of {pct:+.1f}% ({shown}) is above the "
                f"maximum of {limits.max_bid_change_pct:g}% "
                "(max_bid_change_pct)."
            )
    return violations


def evaluate(
    settings: SafetySettings,
    customer_id: str,
    operation_count: int,
    field_changes: Iterable[FieldChange] = (),
) -> list[str]:
    """Returns every policy violation for a proposed mutation."""
    limits = settings.limits_for(customer_id)
    violations = check_customer(settings, customer_id)
    violations += check_operation_count(limits, operation_count)
    for change in field_changes:
        violations += check_field_change(limits, change)
    return violations
