# Architecture

ads-pilot-mcp is a soft fork of
[googleads/google-ads-mcp](https://github.com/googleads/google-ads-mcp). The
rule that shapes the code: **new code lives in new modules, upstream files
change as little as possible**, so `git merge upstream/main` stays easy.

## Layout

```
ads_mcp/
  server.py              upstream  entry point (ads-pilot-mcp / google-ads-mcp)
  coordinator.py         upstream* FastMCP instance; discovers and mounts tools
  config.py              upstream* tools_config.yaml namespaces
  propose_and_confirm.py upstream* propose -> confirm with checksum; safety hook
  utils.py               upstream  Google Ads client, formatting helpers
  tools_config.yaml      upstream* namespaces + safety section
  tools/                 upstream  search, metadata, customers, assets, labels
    pilot/               new       registers the "reports" and "safety" namespaces
      __init__.py                  exposes reports_mcp / safety_mcp, installs hook
      safety_tools.py              get_safety_settings, list_changes, undo_change
  safety/                new       safety layer (see docs/SAFETY.md)
    settings.py                    yaml + env settings, validation
    policy.py                      pure limit checks
    changes.py                     FieldChange, known resources and fields
    audit_log.py                   append-only JSONL, redaction
    undo.py                        snapshot store, current values, reverse ops
    hooks.py                       hook for propose_and_confirm; guarded wrapper
    read_only.py                   which tools read-only mode hides
  reports/               new       curated read-only reports ("reports" namespace)
    gaql.py                        validated GAQL building blocks
    common.py                      querying, row shaping, paging, envelope
    performance.py                 account_overview, landing pages, geo, ads
    search_terms.py                wasted_spend, search terms, keywords
    budget.py                      budget_pacing
    account.py                     change_history, conversion_actions, recommendations
tests/
  pilot/                 new       unit tests for everything above
  smoke/                 upstream* golden tool list and LLM cases include new tools
```

`upstream*` marks upstream files with small, commented changes (each has a
"Modified by ads-pilot-mcp contributors" line). [NOTICE](../NOTICE) lists
them all.

## How new tools are registered

`coordinator.initialize_and_mount_tools` imports every module and package
under `ads_mcp/tools/` and mounts each `FastMCP` instance it finds, using the
instance name as the namespace. `ads_mcp/tools/pilot/__init__.py` imports
`reports_mcp` (`FastMCP("reports")`) and `safety_mcp` (`FastMCP("safety")`),
so both are discovered without changing the discovery code. Namespaces are
enabled in `tools_config.yaml` (`reports: true`, `safety: true`) and listed in
`config.ALL_CATEGORIES` for the no-config fallback.

Stage 2 write tools will follow the same pattern (a new namespace defined in
`ads_mcp/tools/pilot/`).

## How the safety layer plugs in

```
tool (upstream or ads-pilot)
  └─ guarded_propose_and_confirm (ads-pilot tools only: adds FieldChanges
     │                             to a context variable and the checksum)
     └─ propose_and_confirm (upstream)
          ├─ hook.before(...)        read-only, allow-list, op count, limits
          │                          -> ToolError + BLOCKED audit entry
          ├─ validate_fn()           API validate_only dry run
          ├─ checksum issue / verify
          └─ hook.wrap_commit(commit_fn)()
               ├─ commit_fn()        the real mutate call
               ├─ audit COMMITTED / FAILED
               └─ undo snapshot (reversible FieldChanges only)
```

The only change in `propose_and_confirm.py` is a module-level `_safety_hook`
(default `None`), a `set_safety_hook()` setter, and a few lines at the start
of `propose_and_confirm()` that call the hook when one is installed. With no
hook, upstream behaviour is byte-for-byte the same; the upstream tests run
both ways.

The hook is installed when `ads_mcp.tools.pilot` is imported, which happens
whenever the server mounts its tools, so every mutate tool on a running
server is covered. `coordinator.py` additionally asks
`safety.read_only.should_hide(tool)` while mounting, to drop mutate tools in
read-only mode.

## Report tools

Every report:

1. validates its inputs (`gaql.customer_id`, `gaql.id_value`,
   `gaql.enum_value`, `gaql.date_range`, `gaql.to_micros`); field and
   resource names are constants, never caller text;
2. builds GAQL with `gaql.build_query` (with
   `omit_unselected_resource_names=true`, like upstream `search`);
3. fetches one page with `common.fetch_page` — GAQL has no OFFSET, so it
   requests `offset + limit + 1` rows and slices; the extra row sets
   `next_offset`;
4. shapes rows through `Column` specs: short keys, micros → currency units,
   enums → names (UNSPECIFIED/UNKNOWN dropped), long strings truncated;
5. returns the common envelope (`report`, `customer_id`, `currency`,
   `date_range`, `rows`, `row_count`, `next_offset`, `totals`, `notes`,
   `summary`).

The account currency and time zone come from one cached `customer` query per
account. `GoogleAdsException` becomes a readable `ToolError`
(`common.google_ads_errors`).

Report fields are checked against the Google Ads API protos of the installed
client library in `tests/pilot/report_contract_test.py`, so an API version
bump that drops a field fails a unit test instead of failing for users.

## State on disk

| What | Default path | Format |
| --- | --- | --- |
| Audit log | `<state_dir>/audit.jsonl` | append-only JSON lines |
| Undo snapshots | `<state_dir>/undo/<change_id>.json` | one JSON file per change, written atomically |

`<state_dir>` is the per-user data directory unless `state_dir` /
`ADS_PILOT_STATE_DIR` is set. Directories are created with mode `0700`.

## Merging upstream

```shell
git fetch upstream
git switch -c sync/upstream-YYYY-MM main
git merge upstream/main
# resolve conflicts (normally only in the upstream* files listed above)
nox -s tests-3.13
nox -s update_smoke_golden   # Python 3.13, regenerates the tool list
git push origin sync/upstream-YYYY-MM   # open a PR into duntik/ads-pilot-mcp main
```

Things to check after a merge:

- `propose_and_confirm()` still calls the hook before validation and wraps
  `commit_fn`;
- `initialize_and_mount_tools()` still calls `should_hide()`;
- new upstream mutate tools carry `readOnlyHint=False` (they will then be
  hidden in read-only mode automatically);
- a new Google Ads API version (`v25` → `v26` imports) keeps the report and
  undo fields (the contract test tells you).

Pull requests go to `duntik/ads-pilot-mcp`; fixes that belong upstream are
proposed to googleads/google-ads-mcp separately under its own rules.
