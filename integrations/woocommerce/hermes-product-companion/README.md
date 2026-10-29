# Hermes Product Companion Connector 0.2.2

This WordPress plugin connects the Print It custom CSV calculator to Hermes.
It updates source-cost data in `wp-content/uploads/wcdcp/{product_id}-printit.csv`.
It preserves WooCommerce's native prices, existing product copy, Elementor data,
and the site's retail rules. Templates added by the connector use separate
product metadata and a small artwork-template section below the cart form.

The supported CSV must contain `TotalPrice`, `UnitPrice`, a quantity/RunSize
column, valid positive source costs, and unique complete configurations. A
different calculator owner or unsupported schema is held for review.

## Connection setup

1. Install and activate the ZIP in WordPress → Plugins → Add New → Upload Plugin.
2. Open Tools → Hermes Product Companion.
3. Enter only the reviewed product IDs, initially `9532` for Brown Kraft.
4. Select **Generate a new connection token**, enable the external connection,
   and save. The token appears once. Save it in Hermes's protected connection
   configuration; do not paste it into product data, source exports or logs.
5. Test a snapshot before enabling writes in Hermes. WordPress administration
   calls also work through the normal logged-in REST cookie and nonce.

External access is disabled until explicitly configured. Only a SHA-256 token
hash is stored by WordPress. A valid bearer token also needs the requested
product in the allowlist. Rotate the token or disable the connection from the
same Tools page to revoke access.

### Protected staging review

On `staging3.tonyl36.sg-host.com` only, **Tools → Hermes staging connector
review** provides visible forms for Read snapshot, Create preview, Apply
displayed preview, and Restore saved original. It uses the existing signed-in
WordPress session and a form nonce, so staging's separate preview protection
can remain enabled. It is limited to configured allowlist products 9532 and
6862. The page shows CSV hashes, row counts, document metadata and safe receipts;
private backup bytes and connection secrets are never returned.

Read a snapshot first. Paste a source payload containing only `audit_id`,
`evidence`, and `changes`, then create and inspect the preview before applying.
Use the storefront link for a separate rendered-page recheck, restore the saved
original, and verify the original revision and CSV hash returned. This page
does not claim a successful storefront check from a server receipt alone.

## REST protocol

All routes live under `/wp-json/hermes-companion/v1` and require authorization.
Legacy external calls use `Authorization: Bearer <connection token>` over HTTPS.
Environment connections from **Tools → Hermes audit connections** instead use
`X-Hermes-Companion-Token`, with separate production-read, staging-write and
human-approved production-release identities. Read-all catalog access and the
product write allowlist remain separate permissions.

Protected staging also sends its normal HTTP Basic preview credentials. For
exact registered connector routes only, a valid staging-write token bound to
the staging origin prevents WordPress from interpreting that preview Basic
header as an Application Password. This uses WordPress's
[application_password_is_api_request filter](https://developer.wordpress.org/reference/hooks/application_password_is_api_request/).
It does not disable preview protection, authenticate a WordPress user, clear
other authentication errors, change the product allowlist or affect other REST
namespaces. Invalid tokens and production connections keep normal WordPress
authentication behavior.

- `GET /catalog?page=1&per_page=100` returns only published products through a
  bounded, prepared, read-only query of WordPress's core posts table, without
  loading calculator profiles or prices. The ID query fixes `post_type=product`,
  `post_status=publish` and `ORDER BY ID ASC`; WooCommerce product-query filters
  cannot silently change that inventory order. The core table identifier is
  validated, and count, identity and page-cardinality checks remain strict.
  It requires
  a read-all environment token or the existing WooCommerce admin permission.
  The response is `{schema: "hermes-companion-catalog/v1", site_origin,
  environment, items: [{id, name, permalink, categories: [{id, name}]}], page,
  total, pages, next_cursor}`. The page size is fixed at 100; an empty catalog
  has `pages: 1` and `next_cursor: null`. Missing products, incomplete pages and
  invalid category evidence fail the request rather than disappearing from the
  inventory. `/capabilities` advertises `published_catalog: true`.
- `GET /inventory` retains its existing broader published/draft/private baseline
  inventory behavior; it is not replaced by the published-catalog route.

- `GET /snapshot/{product_id}` returns the CSV revision, original headers,
  normalized rows (`selection`, `production`, `cost`, `raw`), document links,
  product URL, and a retail-source fingerprint. The effective rule is explicitly
  unknown until Hermes verifies the storefront and cart behavior.
- `POST /previews` accepts `product_id`, `expected_revision`, a 32-character
  hexadecimal `audit_id`, fresh `evidence`, and a list of scoped `changes`.
  It saves an immutable preview for 15 minutes and returns the exact diff.
  It does not alter the calculator.
- `GET /previews/{preview_id}` returns a safe status and saved receipt for
  recovering a lost HTTP response, plus the current product revision; private
  backup bytes are excluded.
- `POST /apply/{preview_id}` accepts `expected_revision`. It saves an exact
  backup before applying the preview and returns `change_id`, the current
  `revision`, status, and before/after revisions. A repeated apply is idempotent.
- `POST /rollback/{change_id}` accepts the current `expected_revision`. It
  restores the exact original CSV bytes and document metadata. Manual edits
  after apply block rollback rather than overwriting the later work. Repeated
  rollback is idempotent when the restored state remains unchanged.

`evidence` must be `{verified: true, source_observed_at: <integer Unix time>,
sha256: <64 lowercase hexadecimal characters>}` and at most six hours old.
Hermes must prove the supplier mapping and applicable option/price coverage
before marking evidence verified. This plugin does not certify supplier facts.

Supported changes:

```json
{"kind":"set_cost","selection":{"size":"2 x 3.5","quantity":"100","stock":"Kraft"},"cost":"17.25"}
{"kind":"add_row","selection":{"size":"2 x 3.5","quantity":"250","stock":"Kraft"},"production":"4 Business Days","cost":"22.00"}
{"kind":"add_document","url":"https://4over.com/media/asset/templates/cards/example.eps","label":"2 x 3.5 artwork template"}
```

The selection must contain every existing non-price option field, normalized to
lowercase alphanumeric names, with RunSize/Qty represented as `quantity`.
For a cost change, omitted production preserves the existing production when
the other selections identify one unique row. Added rows require production
when the CSV has a production column. `UnitPrice` is derived from the total
source cost and quantity. Removals, description changes, rule changes, and
native WooCommerce price writes are rejected.

## Recovery and verification

Each product has an exclusive update lock. Revisions include original CSV
bytes, connector-owned document metadata, and available calculator source
fingerprints. A changed revision invalidates a preview. Atomic file replacement
keeps storefront readers from seeing a partially written CSV. Non-autoloaded
WordPress options hold private preview/change records and exact backups.

The preview points to its prepared backup before any write. A retry can settle
a completed write or resume the same prepared backup. Rollback intent is saved
before restoring either resource. When a crash leaves CSV and document metadata
at an exact combination of their saved before/after endpoints, and the rule
fingerprint is unchanged, recovery compensates to the original state. An
unrecognized file, document edit, or rule change is held for inspection. The
`recovery_required` status retains backups if compensation cannot finish.

Every successful apply or rollback returns `storefront_recheck_required: true`.
Hermes must then verify the public calculator rows and rendered customer prices.
An `applied` receipt alone is not a successful storefront verification.

Run the offline tests with:

```sh
php integrations/woocommerce/hermes-product-companion/tests/test-connector.php
php integrations/woocommerce/hermes-product-companion/tests/test-admin-review.php
php integrations/woocommerce/hermes-product-companion/tests/test-environments.php
php integrations/woocommerce/hermes-product-companion/tests/test-preview-basic-auth.php
php integrations/woocommerce/hermes-product-companion/tests/test-catalog.php
```
