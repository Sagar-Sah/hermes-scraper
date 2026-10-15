# Reviewed supplier pricing contract

This is separate from the frozen snapshot-only audit. The parent task acquired
two public JavaScript bundles in a bounded probe. This review and both math
audits made no requests. Neither audit changes saved rows or verifies a cart.

| Saved bundle | SHA-256 |
| --- | --- |
| `work/planner-contracts/orderForm.js` | `fd26a9c72ba1a162d5d12aa0f0f845dfed7d1a136f276440f1b5b06a465a1d8b` |
| `work/planner-contracts/chunk-common.js` | `577bddf60b70e3c2c8362de3f358cd9477dc21232aa1d3c41839d5e1db1ed88d` |

Offsets below are zero-based character offsets in these exact saved files, not
portable line numbers or claimed byte offsets. Changed hashes require review.

## Formula and rounding

`orderForm.js` `totalPrice` at 1595750 (also 1737473 and 1774315) computes:

```
base product price
+ manual proof[manualProofType], only when proofType == 100
+ selected configured option prices
+ design service[designServices], only when proofType == 101
= subtotal, formatted with JavaScript toFixed(2)
```

The normal-product price is a subtotal for the selected quantity. It is not
multiplied by quantity again. Selected option prices are also their returned
quantity-specific totals, except a separate accessory branch irrelevant to the
three products here.

`updateProductPrice` reads `markupPrice`, removes US grouping commas and applies
`parseFloat(...).toFixed(2)` (230583). `updateOptionPriceFromCache` does the same
per option (239980), then `totalPrice` adds the resulting amounts. Its option
lookup permits exact quantity or a one-entry table. The audit conservatively
requires an exact quantity and reports fractional-cent components unresolved;
it does not approximate JavaScript binary rounding with guessed tolerances.

The generic-product and free-sample exceptions do not apply to product types 1,
27 or 36: `isGeneric` at chunk-common 733163 requires a type of at least 10000,
and `canOrderFreeSampleKit` at 388042 requires product type 100000/type 2006.

## Component mapping

| Controller field | Source type IDs / behavior | Saved evidence requirement |
| --- | --- | --- |
| `roundedCorners` | 2, catalog group 2 | Actual selected raw value and exact option price |
| `spotCoating` | 3, 4, 39, group 5 | Actual selection and price |
| `raisedPrint` | 135–140, group 50 | Actual selection and price |
| `raisedFoilColor` | 141–143, group 51 | Actual selection and price, even though its UI schema says `hasPrice:no` |
| `coating` | No applied-price bucket or cache assignment | Source contract proves no separate additive component |
| `windPosition` | No applied-price bucket or cache assignment | Source contract proves no separate additive component |
| `paperFinish` | No applied-price bucket or cache assignment | Source contract proves no separate additive component |
| `proofType=31` | Instant mode contributes no proof component | The mode must be observed; no selected-mode default is inferred |
| `proofType=100` | Uses separate `manualProofType` in group 1 | Observe the secondary selection; mode 100 alone is insufficient |
| `proofType=101` | Uses selected `designServices`, group 13 | Observe selected package and its price |
| `fileDelivery` | Omitted from the subtotal formula | Retain mode and delivery identity; this says nothing about shipping charges |

Cache assignments are explicit switch cases in `updateOptionPriceFromCache`
(239980). The `optionsHavingPrices` list (56423) excludes coating 5/40, wind
32–35/54 and instant proof 31. `appliedPrices` initialization is at 45417.
Initialization to zero is **not** evidence of a successful price request for a
priced selection; a missing response remains unresolved.

Broker field definitions for Business Cards and Club Flyers include
`roundedCorners`, `coating`, `spotCoating`, `raisedPrint`, `raisedFoilColor`, and
`paperFinish` (83784 onward). Roll Labels uses `windPosition`. The total formula
reads the corresponding controller state even if the DOM control is hidden.
Therefore, a visible-control snapshot cannot prove a hidden priced field empty.

## Manual proof and file delivery

The `proofTypeForManual` radio at 1498365 is bound to
`userSelectedOptions.manualProofType`. Its schema at 134298 lists:

| Raw value | Label |
| --- | --- |
| 1 | PDF Proof |
| 36 | No Proof |
| 38 | PDF Proof 24Hrs |
| 37 | High-Res Hard Copy Proof |

Although the initial object assigns manual proof 1, option loading can change
it to 36 (`setNoProof` path around 206254). The historical $8.60 quote with mode
100 does not establish which of these was selected. Its $3 residual cannot be
used to infer the missing value.

`fileDeliveryForIop` value 5 means Upload Files Now; other listed values are 22
Design Online, 33 Basic Creator and 34 Design Inline. The manual-delivery group
has 5 Upload Files Now and 6 Media via Mail; design-service delivery has a
separate set. Their schema has no additive price, and the subtotal formula does
not read file delivery. Mode 31/delivery 5 and mode 100/absent IOP-delivery group
must still remain distinct recorded selections. The absent IOP group does not
establish which manual-delivery value was selected.

## Remaining closure requirements

The old diagnostic scope records only `proofType` and `fileDeliveryForIop`.
It omits `manualProofType`, manual/design delivery, design package, and hidden
priced option state. These must be observed from narrowly allowlisted source
state or supplied in an explicit read-only quote payload before full math
reconciliation. Do not copy customer files, account tokens or unrelated state.

`getCouponPrice` at 1593100 can replace the displayed subtotal with a percentage
discount capped by a maximum. Admin override/waiver and special price-group
state also affect interpretation. The audit requires explicit normal context;
it does not infer absence of discounts or overrides from missing captures.

`crawler.evidence_math` keeps snapshot-only and post-contract reports separate.
The post-contract phase resolves the proven noncontributing fields and known
instant mode, but preserves missing runtime values as unresolved. Partial-sum
matches are not full passes, cart verification, or permission to upgrade stored
`source_table` rows.

## New explicit capture contract

`crawler.price_contract` is separate from the existing service schema, stored
rows, browser traversal and quote transport. Its public functions are:

- `normalize_price_context(capture)`: canonical whitelisted selections, source
  hashes and normal-account flags, or explicit unresolved reasons.
- `calculate_source_total(capture)`: component ledger, pre-coupon subtotal,
  complete coupon-adjusted subtotal when available, and exact comparison with
  `displayed_total` if supplied.
- `round_source_component(markup_price)`: the reviewed US-grouping removal and
  JavaScript binary `toFixed(2)` cache conversion. Already applied controller
  prices are consumed as recorded, in the original addition order.
- `CONTROLLER_PRICE_STATE_JS`: a read-only whitelist extractor for the mounted
  `#order-form` Vue controller. The caller must first verify both actual loaded
  bundle hashes and establish page/request stability. Passing `settled:true`
  is a caller assertion, not a stability check performed by this helper.

The capture shape uses `source_contract` (the module's two pinned hashes),
`capture_kind` (`controller` or `explicit_request`), `observed`, `settled`,
`product_type`, `store_type`, `currency`, `field_options`, `userSelectedOptions`,
`appliedPrices`, `other_context` and `coupon`. Controller captures also require
`controller_total`; an independent rendered quote may be supplied separately as
`displayed_total`. No legacy snapshot is retroactively converted to this shape.

The selections include every source-defined base and option field, including
hidden finishing fields, all four service values (`proofType`,
`manualProofType`, `designServices`, `fileDelivery`), quantity, product ID and
production mode. An explicitly empty priced option contributes nothing; a
selected option with a missing applied price is unresolved. An explicit zero
can reconcile the controller arithmetic but does **not** prove a successful
price fetch: freshness must be bound independently to source captures.

`other_context` requires explicit booleans `is_admin`, `waive_fees`,
`has_item_id`, `has_shopping_cart_item`, `has_price_group` and
`has_edit_order_item`. These must all be false in the initially supported
normal broker context. The extractor records these presence flags without
copying order identifiers, account credentials, addresses, uploads or notes.

An explicit coupon parameter capture provides `observed:true`,
`mode:"parameters"`, `is_coupon_spec_changed`, `valid_for_specs`,
`qualification_level`, `status_id`, `amount_percent` and `max_amount`; the last
two may be explicitly null. Missing parameters are not inferred. Further
source inspection confirmed the controller exposes the closure through
`couponData:H` (1592107). The extractor now reads only the applicability flags,
status ID and first tier's percentage/cap, plus a count of the actual coupon
object's keys. It never reads or emits a coupon code or browser storage.

An observed empty coupon object, false specification-change flag and settled
controller establish the reusable `empty_source_coupon` state within the same
source epoch. The reviewed object starts empty (178182), is synchronously
populated at mount (1592318), and eligibility requests require qualification
level 1 (196037). Responses merge into the object rather than clearing it.
Missing coupon state is not an empty object. Nonempty state is bound to the
selected quantity because `sendCoupon` includes quantity and file delivery in
its eligibility request (196324); it is not reusable across quantities.

When the controller does not expose couponData, the extractor retains the
reviewed pure `getCouponPrice(controller.totalPrice)` result as a fallback.
Its numeric zero sentinel establishes inactivity for that exact quote only.
An active formatted result remains unresolved without percentage/cap inputs.
Neither result-only form supplies a reusable quantity-wide rule.

The context hash excludes component prices, observed coupon output amounts
and evidence identifiers. A price change therefore cannot manufacture a new
context and conceal a conflict. Coupon parameters and actual source selections
remain part of the hash. Complete arithmetic is explicitly marked
`fresh_source_verified:false` and `cart_verified:false`; it neither establishes
shipping/tax totals nor upgrades stored verification states.

Validation: 68 focused tests passed across the new contract and both offline
audit phases, including an intercepted local Chromium controller fixture. The
fixture made no supplier calls and confirmed unrelated private state is absent
from extractor output.
