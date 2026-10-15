# Hermes optimization: evidence and implementation status

This update distinguishes saved observations, tested code, live supplier results,
and the unfinished catalog acceptance test. All three browser walkers are
individually paused. Explicit HTTP table stages use the retained authenticated
session while preserving those pauses and previously saved rows. Those finite
table stages have drained. The live dashboard now includes the
[Business Cards option inventory and candidate matrix](inventory-first-results.md):
43,700 candidates with pre-coupon component subtotals and a separate CSV.
These counts do not mark a product complete or upgrade its existing quote rows.
Three new [Business Cards dropdown canaries](business-card-canaries.md) match
the saved tables and fresh displayed totals to the cent. They are scoped sample
checks, separate from the frozen historical evidence audit below.

## 1. Offline evidence mining

The frozen inventory examined 39,813 relevant response observations without a
supplier or model request. Excluding DOM quote snapshots, exact URL/status/body
repetition was:

| Product | Saved route observations | Repeated observations | Repeat ratio |
| --- | ---: | ---: | ---: |
| Business Cards | 12,045 | 10,702 | 88.85% |
| Club Flyers | 13,293 | 12,182 | 91.64% |
| Roll Labels | 5,063 | 3,824 | 75.53% |

These are capture ratios, not a complete network census. Identical bodies across
unknown service/session contexts are evidence of potential savings, not proof
that a request can be omitted. The [route inventory](evidence-inventory.md) and
its JSON companion include parameters, response shapes and attribution limits.

The [complete-component math audit](evidence-math-post-contract.md) examined
6,019 displayed quotes: **0 fully resolvable passes, 0 definite complete-sum
mismatches, and 6,019 unresolved observations**. The pass rate is undefined.
Missing historical coupon/override state, hidden options and proof subselections
cannot be reconstructed by assuming that a residual is a particular fee. Every
observation and missing component is listed in the companion JSON.

## 2. Live read-only probes

Authenticated HTTP reads work using the browser's retained session, with cookie
state kept in memory and ordinary TLS verification. A source listing returned
180 Business Card, 304 Club Flyer and 459 Roll Label base IDs at a larger page
limit. Those IDs are not a proven complete set of currently valid form rows.

The Business Cards base table returned 26 quantity tiers; manual proof returned
27. An unfiltered foil table returned four tiers, while a two-quantity parameter
returned only one. Thus a multi-quantity option contract is not established.
Sparse tables can reflect real availability: selected option and production
domains must be intersected before generating rows.

The controlled browser experiments have encountered uninitialized forms, source
page navigation and closed targets. They do not yet establish independent table
dependencies. The rerunnable [probe report](dependency-probe-results.md) records
the current budget, every outcome, formula gaps and eligible comparisons. A
finished browser experiment is not automatically a valid priced baseline.

## 3. Planner and harvester code

Implemented and tested components include source-derived discovery candidates,
configuration constraints, exact request identities, finite dependency
certificates, a durable shared queue/coordinator, response binding/checkpoints,
and scoped quantity-row generation. Unknown selections, missing tiers and
unproven coupon rules remain unresolved. No component fabricates a valid
Cartesian product or silently turns a one-tier response into a flat fee.

The separate source-domain stage budgets known reads before final option
availability is known; its request-duration estimate is not an end-to-end catalog
ETA. The first Roll Labels stage completed 918 of 918 requests: 459 base-price
tables and 459 option catalogs. All 918 source responses were HTTP 200, within
the 1,050-attempt ceiling. The tables contain 6,886 base-price entries (201
distinct full base-table bodies), not 6,886 verified complete configurations.
These entries exclude selected option/service charges, shipping and tax.

The next stage completed all 1,122 known reads with a 1,290-attempt ceiling:
484 base-price tables and 484 option catalogs for Business Cards and Club Flyers,
plus 154 exact Roll Labels selector lookups. The selector candidates cover only
153 of the 459 listed base IDs and require fresh validation. This stage uses
the same retained session and leaves browser walkers paused. It produced 2,130
Business Cards and 3,840 Club Flyers base-price entries. Together with Roll
Labels, 12,856 base-price entries are retained. This stage took about 232 seconds
from its durable preflight event to its last saved response, averaging 4.83
successful reads per second. All 1,122 requests returned HTTP 200 with no retries.

A subsequent Roll Labels manual-proof stage completed 918 of 918 reads, also
all HTTP 200 without retries, within its 1,050-attempt ceiling. Those responses
contain 24,786 option-price entries (27 tiers per table). These are component
prices, not independently verified final configuration totals.

Four bounded filter experiments found no contradictions against 154 fresh exact
Roll lookups. The next 154-read membership/specification stage completed with
all HTTP 200 responses. The fresh specification bodies match the saved source
domains. Filtered membership covers the observed base domain for all three
products, but does not independently prove arbitrary intersections or pagination
exhaustion. After reusing only the 154 exact same-context lookups, 1,505 full
configuration checks were collected in exactly 1,505 successful requests, within
the 1,730-attempt ceiling. The completed audit found all 1,659 exact selector
tuples matched their predicted base entries, including the 154 retained reads:
360 Business Cards, 570 Club Flyers and 729 Roll Labels selector mappings. No
unavailable, ambiguous or mismatched lookups were observed. These are base
selector bindings, not final option combinations, verified prices or a proof of
unlimited catalog pagination.

HTTP table collection is active, but the user's complete-catalog acceptance
test remains outstanding: valid combination coverage, full price composition,
and independent verification are separate gates.

The dashboard now separates current extraction rows from historical records and
shows an unknown final valid configuration count until it is established. A row
count or a product status does not create a known denominator.

The user-facing grid and standard CSVs now omit supplier-ID fallbacks and raw
technical JSON. Readable option labels, prices, proof and production fields are
retained; unknown option labels stay explicitly unavailable and genuine numeric
dimensions remain numeric. Source IDs remain in internal/audit records. Current
snapshots were republished from 13,007 saved scoped rows without any supplier
requests or change to product completion/verification. This consists of 949
Business Cards, 898 Club Flyers and 11,160 Roll Labels rows; it is still partial.
The local download API was checked against that same 13,007-row CSV.

## 4. Cart verification

Cart verification is not enabled. Source review established price composition
and some conditional artwork-free add paths, but not the targeted removal body
or an independently recomputed server line-price response. The source's add
receipt identifies item IDs; it does not itself prove a price recalculation.
Manual-proof rows with the existing artwork delivery setting require images in
the reviewed source path. Changing delivery context would not verify the same
row. The [cart lifecycle report](cart-line-lifecycle.md) identifies the exact
remaining read-only source artifacts and recovery requirements. No cart items,
orders, payments or artwork were created or changed.

A separate public-source acquisition attempted one ordinary unauthenticated
order-page GET and received a redirect to sign-in, with no page body. The
redirect was not followed. The actual cart entry script must still be obtained
from the authorized session; no cart endpoint or mutation was attempted.

## Validation and remaining limits

The broad backend run passed 664 tests. Additional focused checks cover newer
probe, planner, source-contract, queue, browser recovery and dashboard changes;
their fixture results do not certify supplier behavior. The frontend build and
intercepted local browser dashboard checks passed.

Hermes remained available while the Printograph probe pages reloaded or closed.
Those failures are recorded as source/browser interruptions; the reason for all
page drops is not established. A separate local defect was reproduced: pausing
a browser walker leaked an interruption through Playwright's shared connection,
causing the next HTTP-session cookie read to fail before a supplier request.
The route handler now isolates that interruption, covered by a real Chromium
regression test. The one interrupted Roll table was recovered in the same live
session without refetching the other 917 successful responses. Saved extraction
rows remain intact. The new handler and explicit DOMAIN-only resume/retry API
are tested and deployed. The retained HTTP table session ended with the
coordinated reload; the native browser login profile remains in place.

Still outstanding: complete valid row totals, independently established route
dependencies, a completed three-product HTTP harvest within a published request
budget, and independent cart verification. Shipping and account-specific fees
also require their own verified quote context.
