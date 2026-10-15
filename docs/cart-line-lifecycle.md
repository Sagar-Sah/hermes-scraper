# Temporary cart-line verification: source contract and remaining gates

The user has authorized cart verification. This document identifies technical
contracts still needed before enabling a temporary cart-line lifecycle; it is
not a request to repeat that authorization. The review made no supplier
requests or mutations and changed no cart/runtime code.

Sources are the two saved bundles pinned in `evidence-math-contract.md`.
Offsets below are zero-based character offsets. There were no saved cart
response URLs in the runtime response table at this inspection, so response
body claims below are limited to what the source consumer establishes.

## Source-owned preparation

The source's wrapper `parametersOnAddToCart` (`chunk-common.js`, 415279)
delegates to `parametersOnAddToCartStateless` (409644). That builder calls
`getParameters`/`getStatelessParam` (377324/377393), then `updateOptions`
(399770) and, where required, image/design preparation. Its return value is a
JSON string. The action parses that string and the HTTP wrapper JSON-serializes
the object once; a verifier must not double-encode it.

For ordinary, non-generic, non-dynamic product types 1, 27 and 36, the mapped
item-detail fields are:

| Field | Source rule |
| --- | --- |
| `type` | Actual `userSelectedOptions.fileDelivery`; not collapsed proof mode |
| `subTotal` | Client `getTotal`: base + selected option totals + active manual/design service, `toFixed(2)`; before the separate displayed coupon transformation |
| `specifications.printProduct.productType` | Actual product type |
| Remaining print-product specs | Source `fields[type].specs`, excluding orientation and empty selections |
| Business Cards/Club specs | size, paper, color, plus turnaround from productionTime |
| Roll Labels specs | shape, size, paper, finish, color, plus turnaround |
| `specifications.other` | Empty for these ordinary branches; do not invent orientation or generic `hid*` fields |
| `quantity` | Actual selected quantity |
| `initials` | Value from `#initials`, trimmed; builder uses `OK` only if that element is absent |
| `productOptions` | Source-selected option **type IDs**, not catalog price-record IDs |

`updateOptions` adds manual secondary proof ID 1/36/37/38 when process mode is
100. It does not send 100 as the proof option. It adds instant 31 except for
the source's design-online/inline/basic-creator delivery exceptions. It adds
selected ordinary fields from `fields[type].options`, skipping empty values
and paperFinish; this covers corner, coating, spot, raised print, foil color
and wind position as applicable. Design mode 101 adds its package ID later,
together with design-service details, and is outside a no-design verifier.

`sendAddToCart` (`orderForm.js`, 181870) rejects instant processing combined
with nonempty spotCoating or raisedPrint. It removes `mailingOpted`, sets
`isAdaToolEnabled` from the actual optional source function, and can set
`saveForLater` from actual source state. A nonempty shoppingCartItemId selects
the **PUT existing-item** path; the verifier must reject that state, not clear
or reuse it silently. The new-line path calls the exact POST wrapper for
`/service/rest/v1/users/self/cart?action=5`.

The observed source initials validation requires letters only and a trimmed
length between 2 and 10. A verifier must not fabricate an artwork approval or
pretend missing source state was observed merely because a fallback exists.
Complete pricing captures currently omit some cart-specific state, including
generateThumbnail, source initials and ADA/save-for-later settings. Those must
be captured or explicitly resolved for a preparation-only helper. No generic
ready-to-send helper was added from incomplete inputs.

## Artwork-free cases are conditional

`orderForm.js` `addToCart` (178251) has these distinct branches:

- File delivery 5 + proof 31 + **actually observed** generateThumbnail=false
  can call sendAddToCart without an image-count requirement. The flag starts
  false (50056), but editing/design paths can set it true. Its default is not
  evidence of its current value.
- File delivery 5 + manual mode 100 requires a nonempty prepared image array.
  This source path does not establish upload-later support for those rows.
- Media delivery 6/7 sends without images; the serializer removes image/design
  details. This is a different delivery context, not a substitute for value 5.
- Design-service delivery 15 permits no artwork but enters a design-service
  flow. It is outside this verification task, as are upload/design actions.

Neither saved bundle supplies an “upload later” option. There is no basis to
invent one or reuse a user's existing artwork. No-image permission in one
branch is not proof that every proof/delivery combination can be added without
artwork. A successful sample in delivery 6 cannot be labeled an exact cart
verification of delivery 5.

## Receipt and independent price evidence

The add wrapper returns response.data unchanged. The success consumer
`processAddToCart` (`chunk-common.js`, 753154) reads a nonempty `response.ids`
array and saves recent-added IDs, then navigates. It does not read a corrected
price from that response. A returned ID can support ownership checks; it does
not establish that an echoed subtotal was independently recalculated.

The shared HTTP interceptor (89829) recognizes HTTP 409 for action 5 and shows
a generic detected-price-difference message. It rejects the request promise.
It does not extract a replacement numeric price, and it does not establish
that a 409 always leaves the cart unchanged. The action also recognizes errors
whose resource mentions `CartItemResource.subTotal` or `productPrice`, but only
shows a refresh message. No corrected-amount schema is established.

Read-back wrappers exist for a cart item and cart inventory. The separate
checkout-price POST also exists, but its source builder includes shipping
style, selected item IDs, destination/context fields and possibly coupons or
payment-related fee context. No saved response proves its nested independent
line-subtotal schema. Never invent destinations, payment context or an item
ID, and never interpret shipping/tax-inclusive totals as product subtotals.

## Targeted removal remains unproven

`removeCartItems` (`chunk-common.js`, 635788) performs
`DELETE /service/rest/v1/users/self/cart` with JSON.stringify of arbitrary
input. Its name occurs **only at that definition** in the two saved bundles;
no caller establishes whether the target body is an ID array, an object, or
another structure. None of those shapes should be guessed. The actual cart
entry bundle/caller or a captured targeted-removal request is needed before
the first verifier-owned line is created.

There is no established isolated-cart namespace in these sources. An owned
line in the account cart is not a separate cart. A POST could also conceivably
merge with an existing line; no source here proves otherwise. If an add receipt
names any preexisting ID, that ID must never be deleted by the verifier.

## Intended lifecycle after the missing contracts are captured

1. Read all cart pages/types required by the real inventory contract and record
   preexisting IDs plus immutable fingerprints of their configuration, quantity
   and prices. Incomplete inventory cannot protect the cart. Keep account and
   artwork details out of routine logs.
2. Prepare one source-shaped, resolved, non-edit, non-design, no-upload sample.
   Record its full pricing/service scope and exact payload fingerprint. Avoid
   a duplicate configuration until non-merging semantics are demonstrated.
3. Under a single verifier mutation lease, recheck the baseline immediately
   before the one exact POST. No blind POST retries after an uncertain result.
4. Require authoritative created IDs from the receipt, all absent from the
   baseline. Read back those exact lines; validate full configuration, quantity
   and service selections. Do not assign ownership from a cart-wide set
   difference alone when concurrent activity or a missing receipt is possible.
5. Obtain an independently established server line-price result for only those
   IDs and compare the same product-subtotal basis. Keep “accepted client
   subtotal” separate from “independently recomputed subtotal.”
6. In a finally/recovery path, invoke the **proven targeted** removal contract
   for only the authoritative owned IDs. Never clear the cart, update a
   preexisting line or remove all newly observed IDs indiscriminately.
7. Read inventory again. Verify owned IDs are gone and all preexisting line
   fingerprints are unchanged. Persist unresolved creation/cleanup states so
   a restart never retries an add or broadens a deletion target.

Failure/409 still requires inventory reconciliation; do not assume either
successful creation or no side effect. If ownership, removal schema, price
independence or unchanged preexisting state cannot be established, record the
specific unresolved verification result. No order, payment, upload, design or
RFQ action belongs in this lifecycle.

## Cached entry-bundle search and exact next read

A second, zero-request search found no saved supplier HTML or cart entry
JavaScript in project/work scratch files. The runtime `responses` table also
contained no HTML/JavaScript/cart-response candidates by content type or URL.
The only saved supplier bundles are `orderForm.js` and `chunk-common.js`;
`removeCartItems` still occurs only at its wrapper definition.

The existing source does establish **page destinations**, not their script
URLs: `navigateToOldOrNewCart` (`chunk-common.js`, around 753600) selects
`/shop/cart.html` when the actual `window.vueCartAndCheckoutEnabled` flag is
true, otherwise `/store/shopping-cart.html`. The order action uses the same
choice (184457). No cart bundle filename should be guessed from these names.

The exact next read-only artifact is the HTML and declared script list from
that source-selected cart page, followed by the actual referenced cart entry
bundle. First inspect its targeted `removeCartItems` caller and line-price
read/recalculation consumer offline. Reading a cart page can reveal private
line/artwork details; retain only the necessary schema and IDs privately and
omit such contents from portable reports. No such GET was made by this review,
and none is assumed to be inside an existing probe budget.
