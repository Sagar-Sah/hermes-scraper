# Hermes forensic audit and repairs

Audit date: September 25, 2026 (Phoenix); September 26 UTC. Supplier run:
`36c1c4b084664d36b7b32dbca7cf8f08`.

**The slow and incomplete harvest has real code-level causes.** Two products
stopped while the dashboard continued to show a running catalog. One failure
was a pricing-identity collision; another was an unrecovered connection reset.
Repeated page reconstruction consumed requests without adding configurations.
The implementation also lacks several parts of the original high-throughput
design. A faster language model cannot repair those execution paths.

This audit combines source review, saved DOM/network evidence, database events,
an independent review of the fixes, and local Chromium regressions. It does not
infer historical service choices, silently replace prices, or certify pending
table rows as verified.

## What the saved job actually shows

At **02:34:17 UTC on September 26**, before this repair deployment:

| Product | State | Saved rows | Counted requests | Immediate problem |
| --- | --- | ---: | ---: | --- |
| Business Cards | Needs attention | 693 | 8,229 | Different proof services collided under one identity |
| Club Flyers | Needs attention | 730 | 11,225 | Connection reset after repeated restoration/navigation |
| Roll Labels | Running | 5,104 | 3,990 | Still traversing; calculated quantity rows remain pending |
| Total | 0 of 3 complete | 6,527 | 23,444 | Running catalog did not mean three healthy workers |

These are saved observations, including calculated candidates. They are not
verified configuration counts or a coverage percentage. The pre-repair records
lack the service-scoped identity required by this repair and are retained as
historical evidence. Their original bytes are not changed.

## Confirmed defects

P1 means a defect that stops useful work or undermines price identity. P2 means
incorrect comparison, status, recovery diagnostics or publication behavior.

| Severity | Defect | Result | Repair |
| --- | --- | --- | --- |
| P1 | Proof/file-delivery selections omitted from row identity | $5.60 and $8.60 were compared as the same configuration | Preserve raw observed service IDs and a canonical versioned pricing context |
| P1 | Bulk capture and verification did not require equal service selections | Samples could agree without establishing equal pricing context | Bind request, settled form, samples and derived rows to the same observed context; otherwise use direct quotes |
| P1 | Finishing recovery chose a quantity that hid finishing controls | Repeated page loads and price requests, roughly 20 seconds per later tier in the saved Club branch | Learn source-observed supporting quantities; restore at a proven quantity with strict context/filter checks |
| P1 | Transient network errors ended a product immediately | Club Flyers stopped on `net::ERR_CONNECTION_RESET` | Durable delayed retries with epoch fencing and a three-attempt budget without new durable rows |
| P2 | Price analysis grouped by display labels | The same rounded-corner option became separate curves as its displayed surcharge changed with quantity | Compare raw option values, production IDs and service/financial context |
| P2 | Running state concealed failed products; recent-only activity hid older errors | The dashboard looked active while two products were stopped | Separate active, complete, partial and attention counts; page activity and expose polling failures |
| P2 | Export freshness used row count alone | Old unscoped files could survive classification as apparently current exports | Compare scope and source/legacy/current counts; publish immutable generations with freshness metadata |
| P2 | Repeated failures were deduplicated out of diagnostics | Hundreds of recovery attempts could resemble one skipped branch | Bounded exact occurrence counters, sampled detail events and terminal diagnostic totals |
| P2 | Amount parser discarded negative/accounting notation | Local inputs `-$5.60` and `($5.60)` became positive 5.60 | Reject these ambiguous/negative displayed totals; no such saved supplier total was found |
| P2 | Pause/cancellation closed nested browser iterators out of order | Shutdown logged `Browser checkpoint frames must close in order` | Abandon unfinished descendant frames safely; retain committed rows and forbid late completion markers |

### The Business Cards failure was not proven source repricing

For quantity 50, size 101, paper 10, color 1, orientation 1, coating 5 and Regular
production, saved DOM evidence contains:

- `dd9ecbbe1837416ca0f8373cd63f3548`: $5.60, proof 31 (Instantly Processed), delivery 5.
- `559ba535a9af4e6d93186c0df15786bc`: $8.60, proof 100 (Manually Processed), delivery group absent.

The corresponding base-price observations have identical hashes. A captured
manual-proof option is $3.00 in both observations. This explains why the old
identity can generate a false price-conflict alarm; it does not justify adding
$3 universally. The earliest stored $5.60 row predates service diagnostics, so
its proof choice is unknown and remains unknown.

The new code records selected, absent, unselected, unknown and ambiguous service
states explicitly. Observed absence is structural evidence, not a zero-fee
assumption. Unknown/ambiguous context cannot support bulk expansion or an
independent verification match. A genuinely different total under the same new
identity still stops extraction for review.

### The repeated navigation was avoidable

Club DOM `4d973b99042e43ca9709b7aa6150aad6` shows quantity 50 without raised-print
or spot-coating controls. DOM `5ac9512465704465a1344440dff2e1ff` shows quantity 100
with both controls at None, under the same size/paper/color/production branch.
The old recovery code kept selecting the lowest quantity, 50, to reveal controls
that it actually hid. Parent-prefix restoration then triggered another reload.

For one saved time interval, multiple base `100125944` pricing routes were each
fetched 32 times with one semantic response hash. The repair uses quantities
where the relevant control was actually observed, within the same navigation
and pricing branch. Paid options and explicit filters remain strict. The new
local regressions produce all seven expected configurations with **one page
load and no recovery reloads**. This is a proven local improvement, not a
whole-catalog speed multiplier.

### Recovery and checkpoint safety

Connection failures, exhausted transport retries, rate limits and explicitly
classified HTTP 500/502/503/504 can retry. Delays are 5, 10 and 20 seconds, with
at most three scheduled recoveries at the same durable row count. The shared
supplier limiter still honors Retry-After, including HTTP dates. New committed
rows reset the no-progress retry budget. Authentication, HTTP 404, schema and
price-integrity failures are not blanket-retried.

Requeueing, the retry event and worker fencing commit together. A restart cannot
claim a delayed job early; a superseded worker cannot renew its lease. Browser
rows still commit before their completed-branch markers.

The browser journal is now version 2. Markers include the observed service
context; the same product prefix can safely have separate contexts. Unknown or
changed contexts cannot promote a parent marker, and resumption cannot skip
across service selections. Old semantic scopes are intentionally invalidated.
Historical rows are retained, but establishing the corrected dataset requires
re-observation. This adds a one-time replay cost rather than manufacturing
missing provenance.

Retained rows from an unvisited service context are now counted explicitly and
cannot be reported as revalidated on resume. The existing complete-domain
disappearance check remains intact. Real browser cancellation/close tests also
confirm that interrupted nested iterators retain the committed row, write zero
false branch markers and produce no cleanup exception; normal frame-order
invariants remain strict.

## What remains missing from the original plan

These are capability gaps, not claims that the repair implemented them:

1. **A complete minimal-request catalog planner.** There is no proven whole-site
   option graph, exact preflight request count, global prioritized pricing queue
   or defensible catalog ETA. The present browser walker discovers conditional
   controls while executing.
2. **Browser request reuse.** Browser responses are persisted as evidence, but
   ordinary browser traffic is not served from the HTTP transport's response
   cache. A safe cache needs session/pricing context, freshness rules and clear
   provenance. URL-only caching would be unsafe.
3. **Broad validated bulk coverage.** Quantity-table expansion works for guarded
   branches, not every finishing/production combination. Changes in optional
   control domains can force direct traversal. A future optimization must prove
   safe partitions rather than weaken the fingerprint checks.
4. **Automatic independent cart verification.** Comparison helpers exist, but
   no background cart-verification lane is running. Two matching displayed
   samples do not independently verify all calculated rows.
5. **Live shipping and complete rush charges.** Current tools calculate estimates
   from supplied tariffs/calendar inputs. They do not extract every supplier
   shipping, account, combined-cart or rush charge.
6. **Demonstrated supplier throughput at 100 products or 100 requests/second.**
   The 3-million-row benchmark was synthetic. This run uses three browser tabs
   sharing one login, a six-request/second configured ceiling and four in-flight
   source requests. Supplier limits and adaptive pacing still apply.

The LLM currently translates instructions into structured filters. It does not
drive browser recovery, compose supplier prices, or verify the catalog.

## Data and interface contract after repair

- Current browser rows carry schema version 2, explicit observed service context
  and source evidence. Schema version is not a claim of independent verification.
- Older unscoped rows remain accessible as historical/all-row views. Default
  analysis and current exports exclude them. Missing diagnostics are never
  backfilled from a later observation.
- Complete products, partial finished attempts, active products and products
  requiring attention are distinct. A run can be active and require attention.
- Activity is paged; failure to refresh activity or exports is visible even if
  the run-status request succeeds.
- Exports expose generation, creation time, row scope and freshness. Old files
  remain immutable. Product/catalog manifests are rebuilt when scope changes,
  even if their raw row count is unchanged.

## Validation and deployment record

The clean, previously delivered package passed **all 420 existing tests** in
917.25 seconds. This established the baseline; it did not establish supplier
correctness. The defects above required new production-derived regressions.

The integrated **502-test suite passed in partitions**: 44 browser/session/HTTP
tests, 27 bulk/service/production tests, 20 traversal/finishing tests, 398 remaining
backend/API/UI tests and 13 journal tests. One old HTTP503 assertion was updated
to the intended retryable error class and rerun successfully; its stale
expectation was the only failure in that partition. The production frontend
build passed. Subsequent targeted checks covered final manifest metadata and
retry claim cleanup. The last shutdown fix added three real cleanup regressions;
those and 14 related checkpoint/engine checks passed. These tests use local
fixtures and do not spend model credits or claim supplier correctness.

The first controlled deployment at **02:45:49 UTC** resumed the existing job
with all **7,427 historical rows preserved** and the saved supplier session. At
**02:48:28 UTC**, all three products were running with **83 newly scoped records**
(29 Business Cards, 27 Club Flyers, 27 Roll Labels), zero product/request errors
since restart, and **0 of 3 complete**. Sampled new rows had observed service
signatures and direct-quote provenance. Current/legacy/all API views were checked
against the actual live data. See the
[initial live validation](forensic-live-validation-initial.json).

All three initial bulk attempts fell back to direct quotes because early captured
tables did not have a matching known service context. The guard is working, but
**this check does not demonstrate faster live bulk extraction**. Preserving
precision currently costs throughput on those branches. Safe request recapture
or independently established table dependencies need further supplier evidence.

The final cleanup patch was subsequently loaded through another controlled
pause/restart; it preserves both historical and new scoped records. The
[latest live observation](forensic-live-validation.json) contains the final
deployment time, product counts, recent public quote evidence and explicit
validation limits. No completion ETA or three-product completion claim is made
by a passing local test suite.

Detailed evidence and test boundaries:
[pricing/service audit](audit-pricing.md),
[traversal/recovery audit](audit-throughput.md), and
[dashboard audit](audit-dashboard.md).

Source entry points:
[browser traversal and captures](../backend/crawler/adapters/browser.py),
[service identity](../backend/crawler/service_context.py),
[engine and exports](../backend/crawler/engine.py),
[durable store](../backend/crawler/store.py),
[branch journal](../backend/crawler/browser_progress.py), and
[comparison rules](../backend/crawler/commerce.py).
