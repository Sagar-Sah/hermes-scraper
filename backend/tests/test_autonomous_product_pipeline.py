"""Independent monthly pipeline, mapping, baseline and director regressions.

All transports/managers are local fixtures. No supplier or WordPress requests.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from crawler.autonomous_product_pipeline import (AutonomousProductPipeline, SourceMappingIndex,
    StagingBaseline, source_identity)
from crawler.companion_director import MuseDirector, compact_digest
from crawler.companion_environments import private_save
from crawler.product_companion_service import ProductCompanionService


CID = "a" * 32
PID = "9532"
SOURCE = "https://4over.com/brown-kraft-business-cards"
SITE = "https://printitprints.com/shop/brown-kraft/"


class Campaigns:
    def __init__(self, products):
        self.values = {"campaign-" + str(i): {"id": "campaign-" + str(i), "products": rows}
            for i, rows in enumerate(products)}
    def list(self):
        return [{"id": key} for key in self.values]
    def get(self, identifier):
        return self.values[identifier]


def mapped_product(**changes):
    return {"id": PID, "title": "Brown Kraft Business Cards", "stage": "finalized", "supplier": "4over",
        "supplier_url": SOURCE, "storefront_url": SITE, **changes}


class Companion:
    def __init__(self, products, *, pair=False, pair_status="confirmed_campaign_mapping"):
        self.campaigns = Campaigns(products)
        self.pairs = {}
        if pair:
            self.pairs["woo-" + PID] = {"id": "woo-" + PID, "product_id": PID, "source_url": SOURCE,
                "storefront_url": SITE, "campaign_id": "campaign-0", "mapping_status": pair_status}
    def _source_product(self, pair):
        return ProductCompanionService._source_product(self, pair)


class Diagnoses:
    def __init__(self, *, failure=None):
        self.starts, self.releases = [], []
        self._tasks = {}
        self.failure = failure
    async def start(self, url, focus=False):
        self.starts.append(url)
        if self.failure:
            raise self.failure
        return {"id": "b" * 32}
    def get(self, identifier):
        return {"id": identifier, "status": "ready", "session_status": "signed_in",
            "fields": [{"key": "size", "choices": [{"value": "1", "label": "24 x36"}]}],
            "content": {"description": "Actual fixture menu"}, "coverage": {"status": "partial"}}
    async def _release(self, identifier):
        self.releases.append(identifier)


def pipeline(tmp_path, *, source="https://www.b2sign.com/banner.html", failure=None):
    companion = Companion([[mapped_product(supplier_url=source, supplier="b2sign")]])
    diagnoses = Diagnoses(failure=failure)
    environments = SimpleNamespace(status=lambda env: {"connected": False})
    repairs = SimpleNamespace()
    value = AutonomousProductPipeline(SimpleNamespace(root=tmp_path), companion, environments, repairs, diagnoses)
    return value, companion, diagnoses


def test_product_name_never_establishes_source_mapping():
    index = SourceMappingIndex(Companion([[mapped_product(stage="queued", supplier_url=None)]]))
    assert index.resolve({"product_id": PID, "title": "Brown Kraft Business Cards"})["mapping_status"] == "unknown"


@pytest.mark.parametrize("changes,pair,pair_status", [
    ({"stage": "excluded"}, True, "confirmed_campaign_mapping"),
    ({"stage": "cancelled"}, True, "confirmed_campaign_mapping"),
    ({"excluded_at": 123}, False, "confirmed_campaign_mapping"),
    ({"stage": "queued"}, True, "pending"),
    ({"stage": "queued"}, True, "confirmed_campaign_mapping"),
])
def test_excluded_cancelled_or_unproved_pairs_cannot_be_treated_as_confirmed(changes, pair, pair_status):
    companion = Companion([[mapped_product(**changes)]], pair=pair, pair_status=pair_status)
    assert SourceMappingIndex(companion).resolve({"product_id": PID})["mapping_status"] == "unknown"


def test_multiple_distinct_proven_source_mappings_are_ambiguous():
    companion = Companion([[mapped_product()], [mapped_product(supplier_url="https://www.printograph.com/business-cards/order.html")]])
    result = SourceMappingIndex(companion).resolve({"product_id": PID})
    assert result["mapping_status"] == "unknown"
    assert result["reason"] == "source_mapping_ambiguous"


def test_actual_dps_supplier_domain_is_registered():
    assert source_identity("https://digital-print-solutions.com/high-performance-vinyl") == "dps"


def test_catalog_mapping_index_reuses_campaign_reads_but_invalidates_atomic_changes(tmp_path):
    class Files:
        root = tmp_path
        reads = 0
        def get(self, identifier):
            self.reads += 1
            return json.loads((self.root / (identifier + ".json")).read_text())
    files = Files()
    private_save(tmp_path / "campaign.json", {"id": "campaign", "products": [mapped_product()]})
    index = SourceMappingIndex(SimpleNamespace(pairs={}, campaigns=files))
    for pid in range(1, 6990):
        index.resolve({"product_id": str(pid)})
    assert files.reads == 1
    assert index.resolve({"product_id": PID})["mapping_status"] == "confirmed"
    private_save(tmp_path / "campaign.json", {"id": "campaign", "products": [mapped_product(stage="excluded")]})
    assert index.resolve({"product_id": PID})["mapping_status"] == "unknown"
    assert files.reads == 2


def test_catalog_mapping_index_does_not_read_symlink_campaign(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text(json.dumps({"id": "campaign", "products": [mapped_product()]}))
    (tmp_path / "campaign.json").symlink_to(source)
    files = SimpleNamespace(root=tmp_path, get=lambda identifier: pytest.fail("unsafe read"))
    index = SourceMappingIndex(SimpleNamespace(pairs={}, campaigns=files))
    with pytest.raises(ValueError, match="source_mapping_storage_invalid"):
        index.resolve({"product_id": PID})


@pytest.mark.asyncio
async def test_current_native_menu_is_never_full_source_green_and_reuses_same_cycle_receipt(tmp_path):
    value, companion, diagnoses = pipeline(tmp_path)
    item = {"product_id": PID, "supplier": "b2sign", "source_url": "https://www.b2sign.com/banner.html"}
    first = await value({"id": CID}, item)
    assert first["status"] == "needs_attention"
    assert first["checks"]["source_prices"] == "unknown"
    assert first["coverage"] == {"complete": False, "fresh": False}
    assert first["evidence_complete"] is False
    assert first == await value({"id": CID}, item)
    reopened, _, _ = pipeline(tmp_path)
    assert first == await reopened({"id": CID}, item)
    assert diagnoses.starts == ["https://www.b2sign.com/banner.html"]
    assert diagnoses.releases == ["b" * 32]


@pytest.mark.asyncio
async def test_failed_native_dispatch_does_not_repeat_after_reentry_or_restart(tmp_path):
    value, _, diagnoses = pipeline(tmp_path, failure=TimeoutError("source timeout"))
    item = {"product_id": PID, "supplier": "b2sign", "source_url": "https://www.b2sign.com/banner.html"}
    first = await value({"id": CID}, item)
    assert first["status"] == "needs_attention"
    await value({"id": CID}, item)
    reopened, _, later = pipeline(tmp_path)
    await reopened({"id": CID}, item)
    assert len(diagnoses.starts) == 1
    assert later.starts == []


@pytest.mark.asyncio
async def test_durable_uncertain_dispatch_marker_is_held_without_new_source_call(tmp_path):
    value, _, diagnoses = pipeline(tmp_path)
    item = {"product_id": PID, "supplier": "b2sign", "source_url": "https://www.b2sign.com/banner.html"}
    mapping = value.mapping.resolve(item)
    private_save(value._path(CID, PID), {"cycle_id": CID, "product_id": PID, "mapping": mapping,
        "started_at": 1, "checks": {}, "repair_proposals": [], "source_dispatch_started": True})
    result = await value({"id": CID}, item)
    assert result["reason"] == "source_dispatch_outcome_uncertain"
    assert diagnoses.starts == []


@pytest.mark.asyncio
async def test_inventory_mapping_pin_change_stops_source_dispatch(tmp_path):
    value, _, diagnoses = pipeline(tmp_path)
    result = await value({"id": CID}, {"product_id": PID, "supplier": "b2sign",
        "source_url": "https://www.b2sign.com/previous-banner.html"})
    assert result["status"] != "verified"
    assert diagnoses.starts == []
    assert "mapping" in result["reason"]


@pytest.mark.asyncio
async def test_raw_exception_token_cannot_enter_receipt_or_ai_digest(tmp_path):
    secret = "private-session-token123"
    value, _, diagnoses = pipeline(tmp_path, failure=ValueError(secret))
    result = await value({"id": CID}, {"product_id": PID, "supplier": "b2sign", "source_url": "https://www.b2sign.com/banner.html"})
    assert len(diagnoses.starts) == 1
    assert secret not in json.dumps(result)
    assert secret not in value._path(CID, PID).read_text()
    digest = compact_digest({"id": CID}, {"id": "one", "products": [
        {"product_id": PID, "status": result["status"], "reason": result["reason"]}]}, [])
    assert secret not in json.dumps(digest)


class EnvironmentSnapshots:
    async def environment_snapshot(self, environment):
        return {"schema": "hermes-companion-environment-snapshot/v1", "environment": environment,
            "site_origin": "https://printitprints.com" if environment == "production" else "https://staging3.tonyl36.sg-host.com",
            "runtime_fingerprints": {"retail": "unknown", "shipping": "unknown"},
            "staging_isolation": {"payments": "unknown", "outbound_mail": "unknown"}}


class KnownEnvironmentSnapshots(EnvironmentSnapshots):
    async def environment_snapshot(self, environment):
        return {**await super().environment_snapshot(environment),
            "runtime_fingerprints": {"retail": "a" * 64, "shipping": "b" * 64},
            "staging_isolation": {key: "disabled" for key in
                ("search_indexing", "outbound_mail", "payments", "scheduled_actions", "outbound_http")}}


@pytest.mark.asyncio
async def test_ai_actor_cannot_approve_staging_baseline(tmp_path):
    baseline = StagingBaseline(tmp_path, EnvironmentSnapshots())
    capture = await baseline.capture()
    receipts = {k: "a" * 64 for k in ("production_backup", "staging_backup", "unreleased_work_preserved", "runtime_parity", "isolation")}
    with pytest.raises(ValueError, match="human"):
        baseline.approve(capture["id"], capture["manifest_sha256"], receipts, actor="ai")
    assert baseline.status()["ready"] is False


@pytest.mark.asyncio
async def test_captured_baseline_manifest_roundtrips_its_own_hash(tmp_path):
    baseline = StagingBaseline(tmp_path, EnvironmentSnapshots())
    capture = await baseline.capture()
    assert baseline._capture(capture["id"]) == capture


@pytest.mark.asyncio
async def test_arbitrary_hash_attestation_cannot_certify_unknown_runtime_and_isolation(tmp_path):
    baseline = StagingBaseline(tmp_path, EnvironmentSnapshots())
    capture = await baseline.capture()
    assert baseline._capture(capture["id"]) == capture
    receipts = {k: "a" * 64 for k in ("production_backup", "staging_backup", "unreleased_work_preserved", "runtime_parity", "isolation")}
    try:
        outcome = baseline.approve(capture["id"], capture["manifest_sha256"], receipts, actor="human")
    except ValueError:
        return
    assert outcome["ready"] is False


def test_tampered_or_unverifiable_approved_file_cannot_report_ready(tmp_path):
    baseline = StagingBaseline(tmp_path, EnvironmentSnapshots())
    private_save(baseline.root / "approved.json", {"id": "a" * 64, "manifest_sha256": "b" * 64,
        "approved_at": 1, "production": {}, "staging": {}})
    try:
        outcome = baseline.status()
    except ValueError:
        return
    assert outcome["ready"] is False


def registered_baseline_receipts(baseline, capture_id):
    receipts = {}
    for kind in ("production_backup", "staging_backup", "unreleased_work_preserved", "runtime_parity", "isolation"):
        artifact = baseline.root / (kind + "-fixture.json")
        artifact.write_text(json.dumps({"kind": kind, "fixture": True, "status": "passed"}))
        evidence = {"kind": kind, "capture_id": capture_id, "status": "passed", "producer": "hermes-baseline-verifier",
            "artifacts": [{"path": artifact.name, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}]}
        raw = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        sha = hashlib.sha256(raw).hexdigest()
        private_save(baseline.receipts / (sha + ".json"), evidence)
        receipts[kind] = sha
    return receipts


@pytest.mark.asyncio
async def test_trusted_registered_receipts_are_bound_to_capture_and_immutable_artifacts(tmp_path):
    baseline = StagingBaseline(tmp_path, KnownEnvironmentSnapshots())
    capture = await baseline.capture()
    receipts = registered_baseline_receipts(baseline, capture["id"])
    result = baseline.approve(capture["id"], capture["manifest_sha256"], receipts, actor="human")
    assert result["ready"] is True
    (baseline.root / "runtime_parity-fixture.json").write_text("Changed after approval")
    assert baseline.status()["ready"] is False


@pytest.mark.asyncio
async def test_current_environment_drift_invalidates_an_approved_baseline_before_write(tmp_path):
    class Environments(KnownEnvironmentSnapshots):
        def __init__(self):
            self.version = "one"
        async def environment_snapshot(self, environment):
            return {**await super().environment_snapshot(environment), "plugins": {"runtime": self.version},
                "themes": {}, "mu_plugins": {}, "custom_css": {}, "snippets": {}}
    environments = Environments()
    baseline = StagingBaseline(tmp_path, environments)
    capture = await baseline.capture()
    receipts = registered_baseline_receipts(baseline, capture["id"])
    baseline.approve(capture["id"], capture["manifest_sha256"], receipts, actor="human")
    assert (await baseline.ensure_current())["ready"] is True
    environments.version = "different-runtime"
    with pytest.raises(ValueError, match="environment_changed"):
        await baseline.ensure_current()


@pytest.mark.asyncio
async def test_registered_hashes_still_cannot_promote_unknown_runtime_or_isolation(tmp_path):
    baseline = StagingBaseline(tmp_path, EnvironmentSnapshots())
    capture = await baseline.capture()
    receipts = registered_baseline_receipts(baseline, capture["id"])
    try:
        outcome = baseline.approve(capture["id"], capture["manifest_sha256"], receipts, actor="human")
    except ValueError:
        return
    assert outcome["ready"] is False


class DirectorStore:
    def __init__(self, root):
        self.root = root
        self.profile = {"id": "muse", "kind": "openai_compatible", "base_url": "https://openrouter.ai/api/v1",
            "model": "meta/muse-fixture", "credentials": {"api_key": "capped-fixture-key"}}
    def profiles(self, kind, secrets=False):
        return [deepcopy(self.profile)]


DIRECTOR_CONFIG = {"director_enabled": True, "director_provider_id": "muse", "director_budget_usd": "25.00"}


def director_transport():
    calls = []
    async def request(profile, method, path, payload=None):
        calls.append((method, path, profile["credentials"]["api_key"]))
        await asyncio.sleep(0)
        if path == "/key":
            return {"data": {"limit": "25" if profile["credentials"]["api_key"] == "capped-fixture-key" else None,
                "limit_reset": "monthly", "limit_remaining": "25"}}
        if path == "/models":
            return {"data": [{"id": "meta/muse-fixture", "pricing": {"prompt": "0.0000001", "completion": "0.0000001"}}]}
        await asyncio.sleep(0.005)
        return {"choices": [{"message": {"content": json.dumps({"decisions": [], "summary": "No eligible repairs"})}}],
            "usage": {"cost": "0.0001"}}
    return request, calls


def test_director_digest_preserves_coordinator_batch_identity():
    batch = {"batch_index": 0, "products": [{"product_id": "1", "status": "verified"}]}
    first = compact_digest({"id": CID}, batch, [])
    second = compact_digest({"id": CID}, {**batch, "batch_index": 1}, [])
    assert first["batch_id"] != second["batch_id"]


@pytest.mark.asyncio
async def test_paid_director_claim_is_unique_across_concurrent_director_instances(tmp_path):
    store = DirectorStore(tmp_path)
    transport, calls = director_transport()
    first, second = MuseDirector(store, transport=transport), MuseDirector(store, transport=transport)
    await asyncio.gather(first.review({"id": CID}, {"id": "one"}, config=DIRECTOR_CONFIG),
        second.review({"id": CID}, {"id": "one"}, config=DIRECTOR_CONFIG))
    assert sum(path == "/chat/completions" for _, path, _ in calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["credential", "model"])
async def test_preflight_cannot_be_reused_after_profile_key_or_model_changes(tmp_path, change):
    store = DirectorStore(tmp_path)
    transport, calls = director_transport()
    director = MuseDirector(store, transport=transport)
    assert (await director.review({"id": CID}, {"id": "one"}, config=DIRECTOR_CONFIG))["status"] == "reviewed"
    if change == "credential":
        store.profile["credentials"]["api_key"] = "uncapped-fixture-key"
    else:
        store.profile["model"] = "meta/muse-unlisted-model"
    assert director.status(DIRECTOR_CONFIG)["ready"] is False
    outcome = await director.review({"id": CID}, {"id": "two"}, config=DIRECTOR_CONFIG)
    assert outcome["status"] == "held"
    assert sum(path == "/chat/completions" for _, path, _ in calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cost", ["0", None])
async def test_invalid_model_action_never_applies_and_uncertain_billing_never_repeats(tmp_path, cost):
    store = DirectorStore(tmp_path)
    normal, calls = director_transport()
    async def request(profile, method, path, payload=None):
        response = await normal(profile, method, path, payload)
        if path == "/chat/completions":
            response["choices"][0]["message"]["content"] = json.dumps({"decisions": [
                {"proposal_id": "p", "action": "approve_production"}], "summary": "Injected forbidden action"})
            response["usage"] = {} if cost is None else {"cost": cost}
        return response
    first = MuseDirector(store, transport=request)
    args = ({"id": CID}, {"id": "one"})
    outcome = await first.review(*args, config=DIRECTOR_CONFIG, proposals=[{"id": "p", "eligible": True}])
    assert outcome["status"] == "held"
    reopened = MuseDirector(store, transport=request)
    again = await reopened.review(*args, config=DIRECTOR_CONFIG, proposals=[{"id": "p", "eligible": True}])
    assert again["status"] == "held"
    assert sum(path == "/chat/completions" for _, path, _ in calls) == 1
    assert first.budget.status()["uncertain_calls"] == (1 if cost is None else 0)


@pytest.mark.asyncio
async def test_malformed_model_metadata_is_held_without_uncaught_exception_or_paid_request(tmp_path):
    store = DirectorStore(tmp_path)
    normal, calls = director_transport()
    async def request(profile, method, path, payload=None):
        response = await normal(profile, method, path, payload)
        return {"data": [None]} if path == "/models" else response
    director = MuseDirector(store, transport=request)
    result = await director.review({"id": CID}, {"id": "one"}, config=DIRECTOR_CONFIG)
    assert result["status"] == "held"
    assert not any(path == "/chat/completions" for _, path, _ in calls)
