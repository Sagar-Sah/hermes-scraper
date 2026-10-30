# Hermes · Product intelligence

The [Autonomous Product Companion guide](docs/AUTONOMOUS-COMPANION.md) explains
the monthly coordinator, 99-product queues, constrained Muse director, protected
staging connections and human-approved releases. Scheduling and AI reviews start
disabled; environment alignment and complete evidence are required before
supported staging repairs can run.

The [October 25 automatic collection update](UPDATE-2026-10-25.md) adds a resumable,
AI-free supplier worker and source-specific browser repairs. Its release notes
describe the verified progress and remaining live integration holds.

The [October 14 performance update](UPDATE-2026-10-14.md) reduces repeated waits on supported Printograph forms, speeds up DOM reads and exports, and exposes concurrent browser capacity, with reproducible local before/after benchmarks.

A self-hosted product extraction service and dashboard. Give it product URLs or product titles within a source site, capture configurations with source evidence, and export CSV/Parquet datasets. The architecture reuses complete source responses wherever the adapter supports them; ordinary browser extraction handles visible calculators.

This is the first implementation, with explicit coverage and verification states. It does not claim that Printograph's entire catalog can be harvested in ten minutes, that three million live supplier prices have been verified, or that every supplier exposes reusable price tables.

The [September 25 forensic audit](docs/forensic-audit.md) documents the live failures, their evidence, implemented repairs and remaining capability gaps. Browser records use service-scoped schema version 2, or version 3 for the modern Printograph profile. Historical records and example exports without the required scope remain evidence, not members of the corrected current dataset; default analysis and current exports exclude them. Re-observation is required rather than guessing their missing proof/file-delivery selections.

The [optimization status](docs/optimization-status.md) links the zero-request
repeat inventory, complete-component pricing audit, bounded dependency probes,
planner/harvester implementation and remaining cart-verification contracts.
Authenticated browser-session HTTP reads now support finite, preflighted table
collection. The [inventory-first results](docs/inventory-first-results.md) and
[fresh Club Flyers/Roll Labels collection](docs/club-roll-fresh-harvest.md) describe
the captured scope and calculated exports. Complete catalog discovery and
independent cart verification remain pending.

## Start locally

Requirements: Python 3.11+, Node.js 20+, and Git. From the Hermes project directory, run:

```sh
./scripts/start-local.sh
```

The launcher installs locked dependencies, prepares Playwright Chromium, builds the dashboard when needed, and starts Hermes with its update supervisor. It checks an occupied port before changing files and starts no second backend. A failed dependency install or build stops startup. Keep the existing project folder, `.env`, data directory and supplier profiles when updating an existing installation.

### Update from the dashboard

Choose **Update Hermes** in the main header, then **Check for updates**. If a checked release is available, choose **Install tested release**. Hermes waits for finite collection work to finish safely, pauses collection, saves a private database/configuration backup, updates the checked commit from this repository's `main`, rebuilds changed assets and reconnects. Collection stays paused; review queued and held tasks before resuming. The panel retains progress when closed and shows an interrupted or failed update on reopening.

Updates require a clean `main` checkout and a service started with `scripts/start-local.sh`. Local code changes, unowned source work, unknown Git history and an expired update check produce a visible hold. Your collections, supplier sessions and settings remain in the existing data directory. The footer and health endpoint identify the loaded UI and backend builds; **Reload dashboard** loads a newer browser bundle after restart.

For staging preview sign-in, open **Product Companion → Environment baseline and
release readiness → Staging preview protection**. Enter **Preview username** and
**Preview password**, then select **Verify staging write connection**. Saved
fields clear intentionally; check the connection status to confirm verification.
These are the staging preview credentials, separate from the connector token and
WordPress login. Leave blank fields unchanged to reuse saved credentials.

An older installation started directly with Uvicorn needs one initial transition: pause collection, wait for active work to finish, stop that Hermes process, update the software, and run `./scripts/start-local.sh` with its existing configuration. Subsequent updates use the dashboard. Advanced direct-Uvicorn development runs remain unmanaged and cannot install updates through the button.

The launcher uses the locked dependency versions and opens a normal browser on desktop systems so you can complete supplier sign-in. `ATLAS_BROWSER_HEADLESS=true` overrides that desktop default. By default the app keeps a dedicated browser profile and opens each product in another tab of that same session. The supplier can still expire or revoke its login.

Existing `ATLAS_DATA_DIR` in `.env` is preserved; an explicitly exported shell value takes precedence. Without either setting, the data directory is `./data` in this project. Set the local port in the shell, for example `ATLAS_PORT=18879 ./scripts/start-local.sh`; the launcher uses that same port for its existing-service check and Uvicorn. A port value in `.env` alone does not change the local launcher's CLI port.

Open [Hermes](http://127.0.0.1:18877). The API starts its background extraction engine in the same process and serves the built dashboard. SQLite is the default local database. Its database, response objects, row artifacts, exports, and automatically generated encryption key live in `data/`.

The [collection tray](docs/COLLECTION-DELIVERY.md) packages selected products into
one ZIP with a folder per product. Unfinished products require an explicit export
warning override. After confirming the ZIP was saved, choose which products to
retain as compact archives and which exported working copies to remove. Active,
changed or campaign-protected work remains protected from cleanup.

### See time spent on each product

Open a harvest's **Products** list, the upper-left **Your collection** package, or a campaign's **Automatic supplier collection** table. Each timed product or task shows cumulative active collection time, for example **1h 00m active**, with elapsed wall time and its current state. Open the clock breakdown for queued, waiting, paused and attention time.

Active time includes worker-owned source requests, supplier pacing and network waits. It excludes time merely queued, paused, or waiting between tasks. The shared on-screen clock stops advancing when its worker observation expires or the dashboard disconnects. Confirmed intervals survive pauses and restarts; unconfirmed downtime is not added to active time. Existing products with no timing history show **Active time not measured**, and partial histories show a measured lower bound. Per-run products and campaign tasks have separate scopes and may overlap; their times are not summed. Standalone diagnostic and pilot jobs that do not create Product rows remain unmeasured. Retained product archives keep a compact summary from the last durable timing checkpoint.

The registered modern Printograph worker now collects in bounded five-minute batches to reduce repeated browser startup and rediscovery. Other profiles retain their existing limits, supplier pacing and validation. Historical selection checks also use exact indexed lookups. Offline callback benchmarks confirm equivalent selection order with fewer activations; a live supplier-wide speedup is not established by those benchmarks.

For frontend development, run `npm run dev` from `frontend/` in a second terminal. It serves [the development dashboard](http://127.0.0.1:18878) and proxies API calls to port 18877. Set `ATLAS_API_URL` to use another API origin. Vite uses a strict port so it will not silently switch to an unexpected address.

In the dashboard:

1. Choose **New harvest**, then add product URLs or import a CSV of URLs. Quick-add buttons contain Business Cards, Club Flyers, and Roll Labels from Printograph.
2. Leave the adapter on **Automatic** to use normal browser capture for Printograph. The explicit Printograph price API adapter requires appropriate supplier access.
3. Use structured filters to bound a pilot when useful. Natural-language instructions require a selected model provider configured under **Sources & AI**.
4. Follow product states, coverage, and activity; inspect rows and download exports as products finish. Partial coverage remains visible.

### Manage AI providers

Open **Sources & AI → AI providers**. Each saved connection has **Edit**,
**Suspend/Resume**, and **Delete** controls. Edit keeps its ID and history; the
API-key field is always blank, and leaving it blank preserves the saved key.
Saving a connection does not call the model. **Test connection** sends a small
model request and may incur a provider charge.

Suspend blocks new and queued AI calls, including director reviews, while keeping
the connection available for later use. A request already in flight may finish;
its billed usage is recorded, but a suspended or changed provider cannot authorize
a pending staging repair. Collection that does not require AI continues.

Delete is recoverable: enable **Show deleted**, choose **Restore**, review the
connection, then **Resume**. Restoration leaves it suspended. Audit history is
retained. To replace Muse's dedicated director key, choose **Edit** on the Muse
connection, enter the key in its protected field and save. The director readiness
check reports the limit observed for that exact saved key; account credit is
separate from its monthly key limit.

The configured Muse connection's editor shows the director's monthly cap, spent,
reserved and remaining amounts, plus the last observed OpenRouter limit for the
saved key. Use its **Manage key limit in OpenRouter** link to change the provider
limit. Recheck director readiness after saving a replacement key. This director
budget does not cover **Test connection** or general instruction-interpreter calls.

The saved [real-source sample exports](examples/README.md) include five direct quotes across the three supplied product URLs and the separate 26-tier Business Cards table pilot.

### Automatic campaign collection

See the [step-by-step automatic collection guide](docs/AUTOMATIC-COLLECTION.md).

Use **Sources & AI → Open browser** to sign in through Hermes's own supplier
window. Signing in through another Chrome profile does not update that session.
Then choose **Campaigns → Automatic supplier collection → Check remaining
products**. This makes a plan from the active campaign group and saved evidence;
it does not request supplier prices. **Start product tasks** runs the supported
work without a model provider or a chat supervising each choice.

The worker saves its cursor and source receipts, runs different suppliers
independently, and shares each supplier's existing rate limits. After each
finite batch, a continuing product gives other eligible products a turn. That
order is saved and survives a service restart. The configured 99-product
capacity is a queue ceiling; it does not override supplier limits.
Leave the Hermes service and computer running. **Pause collection** fences
active work; **Resume queued work** resumes queued work. Completed selections
remain saved. An interrupted selection whose result is uncertain is held rather
than automatically sent again. Expired supplier authentication and unsupported
source controls appear as explicit blockers.

The task table shows saved collection steps separately from attempts. These
steps confirm durable progress; they are not a count of completed products.
Supplier sign-in, account setup, and required project details have separate
messages. For a new 4over quote, Hermes can give a blank project name the fixed
label “Hermes source quote.” Existing names and saved projects are preserved.
If a source still needs attention, other eligible suppliers continue.

Registered supplier profiles discover native dependent choices and validate
their exact source scope. Package quantities and area-priced/custom products
use different source contracts. Neither a quoted sample nor an inferred area
coefficient supplies prices for unobserved combinations. A completed collection
task is separate from whole-product source finalization, retail markup,
shipping, and publishing to WooCommerce. Hermes records supplier costs;
PrintItPrints applies its own selling-price and shipping rules.

**Fixture benchmark** is a separate, explicitly synthetic adapter. Its default is 3 products × 30,000 rows. You can request 100 × 30,000 to exercise three million generated rows, but those results measure this engine and storage path, not a supplier's throughput or pricing accuracy. Run that benchmark on appropriately sized hardware and inspect its actual results.

The recorded [local capacity benchmark](docs/benchmark.md) generated and exported **3,000,000 synthetic rows across 100 products**. The second run finished generation and all exports in **6 minutes 57 seconds**; independent CSV and Parquet checks confirmed the count and configuration uniqueness. It made zero supplier requests; timings and measured limitations are in the report and [machine-readable results](docs/benchmark.json).

### Captured quantity tables — guarded preview

In **New harvest → Configuration filters & shipping**, **Use captured quantity tables (preview)** enables guarded quantity-table expansion for supported Business Cards, Club Flyers, and Roll Labels configurations. It uses responses from normal browser operation. Two displayed quantity quotes must match the reconstructed table before calculated rows publish. Each remaining quantity retains `source_table` provenance and `pending` verification; its production days require a configuration quote. Unsupported or mismatched branches return to direct browser extraction.

The original live acceptance configuration was U.S. Standard 2 × 3.5, 14 pt. Gloss, full-color front / grayscale back, horizontal orientation, High Gloss UV Front, Regular production, with no corners or the captured rounded-corner option. Use these raw filters to reproduce the baseline branch:

```json
{"size":"101","paper":"1","color":"2","coating":"5","roundedCorners":"","orientation":"1","productionTime":"1"}
```

To reproduce that bounded pilot, use **26 configurations** and a **150-request** per-product ceiling. Leave both bounds empty to traverse the available preset domain. These are optional pilot bounds, not a job timer. The first live table contained 26 valid quantity tiers. The expanded preview binds each supported branch to its own captured lookup and tables; unknown finishing controls and mismatches still fall back to displayed quotes. A table match does not establish shipping, proofing/design-service coverage, custom dimensions, or independent cart verification. The live acceptance run generated **26 tiers in 34 seconds using 39 counted browser/source requests**: two displayed quote samples and 24 calculated rows marked pending. This is one configuration branch, with a saved login session. Details and limits are in [the probe report](docs/live-probe.md).

### Use the commerce calculators

Open **Sources & AI → Commerce calculators**. **Shipping estimator** accepts your tariff's base and weight rates, currency, zone/service, weight increment, and actual package weights. Optional dimensional-weight rules and surcharges are explicit fields. No rates are prefilled; the result is labeled a calculated estimate rather than a live supplier quote.

**Production & arrival** uses Printograph's calendar rules with your processed timestamp, production-day range, optional transit range, and approval/payment state. Rush requires the order-placement timestamp so its separate cutoff can be checked. All timestamps need an explicit timezone. Missing prerequisites or unknown design/mailing durations produce an unresolved state; the calculator does not infer rush fees or guarantee delivery dates.

## Deploy on your own server

The Docker image builds the dashboard, installs Chromium with its Linux dependencies, and runs the Python service as an unprivileged user. Docker Compose includes PostgreSQL and persistent volumes for the database and `/data`.

```sh
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
python3 -c 'import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
python3 -c 'import secrets; print(secrets.token_hex(24))'
```

Place those three generated values in `.env` as `ATLAS_API_TOKEN`, `ATLAS_SECRET_KEY`, and `POSTGRES_PASSWORD`, respectively. Then:

```sh
docker compose up --build -d
docker compose logs -f atlas
```

Open [Hermes on the server](http://127.0.0.1:18877) and enter the API token through **API access**. The browser keeps it only for the current tab session. Compose binds the published port to localhost; expose it through your existing HTTPS reverse proxy when remote access is needed. Keep authentication enabled for any network deployment.

Preserve **both persistent volumes and the same `ATLAS_SECRET_KEY`** across upgrades. The key decrypts supplier and model credentials. Back up PostgreSQL and `/data` together. The first version creates missing database tables at startup; future schema migrations must be applied explicitly before upgrades that change existing columns. No destructive migration is performed automatically.

The service currently combines API and worker lifecycles. Use one Uvicorn worker per instance. PostgreSQL is the intended coordination database for larger deployments; do not place SQLite on a shared network filesystem. Multiple instances need the same database, encryption key, and shared artifact directory. Browser work defaults to four active product tabs per instance. Set `ATLAS_BROWSER_PRODUCT_CONCURRENCY=99` for up to 99 browser products on a machine sized for that many tabs, then restart Hermes; the supported range is 1–256. Start with four and measure memory before raising the cap. See **Sources & AI → Worker capacity** or the harvest detail for the loaded limits. Local `scripts/start-local.sh` and Docker Compose both load this value from `.env`; explicitly exported shell settings take precedence. Tabs still share the database source request limits. The engine shares one browser profile per source across product tabs, so one normal login can serve the catalog. Dedicated profiles live under `data/browser-profiles/` with owner-only directory permissions and persist across service restarts. No passwords, cookies, or API tokens are sent to model providers. Supplier expiry, logout, or revocation can still require another sign-in. Set `ATLAS_BROWSER_PERSIST_SESSION=false` for memory-only sessions. Never mount the same browser profile in concurrent service instances; Chromium profiles must have one owner. An existing login in an unrelated browser is not copied automatically: sign in once in the crawler’s own profile.

Hermes retains the existing `ATLAS_*` settings, `atlas` Compose service, API/session keys, and runtime data/profile paths for compatibility with saved deployments and supplier sessions.

| Setting | Default / purpose |
| --- | --- |
| `ATLAS_DATA_DIR` | `./data` locally; `/data` in Docker |
| `DATABASE_URL` | SQLite when unset; Compose sets a PostgreSQL URL |
| `ATLAS_API_TOKEN` | Optional for localhost development; required by Compose |
| `ATLAS_SECRET_KEY` | Fernet encryption key; local fallback is `data/encryption.key` |
| `ATLAS_PRODUCT_CONCURRENCY` | 100 active product tasks, capped at 256 |
| `ATLAS_SOURCE_RPS` | 2 requests/second ceiling per source unless overridden |
| `ATLAS_SOURCE_CONCURRENCY` | 2 concurrent requests per source unless overridden |
| `ATLAS_CACHE_SECONDS` | 86400 seconds; explicit refresh revalidates source responses |
| `ATLAS_BROWSER_HEADLESS` | `true`; use `false` for an interactive local authentication handoff |
| `ATLAS_BROWSER_PERSIST_SESSION` | `true`: retain the dedicated supplier browser profiles; `false` uses memory-only sessions |
| `ATLAS_BROWSER_PRODUCT_CONCURRENCY` | `4`: active product tabs per instance, configurable 1–256; set `99` explicitly for a suitably sized worker, then restart; shared source request limits still apply |
| `ATLAS_BROWSER_PROFILE_DIR` | Optional exact path to one dedicated persistent Chromium profile; must have one process owner |
| `ATLAS_BROWSER_STORAGE_STATE` | Optional previously authorized Playwright state file, used only in memory-only mode; updated state is not exported |
| `ATLAS_BROWSER_AUTH_WAIT_SECONDS` | 0: wait for interactive authentication until paused/cancelled; positive values impose an optional handoff deadline |
| `ATLAS_BROWSER_INACTIVITY_SECONDS` | 30: report a stalled browser action after no new product response or control/price progress; this is not a total job timer |

HTTPS downloads and provider calls verify certificates through the operating system trust store, using [Truststore](https://truststore.readthedocs.io/en/latest/). Explicit `SSL_CERT_FILE` / `SSL_CERT_DIR` settings can add an operator-provided CA bundle. Certificate and hostname verification remain enabled.

Source profile pacing applies across worker requests. The default limit permits one hundred active product tasks per engine instance, while active browser tabs use the separate `ATLAS_BROWSER_PRODUCT_CONCURRENCY` setting (default four). A run containing 99 URLs queues 99 products; it does not open 99 browser tabs unless that browser cap is configured accordingly. Effective browser extraction capacity is the smaller of the product-task and browser-tab limits. For a single source, its configured concurrent-request ceiling also caps resident extraction pages; this avoids filling every tab with pages waiting on the same supplier and lets other sources progress. The Worker capacity panel distinguishes active browser workers from products waiting for one. Tabs share a browser context and source request ceilings. Server-side rate limits and latency govern the sustainable pace; no one-hundred-requests-per-second supplier rate is assumed.

Optional `ATLAS_S3_BUCKET` and `ATLAS_S3_ENDPOINT` configure response-object mirroring with boto3 credentials. Current row/export artifacts still require the shared filesystem; configuring S3 alone does not make the entire data directory disposable.

## What works, and what still needs validation

| Area | Current behavior |
| --- | --- |
| Durable runs | Queued products, worker leases, pause/resume/cancel, response persistence, deterministic row deduplication, and recoverable failures. A request interrupted before durable commitment can be repeated. Browser resumes may replay controls and source requests; committed rows remain deduplicated. |
| Printograph browser | Normal Chromium page operation; discovers visible dependent controls, reads displayed prices, and retains page/network observations. Unsupported controls, access challenges, and unfinished domains keep coverage partial or require attention. |
| Printograph price routes | Adapter and parser for observed specification, lookup, quantity, base-price, and option-price routes. Live API access returned HTTP 401 in the bounded public probe. Normal browser capture confirmed the baseline Business Cards schemas and selected corner surcharges; the opt-in table path now includes guarded bindings for the three supplied product types, with direct-quote fallback for unsupported branches. Separate direct API access still requires supplier authorization. |
| WooCommerce | Embedded variation JSON and public variation requests where supported. Captures source IDs, option values, displayed variation prices, and image references. Quantity discount completeness and hidden catalog variations are not assumed. |
| PrintItPrints | Captures the embedded `csvdata` table in one product-document request. A reviewed calculator contract translates its base amounts into storefront prices. Keeps supplier attribution, base amounts, and calculated retail amounts separate; pages without a calculator require attention. See [the replacement-source audit](docs/printitprints-fallback-audit.md). |
| Pricing | Decimal monetary handling, provenance, quantity-tier analysis, and verification helpers. Locally derived prices stay pending until independently checked. No sampling result is represented as verification of every row. |
| Shipping / production | Configurable tariff and production-calendar calculations with missing-input reporting. Estimates are separate from supplier quotes. Exact live shipping, account-specific rush fees, and combined-cart charges require a verified supplier quote integration. |
| Images | URL collection and explicit image download modes, with artifact manifests and source associations. |
| Refreshes | Cache reuse, context-sensitive response keys, conditional HTTP requests where supported, and source hashes. This release does not promise complete dependency-aware incremental regeneration for every adapter. |
| Exports | Standard CSVs show option labels and prices without supplier IDs or raw JSON. Audit Parquet and manifests retain source identities and evidence; both formats include per-row verification state. |
| Models | Optional OpenAI-compatible, Anthropic, and configurable JSON HTTP endpoints. Instructions become explicit filters; price arithmetic never depends on model guesses. Provider URLs currently follow the external-network validation rules. |

Complete normal browser authentication in the headed crawler window once. Product tabs then reuse that profile; keep the profile data with the service when restarting it. No harvested API credentials, stealth-browser guarantees, or challenge bypass are used. An access-required result is retained so the user can supply their authorized session or API access and resume.

See [the architecture and review guide](docs/architecture.md) for the implemented data flow and remaining boundaries. See [the captured browser price-table findings](docs/browser-table-findings.md) for actual source quantities, option fees, and production dependencies. See [the bounded Printograph probe](docs/live-probe.md) for recorded API observations and [dashboard behavior](docs/dashboard.md) for UI details. See [the measured 3-million-row benchmark](docs/benchmark.md) and its [machine-readable results](docs/benchmark.json).

## API and verification

Interactive API documentation is available at `/docs`. Main interfaces:

- `/api/v1/runs`: create/list runs; inspect a run and its product states.
- `/api/v1/runs/{id}/{pause|resume|cancel}`: control persistent work.
- `/api/v1/runs/{id}/rows`, `/events`, `/exports`, `/analytics`: inspect records, activity, artifacts, and bounded quantity-price analysis.
- `/api/v1/evidence/{id}`: inspect the retained source response and fingerprint.
- `/api/v1/sources`, `/providers`: configure connections. Provider updates use
  `PUT /providers/{id}`; lifecycle actions use `POST /providers/{id}/{suspend|resume|restore}`
  and recoverable `DELETE /providers/{id}`. `GET /providers?include_deleted=true`
  includes recoverable deleted profiles. `/providers/{id}/test` sends a model request.
- `/api/v1/commerce/shipping`, `/commerce/production`: explicit rule-driven estimates.

Use `Authorization: Bearer <ATLAS_API_TOKEN>` when authentication is enabled. Health checks remain public and do not reveal stored credentials. Supplier/model credentials are encrypted; returned profile lists expose metadata only.

Run the backend and frontend checks from the project directory:

```sh
source .venv/bin/activate
python -m pytest backend/tests
cd frontend
npm run build
```

The Docker Compose configuration is syntax-validated separately; a successful frontend build or fixture test does not validate a supplier's live price formula. Supplier benchmark reporting must name the hardware, adapter, request count, row count, elapsed time, cache state, coverage, and verification status.
