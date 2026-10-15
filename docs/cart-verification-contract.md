# Cart-verification contract: observed evidence and integration gate

**Result:** saved responses prove calculator/table contracts and several service selections. A later offline inspection of the two public bundles establishes exact cart, checkout-price, shipping and generic-calculation wrappers (see the source-code addendum below). It still does **not** prove an account-safe isolated cart or a verified calculation response for these three products. No live cart verifier or speculative endpoint is implemented by this investigation. `verification.py` already supplies local sample planning and exact quote comparison; those helpers do not acquire an independent cart quote.

This inspection made **zero supplier requests**, opened no browser, read no profile/credential tables, and changed no carts, source settings, or runtime services.

## Frozen evidence

The inventory uses `work/planner-evidence-snapshot/evidence.db`, captured **2026-09-26T04:31:05.387561+00:00**, with immutable response bodies under `work/atlas-data/objects/`. The snapshot contains one run, three products, **39,832 response records**, and 36,277 events. Profiles and origin/session coordination tables were excluded by the snapshot creator. These runtime files are private evidence, not public package contents.

Searching all captured response URL paths found **zero** containing `cart`, `checkout`, `shipping`, `shipment`, `delivery`, `calculate`, `orderForm.js`, or `chunk-common.js`. This establishes an evidence gap, not proof that the supplier has no such endpoints.

The earlier `work/printograph-probe.json` records two successful public asset reads at 2026-09-25T18:53:01.284660+00:00:

| Asset | Recorded SHA-256 |
| --- | --- |
| `https://static.printograph.com/tl/cli-app/dist/production/js/orderForm.js?v=0109_20260902_202634-101` | `fd26a9c72ba1a162d5d12aa0f0f845dfed7d1a136f276440f1b5b06a465a1d8b` |
| `https://static.printograph.com/tl/cli-app/dist/production/js/chunk-common.js?v=0109_20260902_202634-101` | `577bddf60b70e3c2c8362de3f358cd9477dc21232aa1d3c41839d5e1db1ed88d` |

That probe retained a partial product/option mapping and four wrapper-presence checks: `getSpec`, `getProductId`, `getPrice`, and `getOptionPrice`. Its script did **not** save the JavaScript bodies. No raw copy was found in the inspected project/work scratch files or matching temporary-file paths. The hashes and wrapper names cannot establish an unrecorded cart endpoint or POST body. At that initial inspection stage, a new bounded public-asset capture was needed. The main task subsequently fetched exactly those two bundles and supplied their local bodies for the addendum below; both hashes matched the initial probe.

## Concrete observed contracts

All paths below use `https://www.printograph.com`. Evidence IDs identify persisted response records; they are not credentials. The response store retains URL/body/status but not a complete outbound method/body contract. GET use for specifications, lookup and price wrappers is supported by the earlier public probe and existing adapter implementation. The method and request payload for `/rules/turnarounds` are **not** established by these response rows alone; its POST wrapper and builder were subsequently located in the saved public code.

| Observed path/query | Body evidence | What it establishes |
| --- | --- | --- |
| `/service/rest/v1/settings/product/specifications?requestValues=true&productType=1` | `33900e18abc4436f8a2f77fb96420180` | Source dimension lists, including turnaround IDs `1` Regular and `3` Rush. |
| Same specifications route, `productType=27` | `66415518dfd64d75948ab9bd7bb98e08` | Club Flyers dimension lists and Regular/Rush identifiers. |
| Same specifications route, `productType=36` | `c94286e6ab0f4e93b419c9f86daf8212` | Roll Labels dimension lists; this saved response lists turnaround `1` Regular. It does not prove every reachable branch has identical availability. |
| `/service/rest/v1/products?productType=36&turnaround=1&shape=5&size=747&orientation=1&paper=79&finish=3&color=1` | `cf94e778bcdf4fe09b4554e7c7635782` | Exact lookup returns `items:[{id:100230600}]`. No quantity or service selection is present in this lookup query. |
| `/service/rest/v1/products/100230600/prices?itemId=null&cid=` | `4775274c69e64579ad40f53e9489120f` | Fifteen quantity rows with `quantity`, `markupPrice`, `listPrice`, and `packageASubItemPrice`; first rows are 100→133.61 and 250→139.73. `itemId`/`cid` appear empty in this observation; their nonempty semantics are unknown. This is a base-price response, not a cart subtotal. |
| `/service/rest/v1/products/100230600/options?requestValues=true&eddm=false` | `254cae1c188c4f02a41cede0854cd83a` | Eleven product-specific option records with internal option ID, type/group IDs, symbolic name, quantity/pricing flags, and IOP/OD support flags. |
| `/service/rest/v1/rules/turnarounds` | `e09b5a03fe9c43259933960bd1762896` | `items` contains `id:1, range:{min:3,max:5,label:null}` and `id:3, range:{min:3,max:null,label:null}`. Request inputs were not persisted; neither null nor the existence of an ID proves that the corresponding mode is selectable. |

The captured production DOM independently supplies availability and the chosen mode. Business Cards DOM evidence `e5572d4f4f404d1b8185f94529f7fbaa` shows selected `productionTime=1`, “Regular 3–5 Business Days”, and a disabled `productionTime=3`, “Rush Not Available”. A verifier must read the effective mode and availability back; production IDs are not durations, and production duration is not shipping transit time.

## Services and delivery are separate contracts

The browser's known service diagnostics come from `#collapseOne` radio groups named `proofType` and `fileDeliveryForIop` (`backend/crawler/adapters/browser.py:180`). Only checked values and their group state are currently captured; this is not a full traversal contract for all service combinations.

| Persisted observation | Exact selected values | Consequence |
| --- | --- | --- |
| Roll Labels DOM `87ddf302516a4a029e3b78e18331cfbb` | `proofType=31` “Instantly Processed”; `fileDeliveryForIop=5` “Upload Files Now” | File delivery here means artwork delivery, **not** carrier shipping. Both groups were present, with one checked value each, although their controls were collapsed. |
| Business Cards DOM `e5572d4f4f404d1b8185f94529f7fbaa` | `proofType=100` “Manually Processed”; no `fileDeliveryForIop` group in that snapshot | Do not inject the old instant/upload default when that group is absent. |
| Club Flyers DOM `d930edbaf4be4171af0299a5fbede212` | `proofType=100` “Manually Processed”; no file-delivery group | The same scope distinction occurs on another product. |
| Earlier Cards DOM `150cb618ab684b13a7e3c7aeb31c7fde` and Flyers DOM `fcd903b6981a46c0af72fe034d8e277a` | Instant `31` and upload `5` | Services can change with the source branch. Product defaults are not global constants. |

The option catalog for base product `100230600` contains these **different identifiers**:

| Internal option ID | Type / group | Symbolic name |
| --- | --- | --- |
| 103421345 | type `1`, group `1` Proof Type | `MANUAL_SOFT_PROOF` |
| 103421337 | type `31`, group `1` Proof Type | `INSTANT_PROOF` |
| 103421346 | type `36`, group `1` Proof Type | `MANUAL_NO_PROOF` |
| 103421332 | type `41`, group `13` Design Service | `DESIGN_SERVICE_1` |
| 103421333 | type `42`, group `13` Design Service | `DESIGN_SERVICE_2` |
| 103421334 | type `43`, group `13` Design Service | `DESIGN_SERVICE_3` |

Therefore **DOM `proofType=100` cannot be sent as an option ID or assumed to mean either API type 1 or 36**. The manual sub-selection and its serialization need a captured source contract. Similarly, the prior summary of design/file-upload choices does not prove the payload for adding artwork, requesting design work, or creating a cart item. No artwork upload or design-service request belongs in price verification unless the supplier exposes a verified calculation-only path for that context.

## Cart integration gate

Implement one of the following only after its endpoint, method, input schema, account context and lack of unintended side effects are proven by public source code and a bounded observed request/response:

1. **Read-only quote equivalent, preferred.** A calculation route returns a single item's effective configuration and separated charges without adding/updating cart contents. POST is not inherently a mutation-free quote; verify its semantics. A displayed calculator subtotal may be recorded as `calculator_quote`, but cannot be labeled independent cart verification merely because it matches a table.
2. **Supplier-supported isolated draft/cart.** The source supports an explicit separate cart/draft identifier and operations are scoped to it, with no account-global cart replacement or merge. A different browser tab, Playwright context, profile, or cookie jar does **not** establish this: an authenticated account may share its cart on the server. Anonymous isolation may also lose account pricing and so cannot verify an authenticated quote without an explicit context distinction.

For an isolated draft implementation, first capture a read-only canonical snapshot/fingerprint of existing cart contents. Create a verifier-owned draft only with a proven independent namespace. Capture the original-cart fingerprint again after creation and after each sample/cleanup. If it changes unexpectedly, stop; never clear or reconstruct the user's cart using guessed payloads. Cleanup may delete only IDs demonstrably created by this verifier, never all cart items or ambiguous same-SKU rows. Idempotency keys and durable owned-item IDs are required to avoid duplicates after crashes. Supplier-supported expiry or abandoned-draft cleanup is preferable where documented.

The current browser adapter blocks non-GET/HEAD cart/checkout/order mutations (`browser.py:678`). Keep that default during harvesting. A future verifier should have its own narrow allowlist and lifecycle, rather than relaxing the global harvester guard. Nothing may submit payment, place an order, upload artwork, or start design fulfillment as part of a quote check.

## Required verification observation

The future source adapter must produce this **internal observation contract**; it is not a proposed supplier request payload:

- Source product URL/type and resolved configured product ID; exact raw options and quantity actually accepted by the source after recalculation.
- Currency, account/pricing context, tax basis, discount context, quantity unit/pack size, selected production identity, and canonical bound service selections. Missing context is inconclusive; never substitute a default.
- Product subtotal, plus separately identified proof/design/rush charges, shipping, discounts and tax where provided. Cart grand total is not equivalent to product subtotal. A zero/missing amount is not an assumed free service.
- Verification channel (`calculator_quote`, proven `read_only_cart_quote`, or `isolated_cart_item`), source evidence IDs, timestamp, request/response correlation, and explicit original-cart preservation result where a cart was used.
- Effective selection readback and quote-settled evidence. An action acknowledgement or a stale visible total alone is insufficient.

Capture only allowlisted product/quote payload fields once their schema is known. Do not record authorization headers, session tokens, checkout secrets, or customer address bodies in portable fixtures. Keep private evidence references for any account-bound request.

## Isolated sample coverage plan

Use `select_verification_sample` (`backend/crawler/verification.py:57`) as the local starting point. It already samples reachable records, each option value within production/process scope, specified interactions, quantity extrema and explicitly marked risky/default/boundary cases. It does not establish coverage of undiscovered choices or obtain quotes.

For each product snapshot and bound account/service/production context:

1. Include observed minimum and maximum quantities, every observed option value, every selected production mode, and previously mismatched configurations. Explicitly add the quantities on both sides of each inferred price break; do not assume the default greedy sampler includes both.
2. Cover observed stock×coating×quantity, production×finish/coating×quantity, shape×size, material×finish and service×production interactions. Use valid observed combinations, not a Cartesian product containing impossible choices. Record budget-limited uncovered tokens.
3. Scope lookup/base/option table dependencies into the sample manifest. A failed shared component invalidates all dependent candidate rows, not just the product used for the check. A successful sample marks only its exact matched observation verified; all unsampled table candidates remain pending.
4. Run samples through one isolated verifier lane governed by the same origin pacing and backoff as harvesting. A request priority must not bypass the supplier ceiling. Bound one sample operation by a recoverable state machine: planned → effective selections established → quote observed → compared → cleanup confirmed/needs attention.
5. Compare through `compare_quote` (`verification.py:221`) and apply `verification_decision` (`:380`). Persist immutable reports linked to expected row identity and observed evidence. Configuration mismatches are not price mismatches. Unknown service/account/tax context stays inconclusive. A mismatch gets one bounded evidence refresh/retry; continued mismatch causes direct-quote fallback or a visible source/snapshot failure, preserving history.
6. Publish sample size, valid-domain scope, uncovered cases, exact matched count, mismatches, inconclusive checks, and cart-preservation status. Do not report “catalog verified” from a sample.

Before live cart use, local regression fixtures must cover account-shared-cart rejection, repeated/retried requests, a crash after item creation, scoped cleanup, unexpected original-cart changes, effective-option resets, changed service defaults, mixed tax/shipping totals, quantity mismatches, expired authentication, and source backoff.

## Next evidence needed

The later public JavaScript capture located candidate quote/cart wrappers without touching a cart. The next evidence is a single normal source quote request with redacted allowlisted inputs, including full response schema and effective selections. If the only available route mutates an account-global cart with no proven isolation, the independent cart verifier remains unavailable; current calculator checks must retain their accurate narrower label.


## Public-code addendum: exact wrappers, no live invocation

The main task fetched `work/planner-contracts/orderForm.js` and `chunk-common.js` in **two public GET requests** after the frozen response inventory. Their SHA-256 hashes match the earlier table. This subtask only parsed those files offline using a JavaScript AST; it did not execute supplier code. The earlier zero-cart-response finding still applies: source-code existence is distinct from an observed successful quote.

Locations below are **zero-based UTF-16 source offsets** in those exact bundles, not line numbers or byte offsets. Minification places the code on very long lines. Function names plus hashes provide stable review anchors. Local generated `*.function-index.json` and `*.method-index.json` files under `work/planner-contracts/` are inspection aids only.

| Operation | Exact method/path and serialization | Source anchor | Validation status |
| --- | --- | --- | --- |
| Generic calculation | `POST /service/rest/v1/product/prices`, JSON body; optional `designService=true` and other source-built query context | `chunk-common.js` `getPriceBySpec` @39233; `getGenericProductsPrice` @551235; `orderForm.js` `getGenericProductsPriceFromApi` @215647 | Candidate calculation-only path. No successful response or applicability to product types 1/27/36 is captured. |
| Turnaround calculation | `POST /service/rest/v1/rules/turnarounds`, application/json | `chunk-common.js` `getTurnaround` @556096 and `turnaroundParam` @388987 | Method/body builder proven by source; captured responses exist. Its returned ID/range still must be reconciled with displayed availability. |
| Shipping estimator | `POST /service/rest/v1/shipping/methods?requestValues=true&submit=false` by default, JSON body | `chunk-common.js` `getEstimatedShipping` @643910; calling builder `shipping` @172283 | Source wrapper supports optional address type and diagnostic fields. No saved successful shipping response; `submit=false` is not a substitute for observed server semantics. |
| Cart inventory | `GET /service/rest/v1/users/self/cart?includeTotal=true&requestValues=true&includeCount=false&type=1`; alternate branch uses `type=2` | `chunk-common.js` `getCartItems` @637313 | Read wrapper proven. The type semantics and response schema have not been observed in this evidence set. |
| One existing cart item | `GET /service/rest/v1/users/self/cart/items/{itemId}` | `chunk-common.js` `getCartItem` @247539 / @552070 | Read wrapper proven. Requires an existing item ID. |
| Checkout shipping/prices | **`POST`** `/service/rest/v1/users/self/cart/checkout/prices`, JSON body | `chunk-common.js` wrapper @605043; payload builder @517520 | Existing cart-item pricing, not arbitrary product specifications. The logger's older GET exception list is not the actual wrapper contract. |
| Checkout split lookup | `GET /service/rest/v1/users/self/cart/checkout/splits?...` | `chunk-common.js` `getCartItemsForCheckout` @497354 | Query includes selected existing `items`, grouping, delivery selection and address context. No new isolated cart is created by this flow. |
| Add item | `POST /service/rest/v1/users/self/cart?action=5`, `JSON.stringify({itemDetail:...})` | `chunk-common.js` `addToCart` @245195 / @554843 | Account-cart mutation; not enabled for verifier use. Source handles HTTP409 as a detected price difference, which does not prove a safe validation-only failure mode. |
| Update item | `PUT /service/rest/v1/users/self/cart/items/{itemId}`; another wrapper appends `?r=<random>`; JSON item detail | `chunk-common.js` `updateCartItem` @245443 / @554586; `orderForm.js` `sendAddToCart` @181870 | Mutates the specified existing item. |
| Duplicate item | `POST /service/rest/v1/users/self/cart?action=7`, body includes `cartItemId` | `chunk-common.js` `duplicateItem` @635550; `postDuplicateItem` @903451 | Adds a copy to the same account cart; not cart isolation. |
| Remove items | `DELETE /service/rest/v1/users/self/cart`, JSON body | `chunk-common.js` `removeCartItems` @635788 | Wrapper forwards arbitrary input; no cleanup payload is authorized by this inspection. Never use as a blanket reset. |
| Place order | `POST /service/rest/v1/users/self/orders`, checkout parameters | `chunk-common.js` `postOrderDetails` builder @538404 and transport @605672 | **Forbidden in quote verification.** |
| Request a custom quote | `POST/PUT/DELETE /service/rest/v1/users/self/quotes[/id]`; requesting price uses `PATCH .../{id}` with `{status:2}` | `chunk-common.js` `addQuote` / `quoteRequestPrice` near @47250 / @47961 | Mutates an RFQ workflow; not a demonstrated instantaneous calculation endpoint. Do not invoke it as a quote check or contact supplier staff automatically. |

### Calculation and turnaround payloads

`getGenericProductsPriceFromApi` builds this source-specific shape:

```json
{
  "properties": {
    "size": "<selected source value>",
    "printProductType": "<source product type>",
    "turnaround": "<selected production ID>",
    "quantity": "<source-selected quantity or 1-maximum range>",
    "other": {
      "paper": "<selected source value>",
      "color": "<selected source value>"
    }
  }
}
```

It derives additional `other` entries from `hid*` fields in the source's own specifications, handles package/sub-item products separately, and may add `priceGroup`, `designService`, `eddm`, or item/account query context. For non-sub-item products it requests `quantity: "1-" + lastDisplayedQuantity`. A body with just guessed paper/color fields is therefore not a generally proven payload.

The source enables this path only when the product type appears in `others.getGenericProductsPriceFromApiList` (`orderForm.js` @224771). That list starts empty (@47744) and can be filled by site settings. The frozen response set contains no store-settings body showing that list for types 1, 27, or 36. A successful generic-product calculation must not be assumed to support the three standard calculators.

`turnaroundParam` builds a separate payload from the source's own prepared item detail:

```json
{
  "quantity": "<selected quantity>",
  "specifications": {
    "printProduct": {
      "productType": "<source type>",
      "turnarounds": [1, 3]
    },
    "other": {}
  },
  "productOptions": ["<source-selected option IDs>"],
  "itemType": "<effective file-delivery/item type>"
}
```

The builder preserves applicable specification fields, removes singular `turnaround`, injects `[1,3]`, and keeps only its explicit product-field allowlist. It uses the source's resolved `productOptions`, not the raw collapsed process value. This is evidence for a pure future turnaround builder, but not proof that omitted services can safely be supplied as defaults.

### Checkout-price payload and response limits

The checkout builder creates `shippingStyle`, `commaSeparatedItems`, `includeShippingBreakdown:true`, `groupByShippingMethods`, `deliveryOpted`, `requestKeys:true`, and `requestValues:true`. It conditionally adds shipping/billing contact references or address objects, `couponCode`, `freightInfo`, `mailingGroupedItems`/`groupType`, and payment-method/store-credit context that may affect fees. The UI requires an acceptable shipping-address state before it calls the wrapper.

The code consumes response `items`, `itemIds`, `addressType` and shipping lookup information, then caches by cart item IDs and context. The exact nested product-subtotal/fee schema has not been observed. A request omitting required destination/account fields or inventing an item ID would not be a verified contract test.

For the separate shipping estimator, the source builds `itemType`, `zip`, `country`, `itemDetails.quantity`, `itemDetails.specification.productSpecification`, and selected `productOptions`. It may include `selectedCartItemIds` or `shoppingCartItemId`; those can mix an estimate with existing account-cart context. The wrapper can add `addressType` and diagnostic query flags; the response consumer reads `items[].method`, `items[].price.totalShipping`, and tax fields, including nonnumeric `N/A`, `TBD`, or `included`. These cannot be coerced into fabricated zero prices.

### Why the discovered identifiers do not isolate a cart

`orderForm.js` `getEditCartIdParamaeter` @192030 reads URL `cartId` and `duplicate`. With `duplicate=false`, that identifier selects an existing **cart item** for the PUT flow. With `duplicate=true`, editing that item is bypassed and the eventual POST adds a new item. `chunk-common.js` `editAndDuplicateCartItem` @901812 constructs those URLs from the item's `id` and can instead call `duplicateItem`.

`flowId` scopes client-side session-cache entries, not the account's server cart. `saveForLater` is an item flag, not a private cart namespace. `cid` appears with item/admin/account price context; this inspection does not establish it as a verifier-owned cart identifier. No isolated draft/cart creation namespace was found in these two bundles.

### Authentication transport and hard exclusions

Wrappers use the shared Axios module `7906`; its full implementation is not present in these two bundles. The common bundle's source-only CSRF interceptor (module `9814`, near @940742) reads the site's `csrfToken` cookie and adds `X-CSRF-Token` only on same-origin requests. It redirects on the source's identified CSRF failure. The authentication interceptor near @82700 uses the normal `window.gpAuth` flow, distinguishes HTTP-only authentication settings, and handles session expiry. This inspection read **source code**, not cookie or token values. A future authorized browser verifier should use the site's normal session transport and stop on expired access; it must not copy/export secrets or fabricate credentials.

Keep an explicit denylist, even if the source exposes a quote wrapper: order submission and order mutation; checkout payment actions; account/card/address mutations; artwork/file uploads; design fulfillment; RFQ/proposal submission; and any unscoped cart clear/remove/update. The only candidate exception to the current cart-path mutation block is the exact checkout-price POST after its semantics and schema are verified. Its existence does not justify allowing arbitrary POSTs under `/cart`.

**Integration decision:** retain the cart mutation block. First prove whether the supplier's configuration-only price endpoint applies to these products, or obtain a supplier-supported separate draft namespace. If neither is available, collect explicit calculator quote verification with accurate provenance and report independent cart verification unavailable. No speculative HTTP integration was added in this pass.
