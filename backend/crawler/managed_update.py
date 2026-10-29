"""Fixed-repository, supervised local updates. No application imports or shell commands.

The HTTP process can request an update, but only its owning supervisor can stop
and restart it. All operation files and backups are private; public status is a
closed projection. A completed drain stays paused across update/install failures;
failures before a safe drain leave existing work untouched.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import quote
from urllib.request import ProxyHandler, build_opener
import uuid

TRUSTED_REPOSITORY = "https://github.com/Sagar-Sah/hermes-scraper.git"
TRUSTED_ORIGINS = {TRUSTED_REPOSITORY, TRUSTED_REPOSITORY[:-4], "git@github.com:Sagar-Sah/hermes-scraper.git"}
SHA = re.compile(r"[0-9a-f]{40}")
ROOT = Path(__file__).resolve().parents[2]
CHECK_TTL = 900
PROTOCOL_VERSION = 1


class UpdateError(ValueError):
    pass


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
        raise UpdateError("unsafe_update_directory")
    return path


def write_private(path, value):
    path = Path(path)
    private_directory(path.parent)
    if path.is_symlink():
        raise UpdateError("unsafe_update_file")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def read_private(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid() or info.st_size > 1_000_000:
        raise UpdateError("unsafe_update_file")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as stream:
        return json.load(stream)


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), "-c", "core.hooksPath=/dev/null", *args],
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/usr/bin/false"},
        capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise UpdateError("git_command_failed")
    return result.stdout.strip()


def clean_head(root, runner=git):
    if Path(runner(root, "rev-parse", "--show-toplevel")).resolve() != Path(root).resolve():
        raise UpdateError("not_repository_root")
    if runner(root, "remote", "get-url", "origin") not in TRUSTED_ORIGINS:
        raise UpdateError("untrusted_repository")
    if runner(root, "symbolic-ref", "--short", "HEAD") != "main":
        raise UpdateError("main_branch_required")
    if runner(root, "status", "--porcelain", "--untracked-files=normal"):
        raise UpdateError("dirty_checkout")
    head = runner(root, "rev-parse", "HEAD")
    if not SHA.fullmatch(head):
        raise UpdateError("invalid_commit")
    return head


def validate_checked(root, checked, expected_commit, check_id, runner=git, now=time.time):
    if (not isinstance(expected_commit, str) or not SHA.fullmatch(expected_commit)
            or checked.get("id") != check_id or checked.get("target_commit") != expected_commit):
        raise UpdateError("checked_commit_mismatch")
    age = now() - checked["checked_at"]
    if not 0 <= age <= CHECK_TTL:
        raise UpdateError("check_expired")
    if clean_head(root, runner) != checked["current_commit"]:
        raise UpdateError("checkout_changed")
    if expected_commit == checked["current_commit"] and checked.get("restart_only") is not True:
        raise UpdateError("no_update")
    runner(root, "merge-base", "--is-ancestor", checked["current_commit"], expected_commit)


def public_operation(value):
    if not value:
        return None
    keys = ("id", "kind", "status", "stage", "error_code", "created_at", "updated_at", "collection_paused", "backup_id")
    return {key: value.get(key) for key in keys}


class ManagedUpdater:
    def __init__(self, root, data_dir, prepare, *, runner=git, clock=time.time, build_snapshot=None, release=None):
        self.root, self.data_dir, self.prepare = Path(root).resolve(), Path(data_dir).resolve(), prepare
        self.directory = private_directory(self.data_dir / "managed-updates")
        self.runner, self.clock = runner, clock
        self.build_snapshot = build_snapshot or (lambda: {})
        self.release = release
        self.lock = asyncio.Lock()
        self.task = None
        existing = self._read("operation.json") or {}
        self.fenced = existing.get("kind") == "apply" and existing.get("status") in {"preparing", "running"}

    def _read(self, name):
        try: return read_private(self.directory / name)
        except FileNotFoundError: return None

    def managed(self):
        owner = self._read("owner.json") or {}
        token = os.environ.get("HERMES_MANAGED_TOKEN", "")
        return bool(token and owner.get("child_pid") == os.getpid()
            and owner.get("protocol_version") == PROTOCOL_VERSION
            and owner.get("supervisor_pid") == os.getppid()
            and owner.get("instance") == os.environ.get("HERMES_MANAGED_INSTANCE")
            and secrets.compare_digest(owner.get("token_sha256", ""), hashlib.sha256(token.encode()).hexdigest()))

    def status(self):
        checked, operation = self._read("checked.json") or {}, self._read("operation.json")
        managed = self.managed()
        database_url = os.environ.get("DATABASE_URL", "")
        reason = None if managed else "managed_restart_required"
        if database_url and not database_url.startswith("sqlite:///"):
            reason = "external_database_backup_required"
        busy = bool(operation and operation["status"] in {"preparing", "running"})
        return {"managed": managed, "can_check": not busy, "can_apply": managed and not reason and not busy
                and bool(checked.get("update_available")), "current_commit": checked.get("current_commit"),
                "target_commit": checked.get("target_commit"), "check_id": checked.get("id"),
                "update_available": bool(checked.get("update_available")), "reason": reason,
                "operation": public_operation(operation)}

    def startup_pause_required(self):
        operation = self._read("operation.json") or {}
        return operation.get("kind") == "apply" and operation.get("status") in {"preparing", "running", "failed"} and operation.get("collection_paused") is not True

    def _operation(self, **changes):
        value = self._read("operation.json") or {}
        value.update(changes, updated_at=self.clock())
        write_private(self.directory / "operation.json", value)
        if value.get("id"):
            write_private(self.directory / "operations" / (value["id"] + ".json"), value)
        return value

    async def check(self):
        async with self.lock:
            if not self.status()["can_check"]:
                raise UpdateError("update_in_progress")
            self._operation(id=uuid.uuid4().hex, kind="check", status="running", stage="checking",
                error_code=None, created_at=self.clock(), collection_paused=False, backup_id=None)
            try:
                current = await asyncio.to_thread(clean_head, self.root, self.runner)
                # A private generated ref binds this fetch, unlike FETCH_HEAD
                # which another ordinary git fetch can replace concurrently.
                target_ref = "refs/hermes-updates/" + uuid.uuid4().hex
                await asyncio.to_thread(self.runner, self.root, "fetch", "--no-tags", "--no-recurse-submodules", TRUSTED_REPOSITORY, "refs/heads/main:" + target_ref)
                target = await asyncio.to_thread(self.runner, self.root, "rev-parse", target_ref)
                if not SHA.fullmatch(target): raise UpdateError("invalid_commit")
                await asyncio.to_thread(self.runner, self.root, "merge-base", "--is-ancestor", current, target)
                build = self.build_snapshot()
                restart_only = target == current and bool(build.get("restart_required") or build.get("frontend", {}).get("rebuild_required"))
                checked = {"id": uuid.uuid4().hex, "current_commit": current, "target_commit": target,
                    "checked_at": self.clock(), "update_available": target != current or restart_only, "restart_only": restart_only}
                write_private(self.directory / "checked.json", checked)
                self._operation(status="completed", stage="completed")
            except (UpdateError, OSError, subprocess.SubprocessError) as error:
                self._operation(status="failed", stage="failed", error_code=str(error) if isinstance(error, UpdateError) else "update_check_failed")
                raise
            return self.status()

    async def apply(self, expected_commit, check_id):
        async with self.lock:
            if not self.managed(): raise UpdateError("managed_restart_required")
            if not self.status()["can_apply"]: raise UpdateError(self.status()["reason"] or "update_not_ready")
            checked = self._read("checked.json") or {}
            await asyncio.to_thread(validate_checked, self.root, checked, expected_commit, check_id, self.runner, self.clock)
            self._operation(id=uuid.uuid4().hex, kind="apply", status="preparing", stage="draining",
                error_code=None, created_at=self.clock(), collection_paused=False, backup_id=None)
            self.fenced = True
            self.task = asyncio.create_task(self._prepare(checked))
            return self.status()

    async def reconcile_failure(self):
        if self.fenced and (self._read("operation.json") or {}).get("status") in {"failed", "completed"}:
            if self.release: await self.release()
            self.fenced = False

    async def _prepare(self, checked):
        try:
            await self.prepare()
            if not self.managed(): raise UpdateError("supervisor_changed")
            await asyncio.to_thread(validate_checked, self.root, checked, checked["target_commit"], checked["id"], self.runner, self.clock)
            operation = self._operation(status="running", stage="backing_up", collection_paused=True)
            owner = self._read("owner.json")
            write_private(self.directory / "request.json", {"operation_id": operation["id"], "instance": owner["instance"],
                "child_pid": os.getpid(), "checked": checked})
        except (Exception, asyncio.CancelledError) as error:
            code = str(error) if isinstance(error, UpdateError) else "drain_failed"
            self._operation(status="failed", stage="failed", error_code=code)
            await self.reconcile_failure()


def database_path(root, data_dir, env):
    url = env.get("DATABASE_URL", "")
    if url and not url.startswith("sqlite:///"):
        raise UpdateError("external_database_backup_required")
    return Path(url[len("sqlite:///"):]).resolve() if url else Path(data_dir) / "atlas.db"


def _sqlite_backup_snapshot(source, target):
    """One independent committed snapshot; never replay copied WAL/SHM files."""
    if source.is_symlink() or not source.is_file() or target.is_symlink():
        raise UpdateError("unsafe_backup_source")
    fd = os.open(target, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    os.chmod(target, 0o600)
    uri = "file:" + quote(str(source.absolute()), safe="/") + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as incoming, closing(sqlite3.connect(target)) as outgoing:
        incoming.backup(outgoing)
        outgoing.execute("PRAGMA journal_mode=DELETE")
        if outgoing.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise UpdateError("database_backup_failed")


def _sqlite_backup_source(path):
    if path.is_symlink() or not path.is_file():
        raise UpdateError("unsafe_backup_source")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        header = stream.read(16)
    return header == b"SQLite format 3\x00" or path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}


def backup_state(root, data_dir, env, operation_id):
    if not isinstance(operation_id, str) or re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
        raise UpdateError("invalid_operation_id")
    destination = private_directory(Path(data_dir) / "managed-updates/backups" / operation_id)
    source = database_path(root, data_dir, env)
    if source.is_symlink() or not source.is_file(): raise UpdateError("database_backup_failed")
    state_trees = []
    for name in ("campaigns", "campaign-automation", "printograph-owned-browser-runs",
                 "product-companion", "autonomous-companion"):
        folder = Path(data_dir) / name
        if folder.is_symlink(): raise UpdateError("unsafe_backup_source")
        if not folder.exists(): continue
        if not folder.is_dir(): raise UpdateError("unsafe_backup_source")
        paths = list(folder.rglob("*"))
        for path in paths:
            info = path.lstat()
            if (path.is_symlink() or info.st_uid != os.getuid()
                    or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
                raise UpdateError("unsafe_backup_source")
        state_trees.append((name, folder, paths))
    wal = Path(str(source) + "-wal")
    needed = source.stat().st_size + 2 * (wal.stat().st_size if wal.is_file() else 0) + 64 * 1024 * 1024
    needed += 2 * sum(path.stat().st_size for _, _, paths in state_trees for path in paths if path.is_file())
    if shutil.disk_usage(destination).free < needed:
        raise UpdateError("backup_space_required")
    _sqlite_backup_snapshot(source, destination / "atlas.db")
    for source_file, target_name in ((Path(root) / ".env", "dotenv"), (Path(data_dir) / "encryption.key", "encryption.key")):
        if source_file.exists():
            if source_file.is_symlink() or not source_file.is_file(): raise UpdateError("unsafe_backup_source")
            shutil.copyfile(source_file, destination / target_name); os.chmod(destination / target_name, 0o600)
    for name, folder, paths in state_trees:
        target = private_directory(destination / name)
        for path in paths:
            if path.is_symlink(): raise UpdateError("unsafe_backup_source")
            relative = path.relative_to(folder)
            if path.is_dir(): private_directory(target / relative)
            elif path.is_file():
                # Online backup has already incorporated committed WAL state.
                # Sidecars must never accompany the independent destination DB.
                sidecar = next((suffix for suffix in ("-wal", "-shm", "-journal") if path.name.endswith(suffix)), None)
                if sidecar:
                    primary = path.with_name(path.name[:-len(sidecar)])
                    if not primary.is_file() or primary.is_symlink(): raise UpdateError("unsafe_backup_source")
                    if _sqlite_backup_source(primary): continue
                private_directory((target / relative).parent)
                if _sqlite_backup_source(path):
                    _sqlite_backup_snapshot(path, target / relative)
                else:
                    if (target / relative).is_symlink(): raise UpdateError("unsafe_backup_source")
                    shutil.copyfile(path, target / relative); os.chmod(target / relative, 0o600)
    write_private(destination / "launch-config.json", {"environment": {k: v for k, v in env.items()
        if k.startswith(("ATLAS_", "AWS_")) or k == "DATABASE_URL"}, "profiles_preserved_in_place": True})
    return operation_id


class StatusServer:
    """Keeps authenticated progress reachable while the application is stopped."""
    def __init__(self, port, updater, token):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/api/v1/updates/status":
                    self.send_error(503); return
                if token and not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                    self.send_error(401); return
                status_value = updater.status()
                status_value.update(managed=True, can_check=False, can_apply=False,
                    reason="local_restart_required" if (status_value.get("operation") or {}).get("status") == "failed" else None)
                body = json.dumps(status_value).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
            def log_message(self, *args): pass
        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown(); self.thread.join(); self.server.server_close()


def launch_environment(root, environment):
    from dotenv import dotenv_values
    merged = {**{k: v for k, v in dotenv_values(Path(root) / ".env").items() if v is not None}, **environment}
    if "ATLAS_BROWSER_HEADLESS" not in merged:
        merged["ATLAS_BROWSER_HEADLESS"] = "false" if sys.platform == "darwin" or merged.get("DISPLAY") or merged.get("WAYLAND_DISPLAY") else "true"
    return merged


def supervise(root, port):
    # Called only after dependency/build preparation. dotenv never overrides the
    # shell's existing data directory, keys, port or browser settings.
    env = launch_environment(root, dict(os.environ))
    data_dir = Path(env.get("ATLAS_DATA_DIR", str(Path(root) / "data"))).resolve()
    directory = private_directory(data_dir / "managed-updates")
    lock_fd = os.open(directory / "supervisor.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    instance, token = uuid.uuid4().hex, secrets.token_urlsafe(48)
    env.update(HERMES_MANAGED_INSTANCE=instance, HERMES_MANAGED_TOKEN=token)
    command = [str(Path(root) / ".venv/bin/python"), "-m", "uvicorn", "crawler.app:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"]
    updater = ManagedUpdater(root, data_dir, None)
    old_operation = updater._read("operation.json")
    if old_operation and old_operation.get("status") in {"running", "preparing"}:
        updater._operation(status="failed", stage="failed", error_code="update_interrupted")
    (directory / "request.json").unlink(missing_ok=True)
    child, sidecar = None, None
    stopping = False
    def stop_signal(*_):
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None: child.terminate()
    signal.signal(signal.SIGTERM, stop_signal); signal.signal(signal.SIGINT, stop_signal)
    try:
        while not stopping:
            if child is None:
                child = subprocess.Popen(command, cwd=root, env=env)
                write_private(directory / "owner.json", {"instance": instance, "child_pid": child.pid,
                    "supervisor_pid": os.getpid(), "protocol_version": PROTOCOL_VERSION, "token_sha256": hashlib.sha256(token.encode()).hexdigest()})
            if child.poll() is not None:
                return child.returncode
            try: request = read_private(directory / "request.json")
            except FileNotFoundError:
                time.sleep(0.2); continue
            (directory / "request.json").unlink()
            if request.get("instance") != instance or request.get("child_pid") != child.pid:
                updater._operation(status="failed", stage="failed", error_code="supervisor_changed"); continue
            operation = updater._read("operation.json") or {}
            if request.get("operation_id") != operation.get("id") or operation.get("kind") != "apply":
                raise UpdateError("update_request_mismatch")
            try:
                checked = request["checked"]
                validate_checked(root, checked, checked["target_commit"], checked["id"])
                try: backup_id = backup_state(root, data_dir, env, operation["id"])
                except sqlite3.Error: raise UpdateError("database_backup_failed") from None
                updater._operation(stage="stopping", backup_id=backup_id)
                child.terminate()
                try: child.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    # Never escalate to SIGKILL while a supplier/browser may be
                    # persisting. The operator can inspect this held shutdown.
                    raise UpdateError("graceful_shutdown_timeout") from None
                sidecar = StatusServer(port, updater, env.get("ATLAS_API_TOKEN", ""))
                updater._operation(stage="updating")
                validate_checked(root, checked, checked["target_commit"], checked["id"])
                git(root, "merge", "--ff-only", "--no-edit", checked["target_commit"])
                updater._operation(stage="installing")
                # The launcher reuses exact effective configuration and stamps;
                # prepare-only never starts another worker/supervisor.
                subprocess.run(["bash", str(Path(root) / "scripts/start-local.sh"), "--prepare-only"],
                    cwd=root, env={**env, "HERMES_UPDATE_PREPARING": instance}, check=True, timeout=900)
                if clean_head(root) != checked["target_commit"]: raise UpdateError("checkout_changed")
                updater._operation(stage="restarting")
                sidecar.close(); sidecar = None
                child = subprocess.Popen(command, cwd=root, env=env)
                write_private(directory / "owner.json", {"instance": instance, "child_pid": child.pid,
                    "supervisor_pid": os.getpid(), "protocol_version": PROTOCOL_VERSION, "token_sha256": hashlib.sha256(token.encode()).hexdigest()})
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    if child.poll() is not None: raise UpdateError("restart_failed")
                    try:
                        with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{port}/api/v1/health", timeout=2) as response:
                            health = json.loads(response.read(65536))
                        try:
                            from .build_info import source_digest
                        except ImportError:
                            from build_info import source_digest
                        build = health.get("build", {})
                        if (build.get("restart_required") is False
                                and build.get("runtime_backend_sha256") == source_digest(Path(root), "backend")
                                and build.get("checkout_id") == hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]
                                and build.get("frontend", {}).get("rebuild_required") is False):
                            break
                    except (OSError, ValueError): pass
                    time.sleep(0.25)
                else: raise UpdateError("restart_health_timeout")
                checked.update(current_commit=checked["target_commit"], update_available=False, restart_only=False)
                write_private(directory / "checked.json", checked)
                updater._operation(status="completed", stage="completed", error_code=None, collection_paused=True)
            except (UpdateError, OSError, subprocess.SubprocessError) as error:
                code = str(error) if isinstance(error, UpdateError) else "update_install_failed"
                updater._operation(status="failed", stage="failed", error_code=code, collection_paused=True)
                if child.poll() is not None:
                    if sidecar is None: sidecar = StatusServer(port, updater, env.get("ATLAS_API_TOKEN", ""))
                    # Keep failure/status reachable. A new explicit local start
                    # is required; never reset Git or guess a rollback schema.
                    while not stopping: time.sleep(0.25)
                    return 1
    finally:
        if sidecar: sidecar.close()
        if child is not None and child.poll() is None:
            child.terminate()
            try: child.wait(timeout=90)
            except subprocess.TimeoutExpired: pass
        (directory / "owner.json").unlink(missing_ok=True)
        os.close(lock_fd)


def verify_preparation(root):
    from dotenv import load_dotenv
    load_dotenv(Path(root) / ".env", override=False)
    directory = Path(os.environ.get("ATLAS_DATA_DIR", str(Path(root) / "data"))).resolve() / "managed-updates"
    owner, operation = read_private(directory / "owner.json"), read_private(directory / "operation.json")
    token = os.environ.get("HERMES_MANAGED_TOKEN", "")
    if (not token or owner.get("instance") != os.environ.get("HERMES_UPDATE_PREPARING")
            or owner.get("protocol_version") != PROTOCOL_VERSION
            or not secrets.compare_digest(owner.get("token_sha256", ""), hashlib.sha256(token.encode()).hexdigest())
            or operation.get("kind") != "apply" or operation.get("status") != "running" or operation.get("stage") != "installing"):
        raise UpdateError("unowned_update_preparation")
    try: os.kill(owner["child_pid"], 0)
    except ProcessLookupError: return
    raise UpdateError("backend_not_stopped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["supervise", "verify-preparation"])
    parser.add_argument("--port", type=int, default=18877)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    if args.action == "verify-preparation":
        verify_preparation(args.root.resolve())
    else:
        raise SystemExit(supervise(args.root.resolve(), args.port))
