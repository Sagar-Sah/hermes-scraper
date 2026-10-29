"""Trusted baseline evidence preparation, separate from baseline approval.

No route accepts a caller's ``passed`` flag. This runner inspects actual exported
WordPress archives, copies them to private storage and invokes the registered
storefront verifier itself. Its receipts certify the declared checks, not a
successful disaster-recovery restore or every product in the catalog. Approval
remains the separate human-only StagingBaseline operation.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import shutil
import tarfile
import time
import uuid
import zipfile

from .autonomous_product_pipeline import StagingBaseline, file_sha
from .companion_environments import digest, private_save
from .companion_repair_workflow import CompanionRepairWorkflow
from .companion_storefront_verifier import ALL_CHECKS, row_signature


SHA = re.compile(r"^[a-f0-9]{64}$")
ENVIRONMENT_KEYS = ("site_origin", "environment", "plugins", "themes", "mu_plugins", "custom_css",
                    "snippets", "custom_calculator_files", "runtime_fingerprints", "staging_isolation")
CODE_EXTENSIONS = {".php", ".js", ".css", ".json", ".html", ".svg"}
MAX_MEMBERS = 200000
MAX_ARCHIVE_BYTES = 64 * 1024**3
MAX_SQL_LINE = 64 * 1024**2
CORE_TABLES = {"options", "posts", "postmeta", "terms", "term_taxonomy", "term_relationships"}
UPDRAFT_PART = re.compile(r"^(?P<cohort>.+)-(?P<kind>db|plugins|themes|uploads|others)(?P<number>[2-9][0-9]*|1[0-9]+)?\.(?P<suffix>gz|zip)$")


def _bytes_sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _php_map_sha(values):
    # Match wp_json_encode(ksort($rows)) used by the connector. PHP escapes
    # slashes and emits an empty associative map as [] in this implementation.
    value = dict(sorted(values.items())) if values else []
    return _bytes_sha(json.dumps(value, ensure_ascii=True, separators=(",", ":")).replace("/", "\\/").encode())


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _safe_member(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ValueError("baseline_backup_member_invalid")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in name.rstrip("/").split("/")):
        raise ValueError("baseline_backup_member_invalid")
    return str(path).rstrip("/")


class _Archive:
    """Read streams only. Never extract an archive into the workspace."""
    def __init__(self, path):
        self.path, self.archive, self.members = Path(path), None, {}
        self.total_bytes = 0

    def __enter__(self):
        try:
            if zipfile.is_zipfile(self.path):
                self.archive = zipfile.ZipFile(self.path)
                entries = self.archive.infolist()
                for entry in entries:
                    name = _safe_member(entry.filename)
                    mode = entry.external_attr >> 16
                    if stat.S_ISLNK(mode) or entry.flag_bits & 1:
                        raise ValueError("baseline_backup_link_or_encryption_unsupported")
                    if not entry.is_dir():
                        self._add(name, entry, entry.file_size)
            else:
                self.archive = tarfile.open(self.path, mode="r:*")
                for entry in self.archive:
                    name = _safe_member(entry.name)
                    if not entry.isdir() and not entry.isfile():
                        raise ValueError("baseline_backup_link_or_special_file_unsupported")
                    if entry.isfile():
                        self._add(name, entry, entry.size)
            if not self.members:
                raise ValueError("baseline_backup_archive_empty")
            return self
        except (OSError, zipfile.BadZipFile, tarfile.TarError):
            self.__exit__(None, None, None)
            raise ValueError("baseline_backup_archive_invalid") from None
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _add(self, name, entry, size):
        if name in self.members or size < 0:
            raise ValueError("baseline_backup_duplicate_member")
        self.members[name] = (entry, size)
        self.total_bytes += size
        if len(self.members) > MAX_MEMBERS or self.total_bytes > MAX_ARCHIVE_BYTES:
            raise ValueError("baseline_backup_archive_bound_exceeded")

    def open(self, name):
        entry = self.members[name][0]
        return self.archive.open(entry) if isinstance(self.archive, zipfile.ZipFile) else self.archive.extractfile(entry)

    def __exit__(self, *_):
        if self.archive is not None:
            self.archive.close()


def _database(stream, origin):
    """Inspect a normal SQL export, without executing SQL or keeping its data.

    A complete mysqldump or phpMyAdmin export is required. Unsupported/custom
    dumps remain held; seeing a URL somewhere in a binary file is insufficient.
    """
    tables, options_prefix, identities, in_options = set(), None, {}, False
    beginning, ending, total = b"", b"", 0
    while True:
        line = stream.readline(MAX_SQL_LINE + 1)
        if not line:
            break
        if len(line) > MAX_SQL_LINE:
            raise ValueError("baseline_backup_sql_statement_bound_exceeded")
        total += len(line)
        if total > 8 * 1024**3:
            raise ValueError("baseline_backup_database_bound_exceeded")
        beginning = (beginning + line)[:32768]
        ending = (ending + line)[-32768:]
        text = line.decode("utf-8", errors="replace")
        created = re.match(r"\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?([A-Za-z0-9_]+)`?", text, re.I)
        if created:
            tables.add(created[1])
            if created[1].endswith("_options"):
                if options_prefix is not None and options_prefix != created[1][:-7]:
                    raise ValueError("baseline_backup_multisite_dump_requires_adapter")
                options_prefix = created[1][:-7]
        inserted = re.match(r"\s*INSERT\s+INTO\s+`?([A-Za-z0-9_]+)`?", text, re.I)
        if inserted:
            in_options = inserted[1].endswith("_options")
        if in_options:
            for item in re.finditer(r"\(\s*\d+\s*,\s*'(home|siteurl)'\s*,\s*'([^'\\]{1,2048})'\s*,", text):
                previous = identities.get(item[1])
                value = item[2].rstrip("/")
                if previous is not None and previous != value:
                    raise ValueError("baseline_backup_site_identity_ambiguous")
                identities[item[1]] = value
            if re.search(r";\s*(?:--[^\n]*)?$", text):
                in_options = False
    normal = b"MySQL dump" in beginning or b"MariaDB dump" in beginning or b"phpMyAdmin SQL Dump" in beginning
    complete = b"Dump completed on" in ending or (b"phpMyAdmin SQL Dump" in beginning and re.search(rb"\bCOMMIT\s*;", ending))
    if not normal or not complete or not options_prefix:
        raise ValueError("baseline_backup_complete_sql_export_required")
    if any(options_prefix + name not in tables for name in CORE_TABLES):
        raise ValueError("baseline_backup_core_database_tables_missing")
    if identities != {"home": origin.rstrip("/"), "siteurl": origin.rstrip("/")}:
        raise ValueError("baseline_backup_database_site_identity_mismatch")
    return {"bytes": total, "tables": len(tables), "core_tables_complete": True,
            "site_identity_matches": True, "export_completion_marker": True}


def inspect_wordpress_backup(path, snapshot):
    """Verify SQL identity/completion and captured file hashes in a full archive.

    It does not claim that an unperformed restore test passed. Database contents
    remain private; only counts, integrity and site identity results are emitted.
    """
    if not isinstance(snapshot, dict) or not SHA.fullmatch(str(snapshot.get("snapshot_sha256", ""))):
        raise ValueError("baseline_backup_snapshot_identity_missing")
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("baseline_backup_file_unavailable")
    with _Archive(path) as archive:
        database_files = [name for name in archive.members if name.lower().endswith(".sql") and "/wp-content/" not in "/" + name]
        if len(database_files) != 1:
            raise ValueError("baseline_backup_one_database_export_required")
        with archive.open(database_files[0]) as stream:
            database = _database(stream, snapshot.get("site_origin", ""))
        prefixes = {name.split("wp-content/", 1)[0] for name in archive.members if "wp-content" in PurePosixPath(name).parts[:-1]}
        if len(prefixes) != 1:
            raise ValueError("baseline_backup_one_wordpress_root_required")
        prefix = next(iter(prefixes))
        files = {name[len(prefix):]: name for name in archive.members if name.startswith(prefix + "wp-content/")}
        if not any(name.startswith("wp-content/uploads/") for name in files):
            raise ValueError("baseline_backup_uploads_missing")
        hashes = {}

        def one(relative):
            name = files.get(relative)
            if name is None:
                raise ValueError("baseline_backup_captured_file_missing")
            if relative not in hashes:
                with archive.open(name) as stream:
                    hasher = hashlib.sha256()
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(block)
                hashes[relative] = hasher.hexdigest()
            return hashes[relative]

        def tree(relative, expected):
            if (not isinstance(expected, dict) or expected.get("status") != "match"
                    or expected.get("complete") is not True or not SHA.fullmatch(str(expected.get("sha256", "")))):
                raise ValueError("baseline_backup_captured_code_inventory_unknown")
            selected = {name[len(relative):]: one(name) for name in files
                        if name.startswith(relative) and PurePosixPath(name).suffix.lower() in CODE_EXTENSIONS}
            if _php_map_sha(selected) != expected["sha256"]:
                raise ValueError("baseline_backup_captured_code_hash_mismatch")

        plugins, themes = snapshot.get("plugins"), snapshot.get("themes")
        if not isinstance(plugins, dict) or not plugins or not isinstance(themes, dict) or not themes:
            raise ValueError("baseline_backup_captured_code_inventory_unknown")
        for plugin, record in plugins.items():
            relative = "wp-content/plugins/" + _safe_member(plugin)
            one(relative)
            if record.get("active"):
                parent = str(PurePosixPath(plugin).parent)
                if parent == ".":
                    if one(relative) != (record.get("code") or {}).get("sha256"):
                        raise ValueError("baseline_backup_captured_code_hash_mismatch")
                else:
                    tree("wp-content/plugins/" + parent + "/", record.get("code"))
        for name, record in themes.items():
            tree("wp-content/themes/" + _safe_member(name) + "/", record.get("code"))
        tree("wp-content/mu-plugins/", snapshot.get("mu_plugins"))
        calculators = snapshot.get("custom_calculator_files") or {}
        if calculators.get("status") != "match" or not isinstance(calculators.get("files"), dict):
            raise ValueError("baseline_backup_calculator_inventory_unknown")
        for name, sha in calculators["files"].items():
            if one("wp-content/uploads/wcdcp/" + _safe_member(name)) != sha:
                raise ValueError("baseline_backup_calculator_hash_mismatch")
        return {"scope": "complete_export_structure_and_captured_wordpress_files",
                "restore_test": "not_performed", "database": database,
                "file_count": len(archive.members), "file_bytes": sum(item[1] for item in archive.members.values()),
                "captured_files_verified": len(hashes), "snapshot_sha256": snapshot["snapshot_sha256"]}


def bundle_updraft_components(destination, database_path, component_paths, expected_parts, snapshot):
    """Stream an explicitly enumerated Updraft generation into a private tar.

    ``expected_parts`` must come from the backup set's displayed component list,
    including every numbered split (for example uploads, uploads2, uploads3).
    We cannot infer a missing final volume from the files someone downloaded.
    No extraction, SQL execution, remote transmission, restore or deletion of
    source files occurs. The returned provenance contains hashes, never SQL.
    """
    if (not isinstance(expected_parts, (list, tuple)) or len(set(expected_parts)) != len(expected_parts)
            or not 5 <= len(expected_parts) <= 200
            or any(not isinstance(part, str) or not re.fullmatch(r"(?:db|plugins|themes|uploads|others)(?:[2-9][0-9]*|1[0-9]+)?", part) for part in expected_parts)):
        raise ValueError("baseline_updraft_expected_component_list_required")
    if not isinstance(component_paths, (list, tuple)) or not 4 <= len(component_paths) <= 199:
        raise ValueError("baseline_updraft_complete_component_set_required")
    sources = [Path(database_path), *(Path(item) for item in component_paths)]
    if len({str(path.resolve()) for path in sources}) != len(sources):
        raise ValueError("baseline_updraft_duplicate_component")
    found, cohorts, provenance = {}, set(), []
    compressed_bytes = 0
    for path in sources:
        if path.is_symlink() or not path.is_file():
            raise ValueError("baseline_updraft_component_unavailable")
        match = UPDRAFT_PART.fullmatch(path.name)
        if not match:
            raise ValueError("baseline_updraft_component_filename_unsupported")
        kind, number = match["kind"], match["number"] or ""
        part = kind + number
        if part in found or (kind == "db" and (number or match["suffix"] != "gz")) or (kind != "db" and match["suffix"] != "zip"):
            raise ValueError("baseline_updraft_duplicate_or_invalid_component")
        if (path == sources[0]) != (kind == "db"):
            raise ValueError("baseline_updraft_database_component_mismatch")
        compressed_bytes += path.stat().st_size
        if compressed_bytes > MAX_ARCHIVE_BYTES:
            raise ValueError("baseline_backup_archive_bound_exceeded")
        found[part] = path
        cohorts.add(match["cohort"])
        provenance.append({"part": part, "filename": path.name, "sha256": file_sha(path), "bytes": path.stat().st_size})
    if len(cohorts) != 1:
        raise ValueError("baseline_updraft_component_generations_differ")
    if set(found) != set(expected_parts) or not {"db", "plugins", "themes", "uploads", "others"}.issubset(found):
        raise ValueError("baseline_updraft_component_set_incomplete")
    for kind in ("plugins", "themes", "uploads", "others"):
        numbers = sorted(1 if part == kind else int(part[len(kind):]) for part in found if part.startswith(kind))
        if numbers != list(range(1, max(numbers) + 1)):
            raise ValueError("baseline_updraft_split_component_gap")
    destination = Path(destination)
    if destination.exists() or destination.is_symlink() or destination.parent.is_symlink():
        raise ValueError("baseline_updraft_bundle_destination_invalid")
    total_bytes, member_count, logical_members = 0, 0, set()
    database_temporary = destination.with_name(destination.name + ".database.tmp")

    def room(additional):
        # Do not fill the machine while combining a large uploads backup. The
        # final register_backup also needs room for the retained private copy.
        if shutil.disk_usage(destination.parent).free < additional + 512 * 1024**2:
            raise ValueError("baseline_updraft_private_backup_space_required")

    def header(name, size):
        nonlocal total_bytes, member_count
        if name in logical_members:
            raise ValueError("baseline_updraft_duplicate_logical_file")
        logical_members.add(name)
        total_bytes += size
        member_count += 1
        if member_count > MAX_MEMBERS or total_bytes > MAX_ARCHIVE_BYTES:
            raise ValueError("baseline_backup_archive_bound_exceeded")
        entry = tarfile.TarInfo(name)
        entry.mode, entry.mtime, entry.size = 0o600, 0, size
        return entry

    try:
        with gzip.open(sources[0], "rb") as source, database_temporary.open("xb") as target:
            os.chmod(database_temporary, 0o600)
            sql_bytes = 0
            for block in iter(lambda: source.read(1024 * 1024), b""):
                sql_bytes += len(block)
                if sql_bytes > 8 * 1024**3:
                    raise ValueError("baseline_backup_database_bound_exceeded")
                room(len(block) + sql_bytes)
                target.write(block)
            target.flush()
            os.fsync(target.fileno())
        with database_temporary.open("rb") as source:
            database = _database(source, snapshot.get("site_origin", ""))
        with destination.open("xb") as handle:
            os.chmod(destination, 0o600)
            with tarfile.open(fileobj=handle, mode="w") as output:
                with database_temporary.open("rb") as source:
                    output.addfile(header("database.sql", database_temporary.stat().st_size), source)
                for part in sorted(found):
                    if part == "db":
                        continue
                    kind = re.match(r"[a-z]+", part)[0]
                    with _Archive(found[part]) as component:
                        if not isinstance(component.archive, zipfile.ZipFile):
                            raise ValueError("baseline_updraft_zip_component_required")
                        room(2 * (total_bytes + component.total_bytes))
                        for name, (_, size) in component.members.items():
                            relative = name[len("wp-content/"):] if name.startswith("wp-content/") else name
                            root = PurePosixPath(relative).parts[0]
                            if ((kind != "others" and root != kind)
                                    or kind == "others" and root in {"plugins", "themes", "uploads"}):
                                raise ValueError("baseline_updraft_component_directory_mismatch")
                            if PurePosixPath(relative).suffix.lower() == ".sql":
                                raise ValueError("baseline_updraft_unexpected_database_in_files")
                            with component.open(name) as source:
                                output.addfile(header("wp-content/" + relative, size), source)
                metadata = {"schema": "hermes-updraft-bundle/v1", "generation": next(iter(cohorts)),
                            "expected_parts": sorted(expected_parts), "components": sorted(provenance, key=lambda item: item["part"]),
                            "database": database, "snapshot_sha256": snapshot.get("snapshot_sha256")}
                raw = _canonical(metadata)
                output.addfile(header("hermes-updraft-bundle.json", len(raw)), io.BytesIO(raw))
            handle.flush()
            os.fsync(handle.fileno())
        if any(file_sha(found[item["part"]]) != item["sha256"] for item in provenance):
            raise ValueError("baseline_updraft_component_changed_during_bundling")
        inspection = inspect_wordpress_backup(destination, snapshot)
        return {**metadata, "inspection": inspection, "bundle_sha256": file_sha(destination)}
    except (OSError, EOFError, gzip.BadGzipFile, zipfile.BadZipFile, tarfile.TarError):
        destination.unlink(missing_ok=True)
        raise ValueError("baseline_updraft_component_corrupt_or_unavailable") from None
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        database_temporary.unlink(missing_ok=True)


class BaselineSetupVerifier:
    """Developer-owned producer. Never approves the baseline or copies a site."""
    def __init__(self, baseline: StagingBaseline, *, storefront_verifier=None, clock=time.time):
        self.baseline, self.environments, self.clock = baseline, baseline.environments, clock
        self.verifier = storefront_verifier
        self.root = baseline.root
        if self.root.is_symlink():
            raise ValueError("unsafe_baseline_artifact_storage")
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir(mode=0o700, exist_ok=True)
        if self.artifacts.is_symlink():
            raise ValueError("unsafe_baseline_artifact_storage")
        os.chmod(self.artifacts, 0o700)

    def _folder(self, capture_id):
        self.baseline._capture(capture_id)
        folder = self.artifacts / capture_id
        if folder.is_symlink():
            raise ValueError("unsafe_baseline_artifact_storage")
        folder.mkdir(mode=0o700, exist_ok=True)
        os.chmod(folder, 0o700)
        return folder

    def _json_artifact(self, capture_id, kind, value):
        raw = _canonical(value)
        path = self._folder(capture_id) / (kind + "-" + _bytes_sha(raw) + ".json")
        private_save(path, value)
        return {"path": str(path.relative_to(self.root)), "sha256": file_sha(path)}

    def _receipt(self, capture_id, kind, artifacts, details):
        capture = self.baseline._capture(capture_id)
        value = {"schema": "hermes-baseline-setup-receipt/v1", "producer": "hermes-baseline-verifier",
                 "capture_id": capture_id, "capture_manifest_sha256": capture["manifest_sha256"],
                 "environment_snapshot_sha256": {env: capture[env].get("snapshot_sha256") for env in ("production", "staging")},
                 "kind": kind, "status": "passed", "verified_at": self.clock(), "artifacts": artifacts, "details": details}
        sha = _bytes_sha(_canonical(value))
        private_save(self.baseline.receipts / (sha + ".json"), value)
        return {"status": "passed", "kind": kind, "receipt_sha256": sha, "details": details}

    def register_backup(self, capture_id, environment, archive_path):
        if environment not in {"production", "staging"}:
            raise ValueError("baseline_backup_environment_invalid")
        capture = self.baseline._capture(capture_id)
        original = Path(archive_path)
        if original.is_symlink() or not original.is_file():
            raise ValueError("baseline_backup_file_unavailable")
        sha = file_sha(original)
        inspection = inspect_wordpress_backup(original, capture[environment])
        if file_sha(original) != sha:
            raise ValueError("baseline_backup_changed_during_inspection")
        suffix = ".zip" if zipfile.is_zipfile(original) else ".tar"
        destination = self._folder(capture_id) / (environment + "-backup-" + sha + suffix)
        temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            if destination.is_symlink():
                raise ValueError("unsafe_baseline_artifact_storage")
            if not destination.exists():
                with original.open("rb") as source, temporary.open("xb") as target:
                    os.chmod(temporary, 0o600)
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        target.write(block)
                    target.flush()
                    os.fsync(target.fileno())
                if file_sha(temporary) != sha:
                    raise ValueError("baseline_backup_changed_during_preservation")
                os.replace(temporary, destination)
            elif file_sha(destination) != sha:
                raise ValueError("baseline_preserved_backup_hash_changed")
        finally:
            temporary.unlink(missing_ok=True)
        artifact = {"path": str(destination.relative_to(self.root)), "sha256": sha}
        detail = self._json_artifact(capture_id, environment + "-backup-inspection", inspection)
        return self._receipt(capture_id, environment + "_backup", [artifact, detail], inspection)

    def _find_receipt(self, capture_id, kind):
        paths = sorted(self.baseline.receipts.glob("*.json"), key=lambda item: item.stat().st_mtime_ns, reverse=True)
        for path in paths:
            if path.is_symlink() or not SHA.fullmatch(path.stem):
                continue
            try:
                value = json.loads(path.read_text())
                if value.get("capture_id") != capture_id or value.get("kind") != kind:
                    continue
                if value.get("producer") != "hermes-baseline-verifier" or value.get("status") != "passed" or file_sha(path) != path.stem:
                    continue
                for artifact in value.get("artifacts", []):
                    candidate = self.root / artifact["path"]
                    if (candidate.is_symlink() or not candidate.resolve().is_relative_to(self.root.resolve())
                            or file_sha(candidate) != artifact["sha256"]):
                        raise ValueError("baseline_preserved_artifact_changed")
                if not value.get("artifacts"):
                    continue
                return path.stem, value
            except (ValueError, KeyError, OSError, json.JSONDecodeError):
                continue
        raise ValueError("baseline_required_setup_receipt_missing")

    def register_updraft_backup(self, capture_id, environment, database_path, component_paths, expected_parts):
        if environment not in {"production", "staging"}:
            raise ValueError("baseline_backup_environment_invalid")
        capture = self.baseline._capture(capture_id)
        temporary = self._folder(capture_id) / (environment + "-updraft-" + uuid.uuid4().hex + ".tmp.tar")
        try:
            provenance = bundle_updraft_components(temporary, database_path, component_paths, expected_parts, capture[environment])
            result = self.register_backup(capture_id, environment, temporary)
            previous = json.loads((self.baseline.receipts / (result["receipt_sha256"] + ".json")).read_text())
            artifact = self._json_artifact(capture_id, environment + "-updraft-components", provenance)
            return self._receipt(capture_id, environment + "_backup", [*previous["artifacts"], artifact],
                                 {**result["details"], "updraft_component_count": len(provenance["components"]),
                                  "source_files_unchanged": True, "remote_transmissions": 0})
        finally:
            temporary.unlink(missing_ok=True)

    def preserve_unreleased_work(self, capture_id):
        capture = self.baseline._capture(capture_id)
        production_sha, production = self._find_receipt(capture_id, "production_backup")
        staging_sha, staging = self._find_receipt(capture_id, "staging_backup")
        differences = {key: {"production_sha256": digest(capture["production"].get(key)),
                             "staging_sha256": digest(capture["staging"].get(key))}
                       for key in ENVIRONMENT_KEYS if capture["production"].get(key) != capture["staging"].get(key)}
        report = {"schema": "hermes-unreleased-work-preservation/v1", "capture_id": capture_id,
                  "production_backup_receipt": production_sha, "staging_backup_receipt": staging_sha,
                  "differences": differences, "staging_remains_unchanged": True,
                  "scope": "captured_environment_database_and_files_not_a_database_merge"}
        artifact = self._json_artifact(capture_id, "unreleased-work", report)
        # Directly reference both complete archives as well. Changing a retained
        # backup after this receipt must invalidate the human baseline approval.
        artifacts = [artifact, *production["artifacts"], *staging["artifacts"]]
        return self._receipt(capture_id, "unreleased_work_preserved", artifacts,
                             {"differences": sorted(differences), "site_copy_performed": False})

    async def _current(self, capture):
        current = await asyncio.gather(*(self.environments.environment_snapshot(env) for env in ("production", "staging")))
        for env, observed in zip(("production", "staging"), current):
            if any(capture[env].get(key) != observed.get(key) for key in ENVIRONMENT_KEYS):
                raise ValueError("baseline_environment_changed_during_setup")
        return dict(zip(("production", "staging"), current))

    async def verify_isolation(self, capture_id):
        capture = self.baseline._capture(capture_id)
        current = await self._current(capture)
        self.baseline._known_runtime(capture)
        evidence = {"capture_id": capture_id, "staging_snapshot_sha256": current["staging"]["snapshot_sha256"],
                    "staging_isolation": deepcopy(current["staging"]["staging_isolation"]),
                    "observed_at": self.clock(), "source": "authenticated_environment_connector",
                    "protected_preview_disabled": False}
        artifact = self._json_artifact(capture_id, "isolation", evidence)
        return self._receipt(capture_id, "isolation", [artifact], {"source": "authenticated_environment_connector"})

    async def verify_runtime_parity(self, capture_id, product_ids):
        if (not isinstance(product_ids, (list, tuple)) or not 1 <= len(product_ids) <= 12
                or len(set(product_ids)) != len(product_ids)
                or any(not re.fullmatch(r"[1-9][0-9]{0,11}", str(pid)) for pid in product_ids)):
            raise ValueError("baseline_runtime_representative_products_required")
        if not callable(self.verifier):
            raise ValueError("baseline_runtime_trusted_verifier_required")
        capture = self.baseline._capture(capture_id)
        await self._current(capture)
        self.baseline._known_runtime(capture)
        cases, holds = [], []
        for product_id in map(str, product_ids):
            production, staging = await asyncio.gather(*(self.environments.snapshot(env, product_id) for env in ("production", "staging")))
            if (any(str(value.get("product_id")) != product_id or not value.get("revision") for value in (production, staging))
                    or any(value.get("calculator", {}).get("family") != "custom_csv" for value in (production, staging))):
                raise ValueError("baseline_runtime_calculator_adapter_required")
            data = CompanionRepairWorkflow._data(production)
            if (row_signature(production.get("rows")) != row_signature(staging.get("rows"))
                    or data != CompanionRepairWorkflow._data(staging)
                    or production.get("retail_rule") != staging.get("retail_rule")
                    or production.get("artwork") != staging.get("artwork")):
                raise ValueError("baseline_runtime_product_data_differs")
            rule = production.get("retail_rule") or {}
            manifest = {"schema": "hermes-baseline-runtime-manifest/v1", "capture_id": capture_id,
                        "product_id": product_id, "production_origin": capture["production"]["site_origin"],
                        "staging_origin": capture["staging"]["site_origin"], "before_data": data,
                        "before_retail_rule": deepcopy(rule), "before_retail_script_sha256": rule.get("script_sha256"),
                        "before_artwork": deepcopy(production.get("artwork")), "changes": []}
            results = []
            for env, snapshot in (("production", production), ("staging", staging)):
                result = await asyncio.wait_for(self.verifier(env, product_id, manifest, snapshot, rollback=True), timeout=450)
                if (not isinstance(result, dict) or result.get("manifest_sha256") != digest(manifest)
                        or result.get("schema") != "hermes-storefront-verification/v1"
                        or str(result.get("product_id")) != product_id or result.get("environment") != env
                        or type(result.get("observed_at")) not in (float, int)
                        or not 0 <= self.clock() - result["observed_at"] <= 300):
                    raise ValueError("baseline_runtime_verifier_receipt_unbound")
                missing = [key for key in ALL_CHECKS if result.get("checks", {}).get(key) != "match"]
                if not missing:
                    self._runtime_evidence(result, rule, env)
                if missing:
                    holds.append({"product_id": product_id, "environment": env, "checks": missing})
                results.append({"environment": env, "snapshot_revision": snapshot["revision"], "verification": result})
            cases.append({"product_id": product_id, "manifest": manifest, "results": results})
        await self._current(capture)
        report = {"schema": "hermes-baseline-runtime-parity/v1", "capture_id": capture_id,
                  "scope": "declared_representative_products_not_full_catalog", "cases": cases, "holds": holds,
                  "observed_at": self.clock(), "configuration_writes": 0, "orders_placed": 0}
        artifact = self._json_artifact(capture_id, "runtime-parity", report)
        if holds:
            return {"status": "unknown", "kind": "runtime_parity", "reason": "baseline_runtime_checks_incomplete",
                    "holds": holds, "artifact": artifact}
        return self._receipt(capture_id, "runtime_parity", [artifact],
                             {"product_ids": list(map(str, product_ids)), "scope": report["scope"], "configuration_writes": 0})

    def _runtime_evidence(self, result, rule, environment):
        """A naked passed/checks dictionary cannot produce a parity receipt."""
        evidence = result.get("evidence") or {}
        browser = evidence.get("browser") or {}
        retail = browser.get("retail_rule") or {}
        if (not SHA.fullmatch(str(browser.get("html_sha256", "")))
                or type(browser.get("observed_at")) not in (int, float)
                or not 0 <= self.clock() - browser["observed_at"] <= 300
                or retail.get("status") != "verified" or type(retail.get("member")) is not bool
                or not SHA.fullmatch(str(retail.get("script_sha256", "")))
                or retail["script_sha256"] != rule.get("script_sha256")
                or not isinstance(browser.get("samples"), list) or not browser["samples"]):
            raise ValueError("baseline_runtime_detailed_browser_evidence_required")
        guards = browser.get("request_guards") or {}
        if any(type(guards.get(key)) is not int or guards[key] != 0 for key in ("mutating_requests", "supplier_requests", "cart_actions")):
            raise ValueError("baseline_runtime_read_only_browser_evidence_required")
        for kind in ("shipping", "customer_flow"):
            value = evidence.get(kind) or {}
            if (value.get("status") != "match" or not value.get("fixture_id")
                    or not SHA.fullmatch(str(value.get("fixture_sha256", "")))
                    or not isinstance(value.get("evidence_ids"), list) or not value["evidence_ids"]
                    or value.get("manifest_sha256") != result["manifest_sha256"]
                    or type(value.get("observed_at")) not in (int, float)
                    or not 0 <= self.clock() - value["observed_at"] <= 300
                    or value.get("mode") not in {"read_only", "staging_sandbox"}
                    or environment == "production" and value["mode"] != "read_only"):
                raise ValueError("baseline_runtime_trusted_flow_evidence_required")

    def receipts(self, capture_id):
        self.baseline._capture(capture_id)
        result = {}
        for kind in ("production_backup", "staging_backup", "unreleased_work_preserved", "runtime_parity", "isolation"):
            try:
                result[kind] = self._find_receipt(capture_id, kind)[0]
            except ValueError:
                pass
        return {"capture_id": capture_id, "receipt_hashes": result, "complete": len(result) == 5,
                "human_approval_required": True, "baseline_approved": False}
