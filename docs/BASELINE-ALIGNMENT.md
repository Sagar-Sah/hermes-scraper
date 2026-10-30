# Review an environment alignment plan

`crawler.companion_baseline_alignment.build_alignment_plan(capture)` compares a
saved, immutable environment capture. The normal caller first loads it through
`StagingBaseline._capture`; the planner repeats the capture ID and manifest hash
checks. It performs no network requests or filesystem/site writes.

The proposal compares plugin versions, activation and complete code hashes;
active themes; MU code; custom CSS; snippets; exact calculator-file identities;
and captured retail/shipping runtime fingerprints. Matching versions alone do
not count as matching code. Matching code alone does not prove working retail,
artwork, shipping or checkout behavior. Per-product document and effective
setting coverage remain explicit unknowns until separately verified.

Every mismatch or unknown record contains a stable `id`, `component`/`domain`,
`name`/`key`, bounded production/staging observations, status, reason, priority,
proposed action and preservation requirement. The proposal includes no raw
credentials, customer records, snippet text or calculator rows. Its
`plan_sha256` binds the entire result. Equivalent inputs produce the same plan.

Staging-only plugins, themes and calculator files need preservation and review.
Same-version code changes also preserve both sides before selective alignment.
Plugins named as release, draft, recovery, import, QA, backup or migration
helpers are omitted from copy recommendations by default: their site and
one-time mutation behavior requires review. This heuristic never authorizes
removal or decides that a helper is obsolete.

Staging protection is reviewed separately from production configuration. A
reported safe state is labeled `declared_safe`, still requiring functional
evidence. An HTTP guard does not prove that direct SDK or socket calls are
isolated. The planner never recommends copying production payment, mail,
scheduler or indexing settings onto staging.

The result always has `ready: false`, `status: review_required` and an empty
`automatic_actions` list. It cannot approve the baseline or a production
release. Verify backups, preserve unreleased work, review selective changes,
refresh the capture after any change, then obtain actual runtime, isolation and
storefront receipts through the existing baseline verification workflow.
Never replace production's database with staging's database.

At most 500 review records are returned. Summary totals still count every
observed item; `records_truncated` and `summary.omitted_records` make an exceeded
display bound explicit. A truncated view requires further review and cannot
silently certify inventory completeness. Capture and inventory limits also
bound processing. This module supplies planning data only; the server/UI owns
the read-only endpoint and display.

The authenticated dashboard restores its latest saved capture with GET `/api/v1/companion/autonomy/baseline/latest`. GET `/api/v1/companion/autonomy/baseline/alignment?capture_id=<id>` compares that exact saved capture locally; it never recaptures a website. Baseline reads use a generation guard so an older response cannot replace a newly captured comparison. Corrupt or renamed capture records are held instead of falling back silently.
