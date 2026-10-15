# Printograph bounded live probe

Observed: 2026-09-25T18:53:01.284660+00:00

Outcome: **access_required**. Four sequential public GET requests; no cart/order writes, credentials acquired, or load test.

The direct product-page request returned **HTTP 302**. The specifications endpoint returned **HTTP 401**.

The JSON response identifies `API-Vendor-Token` as invalid. API probing stopped; no bypass or token extraction was attempted.

Public browser DOM separately identified business cards as product type 1, club flyers as 27, and roll labels as 36.

The current public JavaScript confirms GET specifications, product lookup, quantity tables, base-price tables, option lists, and option-price tables. Price consumers read `items`, `quantity`, `markupPrice`, and `listPrice`.

**This direct HTTP probe did not verify live product/quantity/price JSON or additive composition.** Synthetic tests validate parser behavior, not supplier prices. The adapter rejects unfamiliar schemas, preserves evidence IDs, and marks calculated option combinations pending with partial coverage. Later browser observations are separate from this four-request probe.

| Request | HTTP |
|---|---|
| `https://www.printograph.com/products/business-cards/order` | 302 |
| `https://static.printograph.com/tl/cli-app/dist/production/js/orderForm.js?v=0109_20260902_202634-101` | 200 |
| `https://static.printograph.com/tl/cli-app/dist/production/js/chunk-common.js?v=0109_20260902_202634-101` | 200 |
| `https://www.printograph.com/service/rest/v1/settings/product/specifications?requestValues=true&productType=1` | 401 |

The user subsequently selected ordinary browser extraction instead of direct API access. No API authorization is required by that mode beyond the source's normal browser session.

## Browser engine validation

Chromium was installed and the browser/discovery tests passed, including five real local-browser scenarios. The fixtures exercised dependent Size/Paper dropdowns, quantity filters, hidden native production radios through their visible labels, a subtotal rendered outside the product form, ordinary navigation through an authentication shell, source session persistence across product pages, and the observed outside-form turnaround widget with product gallery images. A bounded synthetic configuration produced its expected $41.00 displayed subtotal; this is a test fixture, not a Printograph price.

Browser records retain the visible configuration, displayed subtotal, normal source JSON responses, and evidence IDs. They are labeled direct quotes without claiming independent cart verification. The engine refuses zero/ambiguous subtotals and source errors that could leave a stale price visible. Browser shipping remains explicitly unavailable.

The ordinary browser mode honors shared request limits and supports `max_configurations=1` for a bounded pilot. The running engine now keeps a dedicated native Chromium profile per source under the runtime data directory. Duplicate product tabs share its login, and Chromium retains that profile across service restarts. A real local Chromium fixture verified both duplicate-tab sharing and restart retention of a session-only HttpOnly cookie. Profile directories have owner-only permissions. This establishes local browser persistence, not a guarantee that a supplier will never expire or revoke a session. `ATLAS_BROWSER_PERSIST_SESSION=false` opts out; only this memory-only mode uses an optional `ATLAS_BROWSER_STORAGE_STATE` seed. Profiles and login data are excluded from source archives and exports.

`ATLAS_BROWSER_HEADLESS=false` enables ordinary user authentication in a headed browser. `ATLAS_BROWSER_AUTH_WAIT_SECONDS=0` (the default) waits for the user without a job timer; an operator may set a positive handoff limit. Pause and cancel remain available. The engine does not harvest tokens, replay browser authorization headers, or bypass challenges.

Live browser price coverage must be reported separately from these local fixture results.


## Live browser results

The bounded browser runs reached a real Business Cards calculator and captured the selected raw option IDs, quantity, displayed subtotal, source responses, and evidence references. The latest successful direct row in run `7ebd9bb39377469ea9c76671aff0d284` has quantity **50**, size **101**, paper **1**, color **2**, coating **5**, horizontal orientation **1**, no rounded corners, and subtotal **USD 4.69**. It records **Regular, 2–4 business days** from the displayed production widget. Seven product-gallery JPEGs were downloaded successfully and associated with the export manifest.

These are source calculator quotes, not independent cart checks. The pilot is intentionally bounded at one configuration per product attempt. Resumes retain earlier rows and may add another distinct selection if the source traversal changes. These samples do not establish full Business Cards coverage. Club Flyers also completed its one-configuration pilot: quantity **50**, size **272** (2 × 5.5 inches), 14 pt. Gloss, full-color front / grayscale back, High Gloss UV Front, horizontal, no corners, **USD 4.29**, and **Regular 2–4 business days**. Its five image files downloaded successfully. Roll Labels then completed its first direct quote after the custom radio-group fix: quantity **100**, **USD 66.23**, Rectangle – Rounded, 0.5 × 1 inch, Clear BOPP, Clear Gloss Laminate (Indoor), full-color front / no back, horizontal, **Off Bottom** wind position (raw value `32`), and **Regular 2–4 business days**. Six image files downloaded successfully. No shipping cost or rush fee was fabricated when the calculator did not provide it.

The opt-in Business Cards quantity-table path has five real local Chromium integration tests: successful two-sample publication, a price mismatch falling back to direct quotes, a one-row budget, an anchor already at the highest quantity, and changing availability of an unselected production mode. Its parser/binding tests use captured supplier table bodies and the observed lookup schema. The later live acceptance run below successfully expanded the 26 tiers through this integrated path.

The complete backend suite passed **183 tests** after the persistent-profile, dependent-control, and Roll Labels radio fixes; the production frontend build and Docker Compose syntax check also passed. Docker image execution was not tested because no Docker daemon was available.

The supplier session also survived an actual Hermes restart during validation: the headed crawler reopened the authenticated Club Flyers order calculator in the saved profile, with the source's My Account / Log Out controls visible and no new sign-in step. Source-controlled expiry and revocation remain possible.

The completed three-product pilot contains **five distinct saved configurations** after retries: two Business Cards rows ($4.69 grayscale back and $4.20 blank back), two Club Flyers rows ($4.29 grayscale back and $3.30 blank back), and the $66.23 Roll Labels row. Earlier rows remained intact. All three products reached their bounded pilot limit; coverage is **partial** and the run is **completed_with_gaps**. The export panel contains 18 associated image files. The small real-source datasets are included under [`examples/`](../examples/README.md).


## Successful live quantity-table acceptance

Run `f1e1ebc57ed047399d03a0fecc8dd1ed` completed on 2026-09-25 at 20:31:23 UTC. It generated and exported **26 distinct quantity tiers in 34.03 seconds**, with **39 counted browser/source requests** and zero application response-cache hits. The normal saved login profile was reused; this was not a clean-browser or sustained-throughput benchmark.

The branch was size `101`, paper `1`, color `2`, coating `5`, horizontal `1`, no corners, Regular production `1`, configured base product `100000021`. The initial quantity selection revealed the page's production controls. The adapter then matched the source table against displayed quotes at **50 ($4.69)** and **100,000 ($685.93)** before publishing the remaining **24 calculated rows**.

The two samples retain `direct_quote` provenance; the other 24 retain `source_table` and `verification_status: pending`. Production days for unobserved tiers remain unresolved. CSV and Parquet each independently contain 26 records, 26 distinct quantities, and 26 distinct configuration IDs. The exports are included as [`examples/business-cards-tiers.csv`](../examples/business-cards-tiers.csv) and the matching Parquet/JSON files.

The run is **completed_with_gaps / partial**: it covers one supported configuration's quantity domain, not every Business Cards option combination. It does not establish cart verification, general additive pricing, exact shipping, rush fees, whole-catalog timing, or a 100-requests-per-second supplier rate.

After the 183-test full-suite pass, the final bulk changes passed all **50 focused tests** (42 schema/binding tests and 8 real Chromium integration tests), covering lazy controls, prefetch context, requested quantities, price conflicts, and fallback behavior.

## Unlimited three-product run: checkpoint, not completion

Run `36c1c4b084664d36b7b32dbca7cf8f08` uses the three supplied URLs, browser extraction, captured quantity tables, image mode `both`, and no request or configuration ceiling. Three duplicate product tabs share the saved authenticated browser context and the source pacing limit. At the 2026-09-25 22:01:41 UTC export checkpoint it held **1,652 distinct rows**: 448 Business Cards, 604 Club Flyers, and 600 Roll Labels. No product had exhausted its preset domain. Of these rows, 635 were displayed quotes and 1,017 were table-derived rows still pending verification.

The run exposed two traversal failures: changing stock while a prior Rush selection remained active caused a structured product-lookup 404; a Roll wind-position action took effect but its browser acknowledgment timed out. The adapter now returns to an observed, enabled Regular choice before changing a supported base dimension from Rush, then resolves the new control again. Roll acknowledgment recovery is limited to the known wind-position radio, unchanged navigation, an enabled requested value still selected, and a fresh normal settle; extraction must still perform its normal quote checks. Genuine source errors continue to stop the product. Thirteen local Chromium regression tests passed for these transitions and traversal behavior.

The source files also correct quantity-dependent rounded-corner labels copied into derived rows. API/export projections use the neutral option name and retain the copied annotation as `source_label`; immutable checkpoints, subtotals, components and evidence are preserved. The focused projection, API, exporter, catalog, parser and identity checks passed 144 tests.

This checkpoint is not a whole-product benchmark or an exact completion estimate. The saved rows still represented only the first size of each product. Restarting currently replays browser traversal and deduplicates committed rows; it does not restore a durable traversal cursor. Collapsed proof/design services, custom dimensions, live shipping and independent cart verification remain incomplete.
