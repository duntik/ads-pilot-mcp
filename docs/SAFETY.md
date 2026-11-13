# Safety layer

ads-pilot-mcp is built to let an AI assistant change a Google Ads account
without surprises. This page explains what protects your account, how to
configure it, and what it does not protect against.

Code: [`ads_mcp/safety/`](../ads_mcp/safety). Tests: `tests/pilot/`.

## Layers

| Layer | What it does | Where |
| --- | --- | --- |
| Propose → confirm | Every mutate tool first validates with the API (`validate_only=True`, the default) and returns `change_items` and a `checksum`; nothing changes until the same call is repeated with `validate_only=False` and that checksum. | `ads_mcp/propose_and_confirm.py` (upstream) |
| Read-only mode | Hides every tool not annotated `readOnlyHint=True` and refuses every mutation. | `safety/read_only.py`, `safety/hooks.py` |
| Customer allow-list | Only the listed customer IDs can be changed. | `safety/policy.py` |
| Policy limits | Budget, bid and operation-count limits, checked at propose time and again at confirm time. | `safety/policy.py` |
| Audit log | Append-only JSONL record of every committed, failed and blocked mutation. | `safety/audit_log.py` |
| Undo | Before-values of simple field updates are stored; `undo_change` reverts them through propose → confirm. | `safety/undo.py`, `tools/pilot/safety_tools.py` |

All layers apply to the upstream mutate tools (`create_asset`,
`upload_video_asset`, `manage_labels`, `manage_label_associations`) as well
as to ads-pilot-mcp tools. Budget and bid limits and undo need the
structured before/after description that ads-pilot-mcp tools provide, so for
upstream tools only read-only mode, the allow-list, the operation limit and
the audit log apply.

## Read-only mode

```shell
ADS_PILOT_READ_ONLY=true ads-pilot-mcp
```

or `safety.read_only: true` in `tools_config.yaml`. The server then:

- does not list `create_asset`, `upload_video_asset`, `manage_labels`,
  `manage_label_associations`, `undo_change`, or any later write tool;
- refuses any mutation that reaches `propose_and_confirm` anyway (defence in
  depth), and logs it as `BLOCKED`.

A tool counts as read-only only if it is explicitly annotated
`readOnlyHint=True`; tools without annotations are treated as mutate tools.
Read-only mode is applied when tools are mounted, so restart the server after
changing it.

## Policy limits

| Setting | Default | Checked on |
| --- | --- | --- |
| `allowed_customer_ids` | `[]` (all accounts) | every mutation |
| `max_operations_per_mutate` | `100` | every mutation |
| `max_budget_increase_pct` | `50` | budget amount increases (decreases are always allowed) |
| `max_daily_budget` | none | new budget amount |
| `max_bid_change_pct` | `50` | CPC bids, tCPA and tROAS, up or down |
| `max_cpc` | none | new CPC bids |

Money limits are in the account's own currency units (not micros). Use
`customer_overrides` for accounts in other currencies or with other needs:

```yaml
safety:
  max_daily_budget: 100
  customer_overrides:
    "1234567890":
      max_daily_budget: 2000
      max_cpc: 8
```

A violation is reported to the assistant with the exact limit that was hit,
for example:

```
Blocked by the ads-pilot-mcp safety policy:
- customers/1234567890/campaignBudgets/11 amount_micros: budget increase of
  60.0% (10.00 -> 16.00) is above the maximum of 50% (max_budget_increase_pct).
```

No checksum is issued for a blocked proposal, so it cannot be confirmed.
Large changes have to be made in several smaller steps (or with a higher
limit you set yourself), which leaves room to watch the effect.

Invalid values and unknown keys (for example a typo such as `max_cpc_bid`)
stop the server at start-up, so a limit is never silently ignored.

`get_safety_settings` shows the limits in force.

## Checksums bind the "before" values

ads-pilot-mcp tools read the current value of every field they change and
include those "before" values in the checksum. If the account changes
between propose and confirm (another user, an automated rule, a second
assistant), the checksum no longer matches and the confirm call is refused;
the assistant has to propose again with fresh values.

## Audit log

One JSON object per line, appended to `<state_dir>/audit.jsonl` (configurable
with `audit_log_path` / `ADS_PILOT_AUDIT_LOG_PATH`). The default state
directory is the per-user data directory:

- Linux: `$XDG_DATA_HOME/ads-pilot-mcp` or `~/.local/share/ads-pilot-mcp`
- macOS: `~/Library/Application Support/ads-pilot-mcp`
- Windows: `%LOCALAPPDATA%\ads-pilot-mcp`

Example entry:

```json
{
  "ts": "2026-01-15T10:42:07Z",
  "event": "COMMITTED",
  "change_id": "20260115T104207Z-3f9c1a2b",
  "tool": "set_campaign_budget",
  "customer_id": "1234567890",
  "operation_count": 1,
  "operations": [
    {"operation": "UPDATE", "entity_readable": "Campaign budget",
     "primary_key_readable": "customers/1234567890/campaignBudgets/11",
     "field_readable": "Daily budget", "value_readable": "12.00",
     "original_value_readable": "10.00"}
  ],
  "field_changes": [
    {"resource_type": "campaign_budget",
     "resource_name": "customers/1234567890/campaignBudgets/11",
     "field": "amount_micros", "before": 10000000, "after": 12000000}
  ],
  "reversible": true,
  "result": "Budget updated."
}
```

Events:

- `COMMITTED` — the API accepted the change;
- `FAILED` — the API rejected the commit (`error` holds the message);
- `BLOCKED` — the safety policy refused it (`phase` is `propose` or
  `confirm`, `violations` lists the reasons).

Proposals that pass the policy are not logged; nothing has changed yet.

What is never written: developer tokens, OAuth tokens, client secrets,
passwords, keys, or binary payloads. Keys that look like credentials are
replaced with `[REDACTED]`, token-shaped strings (`ya29.…`, `1//…`, JWTs,
PEM keys) are replaced inside any value, bytes become `<N bytes>`, and long
strings are cut at 300 characters. The log does contain account data
(customer IDs, resource names, campaign names, values), so keep it private.

The file is opened in append mode for every entry and never rewritten. If it
cannot be written, the error is logged and the API result is still returned.
Turn it off with `audit_log: false` (not recommended).

## Undo

When an ads-pilot-mcp tool commits an update it described with before/after
values, the safety layer stores a snapshot in `<state_dir>/undo/<change_id>.json`
and the tool result ends with:

```
change_id: 20260115T104207Z-3f9c1a2b (can be reverted with undo_change)
```

Reversible fields:

| Resource | Fields |
| --- | --- |
| `campaign` | `status`, `target_cpa.target_cpa_micros`, `maximize_conversions.target_cpa_micros`, `target_roas.target_roas`, `maximize_conversion_value.target_roas` |
| `campaign_budget` | `amount_micros` |
| `ad_group` | `status`, `cpc_bid_micros`, `target_cpa_micros`, `target_roas` |
| `ad_group_criterion` (keywords) | `status`, `cpc_bid_micros` |
| `ad_group_ad` | `status` |

`undo_change(change_id)`:

1. loads the snapshot (`list_changes` shows recent ones);
2. reads the current values and refuses if any field was changed again after
   the original change (pass `force=True` to restore anyway);
3. builds the reverse update (one `GoogleAdsService.Mutate` call, so all
   fields revert together or not at all);
4. goes through the normal propose → confirm flow, including the policy
   limits (reverting a budget cut is a budget increase);
5. on success marks the original change as undone, and records the undo as a
   change of its own, so it can be reverted too.

Creations and removals are not reversible yet; stage 2 adds "remove what was
created" for keywords, negatives, assets and ads (see the
[roadmap](ROADMAP.md)).

## What the safety layer does not do

- It does not judge whether a change is a good idea. Read the
  `change_items` before you confirm.
- It cannot stop changes made outside this server (Google Ads UI, scripts,
  automated rules, Google's auto-applied recommendations).
- Money limits do not convert currencies; set `customer_overrides` per
  account.
- Undo restores field values; it cannot undo spend that already happened.
- On a shared HTTP server, the audit log and undo store are shared by all
  users of that server, and entries are not yet tagged with the user's
  identity (planned). Every user can only change accounts their own Google
  login can access.
- The MCP client and model provider see the account data the tools return.

## Writing a new mutate tool

Use `ads_mcp.safety.hooks.guarded_propose_and_confirm` instead of calling
`propose_and_confirm` directly:

```python
from ads_mcp.safety import hooks, undo
from ads_mcp.safety.changes import FieldChange

current = undo.fetch_current_values(
    customer_id, "campaign_budget", [budget_rn], ["amount_micros"]
)
changes = [
    FieldChange(
        "campaign_budget", budget_rn, "amount_micros",
        before=current[budget_rn]["amount_micros"], after=new_micros,
    )
]
return hooks.guarded_propose_and_confirm(
    tool="set_campaign_budget",
    customer_id=customer_id,
    validate_only=validate_only,
    checksum=checksum,
    operation_payload={"budget": budget_rn, "amount_micros": new_micros},
    change_items=change_items,
    validate_fn=_validate,
    commit_fn=_commit,
    field_changes=changes,
    operation_count=1,
    login_customer_id=login_customer_id,
)
```

Annotate the tool with `ToolAnnotations(readOnlyHint=False, ...)` so
read-only mode hides it, and add tests in `tests/pilot/`.
