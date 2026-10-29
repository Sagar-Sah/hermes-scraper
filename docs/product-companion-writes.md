# WooCommerce custom calculator writes

The PrintItPrints calculator reads `uploads/wcdcp/{product_id}-printit.csv`.
The connector changes supplier-cost CSV rows, then lets the site's existing
retail calculator calculate the customer price. It does not change WooCommerce
native prices or the merchant's markup/margin rules.

## Installation and connection

Install `outputs/hermes-product-companion-connector-0.1.1.zip` on protected
staging first. In WordPress, open **Tools → Hermes Product Companion**, list
the exact products allowed for the connection, generate a token, and enable
the connection. Paste the site origin and token into Hermes → **Product
Companion → WooCommerce write connector**. The token is stored in a local
file with owner-only permissions and is never returned in API responses or
audit exports. WordPress stores only its hash. External access starts disabled
with an empty product allowlist.

## Review, apply, restore

1. Run the supplier/storefront audit, then select individual verified proposals.
2. Create a write preview. Hermes checks that the bridge's CSV matches the
   rendered calculator before asking WordPress for an immutable preview.
3. Inspect the exact field changes. Preview does not change product data.
4. Apply the preview. WordPress rejects a stale revision, saves the original
   CSV bytes and document metadata privately, and replaces the CSV atomically.
5. Hermes checks the entire expected row table and unchanged retail script on
   the public page. For cost changes it checks rendered customer prices for up
   to three affected selections. A failed check triggers restoration of the
   saved state. The UI reports any recovery step still requiring attention.
6. Restore a change from history. A later merchant edit blocks rollback instead
   of being overwritten. The restored storefront must pass its own recheck.

An uncertain apply response can be recovered through the saved remote receipt.
If restoration succeeded but the public page was temporarily unavailable,
**Recheck restored storefront** repeats only the read check.

## Evidence and scope

Cost fixes require the latest audit, complete current child bindings and
quantity tables, and an unchanged source snapshot no older than six hours.
Document additions require a link present in the audited supplier document
list. Row additions require verified production data; the Python workflow
currently holds additions when the child table cannot prove that field.
Copy changes, deletions, and retail rule changes remain outside this connector.
Recurring checks remain read-only; automatic corrections are not enabled.

The plugin has a per-product lock and a revision covering CSV bytes, its own
document metadata, and installed calculator file fingerprints. Other uploaders
do not share that lock; the bridge detects intervening changes at preview/apply
and rollback boundaries. Test staging before connecting production.

## Staging verification on October 28, 2026

Version 0.1.1 was installed and activated on protected staging with product
access restricted to 9532 and 6862. A supplier-evidenced template addition for
9532 passed preview, apply, storefront verification, and rollback. The original
revision and CSV hash were restored exactly; all 256 calculator rows, the
retail script, and the checked $45.22 customer price remained identical.
Product 6862's 5,464-row snapshot was also readable within the allowlist.

Staging's upstream preview authentication returns HTTP 401 to the external
Hermes API client. The scoped token is stored privately with `connected=false`;
this is not a verified unattended connection. The staging-only WordPress
review screen uses the existing signed-in browser session and leaves preview
protection enabled. Evidence is saved in
`work/hermes-connector-staging-20261028/test-result.json` in the parent workspace.
