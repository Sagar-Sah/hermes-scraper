# Offline evidence math audit

Frozen database: `/Users/tl/Documents/Codex/2026-09-25/i-w/work/planner-evidence-snapshot/evidence.db`

Examined **6,019 DOM quotes**, including **1,815** with explicitly known service context.

Results: **0 pass**, **0 mismatch**, **6,019 unresolved**.

Pass rate is **undefined (zero fully resolvable quotes)**. The denominator is pass + mismatch (0); unresolved quotes are excluded from that denominator and disclosed separately. Resolution coverage across every examined quote is 0.00000000.

A partial known-component sum equals the displayed total in 4,553 of 5,335 quotes with an available partial sum. **These are not passes:** an unknown component may not be assumed free or included.

## Unresolved evidence requirements

| Reason | Occurrences |
| --- | ---: |
| `exact_component_price_missing` | 4,825 |
| `service_context_unknown_or_ambiguous` | 4,204 |
| `row_linked_capture_provenance_missing` | 2,364 |
| `selected_option_mapping_unresolved` | 1,580 |
| `included_option_pricing_unproven` | 1,539 |
| `file_delivery_component_contract_missing` | 1,196 |
| `query_orientation_not_in_dom` | 713 |
| `capture_missing` | 684 |

The JSON companion includes every quote, mismatch/unresolved reason, component status, residual, source evidence ID and linked saved-row ID. Legacy unknown service selections are not inferred from later quotes.

The proof31/file-delivery5 selection and proof100/absent delivery are distinct. DOM proof100 is not assumed to equal catalog type1; manual soft proof and manual no-proof choices cannot be selected from a $3 residual. A missing price endpoint or quantityDependentPrice=false does not prove zero cost.

No supplier/model requests, live mutations, fee guesses, cart verification or source-table verification upgrades were performed.
