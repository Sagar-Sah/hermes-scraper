# Business Cards: three scoped subtotal canaries

The bounded canary completed with **3 of 3 observed totals matching the saved source tables, controller formula and displayed subtotal**. One initialized page was used; the only subsequent changes were quantity, then round corners.

| Observed configuration | Base component | Corner component | Table expectation | Controller and display |
| --- | ---: | ---: | ---: | ---: |
| 50, no round corners | $4.69 | — | $4.69 | $4.69 |
| 100, no round corners | $8.61 | — | $8.61 | $8.61 |
| 100, round corners | $8.61 | $3.00 | $11.61 | $11.61 |

The common branch was regular production, source size 101/orientation 1, 14 pt gloss stock, full-color front with grayscale back, UV front coating, instant proof and file delivery 5. Spot coating and raised printing were empty. The source's inactive manual-proof and design-service selections were retained as captured; they were not added as charges.

Both loaded JavaScript bundle hashes matched the pinned source contract. Each observation had complete source-formula context, explicitly observed empty coupon state, false account/cart/item/price-group override flags, and a fresh uncached base-table recheck. The two changes were unconfounded: only quantity changed from 50 to 100, then only rounded corners changed from empty to 2. The exact saved base, catalog and corner response objects were hash-validated before computing the comparison expectations.

The public numeric store remains **unresolved**: neither the captured controller `store` property nor the inspected order-form attribute exposed a numeric value. This is a separate context gap; these three matching subtotals do not certify store-dependent inventory rules. They also do not verify all 43,700 candidates, prove general dependency independence, or constitute independent cart verification.

The same durable 250-attempt job used **205 attempts**, including unsuccessful initialization and all bootstrap assets. One initial navigation failed without its specific error being retained; a subsequent bootstrap exposed a readiness deadlock. The repaired readiness gate recognizes actual nonempty source session headers on an already-allowed startup GET, while leaving its response aborted. It does not manufacture credentials or assert login validity. No cart, order, payment, artwork upload or product-walker action was performed.

Evidence:

- [Machine-readable canary report](/Users/tl/Documents/Codex/2026-09-25/i-w/work/business-card-canary-report.json)
- [Human-readable report and historical table links](/Users/tl/Documents/Codex/2026-09-25/i-w/work/business-card-canary-report.md)
- [Immutable source observation report](/Users/tl/Documents/Codex/2026-09-25/i-w/work/atlas-data/objects/14/140495e069f14a0b6a5da2560120bc151fa80c714f26e325a8af474b608d64e0.response)

The canary report retains response evidence IDs for each phase and all three fresh base rechecks. Re-running its reporting command reads saved evidence and the local API; it does not issue supplier requests. Starting or resuming a probe is a separate explicit command and retains the same durable budget.
