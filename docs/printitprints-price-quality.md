# PrintItPrints quantity-price quality review

Reviewed **20,009 saved rows** with the existing commerce grouping. There are **1,245 comparable quantity curves** and **18,707 adjacent quantity comparisons**. No supplier requests or record changes were made.

A larger order does not always have a lower unit price in these saved tables. The findings below are review flags, not declarations that a supplier price is wrong.

| Product | Comparable curves | Compared quantity pairs | Retail unit discounts | Retail unit increases | Retail total decreases |
| --- | ---: | ---: | ---: | ---: | ---: |
| Roll Labels | 744 | 10,416 | 10,416 | 0 | 25 |
| Brochures | 444 | 7,898 | 7,329 | 569 | 0 |
| Club Flyers | 57 | 393 | 391 | 2 | 15 |

Each curve holds every recorded option, processing/artwork selection, production value, currency and source context constant. Different recorded finishes, proof choices or unwind directions are never pooled. Missing source fields remain unspecified. Unit comparisons use the total divided by quantity; they do not use rounded three-decimal unit labels.

| Product | Base-table unit increases | Base-table total decreases | Retail total drops already in base | Retail total drops from markup-band changes |
| --- | ---: | ---: | ---: | ---: |
| Roll Labels | 0 | 0 | 0 | 25 |
| Brochures | 983 | 0 | 0 | 0 |
| Club Flyers | 2 | 0 | 0 | 15 |

The embedded `TotalPrice` is the base amount. Retail adds the pinned supplier markup and rounds to cents. That markup falls at amount thresholds, so a higher base amount can produce a lower retail total when it enters the next band. All 40 retail-total decreases occur at these markup-band changes; no embedded-base total decreases were found. All 571 retail unit-price increases are already present in the embedded-base unit prices. Lower retail markups hide another 414 base-table unit increases. These observations do not establish supplier errors.

Examples:

- **Club Flyers — unit price increases, 50 → 100.** Retail total $12.66 → $28.71; unit $0.253200 → $0.287100. Embedded base $4.22 → $9.57. Markup 200% → 200%. Classification: already present in embedded base.
  Same selection: artwork delivery: Upload Files Now; coating: High Gloss UV Coating Front; colorspec: Full Color Front, No Back; orientation: Horizontal; proof processing: Instantly Processed; raised print: None; rounded corners: None; size: 2" x 5.5"; spot coating: None; stock: 14 pt. Gloss; production: Rush 2 Business Days.

- **Club Flyers — total price decreases, 250 → 500.** Retail total $102.50 → $101.04; unit $0.410000 → $0.202080. Embedded base $41.00 → $50.52. Markup 150% → 100%. Classification: retail markup band change.
  Same selection: artwork delivery: None; coating: Matte Aqueous Coating; colorspec: Full Color Front, No Back; orientation: Horizontal; proof processing: Manually Processed; raised print: Raised UV Print Front; rounded corners: Round Corners; size: 2" x 5.5"; spot coating: None; stock: 16 pt. Premium Matte; production: Regular 6-8 Business Days.

- **Brochures — unit price increases, 50 → 100.** Retail total $269.32 → $570.16; unit $5.386400 → $5.701600. Embedded base $145.58 → $335.39. Markup 85% → 70%. Classification: already present in embedded base.
  Same selection: coating: Matte Aqueous Coating; colorspec: 4/4; folding: Tri-Fold To 11" x 8.5" (6 pages); size: 11" x 25.5" (6 Page Brochure); stock: 100 lb. Matte Cover; production: Regular Production.

- **Roll Labels — total price decreases, 500 → 1,000.** Retail total $195.44 → $192.22; unit $0.390880 → $0.192220. Embedded base $97.72 → $103.90. Markup 100% → 85%. Classification: retail markup band change.
  Same selection: artwork delivery: Upload Files Now; colorspec: Full Color Front, No Back; finish: Clear Gloss Laminate (Outdoor); orientation: Horizontal; proof processing: Instantly Processed; shape: Rectangle - Rounded; size: 0.75" x 1.5"; stock: White Vinyl Label; unwind options: Off Left; production: Regular 2-4 Business Days.

There are 57 single-quantity groups and 0 conflicting groups excluded from curve comparisons. A pair may have more than one flag, so anomaly columns should not be added together.

An additional live dropdown check reproduced the Brochures example above: 50 pieces displayed $269.32 ($5.386 per piece), and changing only quantity to 100 displayed $570.16 ($5.702 per piece). Both totals match the exported rows to the cent. This confirms that this particular unit-price increase is present in the storefront; Hermes has preserved it rather than smoothing it away. No cart or order was created.

All 20,009 rows remain verification pending. The three earlier storefront samples and this paired quantity check do not validate the other configurations or settle the remaining flags. Shipping, taxes and availability are outside this review.

Full curve contexts, every flagged transition, exact values and response evidence IDs are in `work/printit-price-quality.json`. Reproduce locally with `work/venv/bin/python work/report_printit_price_quality.py`. The original rows and exports remain unchanged.
