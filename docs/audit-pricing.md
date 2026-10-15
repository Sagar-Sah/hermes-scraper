# Pricing and service-context audit — 2026-09-26

This audit used saved observations and local fixtures only. No supplier request,
checkout action, runtime credential read, or live process restart was performed.

## Confirmed findings

### P1 — two proof selections shared one recorded configuration

The original browser `make_record` omitted service selections even though DOM
evidence retained them. `engine.row_identity` consequently had no service value
to distinguish these observations of Business Cards in run
`36c1c4b084664d36b7b32dbca7cf8f08`:

| Evidence | Subtotal | Observed service selection |
| --- | --- | --- |
| `dd9ecbbe1837416ca0f8373cd63f3548` | $5.60 | `proofType=31` (Instantly Processed), `fileDeliveryForIop=5` |
| `559ba535a9af4e6d93186c0df15786bc` | $8.60 | `proofType=100` (Manually Processed), delivery group absent |

Both have quantity 50, size 101, paper 10, color 1, orientation 1, coating 5,
roundedCorners empty, spotcoating empty, raisedprint empty and production 1.
The saved row `e8b99d5c52a1e535cf3b3971139cfc5e3ecf4c0b4e106d4de3d25df171a48213`
was $5.60; its original evidence `7e22e5ac71b345d8858dfde37e3f2943` predates the
service diagnostics. Its actual service selection remains unknown.

Base 100000009 evidence `47ce612560a342ffbd58702b19459b5b` and
`96267ace568e4b9d9478037250c281df` have identical raw and semantic hashes. The
captured manual-proof option 101857030 remains $3.00 in both
`d519647ec3b24370b5c59967ceeffc2d` and `c877294d0efa4067b0337ca6fc2ccb07`.
Catalog `60edd4d3f9de4ed5a529bd5ed3f885da` names it `MANUAL_SOFT_PROOF`.
This supports a service-context collision rather than changed base prices. It
does **not** prove a general mapping from DOM value 100 to API option type 1 or
authorize adding a universal $3 fee.

### P1 — bulk samples and verifier omitted service equality

The browser's old request-capture context and `branch_fingerprint` omitted
services. Equal anchor/boundary totals could therefore pass without proving
equal service selections. The pure `compare_quote` helper also returned matched
for otherwise identical rows carrying different raw proof selections.

At the read-only audit snapshot, all 3,347 saved `source_table` rows were pending;
none contained explicit service context in their row. No differing service
selection was found between their linked sample DOM observations. This is a
confirmed missing guard, **not** a demonstrated incorrect subtotal or a claim
that these rows had been verified.

### P2 — display labels split equivalent quantity curves

`commerce.analyze_price_breaks` and verification comparisons used full option
objects, including labels. Saved BC corner 2 observations
`b470f18ccd9a47b68b5b09f6a51a0437` (quantity 100, $11.54, label +$3.00) and
`d79bc068702743919dc2227142859d44` (quantity 250, $14.38, label +$5.00) have the
same raw product/production selections. Label-based grouping split their price
curve. A label change is not a raw configuration change; a service change is.

### P2 — negative display text was parsed as a positive amount

Local regression inputs `-$5.60` and `($5.60)` both produced positive 5.60 because
`browser.observed_amount` extracted digits without their sign. This was
fixture-proven; no saved supplier observation was found with a negative total.
The parser now rejects negative/accounting-style text instead of changing its
meaning. Ordinary positive subtotals and descriptive parentheses still parse.

## Repair contract

- New browser observations carry `record_schema_version=2`, preserved raw
  diagnostics, an explicit `service_context_observed` marker, and
  `pricing_context.schema_version=2` with a canonical service signature.
- The canonicalizer records raw selected IDs and selected/absent/unselected/
  unknown/ambiguous status for the two inspected service groups. Labels,
  visibility and disabled presentation are not identity. Explicit observed
  absence means only that the group was absent, never that its fee is zero.
- Missing legacy diagnostics remain unknown. Stored row bytes and old evidence
  are preserved. Legacy unscoped rows are retained as history and excluded from
  current price analysis and verification; a fresh scoped observation is needed.
- Price captures must have the same bound request-start service signature as the
  final DOM state. Stability, both DOM sample fingerprints and table rebinding
  include that signature. Unknown, unselected, ambiguous or changed services
  force direct-quote fallback. Explicitly observed absence still supports Roll
  bulk. A price-table prefetch before service metadata is available can now
  conservatively fall back; metadata is never supplied by guessing.
  Derived rows retain the bound request signatures and evidence IDs in
  `table_derivation.binding`; response context hashes alone are not treated as
  readable historical provenance.
- Both exact DOM subtotal gates remain. No manual-proof charge, hidden service
  charge or other arithmetic was added. Derived rows remain pending and retain
  their service scope. Different subtotal under the **same** v2 identity must
  still stop as a genuine unresolved conflict.
- Verification and quantity analysis compare raw option values and production
  selection IDs, while retaining service/pricing/tax/account context. Labels and
  source base-table provenance do not independently create a new price context.

Implementation entry points: `crawler/service_context.py`, browser `make_record`,
request capture, `branch_fingerprint` and stability; `browser_tables` binding;
commerce semantic comparisons; verification helpers. Engine/history/journal
migration is coordinated separately so old branch checkpoints cannot skip the
required scoped observations.

## Validation and limits

Pure regressions cover legacy versus observed-absent state; checked/unselected/
ambiguous groups; presentation-only differences; service-aware row identity;
wrong/unknown/newer-conflicting request contexts; verifier mismatch; and raw-value
price-break grouping. Chromium fixtures exercise stable selected services,
equal-price service changes at a quantity boundary, unselected service groups,
and Roll with explicitly absent groups.

The focused pure suite passed 217 tests. Ten selected Chromium checks passed;
after retaining readable request binding, five service/square-retention checks
passed again. These are local regression results, not a live supplier reharvest.
`commerce.price_comparison_context` supplies the same canonical grouping to
analysis and the API/chart projection; unresolved scopes return no curve context.

The audit found no new confirmed credential-disclosure defect in the provider
adapter: provider failures use generic errors and do not log credential bodies.
No paid provider call was made by this audit. None of these checks establishes
whole-product completeness or cart verification.
