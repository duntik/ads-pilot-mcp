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

"""Undo support: snapshot storage and reverse-operation builder.

When a mutate tool commits an UPDATE that it described with ``FieldChange``
objects, the safety hook stores a ``ChangeRecord`` (one JSON file per change)
holding the "before" values. ``undo_change`` later loads the record, checks
that the fields still hold the "after" values (no drift), and builds the
reverse operations, which go through the normal propose -> confirm flow.

Only simple field updates are reversible: statuses, budget amounts, bids and
bidding targets (see ``changes.RESOURCE_SPECS``).
"""

from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
import datetime
import enum
import json
import os
import re
import secrets
import tempfile
from typing import Any

from ads_mcp import utils
from ads_mcp.safety.changes import (
    RESOURCE_SPECS,
    FieldChange,
    resource_name_pattern,
)

CHANGE_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
_ROAS_TOLERANCE = 1e-6


def new_change_id() -> str:
    """A sortable, unguessable change ID such as ``20260101T120000Z-1a2b3c4d``."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return f"{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"


@dataclass
class ChangeRecord:
    """Everything needed to reverse one committed mutation."""

    change_id: str
    created_at: str
    customer_id: str
    tool: str
    field_changes: list[FieldChange]
    login_customer_id: str | None = None
    undo_of: str | None = None
    undone_by: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def reversible(self) -> bool:
        return bool(self.field_changes) and all(
            fc.reversible for fc in self.field_changes
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "created_at": self.created_at,
            "customer_id": self.customer_id,
            "login_customer_id": self.login_customer_id,
            "tool": self.tool,
            "field_changes": [fc.to_dict() for fc in self.field_changes],
            "undo_of": self.undo_of,
            "undone_by": self.undone_by,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChangeRecord":
        return cls(
            change_id=str(data["change_id"]),
            created_at=str(data.get("created_at", "")),
            customer_id=str(data["customer_id"]),
            login_customer_id=data.get("login_customer_id"),
            tool=str(data.get("tool", "")),
            field_changes=[
                FieldChange.from_dict(fc)
                for fc in data.get("field_changes", [])
            ],
            undo_of=data.get("undo_of"),
            undone_by=data.get("undone_by"),
            extra=dict(data.get("extra") or {}),
        )


class UndoStore:
    """One JSON file per change ID under a private directory."""

    def __init__(self, directory: str):
        self.directory = directory

    def _path(self, change_id: str) -> str:
        if not CHANGE_ID_PATTERN.match(change_id or ""):
            raise ValueError(f"Invalid change_id {change_id!r}.")
        return os.path.join(self.directory, f"{change_id}.json")

    def save(self, record: ChangeRecord) -> None:
        path = self._path(record.change_id)
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(record.to_dict(), file, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def load(self, change_id: str) -> ChangeRecord | None:
        path = self._path(change_id)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as file:
            return ChangeRecord.from_dict(json.load(file))

    def list(
        self, customer_id: str | None = None, limit: int = 20
    ) -> list[ChangeRecord]:
        """Most recent first."""
        if not os.path.isdir(self.directory):
            return []
        ids = sorted(
            (
                name[:-5]
                for name in os.listdir(self.directory)
                if name.endswith(".json") and CHANGE_ID_PATTERN.match(name[:-5])
            ),
            reverse=True,
        )
        records: list[ChangeRecord] = []
        for change_id in ids:
            record = self.load(change_id)
            if record is None:
                continue
            if customer_id and record.customer_id != customer_id:
                continue
            records.append(record)
            if len(records) >= limit:
                break
        return records


# ---------------------------------------------------------------------------
# Reading current values (drift check, and "before" snapshots for stage 2).
# ---------------------------------------------------------------------------


def normalize_value(value: Any) -> Any:
    """Converts an API value to the JSON-friendly form used in FieldChange."""
    if isinstance(value, enum.Enum):  # proto-plus enums are IntEnums
        return value.name
    return value


def values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, bool) or isinstance(b, bool):
            return a == b
        return abs(float(a) - float(b)) <= _ROAS_TOLERANCE * max(
            1.0, abs(float(a))
        )
    return a == b


def validate_resource_name(
    resource_type: str, resource_name: str, customer_id: str
) -> None:
    spec = RESOURCE_SPECS.get(resource_type)
    if spec is None:
        raise ValueError(f"Unsupported resource type {resource_type!r}.")
    match = resource_name_pattern(spec).match(resource_name)
    if not match:
        raise ValueError(
            f"Invalid {resource_type} resource name {resource_name!r}."
        )
    if match.group(1) != utils.clean_customer_id(customer_id):
        raise ValueError(
            f"Resource {resource_name!r} does not belong to customer "
            f"{customer_id}."
        )


def fetch_current_values(
    customer_id: str,
    resource_type: str,
    resource_names: Sequence[str],
    fields: Sequence[str],
    login_customer_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Reads the current value of known ``fields`` for each resource.

    Every input is validated against the known resource types, fields and
    resource-name patterns before it is put into GAQL, so no caller-supplied
    text reaches the query unchecked.
    """
    spec = RESOURCE_SPECS.get(resource_type)
    if spec is None:
        raise ValueError(f"Unsupported resource type {resource_type!r}.")
    unknown = [f for f in fields if f not in spec.fields]
    if unknown:
        raise ValueError(f"Unsupported {resource_type} field(s): {unknown}.")
    for rn in resource_names:
        validate_resource_name(resource_type, rn, customer_id)
    if not resource_names:
        return {}

    select = [f"{resource_type}.resource_name"] + [
        f"{resource_type}.{f}" for f in fields
    ]
    names = ", ".join(f"'{rn}'" for rn in resource_names)
    query = (
        f"SELECT {', '.join(select)} FROM {resource_type} "
        f"WHERE {resource_type}.resource_name IN ({names})"
    )
    service = utils.get_googleads_service(
        "GoogleAdsService", login_customer_id=login_customer_id
    )
    result: dict[str, dict[str, Any]] = {}
    for batch in service.search_stream(
        customer_id=utils.clean_customer_id(customer_id), query=query
    ):
        for row in batch.results:
            resource = getattr(row, resource_type)
            values = {}
            for f in fields:
                values[f] = normalize_value(_get_nested(resource, f))
            result[resource.resource_name] = values
    return result


def _get_nested(obj: Any, path: str) -> Any:
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


MISSING = object()


def read_current_values(
    customer_id: str,
    changes: Iterable[FieldChange],
    login_customer_id: str | None = None,
) -> dict[tuple[str, str], Any]:
    """Returns ``{(resource_name, field): current value}`` for ``changes``.

    Resources that no longer exist map to ``MISSING``.
    """
    grouped: "OrderedDict[str, list[FieldChange]]" = OrderedDict()
    for fc in changes:
        grouped.setdefault(fc.resource_type, []).append(fc)

    values: dict[tuple[str, str], Any] = {}
    for resource_type, fcs in grouped.items():
        names = list(dict.fromkeys(fc.resource_name for fc in fcs))
        fields = list(dict.fromkeys(fc.field for fc in fcs))
        current = fetch_current_values(
            customer_id, resource_type, names, fields, login_customer_id
        )
        for fc in fcs:
            row = current.get(fc.resource_name)
            values[(fc.resource_name, fc.field)] = (
                MISSING if row is None else row[fc.field]
            )
    return values


def find_drift(
    changes: Iterable[FieldChange],
    current: dict[tuple[str, str], Any],
) -> list[str]:
    """Returns a message for every field whose current value is not ``after``."""
    problems: list[str] = []
    for fc in changes:
        now = current.get((fc.resource_name, fc.field), MISSING)
        if now is MISSING:
            problems.append(f"{fc.resource_name} was not found.")
        elif not values_equal(now, fc.after):
            problems.append(
                f"{fc.resource_name} {fc.field} is now {now!r}, but this "
                f"change set it to {fc.after!r}: it was changed again later."
            )
    return problems


# ---------------------------------------------------------------------------
# Reverse operations.
# ---------------------------------------------------------------------------


def _set_nested(message: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        message = getattr(message, part)
    setattr(message, parts[-1], value)


def _coerce(client: Any, resource_type: str, path: str, value: Any) -> Any:
    _, enum_type = RESOURCE_SPECS[resource_type].fields[path]
    if enum_type:
        enum_cls = getattr(
            client.get_type(enum_type), enum_type[: -len("Enum")]
        )
        try:
            return enum_cls[str(value)]
        except KeyError as exc:
            raise ValueError(
                f"{value!r} is not a valid {enum_type} value."
            ) from exc
    if path.endswith("_micros"):
        return int(value)
    return float(value)


def build_mutate_operations(
    client: Any, changes: Sequence[FieldChange]
) -> list[Any]:
    """Builds ``MutateOperation`` objects that set each field to ``after``.

    Changes to the same resource are merged into one update with a combined
    update mask. Pass ``[fc.reversed() for fc in record.field_changes]`` to
    build the undo of a record.
    """
    by_resource: "OrderedDict[tuple[str, str], list[FieldChange]]" = (
        OrderedDict()
    )
    for fc in changes:
        if not fc.reversible:
            raise ValueError(
                f"Field change {fc.resource_type}.{fc.field} on "
                f"{fc.resource_name} cannot be applied automatically."
            )
        by_resource.setdefault((fc.resource_type, fc.resource_name), []).append(
            fc
        )

    operations = []
    for (resource_type, resource_name), fcs in by_resource.items():
        spec = RESOURCE_SPECS[resource_type]
        op = client.get_type(spec.operation_type)
        op.update.resource_name = resource_name
        paths = []
        for fc in fcs:
            _set_nested(
                op.update,
                fc.field,
                _coerce(client, resource_type, fc.field, fc.after),
            )
            if fc.field not in paths:
                paths.append(fc.field)
        op.update_mask.paths.extend(paths)
        mutate_op = client.get_type("MutateOperation")
        setattr(mutate_op, spec.mutate_operation_field, op)
        operations.append(mutate_op)
    return operations
