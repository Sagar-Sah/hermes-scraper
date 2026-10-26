# Collect, export and archive products

The package button at the upper left opens the collection tray. It shows each
saved product's collection status. A checkmark requires both a completed product
state and complete coverage; retained rows alone do not establish completion.
Progress is indeterminate when the source's valid configuration total is unknown.
New completions animate the package button once while the dashboard is open.
Reduced-motion preferences disable that animation.

## Export a ZIP

1. Open the package tray. Completed products are selected by default. Use Select
   all completed, Select none, or individual checkboxes to change the selection.
2. To include an unfinished product, acknowledge its warning. The warning can be
   applied to all unfinished products explicitly. This does not change their
   collection or verification state.
3. Choose an export folder where the browser supports a folder picker, or use the
   browser's Save ZIP flow. Files are written to the customer's computer; a local
   folder path is not sent to the Hermes server.
4. Save the ZIP. Each selected product has its own folder with its saved rows and
   downloaded images, plus metadata describing scope and missing information.
   Saved legacy observations remain labeled; exporting does not approve prices
   or make a file a WooCommerce import.

Exports read saved data only. They do not sign in, request new supplier prices,
download previously uncollected images, upload artwork or publish a storefront.
Image URLs and missing local files are recorded explicitly. Credentials, browser
sessions and unrestricted network-response bodies are not part of an export.

## Keep archives or remove exported working data

After a successful folder write, Hermes presents the archive selection. For a
normal browser download, confirm that the ZIP was saved before this step. A
started download is not evidence that the file reached disk.

All exported products are selected for archiving by default. Select none or
individual products as needed. Confirming this step keeps compact, downloadable
archives for checked products and removes the unchecked exported products from
the working collection. Products outside this export are unaffected. Canceling
the dialog leaves working data intact.

Pending ZIPs remain available after a page refresh. Discarding a pending server
ZIP removes that download only and preserves its original products. If collection
has advanced since an earlier export, a fresh snapshot can still be exported.

Cleanup refuses products whose harvest is still active, changed after the export,
or remains referenced by protected campaign/automation work. Pause the affected work
through its normal controls and export a fresh snapshot when necessary. The
archive selection does not silently stop a supplier job or discard newer data.
Saved export receipts make repeated confirmation safe after a lost response.
If working-file cleanup is incomplete after the database update, Hermes reports
the retained files and offers a retry using the same saved archive selection.

Product-owned export files can be reclaimed after the archive is durable. Shared
content-addressed evidence remains retained when ownership cannot be proved;
the cleanup result reports that limitation. This workflow does not claim to be a
global cache collector or a secure disk erasure tool.

Archives can be downloaded from the same tray for future reference. They preserve
the exported snapshot; downloading an archive does not resume supplier collection.

## API

All routes use the same API authentication dependency as the rest of Hermes:

- `GET /api/v1/collection/products`: products, compact archives and recoverable
  pending exports.
- `POST /api/v1/collection/exports`: `product_ids` and explicit
  `allow_incomplete_product_ids`; returns an immutable ZIP receipt.
- `GET /api/v1/collection/exports/{id}`: receipt metadata.
- `GET /api/v1/collection/exports/{id}/download`: ZIP download.
- `POST /api/v1/collection/exports/{id}/discard`: `discard: true` removes an
  unfinalized server ZIP without deleting the original collection.
- `POST /api/v1/collection/exports/{id}/finalize`: `saved: true` and the exact
  `archive_product_ids` to retain. Unchecked products in that receipt are removed
  only when all safety checks pass.
- `GET /api/v1/collection/archives/{id}/download`: compact product archive.

Export construction runs outside the API event loop. It uses immutable saved
batch boundaries so a partial export has a defined snapshot even while collection
continues. Later cleanup compares the saved fingerprint to current state.
