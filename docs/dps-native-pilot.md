# Digital Print Solutions native calculator pilot

The pilot opens an owned duplicate page in Hermes's existing DPS browser profile.
It observes only native calculator GETs, with no session-header copying, API replay,
cart, upload, checkout, shipping-address or order actions. The original supplier
login window and its selections remain outside the pilot's ownership.

`DPSPilotManager(store, browser_pool)` exposes `await start(url, quantities=None)`,
`get(id)`, `list()` and `await close()`. `quantities=None` captures the current
native configuration only. Up to four explicit quantities may be requested;
each must be supported by the observed quantity control. The initial quote must
pass before any quantity selection. A quantity change that resets another selected
option is held for attention.

The supported initial source family is the seven previously reviewed DPS vinyl
product URLs. It uses the customizable-product controls, not static-size,
crossover, upload or designer calculators. Unsupported form shapes remain held.

## Source contract

Public `https://www.digital-print-solutions.com/Scripts/Frontend.Product.js` was
captured once without credentials. Its 30,193-byte body SHA-256 is
`66305dcc6a737f1c7006e15b13042ec6c7666ef68dd327b1bd90b7f7d93757b3`, identical
to the earlier source review. Local evidence is in `work/dps-research/` at the
workspace root. The live duplicate page must load this same script hash.

- `loadProductOptions`: observed `PrimaryApiPath` + `/products/{ProductIdField}/options`.
- `loadCustomProductQuantities`: observed API base + `/products/{CustProductIdField}/customizable`.
- `GetPrice`: observed API base + `/pricing/{ProductId}` with exact `width`,
  `height`, `quantity`, `turnaroundId` and `options` query fields.
- `getOptionString`: visible option selects, in DOM order, encoded as
  `optionIdEvalueIdCoptionIdEvalueId`. Nested controls are included only while visible.
- `setPrices`: the main calculator renders response `price` as USD; upload/design
  fields are separate source values and are never added to the main amount.
- Source `fetchPrice` caches an exact request URL for 30 seconds. The pilot requires
  a newly observed native response for each accepted quantity and never manipulates
  that cache or substitutes an older saved response.

Each observation binds the exact request and current DOM dimensions, quantity,
turnaround and selected options, then requires a stable displayed-price match.
The report retains whitelisted option/quantity/price projections, response hashes,
source evidence IDs and safe readiness diagnostics. Unknown response properties,
account data, cookies, headers, passwords and arbitrary error text are not retained.

## Limits and current evidence

The initial Hermes generic diagnosis `edccf3486362416a90721f7bea6055d0` found only
the Low Tack title. That scanner result does not establish an authentication
failure: the ASP.NET calculator lies outside its generic form roots.

Local Chromium fixtures verify native quote binding, quantity selection, mismatch
holding, source-hash rejection, unknown-quantity rejection, option-reset holding,
private-field omission and owned-page cleanup. Authenticated supplier execution
is a separate deployment step; no successful DPS supplier quote is claimed here.

The source's quantity domain is **not a price-break table**. Reports use
`price_basis=source_calculator_display_price`; they do not infer per-unit pricing,
shipping, discounts, production dates, full catalog coverage or final eligibility.
