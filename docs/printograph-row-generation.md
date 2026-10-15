# Pure generation from bound source tables

`crawler.printograph_rows` is a separate pure module. It does not change the
existing adapters, storage, identity rules, transport or verification states.
Its output is deliberately a `printograph_calculated_v1` candidate schema for
explicit integration; it does not pretend historical rows contain the newly
required complete pricing state.

One call holds a complete base/option/service selection fixed and enumerates
finite quantities supported by source availability. It never enumerates an
option Cartesian product. The caller must have resolved the joint selection;
the scope binds its source evidence and all hidden pricing inputs. Generated
rows still require independent configuration quotes.

```python
from crawler.printograph_rows import (
    bind_table_scope, source_service_signature, generate_table_rows,
    compare_direct_quote,
)

scope = bind_table_scope(
    complete_capture,
    request_context={
        "origin": "https://www.printograph.com",
        "session_scope": opaque_session_scope,
        "source_epoch": resolved_source_epoch,
        "store_type": "broker",
        "currency": "USD",
        "service_signature": source_service_signature(complete_capture),
    },
    selection_evidence_ids=[complete_selection_evidence_id],
)
```

Bind `scope["request_context"]` **at request start** to new source observations.
Do not populate missing historical context retrospectively. Every observation
has `request` (`method`, source-relative `path`, exact scalar `params`, optional
`body`, and `context`), `status:200`, `complete:true`, `response_id`, and parsed
`data`, matching `route_dependencies` conventions. No credentials belong in
these metadata.

```python
result = generate_table_rows(
    complete_capture,
    lookup=lookup_observation,
    catalog=catalog_observation,
    base_table=base_price_observation,
    option_tables={resolved_catalog_option_id: option_price_observation},
    quantity_support=quantity_observation,
    option_quantity_support=option_quantity_observation,
    scope=scope,
    product_url=product_url,
    quantities=[50, 100, 250],  # Omit to examine the source quantity domain.
)
```

The unique lookup must match the exact base specifications and turnaround. All
captures must share the origin/session/source epoch/store/currency/full service
and pricing selection scope. Selected options resolve by type **and group** in
that base's catalog, then use the catalog record ID for their price endpoint.
Unknown account/order queries, wrong design-service flags, partial responses,
missing context and changed source hashes fail closed.

The module distinguishes:

- `rows`: exact-quantity computed candidates with component amounts, source
  evidence, raw table amounts, source rounding, explicit non-additive fields,
  coupon accounting and full pricing context. Every row remains `pending`.
- `excluded_quantities`: source availability explicitly excludes that quantity
  or turnaround for the product or a selected option.
- `unresolved_quantities`: availability did not exclude the quantity, but a
  component price, support rule or coupon applicability evidence is missing.

A null/missing price never silently removes a valid tier. Single-row option
responses never become unproven constants across other quantities. Ordinary
selected services are explicit: instant 31, manual mode 100 with secondary
1/36/37/38, or design mode 101 with its selected package. File delivery remains
part of the context although the reviewed product-subtotal formula has no
additive file-delivery term. This does not establish shipping or tax totals.

An actual empty captured coupon object can support the same scoped table run.
Every other coupon state requires `scope["coupon_quantity_evidence"]` entries
keyed by exact quantity, each with `selection_scope`, `evidence_id`, and the
explicit `coupon` state. A current-quote zero result alone is insufficient.

Exact finite response-equivalence certificates from `route_dependencies` may
authorize a request named in `scope["expected_requests"]`, but only when both
complete request keys and the identical response hash occur in the supplied
certificate. They cannot drop context dimensions, cover untested values,
cross account/session boundaries or suppress unsupported price parameters.

`expected_comparison_key` returns no key for incomplete legacy context.
`compare_direct_quote` requires a new complete controller capture and an
independent displayed total whose source arithmetic reconciles. It reports a
match, context mismatch, price mismatch or unresolved state; it never mutates
the calculated row or marks it cart verified. Production durations stay
`requires_configuration_quote`.

Validation: 25 focused generator tests passed. Saved public availability proves
raised-print type 135 supports quantity 50 for the sampled Business Cards base
and starts at 100 for the sampled Club Flyers base. Tests preserve those actual
domains; synthetic component prices and request-start metadata are explicitly
identified as fixtures, not supplier observations. Tests also cover manual
proof 1 versus 36, wind position, source binary rounding, partial/null prices,
coupon quantity binding, exact-member certificates and conflicting contexts.
