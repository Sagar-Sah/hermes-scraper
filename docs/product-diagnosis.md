# Product diagnosis and field policies

Use **Product diagnosis** in Hermes before starting a product repair. Analysis
and field review do not start a harvest. The separate run controls queue work
after checking every selected product. This page does not publish a product.

For a new supplier such as WSDisplay, add its public website under **Add
Product Supplier**, then use **Open [supplier] browser** and sign in in that
window. A login in ordinary Chrome, Codex's browser, or another browser profile
does not sign in Hermes. WSDisplay's public ONE CHOICE 10 ft. Circle Hanging
Banner page displays its description but explicitly hides price and purchase
controls until login; Hermes now reports supplier sign-in needed for this state
instead of waiting 30 seconds for controls that cannot appear. Leave the
optional login-page URL blank when the site's Login link itself opens a URL
with query parameters; click Login in the opened supplier browser.

After sign-in, revisit the product URL in that same supplier browser, refresh
the diagnosis and confirm that the price and required options are visible.
**Check sign-in** may say “Check sign-in” when a site exposes no explicit
account indicator; the product page's visible pricing is the stronger check.
If fields are unsupported or prices remain hidden, keep the product in review.
Adding a supplier is not a universal site adapter or permission to invent a
price. Hermes runs without an AI provider for supported source controls and
exact quotes; an AI provider is only needed to interpret natural-language
extraction instructions. A new site's custom calculator may still need an
adapter, account access or a source-authorized pricing feed.

1. Open a supplier browser from the login cards and sign in there. These are
   duplicate pages in the browser profiles owned by Hermes. Cookies are retained
   by the browser; Hermes does not export them or request passwords in the app.
   An ordinary Chrome or Codex browser login is a separate session.
   Use **Add Product Supplier** to save another supplier's name, public HTTPS
   website, and optional login page. The login page must be on the same supplier
   domain (with or without `www`). Saving a supplier enables its retained browser
   session and diagnosis; it does not create a pricing adapter automatically.
2. Enter a public HTTPS product URL. Hermes reads visible control labels,
   available choices, numeric constraints, and product content. A diagnosis is
   one observed page state, not an exhaustive conditional option inventory.
3. Keep the fields that should appear in the output checked. An unchecked
   configurable field needs a selected fixed choice. It remains part of pricing,
   availability, and production validation. Unchecked content is omitted.
4. Save the field policy. Refreshing a changed source page increments the
   diagnosis revision and clears the old selection for review.
5. Optionally choose an existing product/run under **Check saved product rows**.
   Saved quotes and table-generated matrix candidates are separate choices.
   This review uses local evidence and makes no supplier requests.

### B2Sign custom calculators

The B2Sign diagnosis reads the visible quote panel, including its custom option
menus, button choices, static attributes, quantity, turnaround, shipping, and
Email Proof control. It opens each observed menu once to read that menu's own
choices, closes menus it opened, and checks that the selected value did not
change. It does not select a different product option or add anything to a cart.
Job Name and other free-text account or order inputs are excluded.

Some custom controls expose only display labels. Hermes records that limitation
and lets you review and save those field choices, but those labels are not
supplier pricing IDs. The generic browser harvester cannot use that diagnosis
until a supplier adapter supports those controls. Quantity remains an open
numeric field when the source does not expose minimum, maximum, or step bounds.
The visible choices are not proof that every cross-product combination exists.

### Pole Banner Set pricing check

The saved Pole Banner Set policy offers **Check pricing breaks** for quantities
1, 10, 100, and 101. This bounded check uses the retained supplier session and a
fresh duplicate product page for each quantity. It captures the calculator's
quote response and compares the selected options, quantity, production choice,
unit price, and subtotal with the displayed page. Email Proof stays unchecked.

If the response exposes quantity tiers, Hermes displays those tiers. A response
containing only the requested quantity is not evidence that other price breaks
do not exist. Production thresholds are reported separately from price breaks.
The result covers only the sampled configuration and quantities; shipping and
tax remain outside this check. It neither completes the product catalog nor
makes rows eligible for publication.

### B2Sign open-size quote scopes

For a B2Sign width-and-height calculator with no finite size list, the harvest
API accepts `explicit_sizes`, for example `{"_size":["12x12","24x36"]}`,
alongside explicit `quantities`. Hermes checks positive numeric dimensions and
the source's visible min/max rules before planning a quote. Conditional bounds
without the necessary context stay unresolved. The preflight count is the
number of distinct supplier quote requests after duplicate source states are
removed. This is a bounded price sample; an open size domain cannot be marked
complete from those samples.

The check requires the supported source layout and exact saved field policy.
It can reopen a closed diagnosis in a retained session, but revalidates the live
source before accepting prices. A changed layout, expired session, conflicting
selection, or unconfirmed subtotal stops the check with a report for review.

## Publication gate

The review produces an immutable report and a row-by-row reason file. It keeps
three distinctions explicit:

- Rows outside a selected fixed choice are omitted from this output.
- Rows with complete, source-bound evidence that the configuration or production
  is invalid are excluded.
- Unknown availability, incomplete production evidence, unmatched fields,
  conflicting product context, or unverified final prices are held for review.

Only eligible rows enter a separate finalized CSV. A report with zero eligible
rows has no finalized CSV download. Original captures, candidate files, and
stored records remain unchanged. Each report retains its exact field policy.

UV and Spot UV are not globally banned by their names. A combination must be
checked against the supplier's product-specific rules and production evidence.
Current Printograph matrix candidates are not automatically promoted to final
data. Their component arithmetic alone does not establish combined availability,
complete production timing, or a reconciled source subtotal.

## API integration for a future run

The existing run API accepts optional `diagnosis_id` and `diagnosis_revision`.
When supplied, it requires the exact saved policy and one matching product URL,
compiles fixed choices into browser selection filters, and stores an immutable
policy snapshot with the run. Every resulting quote must retain those actual
fixed values. Unsupported fixed controls, missing bindings, or conflicting
filters stop the operation instead of silently choosing another value.

This binding currently supports ordinary single-choice browser controls and
numeric controls. Fixed checkbox sets and multiple-select controls can be saved
for offline policy review but are rejected by the browser-run compiler until
their source-specific traversal contract is implemented. A preserved fixed
selection is still a candidate quote; it does not bypass the publication gate.

## Product tabs and running a batch

Each product tab keeps its own field choices. You can review products individually
or leave their fields included. **Run this product** queues only that tab;
**Run all as configured** checks every product in the displayed workspace.
Unsaved choices are included in the preflight, so switching tabs cannot silently
replace them with defaults.

If any product has a stale diagnosis, unsupported controls, missing fixed choices,
or an unspecified numeric scope, the batch shows blockers and starts nothing.
When all products pass, Hermes creates linked runs in one transaction. Each run
keeps an immutable field policy, and all use the existing shared engine and rate
limiter. Retrying the same launch does not create duplicate runs. A browser-based
preflight does not invent an exact request count or ETA for conditional controls.

Hermes executes queued runs without an active model conversation. Its local
service and computer must remain running; expired supplier sessions and
unsupported calculators still need attention. Completing a harvest does not
bypass the separate evidence-based finalization gate or publish storefront data.
