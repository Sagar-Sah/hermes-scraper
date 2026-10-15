# Roll Labels selector audit

The saved evidence does **not** resolve the 306 missing base-ID-to-selector mappings. This audit proposes **zero new full mappings** and records 81 directly supported partial mappings (size → shape and orientation). No supplier request, database mutation, product-row generation, or price certification occurred.

The reproducible offline script is `work/audit_roll_selector_derivation.py`; its result is `work/roll-selector-derivation-audit.json` in the workspace containing this project. It opens SQLite in read-only mode and checks saved object hashes. Run it from that workspace with `python3 work/audit_roll_selector_derivation.py`.

## Examined evidence

| Evidence | Observed content | What it establishes |
|---|---|---|
| DOMAIN job `17c9eab0cbb91e45a2a68b7a421509f25861d57826910b440a3a07843bad0556` | 918 successful responses: 459 base-price tables and 459 option catalogs | Those listed base IDs have captured tables. The stage is `requests_drained`; this is not catalog completion. |
| Listing `3df35f6457d14c519d30a09d8086c723` | 459 entries, each containing only `id` | An observed ID set, without specifications or pagination-exhaustion proof. |
| Specifications `56c177f0db7241129a53b01b62f4ec61` | 81 sizes with explicit shape and orientation; separate paper, finish, color, turnaround lists | Exact partial size mappings and individual value membership, not valid full combinations or their base IDs. |
| `work/roll-known-selector-map.json` | 154 historical exact selector tuples covering 153 listed base IDs | Existing historical bindings; all 154 agree with the saved specification membership and size relationships. Fresh current-context lookup confirmation remains required. |

Every fresh base-table item has only `quantity`, `markupPrice`, `listPrice`, and `packageASubItemPrice`. Every option item has option `id`, `name`, `type`, `group`, and support/quantity flags. Neither response contains a base specification relationship. The JSON artifact lists all 306 unresolved base IDs with their fresh price and option evidence IDs, all 81 partial mappings with JSON pointers, and all historical cross-checks.

For example, specification `/sizes/51` directly binds size `742` to shape `5`, orientation `1`, and the label `0.5" x 1" Horizontal`. It does not identify a paper, finish, or base product ID. No Cartesian expansion of the independent lists was performed.

## Why price or ordering inference is unsafe

The 459 base IDs have only **201 distinct price bodies**; 134 bodies are shared by multiple IDs. For example, `100134633` and `100217716` have identical price tables despite exact historical lookups selecting paper/finish `15/3` and `73/1`, respectively. Their fresh table evidence is `1eedbb6a134340329b11d8998ce02407` and `d4b12a7500d4471786e38233d5278210`; historical lookup evidence is `1a4ea69ae9f64faf95af36cfd4e02701` and `95fd4ead3d2b4812bc7ff823edac67bb`.

Ignoring distinct option IDs, the option catalogs form only two descriptive groups, containing 453 and 6 base IDs. This is an ambiguity check, not permission to interchange option catalogs. Numeric ID spacing and listing order carry no observed selector contract.

Historical base `100230580` also has two observed aliases: size/orientation `429/2` and `742/1`, both with shape `5`, paper `79`, finish `3`, color `1`, turnaround `1`. Thus a unique base ID cannot be assumed to mean one visible selector tuple.

The missing evidence is an explicit response joining each remaining base ID to its full selector tuple, or a source-supported dependent selector path followed by an exact full lookup. These artifacts do not propose an unobserved endpoint, infer a final valid configuration total, or certify service combinations, account context, prices, or completeness.
