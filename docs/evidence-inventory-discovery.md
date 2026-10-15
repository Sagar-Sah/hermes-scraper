# Offline discovery request manifest

This is a finite candidate discovery plan, not a catalog of valid combinations. No supplier or model requests were made. No price requests are present or unlocked.

| Product | Bound size/orientation/shape tuples | Papers | Colors | Finishes | Modes | Candidate lookups |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| business-cards | 10 | 12 | 3 | 1 | 2 | 720 |
| club-flyers | 15 | 13 | 3 | 1 | 2 | 1170 |
| roll-labels | 81 | 9 | 1 | 3 | 1 | 2187 |

Total candidate base lookups: **4077**. A candidate must still yield a fresh, context-bound unique lookup before its base ID can be used. Historical matches are evidence links only. Invalid and unresolved results need separate durable states; errors cannot count as discovered valid products.

The Roll Labels shape and orientation come from each saved size record. The plan never multiplies every shape by every size. Custom dimensions are outside the finite preset scope.

The client uses GET `/products` with productType, turnaround and selected base selectors. Its consumer accepts exactly one returned item. The observed caller does not establish pagination, omission-of-filter listing, or multiple base-product-ID lookup. Its comma-separated batching is for eligible option **type IDs** in `/products/options/quantities`, not base-product listing.

After a validated base lookup, conditional discovery requests can obtain its option catalog, base quantity/turnaround support, and option quantity/turnaround support. Business Cards and Club Flyers have distinct frontend option variants; the active store variant must be resolved before option-domain exhaustion is claimed. POST `/rules/turnarounds` exists, but its historical response table lacks the request payload, so no invented request body is emitted.

Completion requires all declared finite candidates and dependent domains to be resolved. Ambiguous results, paging markers, source errors, missing service/session context and unsupported custom dimensions remain explicit blockers or scoped exclusions. Candidate count is not a complete request budget: option discovery and unresolved branches can add work.

## Source evidence

Offsets are zero-based character positions in the frozen bundles.

| Evidence | Source function | Offset |
| --- | --- | ---: |
| lookup | orderForm.js:getProductId | 199653 |
| single_result | orderForm.js:processProductIdData | 201844 |
| shape_dependency | orderForm.js:autoDetectShapeForSize | 1858400 |
| option_type_batch | orderForm.js:getOptionQuantityBySpec | 210485 |
| comma_separated_option_types | orderForm.js:getOption | 207783 |
| specifications | chunk-common.js:getSpec | 550835 |
| option_catalog | chunk-common.js:getOption | 555400 |

Bundle SHA-256:

- `orderForm.js`: `fd26a9c72ba1a162d5d12aa0f0f845dfed7d1a136f276440f1b5b06a465a1d8b`
- `chunk-common.js`: `577bddf60b70e3c2c8362de3f358cd9477dc21232aa1d3c41839d5e1db1ed88d`

Specification evidence:

- business-cards: `33900e18abc4436f8a2f77fb96420180`
- club-flyers: `66415518dfd64d75948ab9bd7bb98e08`
- roll-labels: `c94286e6ab0f4e93b419c9f86daf8212`
