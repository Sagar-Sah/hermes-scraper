import asyncio
import copy
import json
import os

import pytest

from crawler.companion_environments import digest
from crawler.companion_repair_workflow import CompanionRepairWorkflow


NOW = 10000
CHANGES = [{"kind": "set_cost", "selection": {"size": "2 x 3.5", "quantity": "100"}, "cost": "17.25"}]


def evidence(changes=CHANGES):
    return {"source_sha256": "a" * 64, "source_observed_at": NOW - 100,
            "changes_sha256": digest(changes), "mapping_confirmed": True, "source_domain_complete": True,
            "checks": {"options": "match", "quantities": "match", "source_prices": "match"}}


class Bridge:
    def __init__(self):
        source = {"product_id": "9532", "revision": "before", "currency": "USD", "pricing_basis": "supplier_cost",
                  "calculator": {"family": "custom_csv", "write_supported": True},
                  "rows": [{"selection": CHANGES[0]["selection"], "cost": "15.00", "production": "4 Business Days"}],
                  "documents": [], "headers": ["SIZE", "RunSize", "TotalPrice"], "columns": {"size": "SIZE"}}
        self.snapshots = {"production": copy.deepcopy(source), "staging": copy.deepcopy(source)}
        self.origins = {"production": "https://printitprints.com", "staging": "https://staging3.tonyl36.sg-host.com"}
        self.previews, self.calls, self.changes = {}, [], {}
        self.crash_once = None

    def status(self, env):
        return {"base_url": self.origins[env]}

    async def snapshot(self, env, product_id):
        return copy.deepcopy(self.snapshots[env])

    async def request(self, env, method, path, payload=None, *, release_authorization=None):
        self.calls.append((env, method, path, copy.deepcopy(payload), release_authorization))
        if env == "production" and method == "POST":
            assert release_authorization["actor"] == "human"
        if path == "/previews":
            assert payload["expected_revision"] == self.snapshots[env]["revision"]
            key = f"{len(self.previews) + 1:032x}"
            diff = ([{"before": "15.00", "after": "17.25", "kind": "set_cost"}] if payload["changes"][0]["kind"] == "set_cost"
                    else [{"kind": "add_document", "before": None, "after": payload["changes"][0]}])
            preview = {"preview_id": key, "diff": diff, "expires_at": NOW + 900,
                       "before": copy.deepcopy(self.snapshots[env]), "status": "preview", "operations": payload["changes"]}
            self.previews[key] = preview
            return copy.deepcopy(preview)
        if method == "GET" and path.startswith("/previews/"):
            return copy.deepcopy(self.previews[path.rsplit("/", 1)[-1]])
        if path.startswith("/apply/"):
            key = path.rsplit("/", 1)[-1]
            preview = self.previews[key]
            if preview["status"] == "applied":
                return preview["change"]
            assert payload["expected_revision"] == self.snapshots[env]["revision"]
            if preview["operations"][0]["kind"] == "set_cost":
                self.snapshots[env]["rows"][0]["cost"] = "17.25"
            else:
                operation = preview["operations"][0]
                self.snapshots[env]["documents"].append({"url": operation["url"], "label": operation["label"]})
            self.snapshots[env]["revision"] = "after"
            receipt = {"change_id": key, "after_revision": "after", "revision": "after"}
            preview.update(status="applied", change=receipt)
            self.changes[key] = preview["before"]
            if self.crash_once == env:
                self.crash_once = None
                raise ConnectionError("simulated disconnect after remote write")
            return receipt
        if path.startswith("/rollback/"):
            key = path.rsplit("/", 1)[-1]
            if self.snapshots[env]["revision"] == "before":
                return {"revision": "before"}
            assert self.snapshots[env]["revision"] == payload["expected_revision"]
            self.snapshots[env] = copy.deepcopy(self.changes[key])
            return {"revision": self.snapshots[env]["revision"]}
        raise AssertionError((method, path))


async def verifier(env, product_id, manifest, snapshot, **kwargs):
    assert product_id == "9532"
    return {"status": "match", "manifest_sha256": digest(manifest), "checks": {
        key: "match" for key in ("calculator_data", "rendered_prices", "artwork_dimensions", "shipping", "customer_flow")}}


def test_staging_preview_apply_and_explicit_human_production_release(tmp_path):
    async def exercise():
        bridge = Bridge()
        flow = CompanionRepairWorkflow(tmp_path, bridge, verifier=verifier, clock=lambda: NOW)
        record = await flow.preview("9532", CHANGES, evidence())
        assert bridge.snapshots["staging"]["rows"][0]["cost"] == "15.00"
        applied = await flow.apply(record["id"], "before")
        assert applied["status"] == "staging_verified"
        assert bridge.snapshots["production"]["rows"][0]["cost"] == "15.00"
        for actor in ("ai", "system"):
            with pytest.raises(ValueError, match="human_approval"):
                await flow.approve_production(record["id"], record["manifest_sha256"], actor=actor)
            with pytest.raises(ValueError, match="manual_production"):
                await flow.promote(record["id"], record["manifest_sha256"], actor=actor)
        with pytest.raises(ValueError, match="manual_production"):
            await flow.promote(record["id"], record["manifest_sha256"], actor="human")
        await flow.approve_production(record["id"], record["manifest_sha256"], actor="human")
        promoted = await flow.promote(record["id"], record["manifest_sha256"], actor="human")
        assert promoted["status"] == "production_verified"
        writes = len([x for x in bridge.calls if x[1] == "POST"])
        assert (await flow.promote(record["id"], record["manifest_sha256"], actor="human"))["status"] == "production_verified"
        assert len([x for x in bridge.calls if x[1] == "POST"]) == writes
        await flow.rollback(record["id"], environment="production", actor="human", manifest_sha256=record["manifest_sha256"])
        assert bridge.snapshots["production"]["rows"][0]["cost"] == "15.00"
    asyncio.run(exercise())


def test_no_verifier_no_supported_family_or_different_baseline_never_writes(tmp_path):
    async def exercise():
        bridge = Bridge()
        flow = CompanionRepairWorkflow(tmp_path, bridge, clock=lambda: NOW)
        record = await flow.preview("9532", CHANGES, evidence())
        held = await flow.apply(record["id"], "before")
        assert held["status"] == "held"
        assert not any("/apply/" in x[2] for x in bridge.calls)
        bridge.snapshots["staging"]["calculator"] = {"family": "pila", "write_supported": False}
        assert (await flow.preview("9532", CHANGES, evidence()))["reason"] == "calculator_family_repair_unsupported"
        bridge.snapshots["staging"]["calculator"] = {"family": "custom_csv", "write_supported": True}
        bridge.snapshots["staging"]["rows"][0]["cost"] = "different"
        assert (await flow.preview("9532", CHANGES, evidence()))["reason"] == "staging_baseline_differs_from_production"
    asyncio.run(exercise())


def test_invalid_evidence_stale_revision_manifest_tampering_and_recheck_rollback(tmp_path):
    async def exercise():
        bridge = Bridge()
        flow = CompanionRepairWorkflow(tmp_path, bridge, verifier=verifier, clock=lambda: NOW)
        for change in (lambda e: e.update(source_domain_complete=False), lambda e: e.update(changes_sha256="b" * 64),
                       lambda e: e.update(source_observed_at=NOW - 21601), lambda e: e["checks"].update(quantities="unknown")):
            data = evidence()
            change(data)
            with pytest.raises(ValueError):
                await flow.preview("9532", CHANGES, data)
        assert bridge.calls == []
        record = await flow.preview("9532", CHANGES, evidence())
        with pytest.raises(ValueError, match="revision_or_state"):
            await flow.apply(record["id"], "wrong")
        async def failing(env, product_id, manifest, snapshot, **kwargs):
            if snapshot["rows"][0]["cost"] == "15.00":
                return await verifier(env, product_id, manifest, snapshot, **kwargs)
            return {"status": "unknown"}
        flow.verifier = failing
        assert (await flow.apply(record["id"], "before"))["status"] == "staging_rolled_back_after_failed_recheck"
        assert bridge.snapshots["staging"]["rows"][0]["cost"] == "15.00"
        path = flow._path(record["id"])
        stored = json.loads(path.read_text())
        stored["manifest"]["changes"][0]["cost"] = "99"
        path.write_text(json.dumps(stored))
        with pytest.raises(ValueError, match="manifest_changed"):
            flow.get(record["id"])
    asyncio.run(exercise())


def test_shipping_unknown_retains_verified_staging_cost_but_blocks_production(tmp_path):
    async def exercise():
        bridge = Bridge()
        async def partial(*args, **kwargs):
            result = await verifier(*args, **kwargs)
            result.update(status="unknown")
            result["checks"].update(shipping="unknown", customer_flow="unknown")
            return result
        flow = CompanionRepairWorkflow(tmp_path, bridge, verifier=partial, clock=lambda: NOW)
        record = await flow.preview("9532", CHANGES, evidence())
        result = await flow.apply(record["id"], "before")
        assert result["status"] == "staging_verified" and result["release_ready"] is False
        assert result["release_hold_checks"] == ["customer_flow", "shipping"]
        assert bridge.snapshots["staging"]["rows"][0]["cost"] == "17.25"
        with pytest.raises(ValueError, match="recheck_incomplete"):
            await flow.approve_production(record["id"], record["manifest_sha256"], actor="human")
        assert not any(c[0] == "production" and c[1] == "POST" for c in bridge.calls)
        flow.verifier = verifier
        assert (await flow.recheck_staging(record["id"]))["release_ready"] is True
    asyncio.run(exercise())


def test_unavailable_production_readonly_fixture_holds_before_any_production_post(tmp_path):
    async def exercise():
        bridge = Bridge()
        async def staged_only(env, product_id, manifest, snapshot, **kwargs):
            result = await verifier(env, product_id, manifest, snapshot, **kwargs)
            if env == "production":
                result["status"] = "unknown"
                result["checks"]["customer_flow"] = "unknown"
            return result
        flow = CompanionRepairWorkflow(tmp_path, bridge, verifier=staged_only, clock=lambda: NOW)
        record = await flow.preview("9532", CHANGES, evidence())
        await flow.apply(record["id"], "before")
        await flow.approve_production(record["id"], record["manifest_sha256"], actor="human")
        with pytest.raises(ValueError, match="recheck_incomplete"):
            await flow.promote(record["id"], record["manifest_sha256"], actor="human")
        assert not any(c[0] == "production" and c[1] == "POST" for c in bridge.calls)
        assert bridge.snapshots["production"]["rows"][0]["cost"] == "15.00"
        assert bridge.snapshots["staging"]["rows"][0]["cost"] == "17.25"
    asyncio.run(exercise())


def test_document_only_requires_document_evidence_not_unrelated_price_domain(tmp_path):
    async def exercise():
        bridge = Bridge()
        changes = [{"kind": "add_document", "url": "https://4over.com/media/asset/templates/card.pdf", "label": "Template"}]
        data = evidence(changes)
        data.pop("source_domain_complete")
        data["checks"] = {"documents": "match"}
        async def documents(env, product_id, manifest, snapshot, **kwargs):
            return {"status": "unknown", "manifest_sha256": digest(manifest), "checks": {
                "calculator_data": "match", "documents": "match", "retail_rule_unchanged": "match",
                "shipping": "unknown", "customer_flow": "unknown", "rendered_prices": "unknown", "artwork_dimensions": "unknown"}}
        flow = CompanionRepairWorkflow(tmp_path, bridge, verifier=documents, clock=lambda: NOW)
        record = await flow.preview("9532", changes, data)
        result = await flow.apply(record["id"], "before")
        assert result["status"] == "staging_verified" and result["release_ready"] is False
        assert bridge.snapshots["staging"]["rows"][0]["cost"] == "15.00"
        assert bridge.snapshots["staging"]["documents"] == [{"url": changes[0]["url"], "label": "Template"}]
    asyncio.run(exercise())


def test_rollback_backend_restore_is_not_claimed_verified_without_frontend_recheck(tmp_path):
    async def exercise():
        bridge = Bridge()
        async def unavailable(*args, **kwargs):
            return {"status": "unknown"}
        flow = CompanionRepairWorkflow(tmp_path, bridge, verifier=unavailable, clock=lambda: NOW)
        record = await flow.preview("9532", CHANGES, evidence())
        with pytest.raises(ValueError, match="recheck_incomplete"):
            await flow.apply(record["id"], "before")
        assert bridge.snapshots["staging"]["rows"][0]["cost"] == "15.00"
        assert flow.get(record["id"])["status"] == "staging_rollback_recheck_pending"
        flow.verifier = verifier
        result = await flow.rollback(record["id"])
        assert result["status"] == "staging_rollback_verified"
        assert result["staging_rollback"]["status"] == "verified"
    asyncio.run(exercise())


def test_history_limits_before_reading_large_records_and_verifies_selected_manifest(tmp_path):
    async def exercise():
        bridge = Bridge()
        flow = CompanionRepairWorkflow(tmp_path, bridge, verifier=verifier, clock=lambda: NOW)
        old = await flow.preview("9532", CHANGES, evidence())
        latest = await flow.preview("9532", CHANGES, evidence())
        flow._path(old["id"]).write_text("not loaded by bounded history")
        os.utime(flow._path(old["id"]), (100, 100))
        os.utime(flow._path(latest["id"]), (200, 200))
        assert [item["id"] for item in flow.history(limit=1)] == [latest["id"]]
        with pytest.raises(json.JSONDecodeError):
            flow.history()
        record = flow.get(latest["id"])
        record["manifest"]["changes"][0]["cost"] = "99"
        flow._path(latest["id"]).write_text(json.dumps(record))
        with pytest.raises(ValueError, match="manifest_changed"):
            flow.history(limit=1)
        for value in (0, 201, True, "1"):
            with pytest.raises(ValueError, match="limit_out_of_bounds"):
                flow.history(limit=value)
    asyncio.run(exercise())


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_disconnect_after_remote_apply_recovers_receipt_without_repeated_write(tmp_path, environment):
    async def exercise():
        bridge = Bridge()
        flow = CompanionRepairWorkflow(tmp_path, bridge, verifier=verifier, clock=lambda: NOW)
        record = await flow.preview("9532", CHANGES, evidence())
        if environment == "production":
            await flow.apply(record["id"], "before")
            await flow.approve_production(record["id"], record["manifest_sha256"], actor="human")
        bridge.crash_once = environment
        async def run():
            return (await flow.apply(record["id"], "before") if environment == "staging"
                    else await flow.promote(record["id"], record["manifest_sha256"], actor="human"))
        with pytest.raises(ConnectionError):
            await run()
        count = len([c for c in bridge.calls if c[0] == environment and c[2].startswith("/apply/")])
        result = await run()
        assert result["status"] == environment + "_verified"
        assert len([c for c in bridge.calls if c[0] == environment and c[2].startswith("/apply/")]) == count
    asyncio.run(exercise())
