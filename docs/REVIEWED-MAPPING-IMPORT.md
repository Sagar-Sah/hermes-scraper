# Reviewed Standard Print mapping import

This offline tool adds the thirteen individually reviewed source mappings from
`selective-mapping-inventory-assessment.json`. It reads the saved catalog and
supplier evidence; it makes no network requests and starts no collection or
scheduled work. It does not switch data roots, import profiles or keys, change
existing products, or mark historical facts current.

The seed includes nine historical 4over table products, three independently
confirmed product-family mappings, and the existing bound Standard Business
Cards candidate (6862). It excludes the already mapped Brown Kraft Business
Cards (9532), unverified mappings, merchant-tabled EDDM/direct-mail products,
excluded products, and unrelated fulfillment offers. The 6862 merchant cap is
25,000; its historical declared binding is not current quantity or price proof.
No price table is fabricated for that candidate.

## Preview and review

Run from the repository with the intended current data directory. Paths must be
regular files under the explicit evidence root; symlinks and aliases are rejected.
Replace `WORK` and `REPO` below with their absolute paths.

```sh
.venv/bin/python scripts/import-companion-mappings.py plan \
  --source-data WORK/atlas-data \
  --target-data REPO/data \
  --evidence-root WORK \
  --assessment WORK/hermes-autonomy-20261029/selective-mapping-inventory-assessment.json \
  --manifest WORK/hermes-autonomy-20261029/reviewed-mapping-import-manifest.json
```

`plan` reads runtime data and writes only the requested immutable review artifact.
The manifest contains exact product IDs, source URLs, target permalinks,
provenance hashes, 27 allowlisted content-addressed historical objects, target
revision preconditions and the proposed queued seed. It does not contain dealer
amounts, credentials, profiles or browser histories. Inspect it and approve its
exact `manifest_sha256` before applying. Reusing a manifest filename with changed
content is refused: use a new review filename when revisions change.

## Apply an approved manifest

Pause coordinator/campaign mutations for this brief offline operation. The import
uses its own singleton lock and checks both source and target revisions before
copying and again before publishing the complete seed. It does not acquire the
running application's campaign locks or rewrite an already inventoried cycle.

```sh
.venv/bin/python scripts/import-companion-mappings.py apply \
  --source-data WORK/atlas-data \
  --target-data REPO/data \
  --evidence-root WORK \
  --manifest WORK/hermes-autonomy-20261029/reviewed-mapping-import-manifest.json \
  --approve-manifest-sha256 EXACT_REVIEWED_MANIFEST_SHA256
```

Only missing raw object bytes and the new seed campaign are added. Existing
content is never overwritten. Equal content is an idempotent hit; conflicting
content, changed revisions, exclusions and incomplete proofs stop the operation.
A stopped copy can leave harmless content-addressed objects but cannot publish a
partial seed. Rerun the same approved operation after an interruption; it reuses
identical objects and never repeats supplier requests.

The seed products remain queued with zero current rows. Historical observations
retain their original timestamps and require current-cycle verification. The
source mapping index can discover these confirmed pairs in the next inventory
or an explicit coordinator remap; this command does not change the mapping
pinned into an existing cycle. Current evidence, authentication, source coverage
and supported adapters still determine which audits can run. In particular,
6862's mapping does not create a historical table-based collector fast path.
