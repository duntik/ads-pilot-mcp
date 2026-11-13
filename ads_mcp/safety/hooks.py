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

"""Plugs the safety layer into ``ads_mcp.propose_and_confirm``.

``propose_and_confirm`` (upstream code) calls an optional hook:

* ``hook.before(...)`` at the start of both phases (propose and confirm). It
  enforces read-only mode, the customer allow-list, the operation-count limit
  and, when the tool described its change with ``FieldChange`` objects, the
  budget and bid limits. A violation raises ``ToolError`` and is written to
  the audit log as ``BLOCKED``; no checksum is issued and nothing is sent.
* ``hook.wrap_commit(commit_fn, ...)`` wraps the commit so that the result
  (``COMMITTED`` / ``FAILED``) is written to the audit log and, for
  reversible field updates, a ``ChangeRecord`` is stored for ``undo_change``.

Upstream mutate tools get read-only mode, the allow-list, the operation limit
and the audit log without any change to their code. ads-pilot-mcp tools call
``guarded_propose_and_confirm`` instead, which also passes the structured
``FieldChange`` list (via a context variable) so the hook can apply budget
and bid limits and record undo snapshots.
"""

from collections.abc import Callable, Mapping, Sequence
import contextvars
from dataclasses import dataclass, field
import logging
from typing import Any

from fastmcp.exceptions import ToolError

from ads_mcp import propose_and_confirm as pac
from ads_mcp import utils
from ads_mcp.safety import audit_log, policy, settings as settings_mod
from ads_mcp.safety.changes import FieldChange
from ads_mcp.safety.undo import ChangeRecord, UndoStore, new_change_id

logger = logging.getLogger(__name__)

# Key added to the checksum payload so the "before" values seen at propose
# time must still match at confirm time.
FIELD_CHANGES_PAYLOAD_KEY = "__ads_pilot_field_changes__"


@dataclass
class MutationContext:
    """Extra information an ads-pilot-mcp tool passes to the hook."""

    tool: str
    field_changes: list[FieldChange] = field(default_factory=list)
    operation_count: int | None = None
    login_customer_id: str | None = None
    undo_of: str | None = None
    # Called after a successful commit with the new change_id.
    on_committed: Callable[[str], None] | None = None


_context: contextvars.ContextVar[MutationContext | None] = (
    contextvars.ContextVar("ads_pilot_mutation_context", default=None)
)


def current_context() -> MutationContext | None:
    return _context.get()


def _tool_name(payload: Any, ctx: MutationContext | None) -> str:
    if ctx is not None:
        return ctx.tool
    if isinstance(payload, Mapping) and payload.get("tool"):
        return str(payload["tool"])
    return "unknown"


def _operation_count(payload: Any, ctx: MutationContext | None) -> int:
    if ctx is not None and ctx.operation_count is not None:
        return ctx.operation_count
    if isinstance(payload, (list, tuple)):
        return len(payload)
    if isinstance(payload, Mapping) and isinstance(
        payload.get("operations"), (list, tuple)
    ):
        return len(payload["operations"])
    return 1


def _summary(change_items: Sequence[Any]) -> list[dict[str, str]]:
    items = []
    for item in list(change_items)[:50]:
        items.append(item.to_dict() if hasattr(item, "to_dict") else dict(item))
    return items


class SafetyHook:
    """The object installed into ``propose_and_confirm``."""

    def __init__(
        self,
        settings_provider: Callable[
            [], settings_mod.SafetySettings
        ] = settings_mod.get_settings,
    ):
        self._settings_provider = settings_provider

    @property
    def settings(self) -> settings_mod.SafetySettings:
        return self._settings_provider()

    def audit(self) -> audit_log.AuditLog:
        s = self.settings
        return audit_log.AuditLog(s.audit_log_path, enabled=s.audit_log_enabled)

    def undo_store(self) -> UndoStore:
        return UndoStore(self.settings.undo_dir)

    def _base_entry(
        self,
        event: str,
        customer_id: str,
        operation_payload: Any,
        change_items: Sequence[Any],
    ) -> dict[str, Any]:
        ctx = current_context()
        entry: dict[str, Any] = {
            "event": event,
            "tool": _tool_name(operation_payload, ctx),
            "customer_id": utils.clean_customer_id(customer_id),
            "operation_count": _operation_count(operation_payload, ctx),
            "operations": _summary(change_items),
        }
        if ctx is not None:
            if ctx.login_customer_id:
                entry["login_customer_id"] = utils.clean_customer_id(
                    ctx.login_customer_id
                )
            if ctx.field_changes:
                entry["field_changes"] = [
                    fc.to_dict() for fc in ctx.field_changes
                ]
            if ctx.undo_of:
                entry["undo_of"] = ctx.undo_of
        return entry

    def before(
        self,
        *,
        customer_id: str,
        validate_only: bool,
        operation_payload: Any,
        change_items: Sequence[Any],
    ) -> None:
        s = self.settings
        ctx = current_context()
        if s.read_only:
            violations = [
                "The server is in read-only mode (safety.read_only / "
                "ADS_PILOT_READ_ONLY). Mutations are disabled."
            ]
        else:
            violations = policy.evaluate(
                s,
                customer_id,
                _operation_count(operation_payload, ctx),
                ctx.field_changes if ctx is not None else (),
            )
        if not violations:
            return
        entry = self._base_entry(
            audit_log.BLOCKED, customer_id, operation_payload, change_items
        )
        entry["phase"] = "propose" if validate_only else "confirm"
        entry["violations"] = violations
        self.audit().append(entry)
        raise ToolError(
            "Blocked by the ads-pilot-mcp safety policy:\n- "
            + "\n- ".join(violations)
        )

    def wrap_commit(
        self,
        commit_fn: Callable[[], str],
        *,
        customer_id: str,
        operation_payload: Any,
        change_items: Sequence[Any],
    ) -> Callable[[], str]:
        def _guarded_commit() -> str:
            ctx = current_context()
            change_id = new_change_id()
            try:
                result = commit_fn()
            except Exception as exc:
                entry = self._base_entry(
                    audit_log.FAILED,
                    customer_id,
                    operation_payload,
                    change_items,
                )
                entry["change_id"] = change_id
                entry["error"] = str(exc)
                self.audit().append(entry)
                raise

            entry = self._base_entry(
                audit_log.COMMITTED,
                customer_id,
                operation_payload,
                change_items,
            )
            entry["change_id"] = change_id
            entry["result"] = str(result)

            if ctx is None or not ctx.field_changes:
                self.audit().append(entry)
                return result

            record = ChangeRecord(
                change_id=change_id,
                created_at=audit_log.utc_now_iso(),
                customer_id=utils.clean_customer_id(customer_id),
                login_customer_id=(
                    utils.clean_customer_id(ctx.login_customer_id)
                    if ctx.login_customer_id
                    else None
                ),
                tool=ctx.tool,
                field_changes=list(ctx.field_changes),
                undo_of=ctx.undo_of,
            )
            entry["reversible"] = record.reversible
            if record.reversible:
                try:
                    self.undo_store().save(record)
                except OSError as exc:
                    logger.error("Could not save undo snapshot: %s", exc)
                    entry["reversible"] = False
                    entry["undo_store_error"] = str(exc)
            self.audit().append(entry)

            if ctx.on_committed is not None:
                # The API change already succeeded; a bookkeeping error must
                # not be reported as a failed mutation.
                try:
                    ctx.on_committed(change_id)
                except Exception as exc:
                    logger.error(
                        "Post-commit step for %s failed: %s", change_id, exc
                    )

            suffix = f"\nchange_id: {change_id}"
            if entry["reversible"]:
                suffix += " (can be reverted with undo_change)"
            return f"{result}{suffix}"

        return _guarded_commit


_installed_hook: SafetyHook | None = None


def install(hook: SafetyHook | None = None) -> SafetyHook:
    """Installs the safety hook into ``propose_and_confirm`` (idempotent)."""
    global _installed_hook
    if hook is None:
        hook = _installed_hook or SafetyHook()
    _installed_hook = hook
    pac.set_safety_hook(hook)
    return hook


def uninstall() -> None:
    global _installed_hook
    _installed_hook = None
    pac.set_safety_hook(None)


def guarded_propose_and_confirm(
    *,
    tool: str,
    customer_id: str,
    validate_only: bool,
    checksum: str | None,
    operation_payload: Mapping[str, Any],
    change_items: Sequence[pac.ChangeItem],
    commit_fn: Callable[[], str],
    validate_fn: Callable[[], Any] | None = None,
    field_changes: Sequence[FieldChange] = (),
    operation_count: int | None = None,
    login_customer_id: str | int | None = None,
    undo_of: str | None = None,
    on_committed: Callable[[str], None] | None = None,
    error_prefix: str = "Failed to execute mutation",
) -> str:
    """``propose_and_confirm`` plus structured safety information.

    Use this from every ads-pilot-mcp mutate tool. ``field_changes`` (with
    the "before" values read from the API) are folded into the checksum, so
    if the account changes between propose and confirm the checksum no longer
    matches and the caller has to propose again.
    """
    if _installed_hook is None:
        install()
    payload = dict(operation_payload)
    payload["tool"] = tool
    payload[FIELD_CHANGES_PAYLOAD_KEY] = [fc.to_dict() for fc in field_changes]
    ctx = MutationContext(
        tool=tool,
        field_changes=list(field_changes),
        operation_count=operation_count,
        login_customer_id=(
            str(login_customer_id) if login_customer_id is not None else None
        ),
        undo_of=undo_of,
        on_committed=on_committed,
    )
    token = _context.set(ctx)
    try:
        return pac.propose_and_confirm(
            customer_id=customer_id,
            validate_only=validate_only,
            checksum=checksum,
            operation_payload=payload,
            change_items=change_items,
            validate_fn=validate_fn,
            commit_fn=commit_fn,
            error_prefix=error_prefix,
        )
    finally:
        _context.reset(token)
