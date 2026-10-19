# Quotation Pipeline V3

## Scope

This change applies across uploaded files and quotation jobs. It does not
rewrite saved quotes, confirmed requirements, or original uploads.

The old generic idle-round message was not evidence of a server crash.
Diagnostics now distinguish missing customer facts, recognition review,
missing price references, plan repair, unavailable services, and actual
execution failures.

## Extraction and Coverage

- Excel supports anchored drawings, WPS DISPIMG cell images, and Microsoft
  rich-value cell image relationships. External references are not fetched.
- Each discovered resource records resolved or unresolved status. A resolved
  image means its bytes were decoded, not that every dimension was understood.
- Excel coverage records source rows independently of model claims. Standalone
  notes and brand constraints are not silently converted into products.
- Source extraction now defaults to an AI tool loop that reads originals, revises
  observations and re-reads sources for omission review. Region inventories remain
  available in compatibility mode. See `visual-recognition.md`. Legacy records retain their
  page-level coverage; arbitrary drawings still require acceptance and review.
- Legacy cache upgrades publish `excel-v2.json` and separate previews only when
  sheet and row identities match. Otherwise re-extraction is required.
- Original files, old manifests, and old previews are retained.

## Measurement Contract

New tool proposals use `contract_version: 3` and an explicit pricing strategy.
The model chooses measurement intent; the program compiles formulas.

Supported measurements: `item_area`, `letter_area`, `letter_length`,
`item_length`, `component_area`, `component_count`, `item_count`,
`labor_hours`, `power_units`, `subtotal_ratio`, and `fixed`.

Whole-item dimensions cannot implicitly become per-character dimensions.
Component and letter-group references use stable `object_id` / `target_id`.
Stale ID/index pairs are rejected. Counts, order quantity, minimum charges,
and applicable difficulty factors are applied by the pricing engine.
Missing dimensions, counts, and rate evidence remain blockers, not zeroes.

Proposed object collections require evidence, remain reviewable, and cannot
replace existing collections or confirmed requirements. Legacy V1/V2 plans
remain readable and retain their calculation contract.

## Diagnostics and Recovery

Structured failures carry a code, cause, affected paths, and repair action.
Unexpected tool failures roll back the failing transaction, record a
diagnostic ID in logs, and defer the affected line while preserving previously
committed progress. Private exception contents are not exposed to customers.

Default per-line limits:

| Setting | Default |
| --- | --- |
| `AGENT_LINE_MAX_TOKENS` | 60000 |
| `AGENT_LINE_MAX_SECONDS` | 600 |
| `AGENT_REPAIR_ATTEMPTS` | 3 |

Repeated unchanged calls are suppressed. A pause retains the actual blocker
and failed plan. Manual continuation resets line budgets; it does not invent
missing facts. Time budgets are checked between operations, not a hard
preemption of an in-flight provider request.

`preview_estimate` calculates without saving. `inspect_source` re-reads at
most two source pages and five Excel rows per page, returns observations
without overwriting requirements, and charges provider usage.

## Audit and Rollout

- `GET /api/jobs/{job_id}/pipeline-audit`: read-only legacy/V3 comparison,
  source coverage, review signals, and run metrics.
- `POST /api/customer-files/{file_id}/reparse-preview`: offline parsing in a
  temporary directory, without publishing caches or changing business data.
- The workspace drawer exposes these checks and actionable line diagnostics.
- Shadow amounts are comparisons against the current stored requirement, not
  a replacement for an approved historical quotation or an approval action.

Before enabling production work:

1. Run backend regression tests, frontend tests/build, and browser smoke tests.
2. Audit representative uploads and inspect unresolved resources.
3. Review changed/blocked lines and confirmed data before any explicit rerun.
4. Stop or finish active extraction/agent runs before restarting the API.
5. Watch line budgets, repeated calls, source omissions, and amount changes.

To roll back new tool proposals, set `ESTIMATE_CONTRACT_VERSION=2` and restart
the API. This does not downgrade stored V3 plans or disable all V3 engine
changes. A full code rollback needs the previous application build and a
compatible data backup; never delete original uploads or overwrite quotes.

## Verification Boundaries

The original incident workbook was reparsed offline: 32 resources discovered,
32 resolved. No paid model re-extraction or full real-provider repricing was
performed as part of this verification. Microsoft rich-value image support
has synthetic archive coverage; a real Microsoft-produced workbook remains
an additional acceptance sample.

Use `scripts/smoke-pipeline-v3.cjs` for mocked desktop/mobile audit workflows
and `scripts/smoke-agent-visibility.cjs` for execution-detail visibility
regression checks. These do not prove external model service availability.
