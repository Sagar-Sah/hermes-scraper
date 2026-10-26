# Collect a campaign without an AI agent

Hermes's automatic supplier collection uses software profiles, not an AI provider.
The Hermes service and this computer must remain running. You can use another app
while its own supplier browser works.

1. Open Hermes. Choose **Sources & AI** and use the supplier's **Open browser**
   control. Sign in in the window Hermes opens. A login in another Chrome profile
   does not sign Hermes in.
2. Choose **Campaigns** and select the campaign. Under **Automatic supplier
   collection**, choose **Check remaining products**. Read the supported tasks and
   **Needs attention** list. Planning reads saved evidence; it does not request prices.
3. Choose **Start product tasks** once. Hermes saves its progress, collects supported
   source choices and costs, and gives continuing products turns between batches.
   Different suppliers can work independently while sharing their own request limits.
4. Leave Hermes running. **Refresh automatic collection** shows current tasks.
   **Saved collection steps** means durable progress, not finished products. A task
   can stop with an explicit reason while other eligible work continues.
5. If a supplier requires sign-in, use its Hermes browser again. If a task reports
   unsupported controls, missing evidence or an uncertain quote, retain that blocker.
   Repeatedly pressing Start or changing the saved data does not resolve it.
6. Use **Pause collection** to stop work, and **Resume queued work** for work that is
   queued. Pausing interrupts active work; an uncertain in-flight selection is held
   instead of automatically sent again. Saved verified selections remain retained.

The 99-product setting is a queue capacity, not a promise of 99 simultaneous browser
tabs or permission to exceed a supplier's request limit. Supported profiles must
exist for the source and product type. Custom dimensions, area pricing and fixed
package quantities need their own exact source contracts.

Hermes records **supplier costs**. It does not apply PrintItPrints retail markup or
shipping. Quote samples and partial exports are not WooCommerce imports. Product
finalization and deployment remain separate from automatic collection; preserve
the coverage and source evidence supplied with each result.

## Reusing data and checking completion

Hermes inspects native form data and captured network responses programmatically.
It uses ordinary form controls when a retained selection still needs validation.
Staggered Cut Flyers already reuses full quantity price tables within a branch.
For Cardboards and Acrylic Boards, base-price tables are now grouped by the
supplier's exact price-request inputs and process type, with source and service
scope retained. A price table can cover multiple option IDs without merging their
separate configurations. Availability and production keep their own checks.

When the required service choices are already selected, one atomic inspection
replaces six repeated service inspections. This is a reduction in inspection
roundtrips, not a measured sixfold improvement in total collection time. Neither
optimization removes the entire visible-control traversal.

An expected row count is exact only after the valid option domain is known.
Multiplying dropdown lengths works for independent finite choices; conditional
choices require their dependency rules. Custom dimensions may have no finite
row count. Equal sampled totals do not prove that a field never affects price.

Track unique configuration identities as well as counts. A matching count can
still contain a duplicate and omit another configuration. Completion requires
every expected valid identity to have the required evidence, with no unexplored
branches or unresolved blockers. Reused pricing evidence keeps its origin and
dependency hashes; it does not turn partial coverage into a finalized product.

For a new installation, follow the main README's setup instructions. Keep existing
data and browser profiles when updating. The software release archive deliberately
does not include accounts, passwords, browser sessions or the local campaign database.

Use **Update Hermes** in the main header to check and install the tested `main`
release. The managed launcher waits for finite work, pauses collection, saves a
private database/configuration backup and restarts its own backend after a
successful update/build. Review queued and held tasks before resuming; an update
never retries a saved uncertain supplier selection. The panel shows retained
progress and failures if a restart interrupts the connection.

An older service started directly with Uvicorn needs a one-time pause, safe stop
and transition to `./scripts/start-local.sh` using the same configuration and data
directory. The launcher preserves `.env` settings and exported overrides, installs
changed locked dependencies and rebuilds changed assets. See the README for update
requirements and holds. The dashboard build stamp and health response identify
which code is loaded and when a page reload is needed.

## Current verification limits

Automatic continuation has been exercised against Printograph's Staggered Cut
Flyers, Acrylic Boards and Cardboards. Their saved steps are still partial
coverage; the worker does not mark these entire products finalized.

The 4over package profile has offline browser regression coverage, but its latest
live checks still hold selections when turnaround controls are not ready or the
rendered total cannot be reconciled. Treat those as unresolved integration checks,
not confirmed prices or proof that the product is unavailable. Failed or uncertain
selections are preserved and are not silently submitted again.

Other product types, including open size or set-count domains, can remain under
**Needs attention** until a compatible source profile and evidence are available.
This release does not promise automatic collection from every website or every
product type.
