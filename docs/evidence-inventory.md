# Offline evidence inventory

Database SHA-256: `40fcf0361e4db80677d9283ffc6aa01a392f6f043730bdc25362d5ad55c8d780`.

This report made **zero supplier or model calls**. Counts below are saved response observations, not all network requests.

| Product / route family | Captures | Distinct contents | Exact URL/status/content repeats | Direct run links |
|---|---:|---:|---:|---:|
| business-cards: `/service/rest/v1/products` | 397 | 23 | 373 | 72 |
| business-cards: `/service/rest/v1/products/options/quantities` | 591 | 8 | 574 | 145 |
| business-cards: `/service/rest/v1/products/quantities` | 348 | 3 | 334 | 47 |
| business-cards: `/service/rest/v1/products/{base_product_id}/options` | 426 | 22 | 404 | 75 |
| business-cards: `/service/rest/v1/products/{base_product_id}/options/{option_id}/prices` | 8808 | 321 | 7572 | 5096 |
| business-cards: `/service/rest/v1/products/{base_product_id}/prices` | 420 | 16 | 398 | 413 |
| business-cards: `/service/rest/v1/rules/turnarounds` | 1034 | 7 | 1027 | 1034 |
| business-cards: `/service/rest/v1/settings/product/specifications` | 21 | 1 | 20 | 2 |
| business-cards: `DOM_QUOTE` | 1575 | 1575 | 0 | 938 |
| club-flyers: `/service/rest/v1/products` | 426 | 23 | 403 | 187 |
| club-flyers: `/service/rest/v1/products/options/quantities` | 634 | 7 | 617 | 292 |
| club-flyers: `/service/rest/v1/products/quantities` | 411 | 3 | 396 | 189 |
| club-flyers: `/service/rest/v1/products/{base_product_id}/options` | 468 | 23 | 445 | 218 |
| club-flyers: `/service/rest/v1/products/{base_product_id}/options/{option_id}/prices` | 9562 | 306 | 8560 | 7254 |
| club-flyers: `/service/rest/v1/products/{base_product_id}/prices` | 462 | 21 | 439 | 460 |
| club-flyers: `/service/rest/v1/rules/turnarounds` | 1315 | 7 | 1308 | 1315 |
| club-flyers: `/service/rest/v1/settings/product/specifications` | 15 | 1 | 14 | 3 |
| club-flyers: `DOM_QUOTE` | 1762 | 1762 | 0 | 929 |
| roll-labels: `/service/rest/v1/products` | 215 | 83 | 131 | 151 |
| roll-labels: `/service/rest/v1/products/options/quantities` | 229 | 1 | 145 | 165 |
| roll-labels: `/service/rest/v1/products/quantities` | 214 | 1 | 130 | 151 |
| roll-labels: `/service/rest/v1/products/{base_product_id}/options` | 210 | 83 | 127 | 151 |
| roll-labels: `/service/rest/v1/products/{base_product_id}/options/{option_id}/prices` | 2060 | 15 | 1240 | 1778 |
| roll-labels: `/service/rest/v1/products/{base_product_id}/prices` | 206 | 60 | 124 | 204 |
| roll-labels: `/service/rest/v1/rules/turnarounds` | 1918 | 1 | 1917 | 1918 |
| roll-labels: `/service/rest/v1/settings/product/specifications` | 11 | 1 | 10 | 2 |
| roll-labels: `DOM_QUOTE` | 2682 | 2682 | 0 | 1790 |
| unattributed: `/service/rest/v1/product/search` | 3 | 1 | 2 | 0 |
| unattributed: `/service/rest/v1/rules/turnarounds` | 3390 | 7 | 3383 | 0 |

## Observed domains

- 130 distinct successful lookup query/base-ID tuples.
- 231 observed quantity-support query/body domains.
- 1284 finishing-control/context groups.

Full parameter values, structural response signatures, quantity/turnaround domains, source evidence IDs and attribution reasons are in the JSON inventory. None establishes full-domain coverage.

## Limits and safe interpretation

- Responses omit HTTP method, request body, session/pricing context and direct run/product IDs; capture count is not network request count.
- Semantic hashes alone do not establish reusable cache equivalence. Safe cache savings lower bound is zero until context and freshness are proven.
- Exact URL/status/content repeats yield only an optimistic hypothetical retained-observation count, not a safe request budget.
- Observed unions and successful lookup tuples are not a complete catalog and are not Cartesian-product domains.
- DOM control absence does not prove unsupported configuration; controls can depend on prior state, quantity, production or hidden services.
- Finishing clue contexts omit both finishing controls to compare visibility; companion finishing selections can confound a causal interpretation. Inspect referenced DOM evidence before deriving dependencies.
- Temporal adjacency is never used to attribute a response to a product or run.
