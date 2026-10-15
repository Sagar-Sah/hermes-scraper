# Sparse option tables and the generic price endpoint

This review used only the two already saved, hash-pinned supplier bundles and
local saved response bodies. It made no requests and changed no runtime code.
Offsets are zero-based character offsets in the files identified in
`evidence-math-contract.md`.

## The four-row Club foil table covers its Regular domain

Saved response `e729a40b4c554f249c6e75fd88d6ca44`, for base `100125944`, option
record `103225753` (type 143, silver foil), contains exactly:

| Quantity | Markup subtotal |
| --- | --- |
| 100 | 0.00 |
| 250 | 0.00 |
| 500 | 0.00 |
| 1000 | 0.00 |

The actual `27_10` saved option-availability fixture gives foil types 141–143
quantities 100, 250, 500 and 1000 for Regular 1 and Rush 3; quantity 2500 is
**Rush 3 only**. Selected raised-foil type 138 itself permits only 100, 250, 500
and 1000, all **Regular 1 only**. Their joint Regular domain is therefore the
four returned price rows, not the 26 base-product quantities. This case requires
no interpolation or extrapolation. A foil color alone is not a complete
raised-foil option combination.

The corresponding Business Cards `1_10` availability permits raised types 135
and 138 at 50, 100, 250, 500 and 1000, Regular only. Its foil colors also list
2500, but only for Rush. The joint selected-option and turnaround intersection
must be applied before looking for missing price tiers.

The later one-row response `6388c26c926f4bd6bca661d74280096d` contains quantity
1000 only. The parent probe recorded that request as filtered with
`quantities=1000,2500`; the response table alone does not establish batching.
Its persisted response URL lacks those parameters, demonstrating why a URL
alone cannot establish that a one-row response was unfiltered. Retain exact
request metadata, not just response URL and body.

## What the browser actually does with option prices

`orderForm.js` `updateOptionPrice` at 234835 requests the selected quantity
first for ordinary products, then loads the unfiltered option table. The
specific request builder at 238209 appends:

```
url += "?quantities=" + [userSelectedOptions.quantity]
```

Here JavaScript concatenates a **one-element array** into a URL string. This
produces one scalar value; it does not demonstrate a JSON-array, repeated-query
parameter or multi-value batch contract. The wrapper in `chunk-common.js`
at 557350 simply performs GET on that already constructed URL. No saved caller
uses multiple values, repeated `quantities` parameters, or a JSON option-price
batch body.

`updateOptionPriceFromCache` at 239980 assigns a price only when:

1. The returned quantity equals the selected quantity; or
2. That cached option table has exactly one entry; or
3. A separate EDDM/accessory exception applies, irrelevant to these products.

It then removes grouping commas and applies `parseFloat(...).toFixed(2)`.
There is no nearest tier, interpolation, per-unit scaling or minimum/maximum
break calculation in this method. Neither saved bundle contains a reference
to the catalog field `quantityDependentPrice`, so a false flag cannot supply
one of those algorithms.

The single-entry cache behavior is a browser application rule, not proof that
any captured one-row response is globally constant. The specific-quantity
request also creates a one-row cache entry, and the subsequent unfiltered
request replaces its prices. If a multi-row table has no exact selected tier,
the updater does not assign that component; it can leave the prior applied
value in place. That is a source-code risk, not a claim of an observed live
misprice in this review.

A future singleton optimization would require immutable evidence of an
**unfiltered**, complete, same-scope response, exactly one entry, valid selected
option/turnaround availability, and independent observed checks. A filtered
one-row response is not eligible. For an actual missing tier inside the legal
domain, use the source's exact single-quantity request and retain its request
binding. The current pure generator conservatively keeps that tier unresolved.

## Successful generic POST is not yet a full configuration quote

Saved response `5ab43499d22b437eba4804245bd69e64` is from the **singular** route
`/service/rest/v1/product/prices`, HTTP 200. It reports quantity 1000,
`productPrice:14.0`, `subtotal:14.00`, `itemOptionsPrice:null`, `proofPrice:null`,
`available:false`, and `reasons:null`.

`chunk-common.js` exposes `getPriceBySpec` at 39218 and
`getGenericProductsPrice` at 551211. Both JSON-serialize the supplied body,
POST it to this pricing route, and return `response.data` unchanged. These
wrappers do not conceal a proof price or availability result. The two saved
bundles never read the response field `proofPrice` or `.available`. The meaning
of `available:false` therefore remains unestablished by these sources; it
cannot be silently treated as true or promoted to a verified valid quote.

The actual `getGenericProductsPriceFromApi` builder at `orderForm.js` 215616
constructs:

```json
{
  "properties": {
    "size": "explicit selected size",
    "printProductType": "explicit selected product type",
    "turnaround": "explicit selected production mode",
    "quantity": "explicit requested quantity",
    "other": {
      "paper": "selected print-product paper",
      "color": "selected print-product color"
    }
  }
}
```

It additionally copies only `specifications.other` keys prefixed `hid`, removing
that prefix and lowercasing the next character. The generic caller is reached
through the generic-product path, while `isGeneric` at `chunk-common.js` 733163
requires a product type of at least 10000 (excluding 100000). The three current
products are types 1, 27 and 36.

The builder does **not** forward `itemDetail.productOptions`, manual proof
selection, or item type/file delivery. `getStatelessParam` at 377393 explicitly
excludes orientation from ordinary print-product specifications. Thus the
observed rejection of an invented `other.orientation` is consistent with the
source-shaped builder, but does not document every accepted API property.

Ordinary selected corner/coating/foil/proof IDs are instead added to a separate
`itemDetail.productOptions` array by `updateOptions` at 399770. Manual mode 100
adds its secondary manual-proof ID; instant mode may add 31; selected ordinary
options add their type IDs, excluding paperFinish. That serialization belongs
to the item/cart/turnaround contract. The generic-price builder discards that
container. Copying it into `properties` or inventing `properties.proofType`
would be an unproven endpoint experiment, not a source-confirmed payload.

The generic caller can replace its quantity with the string `"1-<max>"` for
its supported non-subitem generic products, then caches returned rows by
quantity. This is source evidence for that generic caller's range shape only;
it proves neither option-price batching nor support for ranges on all three
ordinary products. It does not justify additional requests in this offline
review.

The returned $14 establishes a successful base-price computation for the
submitted body. Null option/proof fields are not proof of free selected
services. Until an exact supported configuration payload and its semantics
are independently demonstrated, use this as base-price corroboration only.
Keep the full configuration comparison tied to complete observed controller
state and explicit component evidence; do not upgrade it to cart verification.
