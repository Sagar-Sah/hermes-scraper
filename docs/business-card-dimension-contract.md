# Business Cards dimension contract

This offline review covers the saved Printograph **Business Cards** order form
(product type 1), fresh specifications, all 180 collected option catalogs, and
360 exact selector bindings. It made no source requests, changed no runtime
code, and does not certify a complete variation matrix or its prices.

The reproducible machine inventory is `work/business-card-dimension-contract.json`;
regenerate it with `work/venv/bin/python work/inventory_business_card_dimensions.py`.
It retains exact source labels, IDs, per-base catalogs, selector bindings,
quantity-table references, evidence IDs and content hashes. The source script
pins are the reviewed `work/planner-contracts/orderForm.js` and `chunk-common.js`.
All script offsets below are **zero-based Python character offsets**.

## What the source actually exposes

| Dimension | Saved domain | Role |
| --- | --- | --- |
| Size and orientation | 10 exact size/orientation tuples, listed below | Base lookup inputs; orientation is bound to its size tuple |
| Stock (`paper`) | 12 stocks, listed below | Base lookup input |
| Color (`color`) | Full Color Front, No Back; Full Color Front, Grayscale Back; Full Color Both Sides | Base lookup input; readable aliases 4/0, 4/1 and 4/4 respectively |
| Production (`productionTime`) | Regular, Rush | Sent as lookup `turnaround`; quantity availability also depends on turnaround |
| Quantity | Per-specification/turnaround/selected-option support | A lookup-table axis, never an unrestricted Cartesian dimension |
| Round corners (`roundedCorners`) | Optional None/empty selection or Round Corners | Type 2 is an additive priced option where supported |
| Coating (`coating`) | Source-dependent empty option, UV Front, UV Both Sides | Included/base hypothesis in this client; also changes other option availability |
| Spot coating (`spotCoating`) | None/empty, Front, Both Sides where catalog-supported | Additive option table and compatibility checks |
| Raised print (`raisedPrint`) | None/empty, Raised UV Front/Both, Raised Foil Front/Both where supported | Additive option table; enables foil-color field for foil selections |
| Foil color (`raisedFoilColor`) | Gold, Rose Gold, Silver where enabled | Separately priced option types; do not assume decorative/free |
| Paper finish (`paperFinish`) | Sole source value `1`, “Velvet” | Descriptive included finish, shown for relevant stock; not an independent priced variant |
| Processing/proof | Instant Processing; manual mode with supported secondary proof; design-service mode where offered | Explicit service context, including inactive/active states |
| File delivery | Conditional source schema values described below | Service context; not a product-finishing axis or permission to perform upload/design actions |
| Design package/instructions | Silver, Gold, Platinum plus conditional instructions | Separate design-service context; not ordinary upload/print pricing |

The active source field declaration for Business Cards is at orderForm **83784**:
`specs=[size,orientation,paper,color]`,
`options=[roundedCorners,coating,spotCoating,raisedPrint,raisedFoilColor,paperFinish]`,
`delivery=[productionTime]`, with design instruction and processing support.
The quantity and service controls are provided by the common schema, not that
`options` list. Shape, envelopes, drilling, and corner-position controls are
not established as Business Cards form dimensions by this evidence.

## Sizes, stocks, and known base bindings

The fresh specification response is
`44ba841a648a41e5b5d018ff1a42f982`, SHA-256
`cf334cadfc5354907246dc0e6b0ba39f16f62f3f70789fb73f9daa0994f9c1a7`.

The exact size labels are:

- `101`: 2" x 3.5" Horizontal U.S. Standard; `102`: 2" x 3.5" Vertical U.S. Standard.
- `335`: 1.75" x 3" Horizontal; `336`: 1.75" x 3" Vertical.
- `337`: 1.75" x 3.5" Horizontal; `338`: 1.75" x 3.5" Vertical.
- `260`: 2" x 3" Horizontal; `259`: 2" x 3" Vertical.
- `339`: 2.17" x 3.35" Horizontal European Standard; `340`: 2.17" x 3.35" Vertical European Standard.

The source associates horizontal sizes with orientation `1`/Landscape and
vertical sizes with `2`/Portrait. Do not independently cross every size with
both orientations. The confirmed 360 tuples each have a full exact lookup;
all 180 returned base entries have two observed orientation/size aliases.
This binding is evidence for addressing the same base table. It is not a
rule to merge unrelated products or throw away the displayed orientation.

| Stock | Source value | Confirmed Regular tuples | Confirmed Rush tuples |
| --- | ---: | ---: | ---: |
| 100 lb. Matte Cover | 7 | 20 | 0 |
| 13 pt. Premium Linen | 74 | 20 | 0 |
| 14 pt. Gloss | 1 | 30 | 30 |
| 14 pt. Uncoated | 2 | 30 | 30 |
| 16 pt. Premium Matte | 10 | 30 | 30 |
| 18 pt. Premium Kraft | 76 | 20 | 0 |
| 18 pt. Ultra Premium Pearl | 75 | 20 | 0 |
| 18 pt. Ultra Premium Smooth White | 77 | 20 | 0 |
| 24 pt. Trifecta Green with Velvet Finish | 31 | 20 | 0 |
| 38 pt. Trifecta Black with Velvet Finish | 32 | 20 | 0 |
| 38 pt. Trifecta Blue with Velvet Finish | 66 | 20 | 0 |
| 38 pt. Trifecta Red with Velvet Finish | 36 | 20 | 0 |

The color source values are `1` for Full Color Front, No Back; `2` for Full
Color Front, Grayscale Back; and `3` for Full Color Both Sides. The 20-tuple
stocks have the observed front-only/both-side choices, not all three colors.
The totals above are confirmed base-selector tuples, **not quantity/finishing
variations**. Membership/listing pagination exhaustion remains a separate gap.

## Corner details: do not manufacture the proposed examples

All **150** catalogs that support corners expose exactly the same corner
metadata: type `2`, group `2`, symbolic name `ROUND_CORNERS`, label **Round
Corners**, `quantityDependentPrice=false`, `anyQuantityOrdered=false`.
The other 30 catalogs are the three 38 pt. Trifecta stocks. There is no corner
radius/count/position property in any of the 180 catalog responses.

The common source tooltip at **437067** describes rounding **all four edges**,
an extra production step, and an unchanged final size. This is a description
of one option, not four independent corner checkboxes. The catalog-to-control
switch at orderForm **204226** adds only type 2 to `roundedCorners.list`; its
schema at **131387** is one optional dropdown with `hasPrice:"yes"`.

No selectable quarter-inch/eighth-inch radius, two-versus-four corner count,
or corner position is established for this order form. Do not invent raw
values or multiply the matrix by those examples. They remain an explicit
inventory question if a different source page/widget exposes them later.

Potentially misleading source matches were checked:

- The two `1/4"` strings in orderForm (**848460**, **853576**) are other
  products' material thickness, not Business Card radius choices.
- Common `1/8"` text (**437335** and nearby) describes hole drilling.
- `roundCornerRadius` is response geometry read into the artwork preview
  (`trimZoneCornerRadius`/`canvasCorner`, orderForm **1204678**, **1205747**).
  It is not a `userSelectedOptions` value or a price-request parameter.
- `window.radiusCount` near common **104200** is in a product-type-20 hang-tag
  hole preview branch. It establishes no Business Cards choice domain.
- `Label.roundedCornerTopRight`, `BottomLeft`, `TopLeft`, and `Side` at common
  **923177–923303** are quote-tool translation requests. No corresponding
  Business Cards form selector or price mapping is established.

## All option types observed in the 180 catalogs

Catalog counts are not counts of simultaneous valid choices. Every record is
retained under its exact base entry in the machine inventory.

| Field / source label | Type | Catalogs | Source requests an option price table |
| --- | ---: | ---: | --- |
| Round Corners | 2 | 150 | Yes |
| High Gloss UV Coating Front | 5 | 60 | No |
| High Gloss UV Coating Both Sides | 40 | 20 | No |
| Spot High Gloss UV Coating Front | 3 | 15 | Yes |
| Spot High Gloss UV Coating Both Sides | 39 | 5 | Yes |
| Raised UV Print Front | 135 | 15 | Yes |
| Raised UV Print Both Sides | 137 | 5 | Yes |
| Raised Foil Print Front | 138 | 15 | Yes |
| Raised Foil Print Both Sides | 140 | 5 | Yes |
| Foil Colors: Gold | 141 | 30 | Yes |
| Foil Colors: Rose Gold | 142 | 30 | Yes |
| Foil Colors: Silver | 143 | 30 | Yes |
| Instant Processing Proof | 31 | 180 | No |
| Manually Processed, PDF Proof 24 Hrs. | 1 | 180 | Yes |
| Manually Processed, No Proof | 36 | 180 | Yes |
| Design Service: Silver Package | 41 | 180 | Yes |
| Design Service: Gold Package | 42 | 180 | Yes |
| Design Service: Platinum Package | 43 | 180 | Yes |

The shared source supports more IDs globally (for example coating 6, spot 4,
raised 136/139, manual 37/38), but these are **absent from these 180 catalogs**.
Do not add them to the Business Cards inventory because a global schema lists
them. Spot/raised types above occur on Premium Matte; front/both UV coating
occurs on Gloss and Premium Matte. Foil colors remain separate additive types
in `optionsHavingPrices`, despite their `quantityDependentPrice=false` flag.

## Dependencies and descriptive state

The source exclusion map at orderForm **127062** and its `displaySpecs`
consumer (**1705127**) hide corners for the three 38 pt. stocks. They hide
coating on uncoated/Trifecta/premium specialty stocks, and hide foil color
unless a raised-foil type is selected. The source option-value restrictions
at **124536** additionally constrain coating/spot/color combinations. These
rules are only part of availability: source per-base catalogs and quantity /
turnaround support still apply. Source option processing can reset unsupported
selections at **206497**; retain effective selections, not requested inputs.

The blank coating value is contextual, not universally “None”: the source
label function at **1827516** uses Matte Aqueous for papers 6/7/10 and Gloss
Aqueous for 3/4/5, with other branches for paper 1 and active spot coating.
Among fresh BC stocks, 7/10 are the relevant Matte cases. Preserve exact
observed blank labels and control presence; never convert a blank into a
priced option, assume a fee, or copy one stock's label to every stock.

`paperFinish` at **130391** contains only `1: Velvet`, is marked `hasPrice:no`,
and is hidden by the field map (**126892**) except the relevant stocks. The
cart builder explicitly skips it at common **402091**. It has no applied-price
map or base-lookup spec position. Keep it as descriptive product data; it
does not justify an extra price request or arbitrary finish alternatives.

Processing must retain both mode and selected secondary proof: source mode
`31` is instant, `100` is manual, `101` is design. For these catalogs manual
secondary IDs are 1/36; `100` itself is not an option record/type to price.
Conditional file-delivery schemas at orderForm **134640** include instant
5/22/33/34, manual 5/6, design 9/15/14/29. Those are global schema possibilities,
not proof every mode/action is currently offered. Save actual support and
selection; do not infer a service from a zero fee or an absent visible widget.

## Quantity inventory and price-key boundaries

The 180 saved base tables contain a union of **27 numeric quantity entries**:
25, 50, 100, 250, 500, 1,000, 2,500, 5,000, then 10,000 through 100,000 in steps
of 5,000. **This is a price-table union, not 27 valid quantities per variant.**
Quantity 25 is a concrete example that must not be promoted merely because a
table contains it. The quantity and option support endpoints, turnaround, and
UI minimum/option filtering determine valid selections.

The source contracts are:

- Exact base lookup: `getProductId`, orderForm **198673**, query construction
  **199653**. Inputs are product type, turnaround, base specs (including size /
  orientation), and conditional item/account context. Finishing is separate.
- Base quantity availability: orderForm **210960**, route at **211817**,
  `/products/quantities?productType=…&includeTurnaround=true&…base specs…`.
- Option quantity availability: orderForm **210440**,
  `/products/options/quantities?optionIds=…&includeTurnaround=true&…base specs…`.
  Common `getOptionBySpecKey` at **755619** builds the base projection.
- Base table: orderForm `getPrice` **231417** addresses `/products/{base}/prices`.
- Option table: `getOptionPrice` **236500** addresses
  `/products/{base}/options/{catalog-record}/prices`; the selected type ID and
  catalog-record ID are distinct. The quantity-specific wrapper at **238209**
  adds the current `quantities` value; it does not prove arbitrary batch syntax.
- Both price families conditionally add `priceGroup`, `designService=true`,
  `itemId` and `cid`. Ordinary manual versus instant proof and delivery do not
  appear directly in those GET URLs, but remain part of the full configuration
  and service context. URL projection alone is not server independence proof.
- `optionsHavingPrices` **56423** and `updateOptionPrice` **234835** govern
  priced components. `quantityDependentPrice:false` is not a safe constant-price
  certificate. Cache assignment at **239980** has explicit exact-quantity and
  singleton cases; sparse multi-row tables must not be interpolated casually.
- Common `getTotal` **381096** adds base + active manual-secondary proof +
  selected option applied prices + active design package. Coupon transformation
  is separate (`getCouponPrice`, orderForm **1593100**). No guessed fee or
  quantity multiplication belongs in that formula.

A safe inventory keeps the complete effective configuration (including all
source-observed descriptive selections), while component request keys dedupe
only **identical method/path/full query/body and complete bound context**.
Two size/orientation aliases may address the same exact base table because
both full lookups returned that base; retain both output variants. Do not
collapse distinct option-record paths because their returned prices happen
to match. Keep production, service, coupon and context changes explicit.

Canaries should cover each observed option value and actual dependency branch,
plus minimum/maximum and availability-change quantities. They must compare
settled effective configurations, explicit proof/delivery, source formula
components and displayed subtotal. A changed effective selection invalidates
a same-configuration comparison. A residual amount or mismatch stops reuse
for that scope; it is not evidence for a guessed hidden fee. Radius/count /
position canaries can be planned only after source evidence establishes those
choices. This inventory leaves the final valid-row count unresolved.
