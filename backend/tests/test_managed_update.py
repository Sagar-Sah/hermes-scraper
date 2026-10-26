"""All updater effects are synthetic temp files, SQLite, or intercepted processes."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pytest

from crawler.managed_update import (ManagedUpdater, UpdateError, backup_state, launch_environment,
    read_private, validate_checked, write_private, TRUSTED_REPOSITORY)
from crawler.update_routes import update_router

A, B = "a" * 40, "b" * 40


class GitFixture:
    def __init__(self, root):
        self.root, self.head, self.target, self.dirty, self.origin = root, A, B, False, TRUSTED_REPOSITORY
        self.calls = []
    def __call__(self, root, *args):
        self.calls.append(args)
        if args == ("rev-parse", "--show-toplevel"): return str(self.root)
        if args == ("remote", "get-url", "origin"): return self.origin
        if args == ("symbolic-ref", "--short", "HEAD"): return "main"
        if args == ("status", "--porcelain", "--untracked-files=normal"): return " M source.py" if self.dirty else ""
        if args == ("rev-parse", "HEAD"): return self.head
        if args[0] == "rev-parse" and args[1].startswith("refs/hermes-updates/"): return self.target
        if args[0] in {"fetch", "merge-base"}: return ""
        raise AssertionError(args)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "checkout"; root.mkdir()
    data = tmp_path / "existing-data"; data.mkdir()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("HERMES_MANAGED_TOKEN", "synthetic-owner-token")
    monkeypatch.setenv("HERMES_MANAGED_INSTANCE", "synthetic-instance")
    runner = GitFixture(root)
    async def prepared(): pass
    updater = ManagedUpdater(root, data, prepared, runner=runner, clock=lambda: 1000)
    write_private(updater.directory / "owner.json", {"instance": "synthetic-instance", "child_pid": os.getpid(),
        "supervisor_pid": os.getppid(), "protocol_version": 1, "token_sha256": hashlib.sha256(b"synthetic-owner-token").hexdigest()})
    return SimpleNamespace(root=root, data=data, updater=updater, git=runner)


@pytest.mark.asyncio
async def test_check_fetches_only_fixed_repository_without_source_or_install(workspace):
    result = await workspace.updater.check()
    assert result["managed"] is True and result["can_apply"] is True
    assert result["current_commit"] == A and result["target_commit"] == B
    fetch = next(call for call in workspace.git.calls if call[0] == "fetch")
    assert fetch[:4] == ("fetch", "--no-tags", "--no-recurse-submodules", TRUSTED_REPOSITORY)
    assert fetch[4].startswith("refs/heads/main:refs/hermes-updates/")
    assert ("rev-parse", fetch[4].split(":", 1)[1]) in workspace.git.calls
    assert ("rev-parse", "FETCH_HEAD") not in workspace.git.calls
    assert not any(call[0] in {"merge", "reset", "clean", "checkout"} for call in workspace.git.calls)
    assert not (workspace.updater.directory / "request.json").exists()
    operation = read_private(workspace.updater.directory / "operation.json")
    assert read_private(workspace.updater.directory / "operations" / (operation["id"] + ".json")) == operation


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["dirty_checkout", "untrusted_repository"])
async def test_dirty_or_other_repository_is_held_without_fetch_or_reset(workspace, reason):
    workspace.git.dirty = reason == "dirty_checkout"
    if reason == "untrusted_repository": workspace.git.origin = "https://example.invalid/other.git"
    with pytest.raises(UpdateError, match=reason): await workspace.updater.check()
    assert not any(call[0] in {"fetch", "reset", "clean", "merge"} for call in workspace.git.calls)
    assert workspace.updater.status()["operation"]["error_code"] == reason


@pytest.mark.asyncio
async def test_exact_checked_commit_and_id_required_before_any_drain(workspace):
    checked = await workspace.updater.check()
    called = []
    async def prepare(): called.append(True)
    workspace.updater.prepare = prepare
    for commit, check_id in (("c" * 40, checked["check_id"]), (B, "d" * 32)):
        with pytest.raises(UpdateError, match="checked_commit_mismatch"):
            await workspace.updater.apply(commit, check_id)
    assert called == []
    assert not (workspace.updater.directory / "request.json").exists()


@pytest.mark.asyncio
async def test_apply_is_durable_before_drain_and_handoff_only_after_safe_checkpoint(workspace):
    checkpoint = asyncio.Event()
    async def prepare(): await checkpoint.wait()
    workspace.updater.prepare = prepare
    checked = await workspace.updater.check()
    result = await workspace.updater.apply(B, checked["check_id"])
    assert result["operation"]["status"] == "preparing"
    assert workspace.updater.fenced is True
    assert read_private(workspace.updater.directory / "operation.json")["stage"] == "draining"
    await asyncio.sleep(0)
    assert not (workspace.updater.directory / "request.json").exists()
    checkpoint.set(); await workspace.updater.task
    request = read_private(workspace.updater.directory / "request.json")
    assert request["checked"]["target_commit"] == B
    assert request["child_pid"] == os.getpid()  # never supplied by HTTP
    assert request["instance"] == "synthetic-instance"
    assert workspace.updater.status()["operation"]["collection_paused"] is True


@pytest.mark.asyncio
async def test_failed_drain_releases_admission_fence_without_handoff_or_retry(workspace):
    released = []
    async def prepare(): raise UpdateError("independent_source_work_active")
    async def release(): released.append(True)
    workspace.updater.prepare, workspace.updater.release = prepare, release
    checked = await workspace.updater.check()
    await workspace.updater.apply(B, checked["check_id"]); await workspace.updater.task
    assert workspace.updater.fenced is False and released == [True]
    assert workspace.updater.status()["operation"]["error_code"] == "independent_source_work_active"
    assert not (workspace.updater.directory / "request.json").exists()


@pytest.mark.asyncio
async def test_checkout_drift_after_drain_cannot_be_installed(workspace):
    async def prepare(): workspace.git.head = "c" * 40
    workspace.updater.prepare = prepare
    checked = await workspace.updater.check()
    await workspace.updater.apply(B, checked["check_id"]); await workspace.updater.task
    assert workspace.updater.status()["operation"]["error_code"] == "checkout_changed"
    assert not (workspace.updater.directory / "request.json").exists()


@pytest.mark.asyncio
async def test_plain_uvicorn_or_changed_owner_cannot_apply(workspace, monkeypatch):
    checked = await workspace.updater.check()
    monkeypatch.setenv("HERMES_MANAGED_TOKEN", "different-synthetic-token")
    assert workspace.updater.status()["reason"] == "managed_restart_required"
    with pytest.raises(UpdateError, match="managed_restart_required"):
        await workspace.updater.apply(B, checked["check_id"])


@pytest.mark.asyncio
async def test_same_commit_restart_only_when_runtime_is_proven_old(workspace):
    workspace.git.target = A
    checked = await workspace.updater.check()
    assert checked["update_available"] is False
    workspace.updater.build_snapshot = lambda: {"restart_required": True}
    checked = await workspace.updater.check()
    assert checked["update_available"] is True
    await workspace.updater.apply(A, checked["check_id"]); await workspace.updater.task
    assert read_private(workspace.updater.directory / "request.json")["checked"]["restart_only"] is True


def test_check_ttl_and_old_head_never_rehydrate(workspace):
    checked = {"id": "d" * 32, "target_commit": B, "current_commit": A, "checked_at": 0}
    with pytest.raises(UpdateError, match="check_expired"):
        validate_checked(workspace.root, checked, B, checked["id"], workspace.git, lambda: 1000)


def test_sqlite_wal_key_config_and_campaign_backup_is_private_and_lossless(workspace):
    database = workspace.data / "atlas.db"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE retained(value TEXT)")
    connection.execute("INSERT INTO retained VALUES ('saved quote')"); connection.commit()
    (workspace.data / "encryption.key").write_bytes(b"synthetic-key")
    (workspace.root / ".env").write_text("ATLAS_DATA_DIR=existing-data\nATLAS_SECRET_KEY=synthetic-only\n")
    campaign = workspace.data / "campaign-automation"; campaign.mkdir()
    (campaign / "saved.json").write_text('{"attempts":200,"history":[1,2]}')
    profiles = workspace.data / "browser-profiles"; profiles.mkdir(); (profiles / "unchanged").write_bytes(b"synthetic-session")
    identifier = "e" * 32
    assert backup_state(workspace.root, workspace.data, {"ATLAS_SECRET_KEY": "synthetic-env-key"}, identifier) == identifier
    backup = workspace.data / "managed-updates/backups" / identifier
    with sqlite3.connect(backup / "atlas.db") as restored:
        assert restored.execute("SELECT value FROM retained").fetchall() == [("saved quote",)]
    assert (backup / "encryption.key").read_bytes() == b"synthetic-key"
    assert (backup / "campaign-automation/saved.json").read_bytes() == (campaign / "saved.json").read_bytes()
    assert (profiles / "unchanged").read_bytes() == b"synthetic-session"
    assert not (backup / "browser-profiles").exists()
    assert all(path.stat().st_mode & 0o077 == 0 for path in backup.rglob("*") if path.is_file())
    assert json.loads((backup / "launch-config.json").read_text())["environment"]["ATLAS_SECRET_KEY"] == "synthetic-env-key"
    connection.close()


def test_external_database_and_symlink_backup_sources_hold(workspace):
    with pytest.raises(UpdateError, match="external_database_backup_required"):
        backup_state(workspace.root, workspace.data, {"DATABASE_URL": "postgresql://synthetic"}, "e" * 32)
    (workspace.data / "atlas.db").symlink_to(workspace.root / "elsewhere")
    with pytest.raises(UpdateError, match="database_backup_failed"):
        backup_state(workspace.root, workspace.data, {}, "f" * 32)


def test_backup_reserves_disk_space_before_copying_or_stopping_a_backend(workspace, monkeypatch):
    import crawler.managed_update as module
    with sqlite3.connect(workspace.data / "atlas.db") as connection:
        connection.execute("CREATE TABLE retained(value TEXT)")
    monkeypatch.setattr(module.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    with pytest.raises(UpdateError, match="backup_space_required"):
        backup_state(workspace.root, workspace.data, {}, "e" * 32)
    assert not (workspace.data / "managed-updates/backups" / ("e" * 32) / "atlas.db").exists()


def test_launch_configuration_preserves_explicit_data_keys_and_browser_session_settings(workspace):
    (workspace.root / ".env").write_text("ATLAS_DATA_DIR=from-dotenv\nATLAS_SECRET_KEY=synthetic-env\nATLAS_BROWSER_PERSIST_SESSION=true\n")
    actual = launch_environment(workspace.root, {"ATLAS_DATA_DIR": "explicit-existing-data", "ATLAS_SECRET_KEY": "explicit-synthetic"})
    assert actual["ATLAS_DATA_DIR"] == "explicit-existing-data"
    assert actual["ATLAS_SECRET_KEY"] == "explicit-synthetic"
    assert actual["ATLAS_BROWSER_PERSIST_SESSION"] == "true"


@pytest.mark.parametrize("saved,shell,expected", [("true", None, "true"), ("false", None, "false"), ("true", "false", "false"), ("false", "true", "true")])
def test_saved_headless_setting_survives_desktop_default_and_shell_still_wins(workspace, saved, shell, expected):
    (workspace.root / ".env").write_text(f"ATLAS_BROWSER_HEADLESS={saved}\n")
    environment = {} if shell is None else {"ATLAS_BROWSER_HEADLESS": shell}
    assert launch_environment(workspace.root, environment)["ATLAS_BROWSER_HEADLESS"] == expected


@pytest.mark.parametrize("outcome", ["success", "install_failed", "dirty_after_install"])
def test_supervisor_updates_exact_owned_child_with_backup_and_no_reset_or_auto_resume(workspace, monkeypatch, outcome):
    import subprocess
    import crawler.managed_update as module
    from crawler.build_info import source_digest
    from contextlib import contextmanager
    (workspace.root / "backend/crawler").mkdir(parents=True)
    (workspace.root / "backend/crawler/fixture.py").write_text("# synthetic backend\n")
    (workspace.root / "backend/requirements.lock").write_text("# fixture\n")
    with sqlite3.connect(workspace.data / "atlas.db") as connection:
        connection.execute("CREATE TABLE retained(value TEXT)")
        connection.execute("INSERT INTO retained VALUES ('unchanged')")
    checked = {"id": "a" * 32, "current_commit": A, "target_commit": B, "checked_at": 1000, "update_available": True}
    calls, children, handlers, sleeps = [], [], {}, []
    original_clean, original_validate = module.clean_head, module.validate_checked
    def runner(root, *args):
        if args[:1] == ("merge",):
            assert args == ("merge", "--ff-only", "--no-edit", B)
            calls.append("merge"); workspace.git.head = B; return ""
        return workspace.git(root, *args)
    monkeypatch.setattr(module, "git", runner)
    monkeypatch.setattr(module, "clean_head", lambda root, supplied_runner=None: original_clean(root, supplied_runner or runner))
    monkeypatch.setattr(module, "validate_checked", lambda root, value, expected, identifier:
        original_validate(root, value, expected, identifier, runner, lambda: 1000))
    monkeypatch.setattr(module, "launch_environment", lambda root, env: {
        "ATLAS_DATA_DIR": str(workspace.data), "ATLAS_SECRET_KEY": "synthetic-only"})
    class Child:
        def __init__(self, command, cwd, env):
            self.pid, self.terminated = 100000 + len(children), False
            assert cwd == workspace.root
            assert command[:3] == [str(workspace.root / ".venv/bin/python"), "-m", "uvicorn"]
            assert env["ATLAS_DATA_DIR"] == str(workspace.data)
            children.append(self); calls.append("start")
        def poll(self): return 0 if self.terminated else None
        def terminate(self): self.terminated = True; calls.append("terminate")
        def wait(self, timeout): assert self.terminated; return 0
        def kill(self): raise AssertionError("must never kill a supplier/browser owner")
    monkeypatch.setattr(module.subprocess, "Popen", Child)
    def prepare(command, cwd, env, check, timeout):
        assert command == ["bash", str(workspace.root / "scripts/start-local.sh"), "--prepare-only"]
        assert env["HERMES_UPDATE_PREPARING"]
        assert env["ATLAS_DATA_DIR"] == str(workspace.data)
        assert children[0].terminated
        calls.append("prepare")
        if outcome == "install_failed": raise subprocess.CalledProcessError(1, command)
        if outcome == "dirty_after_install": workspace.git.dirty = True
    monkeypatch.setattr(module.subprocess, "run", prepare)
    class Sidecar:
        def __init__(self, *args): calls.append("status_server")
        def close(self): calls.append("status_close")
    monkeypatch.setattr(module, "StatusServer", Sidecar)
    monkeypatch.setattr(module.signal, "signal", lambda number, handler: handlers.update({number: handler}))
    @contextmanager
    def response(*args, **kwargs):
        assert args[0] == "http://127.0.0.1:18877/api/v1/health"
        value = {"build": {"restart_required": False, "runtime_backend_sha256": source_digest(workspace.root),
            "checkout_id": hashlib.sha256(str(workspace.root).encode()).hexdigest()[:16], "frontend": {"rebuild_required": False}}}
        yield SimpleNamespace(read=lambda bound: json.dumps(value).encode())
    monkeypatch.setattr(module, "build_opener", lambda *args: SimpleNamespace(open=response))
    def sleep(seconds):
        sleeps.append(seconds)
        assert len(sleeps) < 8
        operation = workspace.updater._read("operation.json") or {}
        if operation.get("status") in {"completed", "failed"}:
            handlers[module.signal.SIGTERM]()
            return
        owner = read_private(workspace.updater.directory / "owner.json")
        workspace.updater._operation(id="d" * 32, kind="apply", status="running", stage="backing_up", collection_paused=True)
        write_private(workspace.updater.directory / "checked.json", checked)
        write_private(workspace.updater.directory / "request.json", {"operation_id": "d" * 32,
            "instance": owner["instance"], "child_pid": owner["child_pid"], "checked": checked})
    monkeypatch.setattr(module.time, "sleep", sleep)
    module.supervise(workspace.root, 18877)
    operation = read_private(workspace.updater.directory / "operation.json")
    assert operation["collection_paused"] is True
    assert (workspace.data / "managed-updates/backups" / ("d" * 32) / "atlas.db").is_file()
    assert calls.index("terminate") < calls.index("merge") < calls.index("prepare")
    assert len(children) == (2 if outcome == "success" else 1)
    assert operation["status"] == ("completed" if outcome == "success" else "failed")
    if outcome == "success":
        assert read_private(workspace.updater.directory / "checked.json")["current_commit"] == B
    assert not any(call[0] in {"reset", "clean", "checkout"} for call in workspace.git.calls)
    with sqlite3.connect(workspace.data / "atlas.db") as connection:
        assert connection.execute("SELECT value FROM retained").fetchall() == [("unchanged",)]


def test_routes_require_existing_auth_and_same_origin_local_client(workspace):
    app = FastAPI()
    async def auth(request: Request):
        if request.headers.get("authorization") != "Bearer synthetic-api-token": raise HTTPException(401)
    app.include_router(update_router(lambda: workspace.updater, auth))
    headers = {"Authorization": "Bearer synthetic-api-token", "Origin": "http://127.0.0.1:18877"}
    with TestClient(app, base_url="http://127.0.0.1:18877", client=("127.0.0.1", 4444)) as client:
        assert client.get("/api/v1/updates/status").status_code == 401
        assert client.get("/api/v1/updates/status", headers=headers).status_code == 200
        assert workspace.git.calls == []
        assert client.post("/api/v1/updates/check", headers={"Authorization": headers["Authorization"]}, json={}).status_code == 403
        assert client.post("/api/v1/updates/check", headers={**headers, "Origin": "https://other.invalid"}, json={}).status_code == 403
        assert client.post("/api/v1/updates/check", headers=headers, json={}).status_code == 200
        assert client.post("/api/v1/updates/check", headers=headers, json={"url": "https://other.invalid/repository"}).status_code == 422
        assert client.post("/api/v1/updates/apply", headers=headers, json={"expected_commit": B, "check_id": "a" * 32, "pid": 1}).status_code == 422
    with TestClient(app, base_url="http://127.0.0.1:18877", client=("192.0.2.5", 4444)) as client:
        assert client.post("/api/v1/updates/check", headers=headers, json={}).status_code == 403


def test_supervisor_preparation_requires_private_token_and_dead_owned_backend(workspace, monkeypatch):
    from crawler.managed_update import verify_preparation
    monkeypatch.setenv("ATLAS_DATA_DIR", str(workspace.data))
    monkeypatch.setenv("HERMES_UPDATE_PREPARING", "synthetic-instance")
    workspace.updater._operation(id="a" * 32, kind="apply", status="running", stage="installing")
    probes = []
    def dead(pid, sig):
        probes.append((pid, sig)); raise ProcessLookupError()
    monkeypatch.setattr(os, "kill", dead)
    verify_preparation(workspace.root)
    assert probes == [(os.getpid(), 0)]  # liveness probe only, never a signal
    monkeypatch.setattr(os, "kill", lambda *args: None)
    with pytest.raises(UpdateError, match="backend_not_stopped"): verify_preparation(workspace.root)
    monkeypatch.setenv("HERMES_MANAGED_TOKEN", "unowned")
    with pytest.raises(UpdateError, match="unowned_update_preparation"): verify_preparation(workspace.root)


def test_restart_status_server_is_authenticated_and_never_exposes_private_launch_config(workspace):
    from crawler.managed_update import StatusServer
    from urllib.request import Request as URLRequest, urlopen
    from urllib.error import HTTPError
    workspace.updater._operation(id="a" * 32, kind="apply", status="running", stage="installing", collection_paused=True)
    server = StatusServer(0, workspace.updater, "synthetic-api-token")
    try:
        url = f"http://127.0.0.1:{server.server.server_port}/api/v1/updates/status"
        with pytest.raises(HTTPError) as error: urlopen(url)
        assert error.value.code == 401
        with urlopen(URLRequest(url, headers={"Authorization": "Bearer synthetic-api-token"})) as response:
            raw = response.read(); value = json.loads(raw)
        assert value["managed"] is True and value["can_apply"] is False and value["can_check"] is False
        assert value["operation"]["stage"] == "installing"
        assert b"synthetic-owner-token" not in raw and str(workspace.data).encode() not in raw
    finally: server.close()


@pytest.mark.asyncio
async def test_campaign_fence_finishes_current_chunk_and_preserves_job_history(tmp_path):
    from crawler.campaign_automation import CampaignAutomation, Profile
    gate, entered, calls = asyncio.Event(), asyncio.Event(), []
    async def execute(job, check):
        await check(); calls.append(job["product_id"]); entered.set(); await gate.wait(); await check()
        return {"state": "continue", "result_reference": "saved/immutable-receipt.json",
            "progress_count": 1, "progress_token": "a" * 64}
    rows = [{"campaign_id": "campaign", "product_id": name, "supplier": "supplier",
        "source_url": "https://fixture.invalid/" + name} for name in ("one", "two")]
    profile = Profile("profile", "supplier", lambda _: True,
        lambda a, n: {"configurations": [{"mode": "native"}], "blockers": []}, lambda c: c, execute)
    queue = CampaignAutomation(tmp_path, [profile], lambda _: rows)
    try:
        plan = await queue.plan("campaign"); await queue.start("campaign", plan["plan_id"])
        await queue.tick(); await entered.wait(); await queue.begin_maintenance()
        with pytest.raises(ValueError, match="workers_busy"): await queue.pause_drained_for_update()
        await queue.tick(); assert len(calls) == 1
        gate.set(); await asyncio.gather(*(task for _, task in queue._active.values()))
        await queue.tick()
        jobs = deepcopy(queue._records["campaign"]["jobs"])
        assert any(job["progress_count"] == 1 and job["attempts"] == 1 for job in jobs.values())
        await queue.pause_drained_for_update(); await queue.end_maintenance(); await queue.tick()
        assert queue._records["campaign"]["jobs"] == jobs
        assert queue._records["campaign"]["enabled"] is False
        assert len(calls) == 1  # explicit user resume needed
    finally: await queue.close()


@pytest.mark.asyncio
async def test_engine_fence_waits_for_claim_pass_without_cancelling_owned_work():
    from crawler.engine import Engine
    engine = Engine.__new__(Engine)
    engine.admission_lock, engine.maintenance, engine.stopping = asyncio.Lock(), False, False
    called = []
    async def dispatch_once(): called.append(True); return False
    engine.dispatch_once = dispatch_once
    await engine.admission_lock.acquire()
    fence = asyncio.create_task(engine.begin_maintenance()); await asyncio.sleep(0)
    assert not fence.done()
    engine.admission_lock.release(); await fence
    loop = asyncio.create_task(engine.dispatch()); await asyncio.sleep(.01)
    assert called == []
    engine.stopping = True; await loop
    await engine.end_maintenance(); assert engine.maintenance is False


@pytest.mark.asyncio
async def test_busy_engine_admission_lock_respects_update_drain_deadline():
    from crawler.update_routes import drain_for_update
    released = []
    class Queue:
        _active = {}
        async def begin_maintenance(self): pass
        async def tick(self): pass
        async def end_maintenance(self): released.append(True)
    class Engine:
        async def begin_maintenance(self): await asyncio.Event().wait()
    app = SimpleNamespace(state=SimpleNamespace(campaign_automation=Queue(), engine=Engine()))
    with pytest.raises(UpdateError, match="workers_busy"):
        await drain_for_update(app, object(), timeout=.01)
    assert released == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["dps_pilots", "b2sign_harvests", "source_collections", "product_diagnoses", "domain_sessions"])
async def test_independent_source_contexts_hold_update_instead_of_cancellation(workspace, field):
    from crawler.update_routes import drain_for_update
    events = []
    class Queue:
        _active = {}
        async def begin_maintenance(self): events.append("fence")
        async def tick(self): pass
        async def end_maintenance(self): events.append("release")
    manager = SimpleNamespace(_tasks={"active": object()})
    if field == "product_diagnoses": manager = SimpleNamespace(_tasks={}, _pages={"ready": object()})
    if field == "domain_sessions": manager = SimpleNamespace(sessions={"owned": object()})
    app = SimpleNamespace(state=SimpleNamespace(campaign_automation=Queue(), engine=object(), **{field: manager}))
    with pytest.raises(UpdateError, match="independent_source_work_active"):
        await drain_for_update(app, object(), timeout=.1)
    assert events == ["fence", "release"]


@pytest.mark.asyncio
async def test_interrupted_update_pauses_before_any_worker_open_without_resetting_products(workspace):
    from crawler.config import Settings
    from crawler.store import Store, Run, Product
    from crawler.update_routes import reconcile_interrupted_update
    from sqlalchemy import select
    store = Store(Settings(data_dir=workspace.data))
    with store.session() as session:
        session.add(Run(id="retained-run", name="saved", status="queued", spec={}))
        session.add(Product(id="retained-product", run_id="retained-run", url="https://fixture.invalid/item", status="queued", epoch=7))
    workspace.updater._operation(id="d" * 32, kind="apply", status="failed", collection_paused=False)
    calls = []
    class Queue:
        async def begin_maintenance(self): calls.append("fence")
        async def pause_drained_for_update(self): calls.append("paused")
        async def end_maintenance(self): calls.append("release")
    app = SimpleNamespace(state=SimpleNamespace(updater=workspace.updater, campaign_automation=Queue()))
    await reconcile_interrupted_update(app, store)
    with store.session() as session:
        assert session.get(Run, "retained-run").status == "paused"
        product = session.get(Product, "retained-product")
        assert product.epoch == 7 and product.status == "queued"
    assert calls == ["fence", "paused", "release"]
    assert workspace.updater.status()["operation"]["collection_paused"] is True
