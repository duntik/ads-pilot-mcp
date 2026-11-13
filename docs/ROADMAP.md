# Roadmap

ads-pilot-mcp grows in four stages. Each stage ships only when its tools are
tested with mocks and, for stage 2 onwards, exercised on a Google Ads test
account.

| Stage | Scope | Status |
| --- | --- | --- |
| 1 | Project foundation, safety layer, read and report tools | **Done (v0.1.0)** |
| 2 | Write tools through the safety layer | Planned |
| 3 | Keyword Planner, bulk operations, experiments overview | Planned |
| 4 | Test-account end-to-end guide, release workflow, PyPI | Planned |

## Principles for every write tool

1. **Propose → confirm.** `validate_only=True` by default; the API validates
   the change; the user approves the `change_items`; the confirm call needs
   the `checksum`.
2. **Read before write.** The tool reads the current value of every field it
   changes (GAQL through `safety.undo.fetch_current_values` or a dedicated
   query) and passes them as `FieldChange` objects to
   `guarded_propose_and_confirm`. The "before" values are part of the
   checksum, so a change made by someone else in between invalidates the
   proposal.
3. **Policy first.** Read-only mode, the customer allow-list,
   `max_operations_per_mutate`, and the budget and bid limits are checked at
   propose and at confirm time.
4. **One atomic call.** Where possible, operations go through
   `GoogleAdsService.Mutate` with `partial_failure=false`, so a change is
   applied completely or not at all.
5. **Undoable by default.** Field updates are snapshotted for
   `undo_change`. Creations record the created resource names so undo can
   remove them. Removals are only offered where the removed object can be
   recreated from the snapshot, and are always flagged as destructive.
6. **IDs, not names.** Tools take numeric IDs (found with the report tools),
   never names that could match several objects.
7. **Small batches.** Bulk input is capped per call; larger work goes through
   stage 3 bulk operations.

Common inputs, not repeated below: `customer_id`, `login_customer_id`,
`validate_only` (default `True`), `checksum`.

New safety building blocks needed in stage 2:

- `RESOURCE_SPECS` entries for `campaign_criterion`, `ad` (RSA text and
  URLs), `asset`, `campaign_asset`, `conversion_action`, `bidding_strategy`;
- a second record kind in the undo store, `created`, listing resource names
  created by a change (undo = remove them);
- field kinds `url` (domain-change check) and `text_list` (RSA headlines and
  descriptions);
- per-user identity in audit entries when running in OAuth (HTTP) mode.

---

## Stage 2 — write tools

Namespace: `manage` (tools appear as `manage_<tool>`).

### 2.1 `set_status`

Pause or enable campaigns, ad groups, ads and keywords.

| | |
| --- | --- |
| Inputs | `level`: `CAMPAIGN`, `AD_GROUP`, `AD`, `KEYWORD`; `ids`: list of IDs (`{ad_group_id}~{ad_id}` for ads, `{ad_group_id}~{criterion_id}` for keywords); `status`: `ENABLED` or `PAUSED` |
| API | `GoogleAdsService.Mutate` with `CampaignOperation` / `AdGroupOperation` / `AdGroupAdOperation` / `AdGroupCriterionOperation` updates (update mask `status`) |
| Safety | Allow-list; `max_operations_per_mutate`; current status read first and unchanged items dropped from the proposal; `REMOVED` is not accepted (removal is a separate, explicit tool); enabling a campaign whose budget is above `max_daily_budget` is refused; `change_items` show campaign/ad group names for every ID |
| Undo | `undo_change` restores the previous status (already supported by the stage 1 reverse-op builder) |

### 2.2 `set_campaign_budget`

| | |
| --- | --- |
| Inputs | `budget_id` or `campaign_id`; either `amount` (account currency per day) or `change_pct` (e.g. `+15`) |
| API | `CampaignBudgetService` via `GoogleAdsService.Mutate` (`CampaignBudgetOperation.update`, mask `amount_micros`) |
| Safety | `max_budget_increase_pct`, `max_daily_budget`; amounts rounded to the currency's minimum unit; shared budgets list every campaign that uses them in `change_items`; non-daily (total / custom-period) budgets refused with an explanation |
| Undo | Snapshot of `amount_micros` (supported) |

### 2.3 `set_bids`

Manual CPC bids for ad groups and keywords.

| | |
| --- | --- |
| Inputs | `level`: `AD_GROUP` or `KEYWORD`; `ids`; `cpc` (absolute) or `change_pct` |
| API | `AdGroupOperation` / `AdGroupCriterionOperation` updates, mask `cpc_bid_micros` |
| Safety | `max_cpc`, `max_bid_change_pct`; warns (and requires `allow_non_manual=true`) when the campaign's bidding strategy ignores manual bids; keyword bids below the ad group default are shown as such |
| Undo | Snapshot of `cpc_bid_micros` (supported) |

### 2.4 `set_bidding_target`

tCPA and tROAS for campaigns, ad groups and portfolio strategies.

| | |
| --- | --- |
| Inputs | `level`: `CAMPAIGN`, `AD_GROUP` or `PORTFOLIO`; `id`; `target_cpa` (currency) or `target_roas` (ratio, 3.5 = 350 %) |
| API | Campaign: `CampaignService` field chosen from the current strategy (`target_cpa.target_cpa_micros`, `maximize_conversions.target_cpa_micros`, `target_roas.target_roas`, `maximize_conversion_value.target_roas`); ad group: `AdGroupService` (`target_cpa_micros` / `target_roas`); portfolio: `BiddingStrategyService` |
| Safety | `max_bid_change_pct` for both targets; refuses to set a target on a strategy that does not use it (no silent strategy switch); warns when the campaign is in a learning period (from `campaign.bidding_strategy_system_status`) |
| Undo | Snapshot (campaign and ad group fields supported; `bidding_strategy` spec added in stage 2) |

Switching the bidding strategy type is not in this tool; it is a larger,
harder-to-undo change and will get its own tool after stage 2.

### 2.5 `add_keywords`

| | |
| --- | --- |
| Inputs | `ad_group_id`; `keywords`: list of `{text, match_type: EXACT/PHRASE/BROAD, cpc?}`; `status` (default `ENABLED`, or `PAUSED`) |
| API | `AdGroupCriterionService` creates via `GoogleAdsService.Mutate` |
| Safety | Text validated (max 80 characters, max 10 words, no illegal characters); duplicates of existing keywords in the ad group are dropped and reported; `max_cpc` for per-keyword bids; `max_operations_per_mutate`; API policy violations surface in the validate step, and policy exemptions are never requested automatically |
| Undo | `created` record; undo removes the created criteria |

### 2.6 `add_negative_keywords` and `manage_negative_keyword_list`

| | |
| --- | --- |
| Inputs | `level`: `CAMPAIGN`, `AD_GROUP` or `SHARED_LIST`; `target_id`; `keywords`: list of `{text, match_type}`. List tool: `action` (`CREATE`, `ATTACH`, `DETACH`), `name`, `shared_set_id`, `campaign_ids` |
| API | `CampaignCriterionService` (`negative=true`), `AdGroupCriterionService` (`negative=true`), `SharedCriterionService` for list members, `SharedSetService` to create a list, `CampaignSharedSetService` to attach / detach |
| Safety | Before proposing, checks each negative against the last 30 days of `search_term_view`: if it would block search terms that converted, the proposal lists them and requires `allow_blocking_converters=true`; duplicates dropped; operation limit |
| Undo | `created` record (remove criteria / detach list); detaching a list stores the attachment for re-attach |

### 2.7 `create_rsa` and `update_rsa`

| | |
| --- | --- |
| Inputs | `ad_group_id`; `headlines`: 3–15 items `{text, pin?: HEADLINE_1/2/3}`; `descriptions`: 2–4 items `{text, pin?}`; `final_url`; `path1`, `path2`; `status` (default `PAUSED` for new ads). Update: `ad_id` and any of the same fields |
| API | Create: `AdGroupAdService` (`AdGroupAdOperation.create` with `responsive_search_ad`). Update: `AdService.MutateAds` (RSA text and URLs can be updated in place) |
| Safety | Length checks (headline 30, description 90, path 15 characters, counting double-width characters as 2); pin sanity (at least as many unpinned assets as Google requires); API validate catches editorial and policy problems before anything is created; new ads start paused unless `status=ENABLED` is passed; final URL domain must match the ad group's existing ads unless `allow_new_domain=true` |
| Undo | Create: `created` record (undo removes the ad, or pauses it if it already has traffic). Update: snapshot of the previous headlines, descriptions, paths and URLs (`text_list` field kind) |

### 2.8 `set_final_urls`

| | |
| --- | --- |
| Inputs | `level`: `AD`, `KEYWORD` or `SITELINK_ASSET`; `ids`; `final_url`, `final_mobile_url?`, `final_url_suffix?`, `tracking_url_template?` |
| API | `AdService` (ad URLs), `AdGroupCriterionService` (keyword URLs), `AssetService` (sitelink asset URLs) |
| Safety | `https://` required; domain change requires `allow_new_domain=true`; tracking templates must contain `{lpurl}` or a full URL; the server never fetches the URL itself |
| Undo | Snapshot of the URL fields (`url` field kind) |

### 2.9 `set_location_targeting` and `find_locations`

| | |
| --- | --- |
| Inputs | `campaign_id`; `add`: geo target IDs; `exclude`: geo target IDs; `remove`: campaign criterion IDs; `bid_modifiers?`; `presence_only?` (sets `geo_target_type_setting.positive_geo_target_type` to `PRESENCE`). `find_locations` (read only): `query`, `country_code`, `locale` |
| API | `CampaignCriterionService` (`LocationInfo`, negative for exclusions), `CampaignService` for `geo_target_type_setting`, `GeoTargetConstantService.SuggestGeoTargetConstants` for lookups |
| Safety | Refuses a change that would leave a campaign with no positive location (worldwide targeting) unless `allow_worldwide=true`; shows location names, not just IDs; operation limit |
| Undo | `created` record for additions; removed criteria are snapshotted (location ID, negative flag, bid modifier) so undo recreates them; presence setting snapshotted as a field |

### 2.10 `set_ad_schedule`

| | |
| --- | --- |
| Inputs | `campaign_id`; `schedule`: list of `{day, start_hour, start_minute, end_hour, end_minute, bid_modifier?}`; `mode`: `REPLACE` or `ADD` |
| API | `CampaignCriterionService` with `AdScheduleInfo` |
| Safety | Minutes must be 0/15/30/45; no overlapping slots; bid modifier 0.1–10; `REPLACE` shows the full old and new schedule; an empty schedule (= run all the time) needs `allow_always_on=true`; times are in the account time zone and shown as such |
| Undo | Snapshot of the previous schedule criteria; undo replaces the schedule with it |

### 2.11 `link_assets` and `create_call_asset`

Sitelinks and callouts are created with the upstream `create_asset` tool;
stage 2 adds linking and call assets.

| | |
| --- | --- |
| Inputs | `link_assets`: `asset_ids`, `field_type` (`SITELINK`, `CALLOUT`, `CALL`, `STRUCTURED_SNIPPET`), `level` (`CUSTOMER`, `CAMPAIGN`, `AD_GROUP`), `target_id`, `action` (`LINK` / `UNLINK`). `create_call_asset`: `country_code`, `phone_number`, `call_conversion_action_id?`, `ad_schedule?` |
| API | `CustomerAssetService`, `CampaignAssetService`, `AdGroupAssetService`; `AssetService` for the call asset (`CallAsset`) |
| Safety | Phone numbers validated (E.164 after country code); warns when a level already has the maximum useful number of assets of that type; unlinking the last sitelink of a campaign is flagged; operation limit |
| Undo | `created` record (unlink); unlinks are snapshotted for re-link. Assets themselves are never removed by undo (they may be shared) |

### 2.12 `import_ga4_conversion` and `set_conversion_goal`

| | |
| --- | --- |
| Inputs | `import_ga4_conversion`: `property_id`, `event_name`, `category`, `primary` (default `false`). `set_conversion_goal`: `conversion_action_id`, `primary_for_goal?`, `status?` (`ENABLED` / `HIDDEN`), `include_in_conversions?` via goals |
| API | `ConversionActionService` (create / update; `primary_for_goal`, `status`), `CustomerConversionGoalService` and `CampaignConversionGoalService` for goal biddability; GA4 link checked via `product_link` / `data_link` resources |
| Safety | The GA4 property must already be linked to the account; the tool shows how many primary actions exist before and after; making an action primary changes what Smart Bidding optimises for, so the proposal states which campaigns use the affected goal; no change to counting type or attribution in this tool |
| Undo | Snapshot of `primary_for_goal` / `status` / goal biddability (`conversion_action` spec added in stage 2); creation recorded so undo hides the new action (conversion actions cannot be deleted while they have data) |

Open question to confirm on a test account first: which GA4-imported
conversion action types the API can create directly. If creation is not
supported, `import_ga4_conversion` will instead find the GA4 key event
already offered to the account and enable it.

### 2.13 `apply_recommendation` and `dismiss_recommendation`

| | |
| --- | --- |
| Inputs | `recommendation_ids` (from `reports_recommendations_list`); optional per-type parameters (e.g. a different budget amount for `CAMPAIGN_BUDGET`) |
| API | `RecommendationService.ApplyRecommendation` / `DismissRecommendation` |
| Safety | Only types listed in a new `safety.apply_recommendation_types` allow-list (empty by default); the proposal translates each recommendation into the concrete change (budget X → Y, keyword + match type + bid, target A → B) and runs the normal budget and bid limits on it; `ApplyRecommendation` has no `validate_only`, so the proposal says that no API dry run happened; auto-apply settings are never changed |
| Undo | For budget, keyword, bid and target types, the affected fields are snapshotted before applying, so `undo_change` reverts them; dismissals and other types are recorded in the audit log as not reversible |

---

## Stage 3

### 3.1 Keyword Planner (read only)

| Tool | Inputs | API | Notes |
| --- | --- | --- | --- |
| `keyword_ideas` | `seed_keywords` and/or `seed_url`, `language_id`, `geo_target_ids`, `network`, `limit` | `KeywordPlanIdeaService.GenerateKeywordIdeas` | Paged and capped; competition and bid ranges converted from micros |
| `keyword_historical_metrics` | `keywords`, `language_id`, `geo_target_ids`, `months` | `KeywordPlanIdeaService.GenerateKeywordHistoricalMetrics` | Monthly volumes compacted to a short series |
| `keyword_forecast` | `keywords` with match types and CPC, `daily_budget`, `geo_target_ids`, `language_id`, date range | `KeywordPlanIdeaService.GenerateKeywordForecastMetrics` | No account changes; result is an estimate |

Keyword Planner needs a developer token with Basic access or higher; with
test access the API returns test data, which the tools will label.

### 3.2 Bulk operations

| | |
| --- | --- |
| Tool | `bulk_change` (propose → confirm) and `bulk_status` (read) |
| Inputs | A list of up to N changes in the stage 2 formats (status, budget, bid, keyword, negative), or a CSV string with the same columns |
| API | `GoogleAdsService.Mutate` for up to `max_operations_per_mutate` operations; `BatchJobService` for larger sets (asynchronous, polled with `bulk_status`) |
| Safety | Every row goes through the same per-field policy checks; the proposal summarises totals (budget delta, number of pauses, …) and lists every row; one `change_id` for the whole batch |
| Undo | One undo record for the batch; reverse operations submitted the same way |

### 3.3 Experiments overview (read only)

| | |
| --- | --- |
| Tool | `experiments_overview` |
| Inputs | `status?`, `date_range` |
| API | GAQL on `experiment` and `experiment_arm`, campaign metrics for each arm's campaign |
| Output | Experiment, arms, traffic split, dates, status, and per-arm cost, conversions, cost/conv and conversion rate, with the difference between arms |

Creating, promoting and ending experiments (`ExperimentService`) may follow
once the read view is in use.

---

## Stage 4

### 4.1 Test-account end-to-end guide

- `docs/E2E.md`: create a test manager account, a test client account and
  a developer token (test access is enough), seed the account with a script.
- `nox -s e2e`: runs a scripted scenario against the account in
  `ADS_PILOT_E2E_CUSTOMER_ID` — every report, then propose → confirm →
  `undo_change` for each stage 2 tool.
- The harness first checks `customer.test_account = TRUE` and refuses to run
  against a production account.

### 4.2 Release workflow

- Adapt `.github/workflows/release.yml` (package name already
  `ads-pilot-mcp`) and rewrite `docs/releasing.md` for this repository.
- Trusted Publishing for the `ads-pilot-mcp` project on TestPyPI and PyPI,
  GitHub environments `testpypi` and `pypi` with required reviewers.
- `CHANGELOG.md`, semantic versioning, annotated `vX.Y.Z` tags.
- Monthly upstream sync PR (see [ARCHITECTURE.md](ARCHITECTURE.md#merging-upstream)).

### 4.3 PyPI

- Publish `ads-pilot-mcp` so `pipx run ads-pilot-mcp` and
  `uvx ads-pilot-mcp` work without a Git URL.
- Container image built from the same tag.

## Later

- Tag audit entries with the signed-in user's email in OAuth (HTTP) mode and
  keep per-user undo stores; align with the upstream audit middleware once
  it lands upstream.
- Bidding strategy switch tool with a staged rollout.
- Scheduled checks (budget pacing alerts, disapproved ads, conversion
  tracking gaps) as MCP resources.
