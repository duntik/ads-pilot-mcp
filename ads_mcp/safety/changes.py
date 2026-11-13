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

"""Structured description of field-level changes and the fields we know.

A ``FieldChange`` says "field F of resource R goes from BEFORE to AFTER".
Mutate tools that change simple fields (status, budgets, bids) describe their
change with ``FieldChange`` objects. The policy checks, the audit log and the
undo store all work from this description.
"""

from dataclasses import dataclass
import re
from typing import Any


class FieldKind:
    """How a field is treated by the policy checks."""

    STATUS = "status"
    BUDGET = "budget"  # micros, daily budget amount
    CPC_BID = "cpc_bid"  # micros, max CPC
    TARGET_CPA = "target_cpa"  # micros
    TARGET_ROAS = "target_roas"  # ratio, e.g. 3.5 == 350 %


@dataclass(frozen=True)
class ResourceSpec:
    """How to read and write one resource type."""

    resource_type: str  # GAQL resource name, e.g. "campaign"
    collection: str  # resource-name collection, e.g. "campaigns"
    operation_type: str  # e.g. "CampaignOperation"
    mutate_operation_field: str  # field on MutateOperation
    composite_id: bool  # True for "{ad_group_id}~{id}" resources
    fields: dict[str, tuple[str, str | None]]  # path -> (kind, enum type)


RESOURCE_SPECS: dict[str, ResourceSpec] = {
    "campaign": ResourceSpec(
        resource_type="campaign",
        collection="campaigns",
        operation_type="CampaignOperation",
        mutate_operation_field="campaign_operation",
        composite_id=False,
        fields={
            "status": (FieldKind.STATUS, "CampaignStatusEnum"),
            "target_cpa.target_cpa_micros": (FieldKind.TARGET_CPA, None),
            "maximize_conversions.target_cpa_micros": (
                FieldKind.TARGET_CPA,
                None,
            ),
            "target_roas.target_roas": (FieldKind.TARGET_ROAS, None),
            "maximize_conversion_value.target_roas": (
                FieldKind.TARGET_ROAS,
                None,
            ),
        },
    ),
    "campaign_budget": ResourceSpec(
        resource_type="campaign_budget",
        collection="campaignBudgets",
        operation_type="CampaignBudgetOperation",
        mutate_operation_field="campaign_budget_operation",
        composite_id=False,
        fields={"amount_micros": (FieldKind.BUDGET, None)},
    ),
    "ad_group": ResourceSpec(
        resource_type="ad_group",
        collection="adGroups",
        operation_type="AdGroupOperation",
        mutate_operation_field="ad_group_operation",
        composite_id=False,
        fields={
            "status": (FieldKind.STATUS, "AdGroupStatusEnum"),
            "cpc_bid_micros": (FieldKind.CPC_BID, None),
            "target_cpa_micros": (FieldKind.TARGET_CPA, None),
            "target_roas": (FieldKind.TARGET_ROAS, None),
        },
    ),
    "ad_group_criterion": ResourceSpec(
        resource_type="ad_group_criterion",
        collection="adGroupCriteria",
        operation_type="AdGroupCriterionOperation",
        mutate_operation_field="ad_group_criterion_operation",
        composite_id=True,
        fields={
            "status": (FieldKind.STATUS, "AdGroupCriterionStatusEnum"),
            "cpc_bid_micros": (FieldKind.CPC_BID, None),
        },
    ),
    "ad_group_ad": ResourceSpec(
        resource_type="ad_group_ad",
        collection="adGroupAds",
        operation_type="AdGroupAdOperation",
        mutate_operation_field="ad_group_ad_operation",
        composite_id=True,
        fields={"status": (FieldKind.STATUS, "AdGroupAdStatusEnum")},
    ),
}


def resource_name_pattern(spec: ResourceSpec) -> re.Pattern[str]:
    ident = r"\d+~\d+" if spec.composite_id else r"\d+"
    return re.compile(rf"^customers/(\d+)/{spec.collection}/{ident}$")


def field_kind(resource_type: str, field: str) -> str | None:
    """Returns the ``FieldKind`` of a known field, or ``None``."""
    spec = RESOURCE_SPECS.get(resource_type)
    if spec is None or field not in spec.fields:
        return None
    return spec.fields[field][0]


@dataclass(frozen=True)
class FieldChange:
    """One field of one resource, before and after a mutation.

    Values are JSON-friendly: enum fields hold the enum *name* (``"PAUSED"``),
    money fields hold integer micros, ROAS fields hold floats.
    """

    resource_type: str
    resource_name: str
    field: str
    before: Any
    after: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_type": self.resource_type,
            "resource_name": self.resource_name,
            "field": self.field,
            "before": self.before,
            "after": self.after,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FieldChange":
        return cls(
            resource_type=str(data["resource_type"]),
            resource_name=str(data["resource_name"]),
            field=str(data["field"]),
            before=data.get("before"),
            after=data.get("after"),
        )

    def reversed(self) -> "FieldChange":
        return FieldChange(
            resource_type=self.resource_type,
            resource_name=self.resource_name,
            field=self.field,
            before=self.after,
            after=self.before,
        )

    @property
    def reversible(self) -> bool:
        """True when the field is known and a "before" value was captured."""
        if self.before is None:
            return False
        if field_kind(self.resource_type, self.field) is None:
            return False
        spec = RESOURCE_SPECS[self.resource_type]
        return bool(resource_name_pattern(spec).match(self.resource_name))
