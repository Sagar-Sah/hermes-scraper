"""Real offline archives and the actual verifier, with zero network/site writes."""
import asyncio
from copy import deepcopy
import hashlib
import gzip
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import time
import zipfile

import pytest

from crawler.autonomous_product_pipeline import StagingBaseline
from crawler.companion_baseline_setup import BaselineSetupVerifier, bundle_updraft_components, inspect_wordpress_backup
from crawler.companion_environments import digest
from crawler.companion_storefront_verifier import CompanionStorefrontVerifier, TrustedStorefrontFixture


PROD = "https://printitprints.com"
STAGE = "https://staging3.tonyl36.sg-host.com"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def tree(files):
    values = {key: sha(raw) for key, raw in sorted(files.items())}
    raw = json.dumps(values if values else [], separators=(",", ":")).replace("/", "\\/").encode()
    return {"status": "match", "complete": True, "sha256": sha(raw), "files": len(values), "bytes": sum(map(len, files.values()))}


def sql(origin):
    statements = ["-- MySQL dump 10.13 Distrib 8.0.42\n"]
    for name in ("options", "posts", "postmeta", "terms", "term_taxonomy", "term_relationships"):
        statements.append("CREATE TABLE `wp_" + name + "` (id bigint);\n")
    statements.extend(["INSERT INTO `wp_options` VALUES (1,'siteurl','" + origin + "','yes'),(2,'home','" + origin + "','yes');\n",
                       "-- Dump completed on 2026-10-29 18:00:00\n"])
    return "".join(statements).encode()


def files(origin, **changes):
    value = {"backup/database.sql": sql(origin),
             "backup/public_html/wp-content/plugins/example/main.php": b"<?php /* plugin */",
             "backup/public_html/wp-content/plugins/example/assets/app.js": b"const plugin = 1;",
             "backup/public_html/wp-content/themes/theme/style.css": b"body{color:#111}",
             "backup/public_html/wp-content/uploads/wcdcp/9532-printit.csv": b"size,quantity,cost\n2x3.5,100,20",
             "backup/public_html/wp-content/uploads/2026/10/fixture.png": b"private actual fixture image bytes"}
    value.update(changes)
    return value


def archive(path, values, *, kind="zip"):
    if kind == "zip":
        with zipfile.ZipFile(path, "w") as target:
            for name, raw in values.items():
                target.writestr(name, raw)
    else:
        with tarfile.open(path, "w:gz") as target:
            for name, raw in values.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(raw)
                target.addfile(entry, io.BytesIO(raw))
    return path


def environment(origin, env):
    value = {"schema": "hermes-companion-environment-snapshot/v1", "site_origin": origin, "environment": env,
             "plugins": {"example/main.php": {"active": True, "version": "1.0", "code": tree({"main.php": b"<?php /* plugin */", "assets/app.js": b"const plugin = 1;"})}},
             "themes": {"theme": {"version": "1.0", "code": tree({"style.css": b"body{color:#111}"})}},
             "mu_plugins": tree({}), "custom_css": {"status": "match", "sha256": sha(b"")},
             "snippets": {"status": "match", "count": 0, "sha256": sha(b"[]")},
             "custom_calculator_files": {"status": "match", "files": {"9532-printit.csv": sha(b"size,quantity,cost\n2x3.5,100,20")}},
             "runtime_fingerprints": {"retail": "a" * 64, "shipping": "b" * 64},
             "staging_isolation": {key: "disabled" for key in ("search_indexing", "outbound_mail", "payments", "scheduled_actions", "outbound_http")}}
    value["snapshot_sha256"] = digest(value)
    return value


ROW = {"selection": {"size": "2 x 3.5", "shape": "Rectangle", "stock": "18pt Kraft", "colorspec": "4/4", "coating": "None", "quantity": "100"},
       "cost": "20.00", "production": "4 Business Days"}


class Environments:
    def __init__(self):
        self.values = {"production": environment(PROD, "production"), "staging": environment(STAGE, "staging")}
        self.calls, self.product_changes = [], {}

    async def environment_snapshot(self, environment):
        self.calls.append((environment, "environment"))
        return deepcopy(self.values[environment])

    async def snapshot(self, environment, product_id):
        self.calls.append((environment, str(product_id)))
        return {"product_id": str(product_id), "revision": ("c" if environment == "production" else "d") * 64,
                "storefront_url": (PROD if environment == "production" else STAGE) + "/shop/cards/",
                "calculator": {"family": "custom_csv"}, "retail_rule": {"fingerprint": "e" * 64, "script_sha256": "f" * 64},
                "artwork": {"canonical_unit": "in", "status": "verified"}, "rows": [deepcopy(ROW)], "documents": [],
                "pricing_basis": "supplier_cost", "currency": "USD", "headers": [], "columns": {},
                **self.product_changes.get(environment, {})}

    def status(self, environment):
        return {"connected": True, "base_url": PROD if environment == "production" else STAGE}


async def browser(environment, product_id, manifest, snapshot, **kwargs):
    assert kwargs["rollback"] is True
    return {"status": "observed", "url": snapshot["storefront_url"], "product_id": product_id,
            "html_sha256": "a" * 64, "observed_at": time.time(), "rows": deepcopy(snapshot["rows"]),
            "document_links": [], "connector_documents": [],
            "retail_rule": {"status": "verified", "script_sha256": "f" * 64, "member": True},
            "samples": [{"retained_selection": deepcopy(ROW["selection"]), "retained_production": ROW["production"],
                         "displayed_retail": "60.00", "artwork_dimensions": [{"value": "2 x 3.5", "unit": "in"}]}],
            "request_guards": {"mutating_requests": 0, "supplier_requests": 0, "cart_actions": 0, "blocked_requests": 0}}


def fixture(kind):
    def run(context):
        return {"status": "match", "manifest_sha256": context["manifest_sha256"], "product_id": context["product_id"],
                "environment": context["environment"], "fixture_sha256": "b" * 64,
                "snapshot_revision": context["snapshot_revision"], "observed_at": time.time(),
                "evidence_ids": [kind + "-independent-observation"], "orders_placed": 0, "payments_submitted": 0,
                "supplier_orders_placed": 0, "cart_actions": 0, "uploads": 0, "wordpress_writes": 0}
    return TrustedStorefrontFixture(kind, "b" * 64, run, environments=("production", "staging"), mode="read_only")


async def setup(tmp_path, *, complete=True):
    environments = Environments()
    baseline = StagingBaseline(tmp_path, environments)
    capture = await baseline.capture()
    fixtures = {"9532": {key: fixture(key) for key in ("shipping", "customer_flow")}} if complete else {}
    verifier = CompanionStorefrontVerifier(environments, browser_runner=browser, fixtures=fixtures)
    return BaselineSetupVerifier(baseline, storefront_verifier=verifier), capture, environments


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_actual_archive_structure_sql_identity_and_captured_code_are_verified(tmp_path, kind):
    path = archive(tmp_path / ("backup." + kind), files(PROD), kind=kind)
    result = inspect_wordpress_backup(path, environment(PROD, "production"))
    assert result["database"]["core_tables_complete"] and result["database"]["site_identity_matches"]
    assert result["restore_test"] == "not_performed" and result["captured_files_verified"] == 4


@pytest.mark.parametrize("change,reason", [
    ("origin", "site_identity_mismatch"), ("truncated", "complete_sql_export"), ("table", "core_database_tables_missing"),
    ("plugin", "code_hash_mismatch"), ("csv", "calculator_hash_mismatch"), ("uploads", "uploads_missing"),
    ("two_databases", "one_database_export"), ("unknown_code", "inventory_unknown"),
])
def test_missing_stale_or_wrong_backup_content_never_registers_a_pass(tmp_path, change, reason):
    values, snapshot = files(PROD), environment(PROD, "production")
    if change == "origin":
        values["backup/database.sql"] = sql(STAGE)
    elif change == "truncated":
        values["backup/database.sql"] = sql(PROD).replace(b"-- Dump completed on 2026-10-29 18:00:00", b"")
    elif change == "table":
        values["backup/database.sql"] = sql(PROD).replace(b"CREATE TABLE `wp_postmeta` (id bigint);\n", b"")
    elif change == "plugin":
        values["backup/public_html/wp-content/plugins/example/assets/app.js"] = b"Changed after capture"
    elif change == "csv":
        values["backup/public_html/wp-content/uploads/wcdcp/9532-printit.csv"] = b"Changed price"
    elif change == "uploads":
        values = {key: raw for key, raw in values.items() if "/uploads/" not in key}
    elif change == "two_databases":
        values["backup/another.sql"] = sql(PROD)
    else:
        snapshot["plugins"]["example/main.php"]["code"] = {"status": "unknown"}
    with pytest.raises(ValueError, match=reason):
        inspect_wordpress_backup(archive(tmp_path / "backup.zip", values), snapshot)


@pytest.mark.parametrize("name", ["../escape.php", "/absolute.php", "backup/../../escape", "backup\\windows.php"])
def test_archive_traversal_is_rejected_without_extracting_anything(tmp_path, name):
    values = files(PROD)
    values[name] = b"forbidden"
    with pytest.raises(ValueError, match="member_invalid"):
        inspect_wordpress_backup(archive(tmp_path / "backup.zip", values), environment(PROD, "production"))
    assert not (tmp_path.parent / "escape.php").exists()


def test_symlink_tar_and_duplicate_zip_members_are_rejected(tmp_path):
    path = tmp_path / "link.tar"
    with tarfile.open(path, "w") as target:
        entry = tarfile.TarInfo("backup/link")
        entry.type, entry.linkname = tarfile.SYMTYPE, "/private"
        target.addfile(entry)
    with pytest.raises(ValueError, match="link_or_special"):
        inspect_wordpress_backup(path, environment(PROD, "production"))
    path = archive(tmp_path / "duplicate.zip", files(PROD))
    with pytest.warns(UserWarning):
        with zipfile.ZipFile(path, "a") as target:
            target.writestr("backup/database.sql", sql(PROD))
    with pytest.raises(ValueError, match="duplicate_member"):
        inspect_wordpress_backup(path, environment(PROD, "production"))


@pytest.mark.asyncio
async def test_backups_remain_private_preservation_is_bound_and_only_human_can_approve(tmp_path):
    runner, capture, _ = await setup(tmp_path)
    for env, origin in (("production", PROD), ("staging", STAGE)):
        original = archive(tmp_path / (env + ".zip"), files(origin))
        result = runner.register_backup(capture["id"], env, original)
        assert result["status"] == "passed"
        original.unlink()  # Retention cannot depend on the Downloads folder.
    assert runner.preserve_unreleased_work(capture["id"])["status"] == "passed"
    assert (await runner.verify_isolation(capture["id"]))["status"] == "passed"
    assert (await runner.verify_runtime_parity(capture["id"], ["9532"]))["status"] == "passed"
    receipts = runner.receipts(capture["id"])
    assert receipts["complete"] and receipts["human_approval_required"]
    assert runner.baseline.status()["ready"] is False
    for path in runner.artifacts.rglob("*"):
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(ValueError, match="human"):
        runner.baseline.approve(capture["id"], capture["manifest_sha256"], receipts["receipt_hashes"], actor="ai")
    approved = runner.baseline.approve(capture["id"], capture["manifest_sha256"], receipts["receipt_hashes"], actor="human")
    assert approved["ready"] is True
    backup = next(runner.artifacts.rglob("production-backup-*.zip"))
    backup.write_bytes(b"Tampered retention")
    assert runner.baseline.status()["ready"] is False


@pytest.mark.asyncio
async def test_preservation_needs_both_complete_backups_and_retains_differences(tmp_path):
    runner, capture, _ = await setup(tmp_path)
    with pytest.raises(ValueError, match="required_setup_receipt"):
        runner.preserve_unreleased_work(capture["id"])
    for env, origin in (("production", PROD), ("staging", STAGE)):
        runner.register_backup(capture["id"], env, archive(tmp_path / (env + ".zip"), files(origin)))
    result = runner.preserve_unreleased_work(capture["id"])
    assert result["details"]["site_copy_performed"] is False
    assert "site_origin" in result["details"]["differences"]


@pytest.mark.asyncio
async def test_missing_actual_flow_fixtures_stays_unknown_without_parity_receipt(tmp_path):
    runner, capture, _ = await setup(tmp_path, complete=False)
    result = await runner.verify_runtime_parity(capture["id"], ["9532"])
    assert result["status"] == "unknown" and "receipt_sha256" not in result
    assert all(item["checks"] == ["shipping", "customer_flow"] for item in result["holds"])
    assert "runtime_parity" not in runner.receipts(capture["id"])["receipt_hashes"]


@pytest.mark.asyncio
async def test_unknown_connector_isolation_and_drift_cannot_be_attested(tmp_path):
    runner, capture, environments = await setup(tmp_path)
    environments.values["staging"]["staging_isolation"]["payments"] = "unknown"
    with pytest.raises(ValueError, match="environment_changed"):
        await runner.verify_isolation(capture["id"])
    capture = await runner.baseline.capture()
    with pytest.raises(ValueError, match="isolation_unknown"):
        await runner.verify_isolation(capture["id"])


@pytest.mark.asyncio
async def test_naked_pass_flags_are_not_detailed_runtime_verification(tmp_path):
    runner, capture, _ = await setup(tmp_path)
    async def flags(env, pid, manifest, snapshot, **kwargs):
        return {"schema": "hermes-storefront-verification/v1", "product_id": pid, "environment": env,
                "manifest_sha256": digest(manifest), "observed_at": time.time(), "status": "match",
                "checks": {key: "match" for key in ("calculator_data", "rendered_prices", "artwork_dimensions", "shipping", "customer_flow", "documents", "retail_rule_unchanged")}}
    runner.verifier = flags
    with pytest.raises(ValueError, match="detailed_browser_evidence"):
        await runner.verify_runtime_parity(capture["id"], ["9532"])
    assert "runtime_parity" not in runner.receipts(capture["id"])["receipt_hashes"]


@pytest.mark.asyncio
async def test_stale_or_unbound_verifier_receipt_is_rejected(tmp_path):
    runner, capture, _ = await setup(tmp_path)
    original = runner.verifier
    async def stale(*args, **kwargs):
        result = await original(*args, **kwargs)
        result["observed_at"] = time.time() - 301
        return result
    runner.verifier = stale
    with pytest.raises(ValueError, match="receipt_unbound"):
        await runner.verify_runtime_parity(capture["id"], ["9532"])


@pytest.mark.asyncio
async def test_product_data_mismatch_never_uses_a_price_canary_as_parity(tmp_path):
    runner, capture, environments = await setup(tmp_path)
    changed = deepcopy(ROW)
    changed["cost"] = "21.00"
    environments.product_changes["staging"] = {"rows": [changed]}
    with pytest.raises(ValueError, match="product_data_differs"):
        await runner.verify_runtime_parity(capture["id"], ["9532"])


@pytest.mark.asyncio
async def test_late_environment_change_invalidates_even_successful_render_checks(tmp_path):
    runner, capture, environments = await setup(tmp_path)
    original = runner.verifier
    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        if args[0] == "staging":
            environments.values["production"]["runtime_fingerprints"]["shipping"] = "c" * 64
        return result
    runner.verifier = changed
    with pytest.raises(ValueError, match="environment_changed"):
        await runner.verify_runtime_parity(capture["id"], ["9532"])
    assert "runtime_parity" not in runner.receipts(capture["id"])["receipt_hashes"]


def components(tmp_path, *, origin=PROD, split=True):
    cohort = "backup_2026-10-29-1500_printit_0123456789ab"
    paths = {}
    for kind, values in {
        "plugins": {"plugins/example/main.php": b"<?php /* plugin */", "plugins/example/assets/app.js": b"const plugin = 1;"},
        "themes": {"themes/theme/style.css": b"body{color:#111}"},
        "uploads": {"uploads/wcdcp/9532-printit.csv": b"size,quantity,cost\n2x3.5,100,20"},
        "others": {"index.php": b"<?php // Silence"},
    }.items():
        paths[kind] = archive(tmp_path / (cohort + "-" + kind + ".zip"), values)
    if split:
        paths["uploads2"] = archive(tmp_path / (cohort + "-uploads2.zip"), {"uploads/2026/10/fixture.png": b"actual image"})
    paths["db"] = tmp_path / (cohort + "-db.gz")
    with gzip.open(paths["db"], "wb") as target:
        target.write(sql(origin))
    return paths


def combine(tmp_path, paths, *, expected=None, snapshot=None):
    return bundle_updraft_components(tmp_path / "private-bundle.tar", paths["db"],
                                      [path for key, path in paths.items() if key != "db"],
                                      expected or sorted(paths), snapshot or environment(PROD, "production"))


def test_updraft_components_stream_into_private_complete_archive_without_altering_sources(tmp_path):
    paths = components(tmp_path)
    originals = {str(path): path.read_bytes() for path in paths.values()}
    result = combine(tmp_path, paths)
    assert result["inspection"]["database"]["site_identity_matches"]
    assert result["expected_parts"] == ["db", "others", "plugins", "themes", "uploads", "uploads2"]
    assert stat.S_IMODE((tmp_path / "private-bundle.tar").stat().st_mode) == 0o600
    assert all(path.read_bytes() == originals[str(path)] for path in paths.values())
    assert not list(tmp_path.glob("*.database.tmp"))
    with tarfile.open(tmp_path / "private-bundle.tar") as bundled:
        assert "wp-content/uploads/2026/10/fixture.png" in bundled.getnames()
        assert "hermes-updraft-bundle.json" in bundled.getnames()


@pytest.mark.parametrize("failure,reason", [
    ("omitted_last", "set_incomplete"), ("gap", "split_component_gap"),
    ("mixed_generation", "generations_differ"), ("wrong_database", "site_identity_mismatch"),
    ("wrong_directory", "directory_mismatch"), ("duplicate_file", "duplicate_logical_file"),
    ("corrupt_database", "corrupt_or_unavailable"), ("traversal", "member_invalid"),
])
def test_incomplete_mixed_corrupt_or_unsafe_updraft_set_cannot_create_a_verified_bundle(tmp_path, failure, reason):
    paths = components(tmp_path)
    expected = sorted(paths)
    if failure == "omitted_last":
        del paths["uploads2"]
    elif failure == "gap":
        source = paths.pop("uploads2")
        destination = source.with_name(source.name.replace("uploads2", "uploads3"))
        source.rename(destination)
        paths["uploads3"] = destination
        expected = sorted(paths)
    elif failure == "mixed_generation":
        source = paths["plugins"]
        destination = source.with_name(source.name.replace("0123456789ab", "bbbbbbbbbbbb"))
        source.rename(destination)
        paths["plugins"] = destination
    elif failure == "wrong_database":
        with gzip.open(paths["db"], "wb") as target:
            target.write(sql(STAGE))
    elif failure == "wrong_directory":
        archive(paths["themes"], {"plugins/not-a-theme.php": b"incorrect component"})
    elif failure == "duplicate_file":
        archive(paths["uploads2"], {"uploads/wcdcp/9532-printit.csv": b"duplicate"})
    elif failure == "corrupt_database":
        paths["db"].write_bytes(paths["db"].read_bytes()[:-4])
    else:
        archive(paths["others"], {"../escape.php": b"unsafe"})
    with pytest.raises(ValueError, match=reason):
        combine(tmp_path, paths, expected=expected)
    assert not (tmp_path / "private-bundle.tar").exists()
    assert not list(tmp_path.glob("*.database.tmp"))


@pytest.mark.asyncio
async def test_updraft_registration_retains_bundle_and_provenance_and_does_not_approve(tmp_path):
    runner, capture, _ = await setup(tmp_path)
    paths = components(tmp_path)
    result = runner.register_updraft_backup(capture["id"], "production", paths["db"],
                                             [path for key, path in paths.items() if key != "db"], sorted(paths))
    assert result["status"] == "passed" and result["details"]["updraft_component_count"] == 6
    assert result["details"]["source_files_unchanged"] and result["details"]["remote_transmissions"] == 0
    assert all(path.is_file() for path in paths.values())
    assert len(list(runner.artifacts.rglob("production-backup-*.tar"))) == 1
    assert not list(runner.artifacts.rglob("*.tmp.tar"))
    receipt = json.loads((runner.baseline.receipts / (result["receipt_sha256"] + ".json")).read_text())
    assert any("updraft-components" in item["path"] for item in receipt["artifacts"])
    assert runner.baseline.status()["ready"] is False
