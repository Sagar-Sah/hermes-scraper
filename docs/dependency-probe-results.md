# Saved dependency-probe results

Generated 2026-09-26T06:38:05.679605+00:00. Zero supplier/model requests by this report.
One read transaction; rerun after later probe batches. This report creates no cache certificate or global dependency proof.

## Attempts and outcomes

- `hermes-dependency-probes-20260926`: completed_with_errors; 391/398 job attempts + 2 prior public-bundle GETs = **393** recorded attempts. Error: `none`. Ledger statuses: `{'0': 45, '200': 336, '400': 7, '404': 3}`. Status 0/null means no completed HTTP response, not success.

## Latest product observations

| Product | Experiment | Outcome | Baseline formula |
|---|---|---|---|
| Business Cards | browser-controlled-business-cards-v3 | unresolved | not_captured |
| Club Flyers | browser-controlled-club-flyers-v3 | unresolved | not_captured |
| Roll Labels | browser-controlled-roll-labels-v4-initialized | unresolved | not_captured |

All saved phase captures: **0 matched, 0 mismatched, 3 unresolved**, plus 5 experiments without a captured baseline. Formula pass rate: **undefined (no eligible complete comparisons)**, denominator 0. Matched / (matched + mismatch) complete source-pinned controller and displayed-total captures; unresolved and missing captures excluded, separately reported.

## Unresolved and unsupported phases

- `browser-controlled-business-cards`: baseline not captured.
- `browser-controlled-business-cards-v2`: baseline: base_price_not_positive, comparison_not_established, unselected_required_value:color, unselected_required_value:paper, unselected_required_value:productId, unselected_required_value:quantity; manual_proof_1: control_unavailable; manual_proof_36: control_unavailable; delivery: choice_unavailable; rush: control_unavailable.
- `browser-controlled-club-flyers-v2`: baseline: base_price_not_positive, comparison_not_established, unselected_required_value:color, unselected_required_value:paper, unselected_required_value:productId, unselected_required_value:quantity, unselected_required_value:size; manual_proof_1: control_unavailable; manual_proof_36: control_unavailable.
- `browser-controlled-roll-labels-v2`: baseline: base_price_not_positive, comparison_not_established, unselected_required_value:color, unselected_required_value:finish, unselected_required_value:paper, unselected_required_value:productId, unselected_required_value:quantity, unselected_required_value:shape, unselected_required_value:size; manual_proof_1: control_unavailable; manual_proof_36: control_unavailable; delivery: choice_unavailable; rush: control_unavailable.
- `browser-controlled-business-cards-v3`: baseline not captured.
- `browser-controlled-roll-labels-v3`: baseline not captured.
- `browser-controlled-club-flyers-v3`: baseline not captured.
- `browser-controlled-roll-labels-v4-initialized`: baseline not captured.

Historical failed HTTP result entries (including earlier retries): `{'network_error': 11, 'http_error': 10}`. JSON preserves every result, formula mismatch/unresolved reason, and evidence reference.

## Route observations

Quantity counts count distinct top-level `items[].quantity` values; zero does not establish an empty valid domain. Routes aggregate different request filters; body differences alone are not dependency findings.

| Method/path | Observations | Distinct full JSON bodies | Quantity counts observed |
|---|---:|---:|---|
| GET `/service/rest/v1/product/search` | 4 | 1 | [0] |
| GET `/service/rest/v1/products` | 13 | 12 | [0] |
| GET `/service/rest/v1/products/100125944/options` | 1 | 1 | [0] |
| GET `/service/rest/v1/products/100125944/options/101871186/prices` | 1 | 1 | [27] |
| GET `/service/rest/v1/products/100125944/options/103225753/prices` | 2 | 2 | [1, 4] |
| GET `/service/rest/v1/products/100125944/prices` | 1 | 1 | [26] |
| GET `/service/rest/v1/products/100134417/options` | 1 | 1 | [0] |
| GET `/service/rest/v1/products/100134417/options/101210753/prices` | 2 | 2 | [1, 15] |
| GET `/service/rest/v1/products/100134417/options/101210754/prices` | 2 | 2 | [1, 15] |
| GET `/service/rest/v1/products/100134417/options/101210755/prices` | 2 | 2 | [1, 15] |
| GET `/service/rest/v1/products/100134417/options/101877578/prices` | 2 | 2 | [1, 27] |
| GET `/service/rest/v1/products/100134417/options/101945666/prices` | 2 | 2 | [1, 27] |
| GET `/service/rest/v1/products/100134417/prices` | 1 | 1 | [15] |
| GET `/service/rest/v1/products/options/quantities` | 1 | 1 | [0] |
| GET `/service/rest/v1/products/quantities` | 1 | 1 | [15] |
| GET `/service/rest/v1/settings/product/specifications` | 7 | 3 | [0] |
| POST `/service/rest/v1/product/prices` | 1 | 1 | [1] |
| POST `/service/rest/v1/rules/turnarounds` | 1 | 1 | [0] |

Paired dependency outcomes: `{}`. A pair requires two complete matching formula captures, pinned loaded source, exactly one changed selection with no other context change, and two distinct fresh uncached base-table reads. Multiple changed selections are confounded. Equal bodies prove only that recorded pair and route, never option-price independence or reusable caching.

Rerun from `backend`:

```sh
python -m crawler.dependency_probe_report --db /absolute/path/atlas.db --objects-root /absolute/path/atlas-data --output-dir ../docs
```
