# Roll Labels: proposed option-price table reads

This offline plan uses completed source-domain job
`17c9eab0cbb91e45a2a68b7a421509f25861d57826910b440a3a07843bad0556`:
459 base-price tables and 459 option catalogs, all recorded HTTP 200. It made
**zero supplier requests**, changed no server state, and certifies no product
configuration or final price.

The fresh catalogs contain 5,049 option records. Every option-record ID is
unique across these 459 bases. Option **type IDs**, catalog **record IDs**, UI
option **group IDs**, and the special **priceGroup request context** remain
separate identities.

| Option types | Catalog group | Records | Exact unfiltered price requests |
|---|---:|---:|---:|
| 1 manual soft proof; 36 manual no proof | 1 | 918 | 918 core non-design candidates |
| 41 / 42 / 43 design packages | 13 | 1,377 | 1,377 optional ordinary-query candidates |
| 31 instant proof | 1 | 459 | 0: absent from source priced-option list |
| 32 / 33 / 34 / 35 / 54 wind position | 12 | 2,295 | 0: absent from source priced-option list |

Zero price requests for instant proof/wind means that the reviewed calculator
does not fetch their own option-price table. It does not establish general
free-service semantics or combined option validity.

## Independently established request contract

The saved `orderForm.js` SHA-256 is
`fd26a9c72ba1a162d5d12aa0f0f845dfed7d1a136f276440f1b5b06a465a1d8b`;
`chunk-common.js` is
`577bddf60b70e3c2c8362de3f358cd9477dc21232aa1d3c41839d5e1db1ed88d`.
All offsets below are zero-based character offsets in those pinned files.

- `optionsHavingPrices` at orderForm 56423 includes 1, 36, 41, 42 and 43;
  it excludes 31 and all five observed wind types.
- `updateOptionPrice` at 234835, filtering at 235422, uses catalog `type.id`
  membership in that list. It does **not** select requests using
  `quantityDependentPrice`. Ordinary Roll Labels follows the selected-quantity
  request with an unfiltered request.
- `getOptionPrice` at 236500 builds
  `GET /service/rest/v1/products/{selectedBaseId}/options/{catalogRecordId}/prices`.
  `getOptionPriceForSpecificQuantity` at 238209 adds the single selected
  `quantities` value. The unfiltered endpoint's existence is independently
  established; there is no need to invent a multi-quantity query format.
- Both callers add `designService=true` for active proof mode 101, and can add
  priceGroup or itemId/cid context. Common `getOptionPrice` at 557095 executes
  the supplied URL as a GET. Cache-key logic at common 752948 distinguishes
  priceGroup and the design-service flag.

The proposed manifests contain unfiltered **ordinary non-design** queries
with no special account/item overrides. Their context is deliberately omitted.
The root planner must supply the exact current bridge context and check that
these query conditions hold. Active design-mode tables require a separate
`designService=true` plan and bound service context. A design-package price
observed under ordinary proof mode is not an active design-service quote.

## Exact deduplication and historical observations

After exact method + full path + query + null-body deduplication there are
**2,295 distinct priced routes**: 918 core manual and 1,377 optional design.
All 2,295 priced option-record IDs are distinct. The source includes the base
ID in each path, so matching returned prices cannot collapse routes across
bases. No cheaper shared manual-proof route was found in these callers.

Saved historical unfiltered responses cover 775 routes: 155 routes for each
priced type. There are 2,291 observations and only five distinct full semantic
bodies, one per type in this historical set. Manual types have 27 quantity
entries; design types have 15. Another 2,295 saved responses were explicitly
quantity-filtered and are kept separate. These are historical body observations,
not current-context reuse certificates; they do not reduce the fresh plan.
Returned quantity tiers also need intersection with actual product/option/
turnaround availability. No singleton expansion, interpolation, or inferred
cross-base equivalence is introduced.

## Artifacts and remaining blockers

`work/prepare_roll_option_table_stage.py` reruns this offline inventory and
checks both source hashes. It creates:

- `work/roll-option-table-plan.json`: exact type/record/group identities,
  catalog evidence, historical evidence IDs, body counts, and both plans.
- `work/roll-manual-proof-price-manifest.json`: 918 contextless core reads.
- `work/roll-design-option-price-manifest.json`: 1,377 contextless optional
  ordinary-query reads.

Every candidate is marked `fresh_revalidation_required=true` and
`current_context_certified=false`; each plan is `ready_to_install=false`.
The concrete next requirement is current bridge context and an explicitly
chosen finite request budget. These manifests collect option **price tiers**,
not the separate `/products/options/quantities` validity domain. Selector
binding, combined availability, complete service/coupon composition, shipping,
independent verification, and final configuration counts remain separate work.
