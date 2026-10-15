# Bounded source-domain stages

The domain planner now has a separate durable request stage. Installing or
previewing a stage does not contact Printograph, start a product crawler, change
a product's pause state, or publish calculated product rows. It does not mark
catalog discovery complete. The existing complete-discovery gate on QUOTE
manifests remains intact.

`crawler.source_domains.plan_known_reads(product_id, product_type, context,
lookup_observations=(), include_specs=True, listing_offsets=(0,),
listing_limit=1000, include_base_tiers=False)` returns an explicit DOMAIN
manifest. It can plan specification reads, explicit listing pages, and catalog,
quantity-support and optional tier-table reads for uniquely validated full
selector lookups. An ID-only product listing is not a validated selector lookup.
No pagination completion, AND-set selector semantics, option-pair validity,
unseen quantity domain, or combined service/turnaround result is inferred.

Further observed option-catalog or option-quantity-support reads can be supplied
as explicit manifest entries. Price-table reads in this stage must have purpose
`tier_domain`: the frontend's display rules can depend on which tiers exist.
Reading these tables does not establish valid combined options or final prices.

## API

All endpoints require the application's configured API authentication.

- `POST /api/v1/runs/{run_id}/source-domains/preflight`: body
  `{manifest, budget, measured_rps?}`. A pure preview; does not install a job.
- `POST /api/v1/runs/{run_id}/source-domains`: same body plus `scope`.
  Installs an immutable dormant stage and returns its `job_id` and status.
- `GET /api/v1/source-domains/{job_id}`: compact durable stage/request/budget
  status. Add `?include_responses=true` for successful immutable evidence IDs.
- `GET /api/v1/runs/{run_id}/source-domains`: `{items:[...]}` containing the
  latest twenty compact stage statuses, sorted by existing `updated_at`.
- `POST /api/v1/source-domains/{job_id}/start`: explicit start; returns HTTP409
  until an in-memory session transport factory is installed. Does not resume
  paused product walkers. Run pause/cancel still fences the planner.
  Optional body `{resume_run:true}` explicitly resumes a paused run for this
  planner stage only, atomically with its preflight scope. Every product remains
  paused; cancelled runs cannot be resumed this way. The default is false.
- `POST /api/v1/source-domains/{job_id}/retry`: body
  `{request_keys:["<failed request hash>"], resume_run:false}` retries only the
  explicit failed keys after revalidating the current in-memory session context.
  The immutable manifest, HTTP budget, logical attempt counters and successful
  response checkpoints are preserved. It accepts at most 100 keys with remaining
  logical attempts; completed keys cannot be retried through this endpoint.
- `POST /api/v1/source-domains/{job_id}/pause`: fences the planner epoch and
  returns leased tasks to pending; already successful checkpoints are retained.

A manifest has `phase: "DOMAIN"`, `requests: [...]`, and optional `unresolved`.
Each entry requires `product_id`, `purpose`, and `request`; optional
`evidence_ids` identify source responses used to plan the read. The request has
`method: "GET"`, source-relative `path`, exact `params`, and full opaque
`context`: `origin`, `session_scope`, `source_epoch`, `store_type`, `currency`,
and `service_signature`. Additional context dimensions remain in its identity.
Never place authentication headers, cookies, passwords or other credentials in
a manifest. Only allowlisted public Printograph read routes/parameters are
accepted; customer and cart item targeting are rejected.

Purposes are `specifications`, `base_listing`, `base_lookup`, `option_catalog`,
`base_quantity_support`, `option_quantity_support`, and `tier_domain`.

The response reports `planned_unique_requests`, `completed_requests`,
`remaining_unique_requests`, `attempt_budget`, `attempts_used`,
`request_seconds_at_measured_rate`, `measured_requests_per_second`, and explicit
`unresolved` work. Installed status also reports `attempts_remaining`, task
states, and successful response references. The measured-rate estimate covers
only the currently known stage; `is_end_to_end_eta` is always false. Failed
requests remain unfinished. Bootstrap, retries, waits, undiscovered pages,
conditional option reads, composition checks and exports are not silently folded
into an invented total. `catalog_complete`, `discovery_complete`, and
`pricing_unlocked` remain false; `final_configuration_count` remains null even
when `known_stage_requests_complete` becomes true.

## Session transport integration

The app constructs `app.state.source_domains` dormant with an ordinary native
session factory. Session initialization is also explicit:
`POST /api/v1/runs/{run_id}/source-domain-sessions` with
`{product_id, wait_seconds:20}` observes the next normal product request from
exactly one already open matching tab. It never navigates, clicks, launches a
browser, or creates a supplier request. No matching request within the bound
returns a needs-attention error. A blank duplicate retains only the shared
context/tab lifetime. `GET /api/v1/source-domain-sessions/{session_scope}` reads
its in-memory status; sessions do not survive app restart.
Closing the blank helper tab does not invalidate authentication: session
liveness follows the shared browser context's close event. Context closure
blocks new reads even when an old manifest still contains the matching scope.

Initialization returns `session_scope`, `source_epoch`, and a complete `context`
to copy verbatim into the manifest. The context includes a whitelisted public
controller diagnostic, actual explicitly observed proof/file-delivery values,
and an opaque service signature. Its `source_contract` is empty,
`source_contract_verified=false`, and `composition_verified=false`; it cannot
be used to claim calculated pricing validity. Private observed headers and native
cookies never appear in the API response, manifest, event or evidence object.
The factory refuses requests that omit or change the initialized context.

The reusable `SourceDomainSessions.factory(claim, budget)` returns a dedicated
`Transport` or an async context manager yielding one. The factory must use the
supplied budget during any browser/session bootstrap. `DomainBudget` supports
the same `check()`, `reserve(phase, method, url)`, and
`finish(attempt_id, status, elapsed)` calls as `BrowserHTTPClient`. An existing
client already using this exact budget is not double charged; ordinary HTTPX
clients receive a wrapper that reserves every outgoing attempt before I/O.
Transport's shared source limiter, immutable evidence persistence and exact
context cache remain active. Do not share the mutable Transport object across
planner tasks or legacy crawler workers.

One HTTP connection pool is shared within the initialized session. Per-request
wrappers clear only their own header copy; they never clear another request's
shared cookie jar. Shutdown clears private HTTP headers/jar, closes its HTTP
pool and its own blank duplicate only. Native browser login and all other tabs
remain owned by the browser pool. DOMAIN transports use `refresh=True`.

Pausing the last product walker while this coordinator owns a preflight/running
DOMAIN stage keeps the run active for that separate scope. A user-level run
pause/cancel always wins. After the last stage drains, pauses or fails, the
ordinary engine settles the run back to paused if its product walkers remain
paused. No product is resumed by this lifecycle hook.

Operational budget/claim/status reads defer the manifest/request JSON; status
uses grouped SQL counts. Worker polling does not repeatedly deserialize the
whole stage or return an ever-growing response-reference list.

A shared session/context/access/factory failure trips a stage-wide circuit
breaker. The coordinator fences all in-flight leases, leaves unfinished tasks
pending, emits `source_domain_attention`, and stops claiming further IDs. A
request-specific HTTP404 remains a task failure and does not block unrelated
reads. Pre-I/O session failures spend no HTTP budget. After an application
restart, create a new scope with the newly initialized context; the former
scope remains an unchanged diagnostic record rather than silently rebinding its
immutable requests to different credentials or service state.

The browser route wrapper consumes a paused product's `RunInterrupted` locally
while aborting its route. Otherwise Playwright stores that event-listener error
on its shared connection and can throw it in an independent native cookie read.
The local Chromium regression verifies that pausing a walker does not poison a
subsequent cookie API call from the retained browser context.

The explicit start durably records and publishes `source_domain_preflight`
before workers can fetch. Each task is fenced by planner job epoch plus worker
lease/epoch. Completion requires a persisted successful response with exact
request signature, URL and query binding. A crash after response persistence but
before task commit can reuse the exact fresh Transport cache; it cannot falsely
mark the task complete. Construction never auto-resumes prior running stages.
Successful stage responses are retained across clean shutdown/restart. No
source-domain job or supplier request was started while implementing this API.

Local regression coverage includes preflight publication blocking, dormant API,
paused product isolation, full-context dedup, bounded HTTP attempts, response
checkpoint reuse, stale in-flight completion fencing, strict lookup binding,
unknown future work, and Boolean wire-query encoding.
