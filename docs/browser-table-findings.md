# Browser table findings — Printograph

## Expanded preset bindings (2026-09-25)

The original Business Cards acceptance below remains a historical measurement for one branch. The current guarded binder also supports dynamically resolved Business Cards and Club Flyers base selections in the broker/USD context, High Gloss UV Front coating type5 or Both Sides type40 when present, and no rounded corners or catalog-mapped rounded corners type2 when the selector is present, and Regular1/Rush3 only when the exact selected-turnaround lookup and support tables agree. Absent coating/corner selectors remain absent in the configuration; no defaults are inserted. Additional unselected option-support IDs must belong to the exact bound product catalog and do not add charges or narrow the selected-option domain. Unknown finishing controls use direct quotes.

Roll Labels uses the exact captured shape, size, orientation, paper, finish and color selections, Regular1, and wind-position types32/33/34/35/54 resolved in catalog group12. Standard-size presets can use the table path even when an unselected custom-size option exists; custom dimensions remain a coverage gap. Candidate prices for this supported Roll path use the captured base table and still require two matching displayed subtotals before publication. No fee is inferred from catalog flags. A mismatching displayed price forces fallback.

Captured Club prices include US thousands grouping such as `1,062.49`. Parsing accepts strict grouped decimal strings and rejects malformed or ambiguous grouping. New offline fixtures retain only public product responses and their evidence identifiers; request headers and browser profiles are excluded.

Browser traversal now selects shape/orientation before dependent sizes and restores a low quantity before discovering Regular/Rush branches. Unavailable Rush choices are excluded. Collapsed proofing/design controls are reported as coverage gaps, separately from table eligibility. Calculated tiers remain pending; neither these bindings nor two sample matches constitute independent cart verification or exhaustive supplier coverage.

The captured base table reproduces the observed **$4.69** subtotal for quantity **50**, size **101**, paper **1**, color **2**, coating **5**, horizontal orientation **1**, and rounded corners **empty / None**. Two subsequent visible-browser checks also support `base + rounded-corner surcharge`: **$4.69 + $3.00 = $7.69** at 50 and **$9.45 + $5.00 = $14.45** at 250. These checks apply to this product configuration and these quantities. They are **DOM quote comparisons, not independent cart verification**.

This report and its offline fixture were compiled from existing response objects; the review itself made no new network requests. A later live default-configuration pilot also confirmed the product lookup response described below. The original public browser run is `ce354c188f4148f9929522cd54808a9f`; its single persisted row is `834479be3c0a8fb6b8565395db7307727663edd0e64284336ad9841d2ca7e3e6`.

## Captured source and identifiers

Public product page: [Printograph Business Cards](https://www.printograph.com/products/business-cards/order).

| Meaning | Observed value | Important distinction |
|---|---|---|
| Product type | `1` | Business Cards category/type in configuration requests and DOM |
| Base product used for price tables | `100000021` | Internal configured product ID; differs from product type |
| Store context | `broker` | Taken from DOM evidence; preserve with the price snapshot |
| Quantity | `50`, `100`, `250`, … `100000` | Actual count values, not an ordinal or an opaque variation ID |
| Rounded-corner DOM/type ID | `2` | Option type in group `2`; unrelated to color value `2` |
| Rounded-corner price-endpoint ID | `100000104` | Resolve from the option catalog for base product `100000021` |
| UV coating DOM/type ID | `5` | Maps to catalog option `103132720`, group `4` |
| Turnaround IDs | `1` Regular; `3` Rush | IDs, not a number of production days |

An earlier response for base product `100000001` maps the same rounded-corner type `2` to endpoint option `100000004`. Therefore, cache and join option mappings by **base product + group + type**, not label or raw type ID alone. The DOM size label also differs slightly from the specification label; retain the raw ID and each source label.

## Exact lookup, price schema and quantity domains

A later normal-browser capture confirmed the exact product lookup schema. Evidence `c1ad2c5825774e31b3d8d9343c3b5b22` returned:

```json
{"items":[{"id":100000021}]}
```

Its URL was `/service/rest/v1/products?productType=1&turnaround=1&size=101&orientation=1&paper=1&color=2`. The raw response bytes have SHA-256 `9e0fe66dde4add2abb75b7f0b50319c38bad250ed579ccd8a589deafe11c8700`. This establishes the lookup schema and parameter-to-product mapping for this observed configuration. It does not, by itself, prove a full live bulk expansion or the temporal association of every table with an interaction epoch.

The base response is an object with one `items` array. It has **26** rows. Each row is:

```json
{"quantity":50,"markupPrice":"4.69","listPrice":null,"packageASubItemPrice":null}
```

The selected display subtotal matches `markupPrice`. `listPrice` and `packageASubItemPrice` are unknown/null in this capture; neither is zero nor a usable alternate price. Option-price responses also use `items`, `quantity`, decimal-string `markupPrice`, and nullable `listPrice`.

Base product quantities and DOM choices begin at **50**. The rounded-corner and manual-proof price tables contain an extra **25** tier. It is not a valid product combination merely because an option price exists. Valid quantities must intersect the base prices, product support, selected option support, and the effective DOM domain.

The product quantity-support response uses:

```json
{"items":[{"quantity":50,"turnaroundList":[1,3]},{"quantity":5000,"turnaroundList":[1]}]}
```

Regular supports all 26 observed quantities. Rush is supported only at **50, 100, 250, 500, 1000, 2500** in this configuration. The option-support response is keyed by **option type ID**, e.g. `"2"` and `"5"`, and each value is an array of the same quantity/turnaround records. An option can support Rush at a quantity that the base product does not; use the intersection.

## Price arithmetic and its limits

| Quantity | Base | Rounded corners | Calculated subtotal | Visible-browser subtotal | Scope |
|---:|---:|---:|---:|---:|---|
| 50 | $4.69 | None | $4.69 | $4.69 | Persisted DOM evidence |
| 50 | $4.69 | $3.00 | $7.69 | $7.69 | Subsequent manual DOM check |
| 250 | $9.45 | $5.00 | $14.45 | $14.45 | Subsequent manual DOM check |

The original option label is `Round Corners (+$3.00 )`, consistent with the 50-tier surcharge. The source catalog declares `quantityDependentPrice: false` for this option, yet its table changes: $3 at 50/100, $5 at 250, $7 at 500, $11 at 1000. **That flag is not evidence of a flat charge.** Always use the exact quantity entry.

Other captured option tables contain manual PDF proof ($4), manual processing without proof ($3), and design packages ($49/$59/$79). They were loaded by the page but were not selected in the persisted DOM quote. Loading a table does not mean the charge is active. The UV coating and instant-proof catalog entries have no separate price response in this captured set; do not invent a zero fee or add an unrelated table.

All observed prices already have two decimal places. Exact `Decimal` addition reproduces the three observed totals without rounding. **This does not prove the site's tie-breaking, intermediate rounding, tax rounding, per-unit rounding, or pricing for fractional-cent inputs.** The helper performs no subtotal rounding and retains exact source decimals. Unit prices are derived separately at eight decimal places and must not be multiplied back to derive authoritative subtotals.

No broader additive rule has been established for combinations of options, other papers/colors/sizes, Rush, discounts, taxes, shipping, design services, manual processing, customer-specific terms, or a different snapshot. A matching DOM subtotal does not prove every unseen quantity or option combination.

## Production changes with finishing

The follow-up browser checks observed **Regular 2–4 business days** with no rounded corners and **Regular 3–5 business days** after selecting rounded corners, at both checked quantities. Reusing production from the baseline would therefore be wrong even when price addition is correct. Shipping was not quoted, and these are production ranges rather than delivery dates.

The following is a manual audit record reported by the parent agent during this implementation. Only the baseline has the original persisted DOM evidence ID below; the follow-up checks are not presented as automatically captured evidence or cart validation.

```json
{
  "method": "visible browser DOM, ordinary select changes, no API replay",
  "product_url": "https://www.printograph.com/products/business-cards/order",
  "fixed": {"size":"101","paper":"1","color":"2","coating":"5","orientation":"1"},
  "checks": [
    {"quantity":50,"roundedCorners":"","subtotal":"4.69","production":"Regular 2–4 business days","persisted_baseline_evidence_id":"00e99185f7a849b8b8d2df752211f31a"},
    {"quantity":50,"roundedCorners":"2","subtotal":"7.69","production":"Regular 3–5 business days"},
    {"quantity":250,"roundedCorners":"2","subtotal":"14.45","production":"Regular 3–5 business days"}
  ],
  "independent_cart_verification": false,
  "general_additive_formula_verified": false
}
```

## Evidence inventory

The test fixture contains public response bodies, route URLs, evidence IDs, observation timestamps, and actual SHA-256 hashes of the stored raw bytes. `recorded_response_hash` separately preserves the database response hash, which currently hashes a serialized hex representation and is not the raw-byte hash. No request headers or credentials are included. The lookup was captured in the later pilot, so the fixture records the first run as `original_run_id` rather than claiming all captures came from one run.

| Evidence ID | Body |
|---|---|
| `c1ad2c5825774e31b3d8d9343c3b5b22` | Later live exact product lookup: one item, base ID `100000021` |
| `00e99185f7a849b8b8d2df752211f31a` | Effective DOM selections, 26 visible quantities, $4.69 subtotal |
| `b7014f7f63ff42b5a6c12a213dec302a` | Base product `100000021`, 26 price tiers |
| `0c2242d2b23d4209a9030856eeb2eb2e` | Product quantity and turnaround support |
| `3d7e5568de904775b9a83638e5a6b3d8` | Option type `2` and `5` quantity/turnaround support |
| `19c3beb6ca904e45bce438c69c7e5c39` | Eight option definitions for base product `100000021` |
| `3c4ebbda64e741e8b4d4654e48ea4853` | Earlier option definitions for base product `100000001` |
| `26adc90e73144057aec6f695621a9065` | Rounded-corner endpoint option `100000104`, 27 tiers |
| `e03ec52c677a4fd9b1c0129c301bf8c6` | Manual PDF proof endpoint option `101857042`, 27 tiers |
| `2dd386809bce4a28ae028ecd3e10d66a` | Manual processing without proof option `101925130`, 27 tiers |
| `c9e6f53ff3914f718cda8428b112d8c0` | Silver design option `100581420`, 26 tiers |
| `69729aaa191b41ecbec2180269322153` | Gold design option `100581421`, 26 tiers |
| `cd5604b176604880a0de99d7c05aa352` | Platinum design option `100581422`, 26 tiers |

The browser loaded these ordinary page routes. This inventory documents observed traffic and does not require or assume a separate API subscription:

- `/service/rest/v1/products?productType=1&turnaround=1&size=101&orientation=1&paper=1&color=2`
- `/service/rest/v1/settings/product/specifications?requestValues=true&productType=1`
- `/service/rest/v1/products/quantities?productType=1&includeTurnaround=true&size=101&orientation=1&paper=1&color=2`
- `/service/rest/v1/products/{baseProductId}/options?requestValues=true&eddm=false`
- `/service/rest/v1/products/options/quantities?optionIds=2,5&includeTurnaround=true&productType=1&size=101&orientation=1&paper=1&color=2`
- `/service/rest/v1/products/{baseProductId}/prices?itemId=null&cid=`
- `/service/rest/v1/products/{baseProductId}/options/{endpointOptionId}/prices`, sometimes restricted by `?quantities=50`

## Implemented browser integration

`backend/crawler/adapters/browser_tables.py` supplies the pure parsers, capture binding, domain intersection, and candidate arithmetic. `BrowserAdapter` now calls these helpers when the explicit run option `browser_bulk_quantities: true` is enabled. The helpers perform no network I/O; the browser observes the responses generated by ordinary page operation.

The original acceptance fast path was limited to product type `1`, broker store, USD, size `101`, paper `1`, color `2`, orientation `1`, coating `5`, Regular mode `1`, and rounded corners either empty or type `2`. Unsupported or insufficiently bound branches continue through ordinary DOM traversal.

If quantity is initially unselected or the subtotal is zero, the adapter first selects the first quantity permitted by the user filters without emitting a row. It then revisits the same non-quantity branch so a newly revealed production control is processed before bulk expansion. This warmup is bounded to one attempt at that prefix; a one-row limit retains the ordinary direct path.

For an eligible branch, the adapter:

1. Binds the lookup, base table, option catalog, and support tables to the settled effective selection, and reconstructs its currently displayed subtotal exactly.
2. Selects the requested quantity intersection, respecting the configuration limit.
3. Records the current DOM anchor and a distinct boundary quantity. If the anchor is the maximum requested quantity, the opposite minimum is sampled. A limit of one bypasses bulk sampling and records one direct quote.
4. Rechecks the effective non-quantity selections and their domains, selected production mode, base product ID, quantity domain, and table bodies. The disappearance of an unselected Rush choice does not invalidate a still-selected Regular mode.
5. Compares the sampled DOM subtotals and constructs the remaining candidate rows in memory before publishing any calculated row. A binding, reset, missing-price, table-change, or subtotal mismatch falls back to ordinary DOM quotes for that branch.

The samples are emitted as `direct_quote` rows, and those quantities are excluded from calculated output. Remaining rows use `provenance: source_table`, `verification_status: pending`, exact component/sample evidence IDs, and an explicit candidate formula. Their production retains selected mode `1` with `status: requires_configuration_quote`; no production-day range is copied from a sample. The browser output keeps product type as `source_product_id` and preserves the configured internal ID in `pricing_context.source_base_product_id`.

This verifies two DOM observations before provisional expansion. It does not mark every quantity verified, establish a general additive rule, or perform independent cart checks. Row completeness, source coverage, and verification remain separate.

## Capture binding gate

`bind_business_card_tables(snapshot, captures, navigation_generation=..., selection_epoch=..., filters=None)` requires immutable request-start control values, product/store context, navigation generation, selection epoch, response status, route URL, body, and evidence ID. It rejects another navigation, a future epoch, an intermediate base selection, missing context, ambiguous lookup results, conflicting matching captures in one epoch, and an unsuccessful latest matching capture. Earlier epochs are reusable only within the supported dependency projection; option tables may have been prefetched while rounded corners were unselected. For these routes, request-start size `101`, paper `1`, color `2`, and orientation `1`, plus product/store context, must be present and match. The quantity, finishing, or production widgets may still be missing or unselected when tables are prefetched; they are not assigned defaults. The exact lookup query supplies `turnaround=1`, its configured product ID binds ID-based table routes, and the final settled DOM must independently supply the complete supported selection. Any explicit conflicting production/coating/corner value is rejected.

The gate checks the exact lookup parameter set `productType=1`, `size=101`, `orientation=1`, `paper=1`, `color=2`, `turnaround=1` independent of query ordering. Its single returned ID must match the base-price and catalog routes. Product and selected-option support queries must match the effective selection. Selected corner type `2` resolves within group `2` of that product's catalog. Available quantities intersect DOM choices, product support, option support, production support, base prices, and selected-option price availability. Quantity `25` cannot enter output merely because an option table contains it. A later same-route response with unknown or ambiguous pricing/account query context prevents reuse of an older matching response. A normal narrower `quantities=50` request may coexist with a full table only when its exact requested quantities and prices agree; a changed, failed, or empty scoped response fails closed.

The original capture regex missed the bare `/products?…` route; this was corrected, and the later live lookup response is now part of the saved fixture. **The lookup schema and the narrow live bulk path are now confirmed.** Offline and local Chromium fixtures cover rejection, fallback, and edge cases; their composed page state and request provenance remain test scaffolding. Separate live run `f1e1ebc57ed047399d03a0fecc8dd1ed` exercised the ordinary browser capture, warmup, binding, two DOM samples, and 26-tier publication successfully, as recorded below.

## Current immutable-row and identity behavior

Resume price comparisons use exact numeric subtotals and a shared half-up unit-price calculation. The first unrestricted run exposed two legacy direct-quote half-even ties at quantity80,000 ($510.65 and $1,030.65). Their source subtotals were unchanged. Historical records retain their original strings; new direct quotes and calculated tiers now use the same rounding. This prevents a derived-display rounding difference from being reported as a supplier price change. Genuine subtotal differences still stop the run.


The engine stores immutable row batches, using `row_identity` to identify the commercial selection. Configuration labels are removed: raw option values determine identity. When production provides `value`, `mode`, or `id`, only that selected identifier is used, so display labels, observed day ranges, and verification/production statuses do not split the selection. Provenance and verification status are excluded. Product URL, quantity, currency, and material pricing context remain part of identity; the descriptive `source_base_product_id` field is excluded from that context.

Consequently, a pending candidate with production mode `1` and a later displayed quote for that same mode do not become duplicate combinations merely because production status, range, label, or table provenance differs. On resume, older immutable checkpoints are reindexed through the current semantic identity function without rewriting their original records or evidence IDs. Conflicting stored prices for one resulting identity fail visibly.

Repeating an identity with the same subtotal/unit price is deduplicated. Repeating it with a different price raises `snapshot_changed`, including within a single active pass and before a batch commits. The earlier snapshot remains intact and the product requires attention.

A repeat is still **not a verification update operation**: a later equal-price row does not upgrade a stored pending row to verified. The bulk path avoids duplicate sample/candidate publication and performs its sample comparisons before emitting candidates. Broader automatic verification upgrades or retroactive row-level invalidation would require explicit storage support; they are not claimed by this implementation.

## Validation and limited live acceptance

From `backend/`, `python -m pytest tests/test_browser_tables.py tests/test_browser_bulk.py -q` exercises the captured source schemas and the actual Chromium integration against a local fixture server. The local-browser tests cover two direct samples plus a pending interior tier, boundary mismatch fallback to all direct quotes, configuration limit one, a maximum initial anchor sampling the minimum, disappearing unselected Rush availability, and initially unselected quantity with a production control revealed only after selection. The lazy-control fixture prefetches the real saved table bodies before those controls become ready, and checks requested-quantity filtering and a one-row cap. They make no requests to live suppliers.

Live run `f1e1ebc57ed047399d03a0fecc8dd1ed` completed on **2026-09-25** from **20:30:49.382307Z to 20:31:23.416666Z**: **34.034 seconds**, **39 recorded browser requests**, and **0 cache hits**. Its warmup selected quantity 50 without publishing a row; the expansion event resolved base product `100000021` and published:

- **26 distinct quantity rows**, with no bulk fallback.
- **2 direct DOM samples:** quantities **50** and **100000**, stored with verification status `not_selected`.
- **24 table-derived rows:** verification `pending`, with production-day ranges still requiring configuration quotes.

The run correctly ended as **`completed_with_gaps`**, with **partial** product coverage and `verification_complete: false`. This accepts the narrow configured Business Cards quantity-expansion path. It does not establish complete product/catalog coverage, independently verified cart subtotals, live shipping, broader additive pricing rules, or inferred production ranges. The separately reported rounded-corner DOM comparisons remain limited to their observed configurations and quantities.
