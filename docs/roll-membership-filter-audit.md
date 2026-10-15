# Roll Labels single-filter membership audit

All four observed filters are consistent with the 154 fresh exact lookups. These lists can narrow candidate base IDs; they do not establish valid combinations by intersection.

| Filter | IDs returned | Matching exact IDs found | Contradictory known IDs | Missing matching known IDs |
|---|---:|---:|---:|---:|
| paper `12` | 51 | 18 / 18 | 0 | 0 |
| paper `13` | 51 | 18 / 18 | 0 | 0 |
| finish `1` | 255 | 86 / 86 | 0 | 0 |
| size `742` | 9 | 9 / 9 | 0 | 0 |

The fresh exact-lookup stage is `5f4cef50e653e989f8680b9613ef9f5dfd12b87bdd8c98dfa1cf2ef642034f05`; the four membership reads are stage `93c43693ba25482fbbf13e900479bbf96891ab4f7198956b715a95d0f6e51a40`. All 158 relevant requests have identical full public request context. The 154 lookup results cover 153 base IDs and show no changes from the historical mapping.

The size result includes base `100230580`, which has exact successful witnesses for both size/orientation `742/1` and `429/2`. This is an observed alias, not a contradictory filter result. A base ID can have multiple visible selector tuples. Per-dimension memberships therefore cannot be treated as a unique inverse mapping or proof that any intersection of values can coexist.

The pages add partial membership evidence for **169** of the 306 previously unmapped base IDs; **137** still have none from these four tests. They add **zero full mappings**. The two paper lists are disjoint and are subsets of the finish-1 page. Intersections with size-742 reproduce already known IDs, not new valid configurations.

All four saved responses are HTTP 200 with unique positive IDs and an `items`-only body. Each requests `offset=0, limit=1000` and returns fewer than 1000 entries. There is no saved total, next-page marker, pagination header, or follow-up page. **Pagination exhaustion remains unconfirmed.** Absence from a page cannot yet be treated as definitive absence from the supplier's domain. Unobserved dimension/value filters have not been validated by these four tests.

The offline audit script is `work/audit_roll_membership_reads.py`; its JSON artifact is `work/roll-membership-filter-audit.json`. It verifies source task status, HTTP status, request/response binding, saved object hashes, item shape, duplicate IDs, full context equality, and alias-aware comparison. It performs no supplier calls and opens SQLite read-only.

## Bounded follow-up template

`work/prepare_membership_stage.py` generated `work/three-product-membership-stage-template.json` from `docs/evidence-inventory-discovery.json` and its exact saved specification bodies:

| Product | Single-filter pages | Reusable completed pages | New specification read | New initial requests |
|---|---:|---:|---:|---:|
| Business Cards | 27 | 0 | 1 | 28 |
| Club Flyers | 33 | 0 | 1 | 34 |
| Roll Labels | 95 | 4 | 1 | 92 |
| Total | 155 | 4 | 3 | **154** |

Filters cover source-exposed size, paper, color and turnaround values, plus finish for Roll Labels. Shape/orientation list requests are omitted because the specifications bind them to each size. The four reused pages match the root-supplied `work/roll-domain-context-v2.json`; their evidence IDs and required context fingerprint remain in the template.

The template is **contextless, uninstalled and not ready to dispatch**. Its 154-attempt proposed budget covers only initial pages and three specification checks, with no retry or pagination-tail allowance. Current context must be bound and the stage preflighted before execution. Changed specifications require revising the domain plan. Further pages need an explicit bounded plan; intersections only narrow candidates until a full-selector lookup confirms the exact result.

Experimental accounting supplied by the coordinator is 391 prior probe attempts + 2 public bundle reads + 4 membership tests = **397 / 400**, leaving **3**. A reviewed finite membership harvest is a separately preflighted catalog stage, not an unreported extension of that experiment budget. This audit dispatches nothing and certifies no prices, rows, service combinations, final configuration totals, or catalog completion.
