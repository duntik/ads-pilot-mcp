<!-- Modified by ads-pilot-mcp contributors: rewritten for ads-pilot-mcp; the
     upstream README is at https://github.com/googleads/google-ads-mcp -->

# ads-pilot-mcp — an MCP server for Google Ads

> **Not affiliated with or endorsed by Google. Based on
> [googleads/google-ads-mcp](https://github.com/googleads/google-ads-mcp).**
> "Google" and "Google Ads" are trademarks of Google LLC.

ads-pilot-mcp is an [MCP](https://modelcontextprotocol.io) server that lets an
AI assistant (Claude, Codex, Gemini, Cursor and others) read everything in a
Google Ads account and, in later stages, change it safely. It is a soft fork
of Google's open-source `google-ads-mcp` server: every upstream tool is still
here and works the same way, and ads-pilot-mcp adds:

- **curated report tools** that answer the usual account questions (spend,
  wasted spend, search terms, Quality Score, landing pages, geo, ads, budget
  pacing, change history, conversion actions, recommendations) with compact
  JSON and a short markdown summary;
- **a safety layer** in front of every change: read-only mode, policy limits
  on budgets and bids, a customer allow-list, an append-only audit log, and
  undo for simple field changes.

The project is at **v0.1.0**: the read and report tools and the safety layer
are done. Write tools for campaigns, budgets, bids and keywords come in
stage 2 — see the [roadmap](docs/ROADMAP.md).

## Contents

- [Upstream vs ads-pilot-mcp](#upstream-vs-ads-pilot-mcp)
- [Safety model](#safety-model)
- [Install](#install)
- [Authentication](#authentication)
- [Configuration](#configuration)
- [Tools](#tools)
- [Roadmap](#roadmap)
- [Development](#development)
- [Disclaimer](#disclaimer)

## Upstream vs ads-pilot-mcp

| Area | googleads/google-ads-mcp | ads-pilot-mcp adds |
| --- | --- | --- |
| Reading data | `search` (free-form GAQL), `get_resource_metadata`, `list_accessible_customers` | 11 curated report tools with validated parameters, money in account currency, paging and row caps |
| Writing data | `create_asset`, `upload_video_asset`, `manage_labels`, `manage_label_associations` | Stage 2: status, budgets, bids and targets, keywords and negatives, RSAs, URLs, targeting, assets, conversions, recommendations ([roadmap](docs/ROADMAP.md)) |
| Change safety | Propose → confirm with a checksum (`validate_only=True` by default) | Read-only mode, budget/bid/operation limits, customer allow-list, audit log, undo — applied to upstream tools too |
| Undo | — | `undo_change` for status, budget and bid/target changes, with a drift check |
| Output size | Raw rows | Short keys, micros converted, strings truncated, 25 rows per page by default |
| Configuration | `tools_config.yaml` namespaces | Same file, plus a `safety:` section and `ADS_PILOT_*` env vars |
| Package | `google-ads-mcp` | `ads-pilot-mcp` (the `google-ads-mcp` command still works) |

Upstream changes are merged regularly; ads-pilot-mcp keeps its own code in
separate modules so merges stay easy (see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).

## Safety model

Every tool that can change an account goes through the same steps:

1. **Propose.** The tool is called with `validate_only=True` (the default).
   The server checks the safety policy, asks the Google Ads API to validate
   the change without applying it, and returns human-readable
   `change_items` plus a `checksum`. Nothing changes.
2. **Review.** The assistant shows the `change_items` to you and waits for
   your explicit approval.
3. **Confirm.** The tool is called again with `validate_only=False`, the same
   parameters and the `checksum`. The server checks the policy again,
   verifies the checksum (so the change cannot be swapped between steps),
   applies it, writes the audit log and stores an undo snapshot.

On top of that:

- **Read-only mode** (`ADS_PILOT_READ_ONLY=true`) hides every tool that can
  change anything and refuses every mutation.
- **Policy limits** cap budget increases (%), daily budgets, bid changes (%),
  CPC bids, and the number of operations per call. A change outside the
  limits is refused at propose time.
- **Allow-list**: only the customer IDs you list can be changed.
- **Audit log**: an append-only JSONL file records every committed, failed
  and blocked change, with before/after values. Credentials are never
  written to it.
- **Undo**: `undo_change(change_id)` restores the "before" values of a
  status, budget or bid change, through the same propose → confirm flow, and
  refuses if the account changed again since.

Details and examples: [docs/SAFETY.md](docs/SAFETY.md).

> The MCP server exposes your account data to the AI assistant and model
> provider you connect it to. Start with read-only mode and a test account.

## Install

You need Python 3.10 or newer. The package is not on PyPI yet; install it
from GitHub.

**pipx**

```shell
pipx install git+https://github.com/duntik/ads-pilot-mcp.git
# or run without installing:
pipx run --spec git+https://github.com/duntik/ads-pilot-mcp.git ads-pilot-mcp
```

**uv**

```shell
uv tool install git+https://github.com/duntik/ads-pilot-mcp.git
# or run without installing:
uvx --from git+https://github.com/duntik/ads-pilot-mcp.git ads-pilot-mcp
```

**From source**

```shell
git clone https://github.com/duntik/ads-pilot-mcp.git
cd ads-pilot-mcp
uv venv && uv pip install -e ".[dev]"
```

Pin a release with `...ads-pilot-mcp.git@v0.1.0`.

## Authentication

There is no shared account or shared key: **each user brings their own
Google Ads developer token and their own OAuth credentials.** The server
only ever acts with the access your Google user already has.

### 1. Developer token

1. Sign in to a Google Ads **manager account** (create one for free if you
   do not have one).
2. Open **Admin → API Center** and copy the developer token.
3. Set it as `GOOGLE_ADS_DEVELOPER_TOKEN` in the environment of the server.
   Never commit it.

The token's **access level** decides which accounts it can reach
([Google's access levels page](https://developers.google.com/google-ads/api/docs/api-policy/access-levels)):

| Access level | What you can do | How to get it |
| --- | --- | --- |
| **Test account access** | Only [test accounts](https://developers.google.com/google-ads/api/docs/best-practices/test-accounts). Production accounts return `DEVELOPER_TOKEN_NOT_APPROVED`. | Every new token starts here. |
| **Basic access** | Production accounts, up to 15,000 operations per day. Enough for reports and day-to-day changes on your own accounts. | Apply in the API Center; Google reviews the application (it asks how you will use the API). |
| Standard access | Higher limits, for tools that serve many accounts. | Apply after Basic access. |

Start with a test account to try write tools safely: create a test manager
account, create test client accounts under it, and point the server at them.
Test accounts serve no ads and spend no money. Google may offer other
intermediate levels; check the page above for the current list.

### 2. OAuth credentials

Pick one:

- **Application Default Credentials (local, simplest).** Create an OAuth
  client (type *Desktop app*) in a Google Cloud project with the Google Ads
  API enabled, download its JSON, and run:

  ```shell
  gcloud auth application-default login \
    --scopes https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform \
    --client-id-file=YOUR_CLIENT_JSON_FILE
  ```

  Then set `GOOGLE_APPLICATION_CREDENTIALS` to the credentials file that
  `gcloud` prints, and `GOOGLE_PROJECT_ID` to your Cloud project ID.

- **OAuth proxy over HTTP (shared server).** Set
  `GOOGLE_ADS_MCP_OAUTH_CLIENT_ID` and `GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET`;
  the server then serves Streamable HTTP at `http://localhost:8080/mcp` and
  each MCP client signs in with its own Google account. All storage and
  deployment options of the upstream server apply unchanged; see the
  [upstream README](https://github.com/googleads/google-ads-mcp#configure-credentials).

If you reach accounts through a manager account, also set
`GOOGLE_ADS_LOGIN_CUSTOMER_ID` (or pass `login_customer_id` per call).

### 3. MCP client configuration

```json
{
  "mcpServers": {
    "ads-pilot": {
      "command": "pipx",
      "args": [
        "run",
        "--spec",
        "git+https://github.com/duntik/ads-pilot-mcp.git",
        "ads-pilot-mcp"
      ],
      "env": {
        "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS": "PATH_TO_CREDENTIALS_JSON",
        "GOOGLE_PROJECT_ID": "YOUR_PROJECT_ID",
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "YOUR_MANAGER_CUSTOMER_ID",
        "ADS_PILOT_READ_ONLY": "true"
      }
    }
  }
}
```

The same block works in Claude Code / Claude Desktop, Cursor
(`.cursor/mcp.json`), VS Code (`.vscode/mcp.json`) and other clients. Remove
`ADS_PILOT_READ_ONLY` once you want to allow changes. Keep this file private:
it contains your developer token.

The server adds the upstream usage header (`x-goog-api-client`) to API calls,
as `google-ads-mcp` does.

## Configuration

Configuration lives in `tools_config.yaml`, resolved in this order:

1. the path in `GOOGLE_ADS_MCP_TOOLS_CONFIG`;
2. `tools_config.yaml` in the working directory;
3. the default file bundled with the package.

```yaml
namespaces:
  customers: true
  search: true
  metadata: true
  labels: true      # upstream mutate tools
  assets: true      # upstream mutate tools
  reports: true     # ads-pilot-mcp report tools
  safety: true      # get_safety_settings, list_changes, undo_change

safety:
  read_only: false
  allowed_customer_ids: []          # empty = every account you can access
  max_operations_per_mutate: 100
  max_budget_increase_pct: 50       # per change
  max_daily_budget: null            # account currency, null = no limit
  max_bid_change_pct: 50            # up or down, bids and tCPA/tROAS
  max_cpc: null                     # account currency
  audit_log: true
  audit_log_path: null              # default <state_dir>/audit.jsonl
  state_dir: null                   # default: per-user data directory
  customer_overrides:
    "1234567890":
      max_daily_budget: 500
```

If you keep your own `tools_config.yaml` from upstream, add `reports: true`
and `safety: true` under `namespaces`, or those tools stay hidden.

Environment variables override the yaml file:

| Variable | Meaning |
| --- | --- |
| `ADS_PILOT_READ_ONLY` | `true` hides and refuses every mutation |
| `ADS_PILOT_ALLOWED_CUSTOMER_IDS` | Comma-separated customer IDs that may be changed |
| `ADS_PILOT_MAX_OPERATIONS` | Max operations per mutate call |
| `ADS_PILOT_MAX_BUDGET_INCREASE_PCT` | Max budget increase per change, in % |
| `ADS_PILOT_MAX_DAILY_BUDGET` | Max daily budget, account currency |
| `ADS_PILOT_MAX_BID_CHANGE_PCT` | Max bid / target change per change, in % |
| `ADS_PILOT_MAX_CPC` | Max CPC bid, account currency |
| `ADS_PILOT_AUDIT_LOG` | `false` turns the audit log off |
| `ADS_PILOT_AUDIT_LOG_PATH` | Audit log file |
| `ADS_PILOT_STATE_DIR` | Directory for the audit log and undo snapshots |

Use `none` for "no limit". Invalid values or unknown keys stop the server
at start-up rather than silently dropping a limit.

The upstream variables (`GOOGLE_ADS_DEVELOPER_TOKEN`,
`GOOGLE_ADS_LOGIN_CUSTOMER_ID`, `GOOGLE_ADS_MCP_OAUTH_*`,
`GOOGLE_ADS_MCP_STORAGE_*`, `PORT`) work as documented upstream.

## Tools

Tool names are prefixed with their namespace, e.g. `reports_account_overview`.

### Reports (`reports`, read only)

All report tools take `customer_id` (digits, hyphens allowed), optional
`login_customer_id`, and `limit` / `offset` for paging (the response has
`next_offset`). Date-based reports take `date_range` (`TODAY`, `YESTERDAY`,
`LAST_7_DAYS`, `LAST_14_DAYS`, `LAST_30_DAYS`, `THIS_MONTH`, `LAST_MONTH`) or
`start_date` + `end_date`.

| Tool | What it answers |
| --- | --- |
| `account_overview` | Spend, clicks, impressions, conversions and cost/conv by campaign, plus account totals |
| `wasted_spend` | Search terms and keywords with cost > X and zero conversions, by cost |
| `search_terms_report` | Search terms with match type and a `suggested_negative` flag |
| `keyword_performance` | Keywords with Quality Score and its three components, effective CPC, impression share |
| `landing_page_performance` | Final or expanded landing page URLs with speed score and mobile-friendly click rate |
| `geo_performance` | Locations (`geographic_view` or `user_location_view`) by country, region, city or most specific, with names |
| `ad_performance` | Ads with ad strength and approval; RSA headline/description performance labels |
| `budget_pacing` | Month-to-date spend vs each budget, pace and full-month projection |
| `change_history` | Who changed what in the last 1-30 days (`change_event`), optionally with old/new values |
| `conversion_actions` | Conversion actions: status, primary/secondary, include-in-conversions, GA4 import, 30-day counts |
| `recommendations_list` | Google recommendations with estimated impact (read only; nothing is applied) |

Each report returns `rows` (or named sections), `totals` where useful,
`notes` with caveats, and a `summary` in markdown.

### Safety (`safety`)

| Tool | Description |
| --- | --- |
| `get_safety_settings` | Shows read-only mode, the allow-list and the limits in force |
| `list_changes` | Recent reversible changes and their `change_id` |
| `undo_change` | Reverts a change by `change_id` (propose → confirm) |

### Upstream tools (unchanged)

| Tool | Description |
| --- | --- |
| `search_search` | Any GAQL query |
| `metadata_get_resource_metadata` | Fields available on a resource |
| `customers_list_accessible_customers` | Customer IDs your user can access |
| `assets_create_asset` | Create text, image, video, sitelink, callout and other assets |
| `assets_upload_video_asset` | Upload a local video to YouTube via Google Ads (stdio mode only) |
| `labels_manage_labels` | Create, update and remove labels |
| `labels_manage_label_associations` | Attach labels to campaigns, ad groups, criteria, customers |

The upstream resources (`discovery-document`, `metrics`, `segments`,
`release-notes`) are also available.

### Example prompts

```
What did each campaign spend last month in account 1234567890, and at what cost per conversion?
Which search terms cost more than 20 and never converted in the last 30 days?
Are my budgets on track this month?
What changed in the account this week, and who changed it?
Which conversion actions are primary, and are any of them not recording?
```

## Roadmap

- **Stage 1 (this release):** report tools and the safety layer.
- **Stage 2:** write tools through the safety layer — statuses, budgets,
  bids and tCPA/tROAS, keywords and negative keywords, RSAs, final URLs,
  location and ad schedule targeting, sitelink/callout/call assets, GA4
  conversion import, applying and dismissing recommendations.
- **Stage 3:** Keyword Planner, bulk operations, experiments overview.
- **Stage 4:** test-account end-to-end guide, release workflow, PyPI.

Each planned tool, with its inputs, API service, safety checks and undo, is
in [docs/ROADMAP.md](docs/ROADMAP.md).

## Development

```shell
uv venv && uv pip install -e ".[dev]"
nox -s tests-3.13          # all unit tests (upstream + ads-pilot)
python -m unittest discover -s tests/pilot -t tests -p "*_test.py"
nox -s lint                # black, 80 columns
nox -s smoke_tests         # tool list vs tests/smoke/golden_tools_list.json
nox -s update_smoke_golden # after adding or changing a tool (use Python 3.13)
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Report problems at
<https://github.com/duntik/ads-pilot-mcp/issues>, not on the upstream
tracker.

## Disclaimer

ads-pilot-mcp is an independent open-source project. **It is not affiliated
with or endorsed by Google. Based on googleads/google-ads-mcp.** It is
provided "as is", without warranty of any kind (see [LICENSE](LICENSE)).
You are responsible for every change made to your accounts with it and for
complying with the
[Google Ads API Terms and Conditions](https://developers.google.com/google-ads/api/terms).
Review every proposed change before you confirm it.

Licensed under the Apache License 2.0. See [LICENSE](LICENSE) and
[NOTICE](NOTICE) for the upstream copyright and the list of changes.
