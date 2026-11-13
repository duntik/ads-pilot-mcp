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

"""Tools of the ``safety`` namespace: settings, change list and undo."""

from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from mcp.types import ToolAnnotations

from ads_mcp import propose_and_confirm as pac
from ads_mcp import utils
from ads_mcp.safety import hooks, undo
from ads_mcp.safety.changes import FieldChange

safety_mcp = FastMCP("safety")


@safety_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_safety_settings() -> dict[str, Any]:
    """Returns the active safety settings: read-only mode, the customer
    allow-list and the budget, bid and operation limits.

    Call this before proposing a change to learn which limits apply. Money
    limits are in each account's own currency units, not micros.
    """
    data = hooks.install().settings.to_public_dict()
    if utils.is_remote_oauth_mode():
        # Do not reveal server file paths to remote users.
        for key in ("audit_log_path", "undo_dir"):
            data.pop(key, None)
    return data


@safety_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_changes(
    customer_id: str | int | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Lists recent reversible changes made through this server, newest first.

    Use it to find the `change_id` to pass to `undo_change`. Only field
    updates that the safety layer could snapshot (statuses, budgets, bids,
    bidding targets) are listed.

    Args:
        customer_id: Optional customer ID filter (e.g. '1234567890').
        limit: Maximum number of changes to return (1-100).
    """
    limit = max(1, min(int(limit), 100))
    cid = utils.clean_customer_id(customer_id) if customer_id else None
    records = hooks.install().undo_store().list(customer_id=cid, limit=limit)
    out = []
    for record in records:
        out.append(
            {
                "change_id": record.change_id,
                "created_at": record.created_at,
                "customer_id": record.customer_id,
                "tool": record.tool,
                "undo_of": record.undo_of,
                "undone_by": record.undone_by,
                "changes": [
                    f"{fc.resource_name} {fc.field}: {fc.before!r} -> "
                    f"{fc.after!r}"
                    for fc in record.field_changes
                ],
            }
        )
    return out


@safety_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
def undo_change(
    change_id: str,
    validate_only: bool = True,
    checksum: str | None = None,
    force: bool = False,
    login_customer_id: str | int | None = None,
) -> str:
    """Reverts a change made through this server by restoring the "before"
    values that were saved when the change was committed.

    Works for simple field updates: campaign, ad group, keyword and ad
    status; campaign budget amounts; CPC bids; tCPA / tROAS targets. Use
    `list_changes` to find the `change_id`.

    Before reverting, the tool reads the current values. If any field was
    changed again after the original change, the undo is refused unless
    `force=True`. Budget and bid limits apply to the undo as to any change.

    Propose & Confirm Workflow (`validate_only` and `checksum`):
    1. Call `undo_change` with `validate_only=True` (the default). The tool
       validates the reverse operations with the API and returns
       `change_items` and a `checksum` without changing anything.
    2. Present the `change_items` to the user and wait for explicit approval.
    3. Call `undo_change` again with `validate_only=False`, the same
       parameters and the `checksum`.

    Args:
        change_id: The change ID returned when the change was committed
            (e.g. '20260101T120000Z-1a2b3c4d').
        validate_only: If True (default), only proposes the undo.
        checksum: Checksum from the previous `validate_only=True` call.
            Required when `validate_only=False`.
        force: Revert even if a field was changed again after the original
            change. The fields are set to the saved "before" values.
        login_customer_id: Optional manager customer ID; defaults to the one
            used for the original change.
    """
    hook = hooks.install()
    store = hook.undo_store()
    change_id = (change_id or "").strip()
    try:
        record = store.load(change_id)
    except ValueError as exc:
        raise ToolError(f"Error: {exc}") from exc
    if record is None:
        raise ToolError(
            f"Error: No reversible change with ID '{change_id}' was found. "
            "Use list_changes to see the changes that can be reverted."
        )
    if record.undone_by:
        raise ToolError(
            f"Error: Change '{change_id}' was already reverted by change "
            f"'{record.undone_by}'."
        )
    if not record.reversible:
        raise ToolError(f"Error: Change '{change_id}' cannot be reverted.")

    customer_id = record.customer_id
    login = (
        utils.clean_customer_id(login_customer_id)
        if login_customer_id is not None
        else record.login_customer_id
    )

    try:
        current = undo.read_current_values(
            customer_id, record.field_changes, login
        )
    except GoogleAdsException as exc:
        raise ToolError(utils.format_google_ads_error(exc)) from exc

    missing = [
        fc.resource_name
        for fc in record.field_changes
        if current.get((fc.resource_name, fc.field), undo.MISSING)
        is undo.MISSING
    ]
    if missing:
        raise ToolError(
            "Error: These resources no longer exist, so the change cannot be "
            "reverted: " + ", ".join(dict.fromkeys(missing))
        )
    drift = undo.find_drift(record.field_changes, current)
    if drift and not force:
        raise ToolError(
            "Error: The account changed after this change was made:\n- "
            + "\n- ".join(drift)
            + "\nCall again with force=True to restore the saved values "
            "anyway."
        )

    # "before" of the reverse change is what the account holds right now.
    reverse = [
        FieldChange(
            resource_type=fc.resource_type,
            resource_name=fc.resource_name,
            field=fc.field,
            before=current[(fc.resource_name, fc.field)],
            after=fc.before,
        )
        for fc in record.field_changes
    ]

    client = utils.get_googleads_client(login_customer_id=login)
    service = utils.get_googleads_service_from_client(
        client, "GoogleAdsService"
    )
    try:
        operations = undo.build_mutate_operations(client, reverse)
    except ValueError as exc:
        raise ToolError(f"Error: {exc}") from exc

    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = customer_id
    request.mutate_operations.extend(operations)

    change_items = [
        pac.ChangeItem(
            operation=utils.MutateOperation.UPDATE.value,
            entity_readable=fc.resource_type,
            primary_key_readable=fc.resource_name,
            field_readable=fc.field,
            value_readable=str(fc.after),
            original_value_readable=str(fc.before),
        )
        for fc in reverse
    ]

    def _validate() -> None:
        request.validate_only = True
        service.mutate(request=request)

    def _commit() -> str:
        request.validate_only = False
        response = service.mutate(request=request)
        count = len(response.mutate_operation_responses)
        return f"Reverted change {change_id}: {count} resource(s) updated."

    def _mark_undone(new_change_id: str) -> None:
        record.undone_by = new_change_id
        store.save(record)

    return hooks.guarded_propose_and_confirm(
        tool="undo_change",
        customer_id=customer_id,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "operation": utils.MutateOperation.UPDATE.value,
            "change_id": change_id,
            "force": bool(force),
        },
        change_items=change_items,
        validate_fn=_validate,
        commit_fn=_commit,
        field_changes=reverse,
        operation_count=len(operations),
        login_customer_id=login,
        undo_of=change_id,
        on_committed=_mark_undone,
        error_prefix=f"Failed to undo change {change_id}",
    )
