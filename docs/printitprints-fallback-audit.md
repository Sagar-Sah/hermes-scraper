# PrintItPrints candidate fallback audit

Saved run `11a6fdd915014ce0b83d2b51eebd8ea7` ended **completed_with_gaps**. It made **11 product-document requests** and retained **20,009 rows** from **3 products**. The other **8 products** need adapter attention; they produced no placeholder rows.

These are **candidate replacement products**, not confirmed matches to the user's missing product names. PrintItPrints evidence and prices remain separate from Printograph.

| Supplier product | Saved rows | Calculated retail rows | Unpriced rows | Result |
| --- | ---: | ---: | ---: | --- |
| [Booklets/Catalogs](https://printitprints.com/shop/standard-print/marketing-products/booklets/brochures-copy/) | 0 | 0 | 0 | Embedded calculator absent; extraction unavailable |
| [Roll Labels](https://printitprints.com/shop/standard-print/marketing-products/roll-labels-marketing-products/roll-labels/) | 11,160 | 11,160 | 0 | Calculated; verification pending |
| [Folders](https://printitprints.com/shop/standard-print/marketing-products/presentation-folders/folders/) | 0 | 0 | 0 | Embedded calculator absent; extraction unavailable |
| [Foam Boards](https://printitprints.com/shop/adhesives/quick-decals-by-shape/apartment/foam-boards/) | 0 | 0 | 0 | Embedded calculator absent; extraction unavailable |
| [PVC Boards](https://printitprints.com/shop/adhesives/quick-decals-by-shape/apartment/pvc-boards/) | 0 | 0 | 0 | Embedded calculator absent; extraction unavailable |
| [Corrugated Boards](https://printitprints.com/shop/adhesives/quick-decals-by-shape/apartment/corrugated-boards/) | 0 | 0 | 0 | Embedded calculator absent; extraction unavailable |
| [Cardboards](https://printitprints.com/shop/adhesives/quick-decals-by-shape/apartment/cardboards/) | 0 | 0 | 0 | Embedded calculator absent; extraction unavailable |
| [Acrylic Boards](https://printitprints.com/shop/grand-format/rigid-sign-shapes-grand-format/arrow-rigid-sign-shapes-grand-format/acrylic-boards/) | 0 | 0 | 0 | Embedded calculator absent; extraction unavailable |
| [Brochures](https://printitprints.com/shop/standard-print/marketing-products/flyers-and-brochures/brochures/) | 8,399 | 8,399 | 0 | Calculated; verification pending |
| [Club Flyers](https://printitprints.com/shop/standard-print/marketing-products/flyers-and-brochures/club-flyers/) | 450 | 450 | 0 | Calculated; verification pending |
| [Booklettes/Catalogs](https://printitprints.com/shop/standard-print/marketing-products/booklets/booklettes-catalogs/) | 0 | 0 | 0 | Embedded calculator absent; extraction unavailable |

All retained rows have a finite calculated retail subtotal, an exact source-response binding, PrintItPrints attribution and `verification_status=pending`. No rows are cart verified. The audit found no duplicate row IDs, wrong-supplier attribution, unbound evidence, formula-replay differences or export-count mismatch.

| Product | Quantity | Embedded base total | Calculated retail subtotal |
| --- | ---: | ---: | ---: |
| Roll Labels | 100 | USD 66.23 | USD 132.46 |
| Brochures | 250 | USD 119.59 | USD 221.24 |
| Club Flyers | 50 | USD 3.3 | USD 9.90 |

The embedded `TotalPrice` is the base amount. The storefront applies its product-membership and amount-tier markup before displaying the retail total. All three harvested products use the same pinned calculator:

`948b63c3764d3781dad5c369d1b0b3e6c7f8f3811ec3df80e5b459e1f85bafb5`

Separately observed storefront samples:

| Product | Quantity | Displayed total | Calculated total | Sample result |
| --- | ---: | ---: | ---: | --- |
| Club Flyers | 250 | USD 51.30 | USD 51.30 | Match |
| Brochures | 250 | USD 221.24 | USD 221.24 | Match |
| Roll Labels | 100 | USD 132.46 | USD 132.46 | Match |

Each check covers one selected storefront configuration. All three pass; they do not verify other rows, shipping, tax, inventory or the cart. Exact selections and source report paths are retained in the machine-readable audit. All exported row verification flags remain pending.

The readable combined data is [hermes-printitprints-calculator-data.csv](../../hermes-printitprints-calculator-data.csv), with embedded base and calculated retail prices in separate columns. It contains retained rows only; unavailable products are omitted from the data and listed in this report.

All 11 saved documents returned HTTP 200. Eight lack the supported embedded calculator, so their failures are source-format gaps rather than HTTP access failures. Their available configurations and prices remain unknown. No failed request was retried for this audit.

The normal browser reader requested only each exact product document; scripts, assets, redirects and writes were blocked. This audit opened the SQLite database read-only, checked immutable object hashes, and read no browser profile or credentials.

Reproduce from the workspace root with `work/venv/bin/python work/report_printit_fallback.py`. Machine-readable results, response IDs, document flags and per-product choice counts are in `work/printit-fallback-audit.json`.

Remaining limits:

- All 11 URLs are candidate replacements; correspondence to the user's missing product names is unconfirmed.
- A pinned frontend calculation does not certify stock, checkout, shipping, tax or account-specific prices.
- Unavailable adapter extraction is not zero product availability. No zero-price placeholder rows were created.
- Embedded table exhaustion does not establish the supplier's complete product catalog or future configuration domain.
- Recalculation uses the adapter's pinned formula; it is a data-integrity check, not independent cart verification.
