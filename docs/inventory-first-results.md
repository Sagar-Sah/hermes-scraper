# Printograph: inventory and matrix extraction

The new variation-plan view assembles options from saved Network responses, builds a finite row matrix locally, and offers a separate candidate CSV. It does not drive the old browser walker through every row.

## Business Cards result

- 10 size/orientation choices, 12 stock labels, and the three observed color modes: 4/0, 4/1, 4/4.
- One optional Round Corners choice, described by the supplier as rounding all four edges. Separate radius, corner count, and corner position choices were not found in this calculator's specification, option catalogs or pinned source.
- Source-supported coating, spot coating, raised-print, raised-foil-color, production and processing-service choices remain separate dimensions.
- 360 exact selector/production-mode mappings connect the observed base products to readable choices.
- 43,700 candidate rows remain after known frontend rules and individual quantity/production support are applied: 4,370 for each observed size/orientation.
- Every candidate has a calculated component subtotal from exact observed quantity tiers. Missing/null prices are never zero, and prices are not interpolated.
- 795 distinct base/option table targets are used by those calculations. Equal monetary amounts alone do not permit dropping a dependency or combining option identities.

The missing-table stage performed 1,230 requests: all returned HTTP 200, with no retries. From its durable preflight event to its last saved response, collection took approximately 221 seconds. This was the incremental table stage, not the total cost of the earlier discovery work or end-to-end verification.

## How to use the result

Open the existing three-product harvest in Hermes. The **Product variation plan** panel defaults to Business Cards. Expand **Option inventory**, inspect the per-size counts, or choose **Download candidate rows**.

The candidate CSV contains the supplier URL, readable option labels, quantities, production modes, processing services, and pre-coupon component subtotals and unit amounts. Supplier IDs and raw evidence/context data remain internal. This file is separate from the existing customer quote export.

## Live sample checks

Three Business Cards dropdown canaries matched the saved tables, the freshly captured controller formula and the displayed subtotal to the cent: 50 without corners = $4.69; 100 without corners = $8.61; 100 with rounded corners = $11.61. These used one initialized calculator page, changed quantity and corners one at a time, and observed an explicit empty coupon state. The stage consumed 205 of its 250-attempt ceiling, including startup attempts and fresh base-price checks. See [canary evidence](business-card-canaries.md).

These are three scoped sample passes. They do not verify all 43,700 candidates or the cart, and the numeric store variant remains unresolved.

## Boundaries

Candidates are not a claim of complete product coverage or independent quote verification. Combined turnaround/availability, account/service context, action-dependent coating pairs and fresh price checks remain explicit gaps. Final-price and valid-row totals stay unknown while those checks are unresolved. Shipping, tax, coupons and design-service pricing are outside this candidate subtotal scope.

The same inventory-first path now supports Club Flyers and Roll Labels. Fresh source-table collection completed all 7,220 planned reads: 3,196 for Club Flyers and 4,024 for Roll Labels, with no failures or retries. The audit checked every response binding and all 1,299 exact selector lookups; both fresh inventories have zero missing known table requests. This establishes the captured domain, not discovery of every possible source configuration. See [the collection report](club-roll-fresh-harvest.md). Existing product walkers remain paused and their saved rows are preserved.

| Printograph product | Candidate rows | Calculated subtotals | Missing known table requests |
| --- | ---: | ---: | ---: |
| Business Cards | 43,700 | 43,700 | 0 |
| Club Flyers | 66,750 | 66,750 | 0 |
| Roll Labels | 164,040 | 164,040 | 0 |

Every candidate has a source-table component subtotal, but these are not counts of cart-verified or fully available combinations. Roll Labels preserves shape, size, orientation, stock, finish and wind direction. Source availability rules constrain the matrix before export; missing prices are never interpolated. The fresh two-product collection took 28.17 minutes from preflight to the last saved response, plus separate setup and export time. A diagnosed 250 ms worker-loop pause has been removed for subsequent stages; this timing is the observed pre-fix run and is not a promise of future throughput.

Club Flyers decreased from the earlier provisional 88,230 candidates to 66,750 because the new option-support responses exclude 21,480 combinations on 16 pt. Premium Matte in Regular mode. Raised UV/Foil supports quantities 100, 250, 500 and 1,000, excluding 21,120 provisional rows at 50 or above 1,000; Spot UV starts at 100, excluding another 360 rows at 50. The same 570 exact selector bindings remain. All other stocks and the 7,560 Rush candidates are unchanged. Offline decomposition reproduced the reduction using option support alone; price changes did not change the row count.

The source contract is pinned to the reviewed Printograph bundles. A changed source, filtered quantity response, different account context or missing evidence cannot silently substitute for a full table. The planner merges option-support responses for the same exact selector/context by option type; conflicting support remains unresolved.

## Implementation and checks

- `crawler/variation_inventory.py`: durable, context-bound evidence assembly and deduplicated missing-table manifest.
- `crawler/variation_matrix.py`: finite rule evaluation, quantity support, conservative price identities and source-rounded component arithmetic.
- `crawler/variation_plan.py`: cached public counts and immutable candidate exports.
- `GET /api/v1/runs/{run_id}/variation-plan`: supplier-free dashboard reads.
- `GET /api/v1/runs/{run_id}/variation-plan/export?product_id=...`: authenticated, run-scoped candidate export.
- `browser_dependency_probe.py`: bounded dropdown canaries; pending observations never become successful checks.

Focused tests cover exact context/evidence binding, query filters, deduplication, support merging/conflicts, option interactions, missing/null tiers, rounding, full CSV exports, download authorization, CSV formula escaping and paused-worker preservation. The frontend build and desktop/mobile interaction tests pass.

Two operational defects were also corrected. Dashboard polling previously ran an unused timing join across every stage, taking 14.34 seconds and exceeding the UI’s 12-second timeout. Compact polling now skips that unused calculation (0.024 seconds observed); the detailed timing query uses indexed response-ID batches with the same full-context binding checks (eight real stages in 0.20 seconds offline).

The browser verification bootstrap previously blocked startup API requests while waiting for a later product request to expose transport headers. It now accepts real, nonempty allowed session headers observed on known same-origin startup GET requests. This does not assert authentication success or retain account responses. Each canary still has to initialize, settle, reconcile its components and match the saved price tables.
