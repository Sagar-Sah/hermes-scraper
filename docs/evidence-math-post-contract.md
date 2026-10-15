# Offline evidence math audit

Frozen database: `/Users/tl/Documents/Codex/2026-09-25/i-w/work/planner-evidence-snapshot/evidence.db`

Phase: **post-contract audit** using saved orderForm.js SHA-256 `fd26a9c72ba1a162d5d12aa0f0f845dfed7d1a136f276440f1b5b06a465a1d8b` and the reviewed shared bundle. Public bundles were acquired in a separate bounded phase; this audit makes zero requests and no supplier quote calls.

Examined **6,019 DOM quotes**, including **1,815** with explicitly known service context.

Results: **0 pass**, **0 mismatch**, **6,019 unresolved**.

Pass rate is **undefined (zero fully resolvable quotes)**. The denominator is pass + mismatch (0); unresolved quotes are excluded from that denominator and disclosed separately. Resolution coverage across every examined quote is 0.00000000.

A partial known-component sum equals the displayed total in 4,553 of 5,335 quotes with an available partial sum. **These are not passes:** an unknown component may not be assumed free or included.

## Unresolved evidence requirements

| Reason | Occurrences |
| --- | ---: |
| `discount_override_context_not_recorded_or_unsupported` | 6,019 |
| `service_context_unknown_or_ambiguous` | 4,204 |
| `hidden_priced_option_state_not_recorded` | 3,337 |
| `proof_subselection_not_recorded` | 3,135 |
| `row_linked_capture_provenance_missing` | 2,364 |
| `query_orientation_not_in_dom` | 713 |
| `capture_missing` | 684 |
| `selected_option_mapping_unresolved` | 498 |

The JSON companion includes every quote, mismatch/unresolved reason, component status, residual, source evidence ID and linked saved-row ID. Legacy unknown service selections are not inferred from later quotes.

The proof31/file-delivery5 selection and proof100/absent delivery are distinct. DOM proof100 is not assumed to equal catalog type1; manual soft proof and manual no-proof choices cannot be selected from a $3 residual. A missing price endpoint or quantityDependentPrice=false does not prove zero cost.

No supplier/model requests, live mutations, fee guesses, cart verification or source-table verification upgrades were performed.
