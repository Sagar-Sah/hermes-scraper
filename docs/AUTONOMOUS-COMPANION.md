# Autonomous Product Companion

Hermes does the recurring collection, comparisons and queue management. A saved
Muse provider can review compact results and exceptions within a **$25 monthly
AI cap**. Codex handles new code and unsupported cases. **Every release to the
live store still requires a person’s approval.**

This workflow is separate from the older campaign’s active-group collection.
Each monthly cycle gets its own evidence namespace. Previous observations stay
in history; they do not become fresh prices merely because a new month started.

## What a 99-product window means

The coordinator admits up to 99 pending products and prepares the next 99. It
refills these windows through software; it does not ask AI to click a Next
button. Browser worker capacity starts at four and is configurable from one to
eight for this coordinator. Each supplier’s pacing, cooldown and authentication
limits still apply; this is not a promise of 99 simultaneous supplier tabs.

Registered handlers collect finite, evidenced work. A missing supplier mapping,
unsupported calculator or expired session becomes a visible hold. Other
eligible suppliers can continue. An uncertain in-flight operation is held after
interruption rather than blindly sent again. Saved progress and timing survive
safe pauses and service restarts.

An inventoried cycle pins its supplier mappings. After reviewing additional
mapping evidence, use **Retire unstarted inventory**, then explicitly create and
inventory a new cycle. The old cycle becomes `superseded`; its catalog pages,
mapping snapshot and history remain intact. This action is available only before
any product dispatch, result, repair, paid review or uncertain operation. It
uses the displayed revision and inventory hash and rejects stale requests. It
does not start a replacement or reset an interrupted supplier request. Scheduled
cycles also retain their month identity, preventing an automatic duplicate run.

Collection completion is different from product verification. Quantities,
conditional choices, exact costs, retail calculations, documents, production,
artwork and shipping are checked only where the registered capability can prove
them. Unknown coverage never produces a green result. Unknown evidence required
for a specific repair cannot authorize that repair; a verified document link can
still be staged while unrelated product checks remain unknown.
Shipping comparisons retain their tested configuration, destination and service
scope; a 2% holdout result is not a nationwide or full-domain guarantee.

## Establish staging first

Before enabling a recurring cycle:

1. Back up both sites and inventory their actual loaded plugin, theme, snippet,
   calculator, document and shipping versions. Preserve and classify intended
   unreleased staging changes before copying the live baseline.
2. Align protected staging with that production baseline, then reapply only the
   intended pending staging changes. Keep staging’s domain and access
   protection, payment isolation and outbound-email protections.
3. Verify unattended authenticated access, loaded frontend hashes, effective
   retail rules and required carrier/rate connectivity. An installed version
   label or a signed-in browser alone does not prove this connection works.
4. Attach the reviewed baseline to the cycle. From then on, test repairs on
   staging first; do not reclone production over unreleased work every month.

The existing October 28 connector test proved a scoped preview, apply, storefront
recheck and rollback on protected staging. Its external API connection was held
by preview protection. Later shipping receipts matched module files across the
two sites but still found an older staging Studio asset and blocked carrier
HTTP. These historical receipts are useful starting evidence, not a claim that
today’s unattended staging baseline is ready.

Production product promotion uses an approved package of supported product-field
differences and expected revisions. File and setting deployments remain with
Codex/review. Promotion does not replace production’s database
with staging’s orders, customers, sessions or transactions. A changed live
revision requires a new review. Failed storefront rechecks use the saved
rollback receipt; an uncertain write must be reconciled before another write.

## Prepare the office Mac mini

Use a local checkout on the Mac mini’s own disk, outside iCloud/Dropbox-synced
folders. Install the README’s Python, Node and Git prerequisites, and configure
the project’s existing `.env`. Keep its data directory, encryption key and
dedicated supplier browser profiles together when backing up or updating.
The launch service uses a standard Homebrew/system executable path; ensure
Python, Node/npm and Git are available there rather than only in an interactive
shell manager.

Run the normal launcher and verify its dashboard/backend build identities first:

```sh
./scripts/start-local.sh
```

After pausing and stopping an existing manually launched Hermes instance, the
service installer is explicitly opt-in:

```sh
./scripts/install-macos-service.sh --dry-run
./scripts/install-macos-service.sh --install
./scripts/install-macos-service.sh --status
```

The installer writes a per-user `com.hermes.product-companion` LaunchAgent and
runs the supervised `scripts/start-local.sh`. It refuses an occupied port rather
than stopping another process. It preserves `.env`, data and source profiles,
does not override the saved browser limit, and does not enable an audit schedule.
An existing matching service definition is retained rather than overwritten.
Startup/dependency errors appear in `~/Library/Logs/Hermes`; a registered agent
is not itself proof that the backend has become healthy.

Normal desktop sign-in is required after a reboot. No automatic login, unlocked
desktop, credential copying or disabled security protection is configured. The
Mac must be running and awake; sleep or sign-out can delay work. Missed monthly
work is handled by the coordinator after service recovery. Complete supplier
sign-in, MFA or other normal authentication manually when required.

Pause active collection before removing only the per-user service. Project data
and profiles are retained:

```sh
./scripts/install-macos-service.sh --uninstall
```

## Set up the dashboard

Open **Product Companion → Autonomous Companion**. The manual pair-audit tools
remain below it.

1. Expand **Environment baseline and release readiness**. Connect production
   using a read-only connector identity and staging using its scoped repair
   identity. If protected staging needs preview authentication, enter its normal
   preview credentials in that form. Secrets are sent only to the protected
   local backend, cleared from the fields after saving and omitted from AI
   summaries. Connection readiness must be verified before treating a site as
   available for unattended work.
2. Under **Schedule and AI settings**, choose the existing saved Muse profile
   from **Sources & AI**. Hermes reuses that profile’s server-side credentials;
   no key is entered into the product evidence or prompts. Check **Director
   readiness**. A shared provider key must not have unrelated account limits
   changed automatically to make this check pass; follow the returned bounded
   account/key readiness requirements.
3. Leave monthly scheduling off during the first pilot. Choose **Create Standard Print pilot** (or explicitly create a full-catalog manual cycle),
   read its complete catalog inventory and review its baseline, mappings and
   capability holds. Start only the admitted, supported work.
4. Inspect **Current window**, **Next window** and **Load saved product results**.
   Results are paginated in groups of 99 and show active/elapsed timing, missing
   evidence, held work and staging repair state. Refresh saved results to read a
   new snapshot. The dashboard polls only compact status while visible; closing
   or hiding it does not stop the independent backend service.
5. Once the pilot and baseline pass, enable **Monthly autonomy** and **Run
   calendar-month audits automatically**, then save. The default schedule is the
   first day of each month at 02:00, **America/Phoenix**. Use the cycle’s Pause
   button to checkpoint an active run; turning off future scheduling is not a
   substitute for pausing active work.

Supplier sign-in belongs to **Sources & AI → Open supplier browser**. A login
in a different Chrome profile does not update Hermes’s saved supplier session.
Expired access blocks that supplier and requests attention; it is not bypassed.

## AI and repair boundaries

AI receives a compact summary, evidence references and bounded permitted
decisions. It does not receive supplier cookies, connection tokens, customer
details or the entire variation dataset. Normal collection, scheduling,
continuation and next-window loading make no model calls.

The dashboard distinguishes the month’s spent, reserved and remaining AI
budget. Missing measurements display as unknown, not zero. Additional reviews
wait when the approved cap is exhausted; deterministic collection can continue.
Model failure, malformed output or unavailable budget never certifies a product
or authorizes a release.

Routine verified corrections use registered staging write operations with
preview, revision checks, backup, storefront recheck and rollback. A new
calculator, new vendor driver, new shipping algorithm, destructive option
change or unsupported case goes to Codex/review. Hermes does not invent source
prices, interpolate unproved combinations or promote staging to main by itself.

## Baseline evidence and safe recovery

The setup panel can capture the two authenticated environment inventories. A
capture is not an approval. The five required receipts are generated only by
the trusted local backup and verification runner; neither an AI response nor a
hash pasted into the API can stand in for the underlying files and checks.

Use `scripts/verify-companion-baseline.py --help` with the explicit office data
directory. Its `backup` action inspects and privately retains a complete exported
ZIP/tar backup with SQL and the captured code/calculator files. Register both
environments, then use `preserve` to retain the staging differences. `isolation`
rechecks the actual connector state. `runtime` runs representative storefront
checks through registered fixtures. Missing shipping or customer-flow fixtures
remain unknown. This does not perform a restore test, clone a site, approve the
baseline, or publish anything. Review the receipt hashes before the separate
human baseline approval.

Unattended notifications are saved in a private outbox and, on macOS, delivered
as desktop notifications for completion, failures and required action. Stable
repeated status is deduplicated. Failed or interrupted delivery remains held;
`POST /api/v1/companion/autonomy/notifications/{id}/retry` is an explicit retry,
not an automatic repeating alert. Desktop notification availability depends on
the office Mac's normal notification settings.

If a managed update leaves autonomy in maintenance mode, acknowledge **Clear
maintenance pause** in the dashboard. This clears only the admission fence. It
does not resume a cycle, reset an uncertain supplier request, or replay a repair.
Repeated director decisions reuse the saved repair identity and reconcile its
existing remote receipt rather than creating another product write.

## Current adapter boundaries

The connector reads custom CSV, WooCommerce, PILA, PIGF, DPS and WSDisplay
calculator snapshots. A family that cannot expose verified effective pricing
stays unknown. Automatic typed writes in this release support verified custom
CSV cost changes, priced row additions and source document links. Other family
writes, routing repairs and shipping-parameter changes require a registered
Codex adapter; the coordinator records them as exceptions.

Current source tables and a collapsed or single-child quantity observation do
not prove the full expanded conditional domain. 4over native observation
receipts must be joined to the same cycle, child selection and service context.
Historical tables remain historical. Fresh exact supplier costs and a mismatch
with the storefront are separate findings; a changed cost is not permission to
copy an unverified configuration.

Existing shipping models can be registered through the private
`shipping-evidence/contracts` loader. It requires the actual pinned model,
approved AZ/Florida/New York metropolitan, suburban and rural fixtures, separate
training and holdout contexts, representative configurations and fresh paired
source/site quotes from trusted drivers. A paid model must meet the 2% target
on every declared comparable holdout. Verified vendor-free shipping has its own
terms, eligibility and merchant-approval evidence. A historical report or a
zero-dollar website rate alone cannot certify a current shipping policy.

Activation still requires completed environment alignment, valid scoped
connector credentials, a valid isolated Muse key, trusted checkout/rate
fixtures and installation on the actual office Mac mini. Passing fixture tests
on a developer machine does not establish an unattended live pilot.
