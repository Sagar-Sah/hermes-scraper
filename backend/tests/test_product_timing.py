"""Timing uses temp SQLite and fake clocks; no browser, supplier or live state."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, event, select

from crawler import timing
from crawler.config import Settings
from crawler.store import Product, ProductTiming, Run, Store


def test_active_is_monotonic_owned_time_not_created_elapsed():
    record = timing.new_record("queued", 1000, 10, "one", created_at=1000, complete=True)
    record = timing.advance(record, "active", 4600, 3610, "one")
    actual = timing.snapshot(record, "active", 4610, 3620, "one", live=True, live_until=4660)
    assert actual["queued_seconds"] == 3600
    assert actual["active_seconds"] == 10
    assert actual["elapsed_seconds"] == 3610
    assert actual["active_now"] is True and actual["live_until"] == 4620


def test_waiting_queue_pause_attention_and_finished_are_distinct_and_freeze():
    record = timing.new_record("waiting", 100, 10, "one", created_at=100, complete=True, waiting_until=110)
    record = timing.advance(record, "active", 120, 30, "one")
    record = timing.advance(record, "paused", 140, 50, "one")
    record = timing.advance(record, "attention", 150, 60, "one")
    record = timing.advance(record, "finished", 160, 70, "one")
    actual = timing.snapshot(record, "finished", 9999, 9909, "one")
    assert actual["waiting_seconds"] == 10 and actual["queued_seconds"] == 10
    assert actual["active_seconds"] == 20 and actual["paused_seconds"] == 10 and actual["attention_seconds"] == 10
    assert actual["elapsed_seconds"] == 60 and actual["active_now"] is False


def test_restart_drops_unconfirmed_active_tail_and_downtime():
    record = timing.new_record("active", 100, 10, "one", created_at=100, complete=True)
    record = timing.advance(record, "active", 105, 15, "one")
    resumed = timing.advance(record, "active", 3705, 999, "two")
    assert resumed["seconds"]["active"] == 5
    assert resumed["complete_history"] is False
    result = timing.snapshot(resumed, "active", 3710, 1004, "two", live=True)
    assert result["active_seconds"] == 10 and result["coverage"] == "partial"
    assert result["active_since"] == 3705


def test_clock_change_cannot_add_an_hour_of_active_time():
    record = timing.new_record("active", 100, 10, "one", created_at=100, complete=True)
    actual = timing.snapshot(record, "active", 3701, 11, "one", live=True)
    assert actual["active_seconds"] == 1 and actual["clock_anomaly"] is True
    rollback = timing.advance(record, "paused", 99, 9, "one")
    assert rollback["seconds"]["active"] == 0 and rollback["complete_history"] is False


def test_legacy_missing_timer_is_unknown_even_when_created_elapsed_is_known():
    result = timing.snapshot(None, "queued", 3700, 10, "one", created_at=100, scope="campaign_job")
    assert result["elapsed_seconds"] == 3600
    assert result["active_seconds"] is None and result["coverage"] == "unknown"
    assert result["active_now"] is False
    completed = timing.snapshot(None, "finished", 9999, 20, "one", created_at=100, ended_at=3700)
    assert completed["elapsed_seconds"] == 3600  # does not keep growing after completion
    assert timing.snapshot(None, "finished", 1000, 100, "one", created_at=100)["elapsed_seconds"] is None
    unobserved = timing.new_record("queued", 100, 10, "one", created_at=100, complete=True)
    assert timing.snapshot(unobserved, "finished", 9999, 999, "one")["elapsed_seconds"] is None


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    import crawler.store as module
    clock = SimpleNamespace(wall=1000., mono=100.)
    monkeypatch.setattr(module.time, "time", lambda: clock.wall)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock.mono)
    store = Store(Settings(data_dir=tmp_path))
    with store.session() as session:
        run = Run(id="run", name="timed", status="queued", spec={})
        session.add(run); session.add(Product(id="product", run_id="run", url="https://fixture.invalid/product", status="queued"))
    def step(seconds): clock.wall += seconds; clock.mono += seconds
    return store, clock, step


def sample(store):
    with store.session() as session:
        product = session.get(Product, "product")
        return store.product_timings(session, [product])["product"]


def test_store_claim_heartbeat_pause_resume_and_stale_epoch_preserve_durations(workspace):
    store, clock, step = workspace
    step(10); claimed = store.claim("worker")
    step(20); assert store.heartbeat("product", claimed["epoch"])
    current = sample(store)
    assert current["active_seconds"] == 20 and current["queued_seconds"] == 10
    step(5); store.control_product("run", "product", "pause")
    paused = sample(store)
    assert paused["active_seconds"] == 25 and paused["state"] == "paused"
    step(100); assert not store.heartbeat("product", claimed["epoch"])
    assert sample(store)["active_seconds"] == 25
    store.control_product("run", "product", "resume")
    assert sample(store)["paused_seconds"] == 100
    next_claim = store.claim("new-worker")
    step(5)
    assert sample(store)["active_seconds"] == 30
    assert next_claim["epoch"] > claimed["epoch"]


def test_first_queued_interval_uses_same_process_monotonic_clock(workspace):
    store, clock, step = workspace
    clock.wall += 3601; clock.mono += 1
    store.claim("worker")
    result = sample(store)
    assert result["queued_seconds"] == 1 and result["active_seconds"] == 0
    assert result["clock_anomaly"] is True


def test_retry_delay_is_waiting_then_queued_not_active(workspace):
    store, clock, step = workspace
    claimed = store.claim("worker"); step(10)
    store.retry_product("run", "product", claimed["epoch"], "network_error", "synthetic")
    step(8); current = sample(store)
    assert current["active_seconds"] == 10
    assert current["waiting_seconds"] == 5 and current["queued_seconds"] == 3
    assert current["state"] == "queued"


def test_store_restart_never_projects_expired_owned_interval(workspace):
    store, clock, step = workspace
    claimed = store.claim("worker"); step(10); store.heartbeat("product", claimed["epoch"])
    step(3600)
    restarted = Store(store.config)
    result = sample(restarted)
    assert result["active_seconds"] == 10 and result["active_now"] is False
    assert result["coverage"] == "partial" and result["state"] == "unknown"
    started = clock.wall
    reclaimed = restarted.claim("new-worker"); step(3)
    assert sample(restarted)["active_seconds"] == 13
    assert sample(restarted)["active_since"] == started


def test_legacy_product_does_not_borrow_run_creation_or_mutate_on_list(workspace):
    store, clock, step = workspace
    with store.session() as session:
        session.execute(delete(ProductTiming))
        before = dict(session.get(Product, "product").__dict__)
    step(3600)
    result = sample(store)
    assert result["active_seconds"] is None and result["elapsed_seconds"] is None
    assert result["coverage"] == "unknown"
    with store.session() as session:
        after = dict(session.get(Product, "product").__dict__)
        assert {k:v for k,v in before.items() if not k.startswith("_")} == {k:v for k,v in after.items() if not k.startswith("_")}
        assert session.get(ProductTiming, "product") is None


def test_corrupt_optional_timing_is_unknown_and_does_not_break_worker_heartbeat(workspace):
    store, clock, step = workspace
    claim = store.claim("worker")
    with store.session() as session:
        session.get(ProductTiming, "product").data = {"schema": 1, "state": {"invalid": True}}
    result = sample(store)
    assert result["coverage"] == "unknown" and result["active_now"] is False
    assert store.heartbeat("product", claim["epoch"]) is True
    with store.session() as session:
        assert session.get(Product, "product").epoch == claim["epoch"]
        assert session.get(Product, "product").status == "running"


def test_timing_lookup_for_99_products_is_one_query_and_not_99_writes(workspace):
    store, clock, step = workspace
    with store.session() as session:
        session.add_all(Product(id=f"product-{i}", run_id="run", url=f"https://fixture.invalid/{i}") for i in range(98))
    statements = []
    def observe(connection, cursor, statement, *args): statements.append(statement)
    event.listen(store.engine, "before_cursor_execute", observe)
    try:
        with store.session() as session:
            products = list(session.scalars(select(Product)))
            result = store.product_timings(session, products, {"run": session.get(Run, "run")})
        assert len(result) == 99
        assert sum("FROM product_timing" in text for text in statements) == 1
        assert not any(text.lstrip().upper().startswith(("INSERT", "UPDATE")) for text in statements)
    finally: event.remove(store.engine, "before_cursor_execute", observe)


def test_global_history_uses_bounded_batches_for_timing_and_scope_queries(workspace):
    from crawler.collection_exports import list_collection_products
    store, clock, step = workspace
    with store.session() as session:
        session.add_all(Product(id=f"history-{i}", run_id="run", url=f"https://fixture.invalid/{i}") for i in range(1000))
    counts = []
    def observe(connection, cursor, statement, parameters, *args):
        if "FROM product_timing" in statement or "FROM product_record_scopes" in statement:
            counts.append(len(parameters))
    event.listen(store.engine, "before_cursor_execute", observe)
    try:
        result = list_collection_products(store)
        assert len(result["items"]) == 1001
        assert len(counts) == 6 and max(counts) <= 500
    finally: event.remove(store.engine, "before_cursor_execute", observe)


def test_finished_store_timer_and_archive_projection_are_stable(workspace):
    store, clock, step = workspace
    claim = store.claim("worker"); step(20)
    with store.session() as session:
        product = session.get(Product, "product"); product.status = "completed"
        store.sync_product_timing(session, product, session.get(Run, "run"))
    first = sample(store); step(1000); second = sample(store)
    assert first["active_seconds"] == second["active_seconds"] == 20
    assert first["elapsed_seconds"] == second["elapsed_seconds"] == 20
    with store.session() as session:
        record = session.get(ProductTiming, "product").data
        archived = timing.stable_summary(record)
    assert archived["active_seconds"] == 20 and archived["recorded_at"] == 1020
    assert "server_now" not in archived and "live_until" not in archived and "session" not in archived


@pytest.mark.asyncio
async def test_product_detail_and_collection_list_share_compact_timer(tmp_path, monkeypatch):
    import httpx
    import crawler.app as api
    from crawler.collection_exports import list_collection_products
    store = Store(Settings(data_dir=tmp_path))
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api.settings, "api_token", "synthetic-timing-token")
    headers = {"Authorization": "Bearer synthetic-timing-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://testserver") as client:
        created = await client.post("/api/v1/runs", json={"name":"timed","adapter":"fixture","fixture_products":1}, headers=headers)
        assert created.status_code == 200, created.text
        run = created.json(); product = run["products"][0]
        assert product["timing"]["state"] == "queued" and product["timing"]["active_seconds"] == 0
        claimed = store.claim("worker")
        response = await client.get("/api/v1/runs/" + run["id"], headers=headers)
        actual = response.json()["products"][0]["timing"]
        listed = list_collection_products(store)["items"][0]["timing"]
        assert actual["active_now"] is True and listed["active_now"] is True
        assert actual["scope"] == listed["scope"] == "product_worker"
        assert "seconds" not in actual and "session" not in actual
        await client.post(f"/api/v1/runs/{run['id']}/pause", headers=headers)
        paused = (await client.get("/api/v1/runs/" + run["id"], headers=headers)).json()["products"][0]["timing"]
        assert paused["state"] == "paused" and paused["active_now"] is False
        assert not store.heartbeat(product["id"], claimed["epoch"])
    store.engine.dispose()


@pytest.mark.asyncio
async def test_campaign_continuations_accumulate_active_and_pause_without_replaying(tmp_path):
    from crawler.campaign_automation import CampaignAutomation, Profile
    wall, mono, calls = [1000.], [10.], []
    async def execute(job, check):
        await check(); calls.append(job["attempts"]); wall[0] += 30; mono[0] += 30
        return {"state": "continue", "result_reference": "saved/receipt.json", "progress_count": job["progress_count"]+1,
            "progress_token": (str(job["progress_count"]+1)*64)[:64]}
    association = {"campaign_id": "campaign", "product_id": "product", "supplier": "vendor", "source_url": "https://fixture.invalid/product"}
    profile = Profile("profile", "vendor", lambda _: True, lambda a,n: {"configurations": [{}], "blockers": []}, lambda c:c, execute)
    manager = CampaignAutomation(tmp_path, [profile], lambda _: [association], clock=lambda:wall[0], monotonic=lambda:mono[0])
    try:
        plan = await manager.plan("campaign"); await manager.start("campaign", plan["plan_id"])
        for _ in range(2):
            wall[0] += 5; mono[0] += 5
            await manager.tick(); await asyncio.gather(*(task for _, task in manager._active.values()))
        await manager.tick(); await manager.begin_maintenance()
        # Last tick may schedule a third task; let any admitted task finish.
        await asyncio.gather(*(task for _, task in manager._active.values()))
        await manager.tick(); before = manager.snapshot("campaign")["jobs"][0]
        await manager.pause_drained_for_update(); wall[0] += 100; mono[0] += 100
        after = manager.snapshot("campaign")["jobs"][0]
        assert after["timing"]["active_seconds"] == 30*len(calls)
        assert after["timing"]["paused_seconds"] == 100
        assert after["attempts"] == len(calls) and after["progress_count"] == len(calls)
        assert after["result_reference"] == before["result_reference"]
    finally: await manager.close()


@pytest.mark.asyncio
async def test_campaign_crash_restart_keeps_spent_intent_but_drops_unknown_active_tail(tmp_path):
    import json
    from crawler.campaign_automation import CampaignAutomation, Profile
    gate, began = asyncio.Event(), asyncio.Event()
    wall, mono = [1000.], [10.]
    async def execute(job, check):
        await check(); began.set(); await gate.wait()
    association = {"campaign_id":"campaign","product_id":"product","supplier":"vendor","source_url":"https://fixture.invalid/product"}
    profile = Profile("profile","vendor",lambda _:True,lambda a,n:{"configurations":[{}],"blockers":[]},lambda c:c,execute)
    first = CampaignAutomation(tmp_path/"first",[profile],lambda _:[association],clock=lambda:wall[0],monotonic=lambda:mono[0])
    restarted = None
    try:
        plan=await first.plan("campaign");await first.start("campaign",plan["plan_id"]);await first.tick();await began.wait()
        saved=(first.root/"campaign.json").read_bytes()
        copied=tmp_path/"restarted"/"campaign-automation";copied.mkdir(mode=0o700,parents=True)
        path=copied/"campaign.json";path.write_bytes(saved);path.chmod(0o600)
        wall[0]+=3600;mono[0]+=3600
        restarted=CampaignAutomation(tmp_path/"restarted",[profile],lambda _:[association],clock=lambda:wall[0],monotonic=lambda:mono[0])
        actual=restarted.snapshot("campaign")["jobs"][0]
        assert actual["status"]=="held" and actual["reason"]=="worker_interrupted"
        assert actual["attempts"]==1 and actual["timing"]["active_seconds"]==0
        assert actual["timing"]["coverage"]=="partial" and actual["timing"]["active_now"] is False
        assert actual["timing"]["elapsed_seconds"]==3600
    finally:
        await first.close()
        if restarted:await restarted.close()


@pytest.mark.asyncio
async def test_legacy_completed_campaign_pause_resume_start_keeps_original_known_end(tmp_path):
    from crawler.campaign_automation import CampaignAutomation, Profile
    calls = []
    async def execute(job, check): calls.append(True)
    association = {"campaign_id":"campaign","product_id":"product","supplier":"vendor","source_url":"https://fixture.invalid/product"}
    profile = Profile("profile","vendor",lambda _:True,lambda a,n:{"configurations":[{}],"blockers":[]},lambda c:c,execute)
    manager = CampaignAutomation(tmp_path,[profile],lambda _:[association],clock=lambda:1000.,monotonic=lambda:100.)
    try:
        plan=await manager.plan("campaign");await manager.start("campaign",plan["plan_id"])
        job=next(iter(manager._records["campaign"]["jobs"].values()))
        job.pop("timing");job.update(status="completed",created_at=100.,updated_at=200.)
        manager._save(manager._records["campaign"])
        original=deepcopy(job)
        def assert_same_projection():
            actual=manager.snapshot("campaign")["jobs"][0]["timing"]
            assert actual["elapsed_seconds"]==100 and actual["active_seconds"] is None
            assert actual["coverage"]=="unknown" and job==original
        assert_same_projection()
        await manager.control("campaign","pause");assert_same_projection()
        await manager.control("campaign","resume");assert_same_projection()
        await manager.pause_drained_for_update();assert_same_projection()
        await manager.start("campaign",plan["plan_id"]);assert_same_projection()
        await manager.tick();assert calls==[]
    finally:await manager.close()


@pytest.mark.asyncio
async def test_observed_current_campaign_completion_still_records_actual_finish(tmp_path):
    from crawler.campaign_automation import CampaignAutomation, Profile
    wall,mono=[1000.],[100.]
    async def execute(job,check):
        await check();wall[0]+=5;mono[0]+=5
        return {"state":"completed","result_reference":"saved/real-finish.json"}
    association={"campaign_id":"campaign","product_id":"product","supplier":"vendor","source_url":"https://fixture.invalid/product"}
    profile=Profile("profile","vendor",lambda _:True,lambda a,n:{"configurations":[{}],"blockers":[]},lambda c:c,execute)
    manager=CampaignAutomation(tmp_path,[profile],lambda _:[association],clock=lambda:wall[0],monotonic=lambda:mono[0])
    try:
        plan=await manager.plan("campaign");await manager.start("campaign",plan["plan_id"]);await manager.tick()
        await asyncio.gather(*(task for _,task in manager._active.values()));await manager.tick()
        first=manager.snapshot("campaign")["jobs"][0]["timing"]
        assert first["active_seconds"]==5 and first["elapsed_seconds"]==5 and first["coverage"]=="complete"
        wall[0]+=900;mono[0]+=900
        await manager.control("campaign","pause");await manager.control("campaign","resume")
        actual=manager.snapshot("campaign")["jobs"][0]["timing"]
        assert actual["active_seconds"]==5 and actual["elapsed_seconds"]==5 and actual["complete_history"] is True
    finally:await manager.close()
