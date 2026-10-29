# Product Companion: 4over business cards

The first release is a read-only comparison of a confirmed 4over product with
its PrintItPrints WooCommerce product. Open **Product Companion** in the local
Hermes dashboard. An audit can be run on demand; recurring checks are off by
default. Product names alone do not create a supplier mapping.

## First product

Brown Kraft Business Cards is WooCommerce product 9532, mapped to
`https://4over.com/brown-kraft-business-cards` by the finalized
`printit-89-20260927` campaign. The one-product evidence seed in
`data/campaigns/companion-4over-business-cards.json` was copied by
`scripts/import-companion-evidence.py`. This seed copies only hashed source
evidence, not account sessions or the rest of the campaign.

The first audit compared 256 saved supplier rows with 256 live calculator
rows. The authenticated refresh then collected all 64 current child tables,
yielding 256 quantity-price rows. The current source bindings, quantities,
and child table prices match the live calculator rows, with no missing or extra
configuration and no stored cost difference. Three rendered customer prices
also matched the site's pinned retail function. One linked 4over template was
absent from the storefront and requires review. Supplier service fees and
payable totals, the full customer-price domain, production, and content/image
coverage remain unknown. **This product is not green.**

The live Woo Store API returns a native price of zero and no native Woo
options for this simple product; its customer prices come from the custom
calculator. The Companion records both contexts and must not treat the native
zero as the configured customer price. New audits include the public Woo Store
API snapshot, its hash, and a storefront gallery cross-check.

## Using the dashboard

1. Select **Product Companion** and choose a mapped product.
2. Select **Run audit** to compare current public pages with saved supplier
   evidence. The supplier page request is serialized; schedules are opt-in.
   After signing into Hermes's own 4over browser, **Refresh 4over child tables**
   reads each declared child once with the shared supplier limiter. It saves a
   checkpoint after each child, resumes incomplete batches without refetching
   captured children, and reuses a completed snapshot for six hours. Select
   **Run audit** again to compare those current table prices and quantity tiers.
3. Read every check. `Unknown` is evidence that the field is not yet proven;
   it never contributes to a green result. A `Mismatch` proposes a scoped
   field-level review rather than a whole-product reimport.
4. Review individual proposals and select only the ones approved for handoff.
   Approval records a decision; it does not edit WordPress.
5. Export the evidence ZIP for the audit JSON, hashed saved supplier table,
   child-option map, and price-table evidence. The ZIP is not a Woo import.

The scoped WooCommerce write connector now supports reviewed previews,
application, rollback, and storefront rechecks. It must be installed and
connected before these actions are available. See
[the connector instructions](product-companion-writes.md). Recurring checks
remain read-only and the API still rejects `auto_correct=true`.

## Business-card rollout

Use **Inventory business cards** to list live WooCommerce candidates. Only
confirmed 4over campaign mappings can be imported; Printograph and unresolved
products stay outside this run. In the initial inventory, 22 storefront names
contain “business card” and only Brown Kraft has a confirmed 4over mapping.
Supplier quantity expansion, conditional controls, and fresh exact pricing must
be established before those checks can pass. A fresh child table is scoped to
one set with sample and digital proof off; it is not a verified payable order
total or a shipping quote.

## Repeated fields and headings

Companion now captures purchase controls and headings in its normal rendered
storefront visit, including products whose retail-rule sampling is unsupported.
It records public DOM references, names/bindings, purchase form, Front/Back,
artwork-set or variation-row scope, computed visibility, enabled/editable state,
viewport, observation time and a projection hash. It does not capture entered
values, hidden tokens, customer addresses, account storage or credentials.

Two simultaneously visible, editable controls with the same scalar binding in
the same purchase scope are a mismatch. Similar labels with different or unknown
bindings remain an unknown overlap requiring review. In particular, native Woo
quantity and calculator RunSize are not assumed equivalent: one may count
artwork sets while the other counts pieces per set. Explicit sets/pieces axes,
Front/Back scopes and separate variation rows remain separate. Computed-hidden
responsive copies and disabled conditional controls are retained as inactive
observations. A CSS class in static HTML does not prove a field is hidden.
Unknown vocabulary still has a named binding: grommets, drill holes, pole
pockets and supplier-specific finish fields receive the same checks. Array or
multi-select bindings are reviewed rather than assumed to be duplicate scalars.

A standalone calculator can sit outside the Woo cart form. Controls in these
separate containers are also compared when public product-region or Woo
product-ID evidence establishes that both belong to the primary product.
Their overlap is **unknown**, never a confirmed duplicate merely because the
purpose or binding matches. Intentional Front/Back, artwork-set and variation
scopes remain separate; related-product, upsell and other product owners are
excluded. The snapshot retains the numeric owner ID and its DOM evidence, not
other hidden-field values. The dashboard and export show this ownership evidence
beside the proposed review.

Repeated Product Options headings receive a presentation review; a tab label
plus its panel heading does not establish a duplicate. No finding automatically
removes a control or changes copy. The dashboard links each proposed review to
the observed fields and evidence; audit exports include the observations in
`audit.json` and a `duplicate-field-review.csv` when findings exist.

The field check covers the current rendered state and viewport only. Static,
truncated, empty or ambiguous observations remain unknown. Other conditional
states and responsive widths need their own observations before full product
coverage can be claimed. Existing CSV duplicate-column/configuration rejection
continues independently.
