# Browser traversal and throughput audit

Audit scope: saved run `36c1c4b084664d36b7b32dbca7cf8f08`, local browser/network code, and local Chromium regressions. This audit made no supplier requests and did not restart the running application.

## Findings and repairs

### P1 — Finishing recovery repeatedly rebuilt the page

`BrowserAdapter.extract.restore` required quantity to be part of the prefix before accepting a previously witnessed inactive finishing control's absence. The quantity loop restores its parent before selecting the next quantity, so the condition rejected that normal intermediate state. Recovery then selected the smallest offered quantity to reveal finishes, even when that quantity itself hid the controls.

Saved Club Flyers evidence proves the mismatch:

- `4d973b99042e43ca9709b7aa6150aad6`: quantity 50, no raised-print or spot-coating controls.
- `5ac9512465704465a1344440dff2e1ff`: same size 272, paper 10, color 2, Regular production; quantity 100 exposes both controls with None selected.
- Later quantity 30,000 through 100,000 quotes took approximately 20 seconds per tier and showed newly generated select IDs. Between Unix times 1790388000 and 1790388650, multiple base `100125944` pricing routes were fetched 32 times each; each route had one semantic response hash.

Repair: remember finite quantities where each finishing choice was actually available, scoped to the same navigation, base dimensions, coating, corners and production mode. Prefer one of those currently permitted quantities when revealing a finishing sibling. An inactive None control can remain absent while restoring a quantity child if its current quantity is an observed enabled positive choice, the production prefix reads back exactly, and the same-context inactive witness exists. The exported record retains the actual absent control. Paid choices and explicit finishing filters cannot use this exemption.

The witness is a recovery aid, not a pricing rule. Every selected quantity still uses normal source controls, settlement, price evidence and existing integrity checks. Witnesses are bounded and cleared on navigation.

### P1 — A transient connection reset stopped Club Flyers

Events 21733 and 21737 record a document `net::ERR_CONNECTION_RESET` during navigation generation 440 → 441. This is a network failure, not a price mismatch. Silently ignoring it would permit stale quotes.

The engine repair is tracked separately: bounded retries rebuild browser state and resume durable work. Browser HTTP 500/502/503/504 responses are classified as `source_unavailable` for that retry policy. HTTP 404 remains `source_error`, 401/403 remain `access_required`, and 429 remains `rate_limited`. Authentication failures, source errors outside the allowlist, and pricing-integrity conflicts remain explicit failures. Browser Retry-After parsing now uses the shared parser for both delta-seconds and HTTP dates on 429/503 responses; response failure is still propagated.

### P2 — Deduplicated diagnostics concealed repeated failures

Previously only one event per reason/control was emitted, so hundreds of reloads could look like one skipped branch. Diagnostics now retain bounded exact occurrence totals, emit the first and power-of-two occurrences, and include the requested prefix, effective public control values, next choice, reload reason and navigation generation. Completion reports expose totals; a terminal diagnostic summary is also attempted on failure. No headers, cookies, credentials or account fields are included.

## Remaining work, not claimed as implemented

- The bulk quantity fingerprint deliberately rejects changes in non-quantity control domains. Club event 17513 reports this fallback. Optional inactive finishes appearing or disappearing could eventually be handled by proven domain partitions, but broad fingerprint relaxation would risk mispriced rows. This audit does not weaken it.
- Browser responses are persisted as evidence; they are not an application-level browser request cache. `Transport.fetch` caching does not make ordinary `route.continue_` browser traffic reusable. A future cache needs complete request and session/pricing context, source freshness rules, and honest cached-observation provenance. URL-only reuse is unsafe.
- The durable browser journal skips previously exhausted supported branches. It is not a general dependency planner, nor does it eliminate replay of unfinished work or retroactively certify old rows.
- No reliable full-domain count or completion ETA follows from the three saved row counts. The source exposes conditional controls and unsupported gaps; throughput claims must distinguish generated table candidates, direct quotes, source requests and actually exhausted domains.

## Validation

Final code passed **11 focused ordinary-Chromium regressions** in two independent selections:

- Six supporting-quantity, paid-sibling and existing finishing-recovery cases: passed in 173.05 seconds.
- Five strict paid/explicit-None/Rush-filter, fatal source-404 and bounded parent-prefix-recovery cases: passed in 118.05 seconds.

The two new unfiltered fixtures each assert all seven valid rows: five inactive quantity states (50, 100, 250, 5,000 and 10,000) and two paid-finishing rows (100 and 250). Both assert exactly one document load and no recovery reload or skipped branch. The strict filter fixtures retain only the actually visible selected finishing configurations and report partial coverage for unresolved choices. Terminal diagnostic totals and public prefix/effective-state fields are also asserted. Witnesses are learned only after normal stable settlement, and the Python module compiles successfully.

These tests establish local traversal behavior. No post-deployment supplier sweep or improved live completion time is claimed by this report.

An additional **nine Chromium regressions passed in 18.98 seconds**: all eight HTTP statuses above stop extraction before a stale displayed quote can be published, and a delayed successful response changing the selected proof service settles correctly despite continuously changing service labels. The service signature is computed synchronously; network request capture never calls `settle`, so the new signature does not introduce an await cycle.
