# Product membership and cross-option discovery

This analysis uses the saved frontend bundles and local evidence only. It made no supplier or model requests. The new bulk-listing results described below were reported by the coordinating agent's separate live probe; they are not additional calls or independent validations by this module.

## Membership inversion

The reported `/products?productType=...&limit=1000&offset=0&requestValues=true` responses contain 180 Business Cards IDs, 304 Club Flyers IDs and 459 Roll Labels IDs. The reported default response cap is 25, and an offset of 25 advances the response. These observations establish a promising listing mechanism. They do not, by themselves, establish that every selector filter behaves as an independent predicate whose intersections reproduce ordinary full queries.

Using the frozen specification axes, an inverted plan needs **173 initial requests**: one universe query and one query for every finite selector value per product (30 Business Cards, 36 Club Flyers, 107 Roll Labels), followed by any pagination. These are discovery-query estimates, not a complete extraction budget or a source guarantee. Roll size/orientation/shape values retain their specification-record bindings; they must never be expanded as independent geometry axes.

A safe implementation should:

1. Save every page with exact query, method, source/session/pricing scope and evidence ID. Advance offset by the **actual number returned**, not the requested limit, and require an empty terminal page or a separately validated equivalent terminal contract. This avoids silently skipping records when the server caps page size. Detect duplicate IDs, inconsistent totals, overlap and source changes.
2. Build `base_id → observed values per selector` only from complete, same-context membership queries. An ID outside the captured universe, uncovered selector, missing page or ambiguous scalar membership is unresolved. Keep turnaround memberships distinct because more than one mode may map to one base ID.
3. Produce candidate selector tuples from each ID's memberships, preserving bound geometry. Do not declare those tuples valid through set intersection alone.
4. Validate each emitted tuple with the ordinary complete-selector `/products` query and require the exact predicted single ID. A mismatch invalidates that inference and requires direct discovery or a revised source contract. Sampling demonstrates only the sampled tuples; it does not establish global AND semantics for untested tuples.
5. Deduplicate accepted full-query tuples and record which valid base ID and context each represents before creating pricing manifests. A scalar-membership mapping can reduce candidate lookups substantially when it produces one tuple per ID, but that reduction must be measured from actual membership responses.

No inverted membership engine has been enabled by this change. The existing 4,077-tuple discovery artifact remains explicitly unvalidated and historical; it is not a claim that all those requests are necessary after the new listing capability is verified.

## Actual cross-option rules

The static source configuration is not an independent list of option axes. The classic frontend applies both predicates and state corrections. Important examples from the pinned source are:

| Source | Offset | Relevant behavior |
| --- | ---: | --- |
| `displayOptionsSettings` literal | 123052 | Hides coating 6/40 for color 1; constrains spot choices by color/coating; hides several back-side raised choices for color 1; restricts Roll finishes by stock. |
| `displayFieldSettings` literal | 126797 | Hides entire finishing fields for specified stocks. Foil color is hidden for absent raised printing and raised types 135/136/137. |
| Shared validation array `Al`/`Pl` | 1342258 | 39 ordered corrections, including stock/color and finish corrections, Rush restrictions, coating defaults, foil removal, and the required foil default 141 for raised types 138/139/140. |
| `specChange` | 1420856 | Coating and spot selections clear raised printing/foil; raised printing clears coating/spot. Color-specific rules further couple coating and spot. Selecting spot/raised can change proof 31 to proof 100 and file delivery 5. |
| `displayOptions` | 1446199 | Applies static predicates and exceptions, turnaround filtering, and quantity-dependent price-tier visibility. A special paper 10/color 3/coating 40 override prevents treating the static spot matrix as the whole rule. |
| `displaySpecs` | 1600416 | Hides entire option controls when their captured price tables lack the current quantity. It also hides coating on stocks 3/4/5 below quantity 250. |
| `rushCondition` | 1535009 | Combines stock exclusions, base quantity modes, each selected option's supported quantity/modes and computed turnaround-rule availability. Club Flyers stock 4 is an additional Rush exclusion. |
| `getTurnaround` | 227516 | Calls the combined rule service using the constructed selected-configuration payload. |
| `turnaroundParam` in chunk-common | 388971 | Constructs one quantity-specific payload containing selected option IDs, product specifications and other specification state; requests both turnaround IDs 1 and 3. |
| `processTurnaround` in chunk-common | 756731 | Reads source ranges and can restore Regular when Rush is absent. |

The product-specific validation arrays for product types 1, 27 and 36 are empty in this bundle. That does not remove the shared rules above.

Store variant selection is explicit at offset 1240747: numeric store **100** selects one field configuration; other stores select the other. The first omits spot coating for Business Cards/Club Flyers, while the second includes it. A `currentstoretype` label alone does not establish this numeric selector.

`paperFinish` is a required singleton choice, ID 1 labeled Velvet, when its stock visibility predicate permits it. It is not a new free Cartesian axis. Roll wind position is required and has its own catalog/support domain.

The source price cache key functions in chunk-common at 752948 and 757592 include price group and whether proof type is 101 when the relevant cache mode is enabled. Account, price-group and service context therefore cannot be dropped from request identity merely because two responses happen to match.

## Pure validator

`crawler.printograph_constraints` implements a bounded validator for the reviewed bundle hashes. `extract_rule_contract` extracts the static option predicates, field predicates, ordered corrections, store variants and paper-finish singleton as data. It does not execute the JavaScript. Unknown bundle hashes fail closed.

`validate_configuration(product_type, selection, contract=..., lookup=..., catalog=..., base_support=..., option_support=..., option_prices=..., context=..., turnaround_rule=...)` returns:

- `invalid` when a known rule excludes the requested state or changes its selected fields. Corrections are returned explicitly in `changes` and `resulting_selection`; no price is silently assigned to a corrected selection.
- `unresolved` for missing or unknown state, unsupported services/artwork/admin/edit/mailing/custom-dimension context, missing numeric store variant, stale lookup context, ambiguous action-dependent coating/spot states, incomplete support, or missing combined turnaround evidence.
- `supported` only within the stated classic, no-upload, standard-service scope after the exact full-selector lookup, bound catalog/support/price-tier domains and combined turnaround rule have all passed. This is not a price-composition certificate or proof that every catalog combination was discovered.

Inputs use raw source field names and IDs. The context must explicitly provide the numeric `store_id`, store/price-group/session/source/service identity, ordinary no-upload flags and a `domains_bound=True` assertion supplied by the evidence-binding layer. The supported service scope is file delivery 5 with proof 31 or 100 and no design service; other services remain unresolved. Catalog/support/price inputs must already be bound to that exact base and context; the validator cannot recover missing request provenance from response bodies alone.

The combined turnaround result must carry its evidence ID and `configuration_fingerprint(...)` of the complete selection, service/context and base ID. Reusing a same-shaped turnaround response from another option selection does not satisfy that gate. Selected options' quantity and production-mode domains are intersected. Foil supporting 2500 in Rush does not make a selected raised-print option support that quantity/mode.

Neither `quantityDependentPrice=false` nor a single returned price row is used to expand prices across quantities. A single-tier visibility ambiguity remains unresolved; multi-tier absence is an explicit unsupported selected quantity. Full totals, additive fee composition, shipping and independent quote verification remain outside this validator.

## Discovery ordering consequence

The source uses option **price-tier presence** as part of control availability. Catalog IDs and individual option quantity support alone therefore cannot prove the full visible finite option domain. A strict promise of complete option discovery before reading any price table is not established by this frontend. The planner must either retain that gate as unresolved or explicitly budget price-table reads as domain evidence after a published preflight, before publishing calculated rows. This module does not change the coordinator's existing no-quote-before-preflight rule.

A practical data-driven enumerator should enumerate each validated base's actual catalog options, prune static exclusions, compute explicit coupled corrections, intersect selected quantity/mode domains, and validate the combined rule context. It should deduplicate stable resulting states instead of multiplying every dropdown option independently. Unknown action-dependent or service-dependent branches require normal source validation/browser fallback. Price composition should be certified separately against direct quotes.

Validation: 26 focused tests pass, using a compact static-rule fixture extracted from the actual saved bundles. Tests cover all three product types, actual cross-option and service corrections, stock/finish restrictions, store variants, Velvet singleton state, selected-option support intersections, exact turnaround binding and unsupported context. No live source calls were made.
