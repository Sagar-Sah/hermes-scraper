import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import types

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "backend/crawler" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


m = load("campaign_automation")


def association(pid, supplier="a"):
    return {"campaign_id": "campaign", "product_id": pid, "supplier": supplier,
            "source_url": "https://supplier.example/" + pid}


async def completed(job, check):
    await check()
    return {"state": "completed", "result_reference": "objects/receipt-" + job["product_id"] + ".json"}


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.rows = [association("one"), association("two"), association("three", "b")]
        self.managers = []
        self.now = 1000.0

    async def asyncTearDown(self):
        for manager in self.managers:
            if not manager._owner.closed: await manager.close()
        self.temp.cleanup()

    def make(self, execute=completed, *, poll=None, profiles=None, **kwargs):
        if profiles is None:
            profiles = [m.Profile(s, s, lambda a: True,
                lambda a, limit: {"configurations": [{"scope": "saved_domain"}], "blockers": []},
                lambda c: c, execute, poll) for s in ("a", "b")]
        runner = m.CampaignAutomation(self.temp.name, profiles, lambda cid: self.rows,
                                      clock=lambda: self.now, **kwargs)
        self.managers.append(runner)
        return runner

    async def start(self, runner, **kwargs):
        plan = await runner.plan("campaign", **kwargs)
        self.assertIn("plan_id", plan)
        await runner.start("campaign", plan["plan_id"])
        return plan

    async def settle(self, runner, n=10):
        for _ in range(n):
            await runner.tick(); await asyncio.sleep(0)
        await runner.tick()

    async def test_plan_only_discovers_no_execution(self):
        calls = []
        async def execute(*a): calls.append(1); return await completed(*a)
        r = self.make(execute)
        p = await r.plan("campaign", max_jobs=2)
        self.assertEqual(len(p["jobs"]), 2)
        self.assertEqual(calls, [])
        self.assertFalse(r.snapshot("campaign")["enabled"])
        self.assertEqual(r.snapshot("campaign")["summary"]["queued"], 0)
        self.assertEqual(p["blockers"][-1]["reason"], "plan_limit_reached")

    async def test_other_supplier_keeps_advancing_while_one_waits(self):
        self.rows = [association("one"), association("two", "b"), association("three", "b")]
        gate, called = asyncio.Event(), []
        async def execute(job, check):
            called.append(job["product_id"])
            if job["supplier"] == "a": await gate.wait()
            return await completed(job, check)
        r = self.make(execute)
        await self.start(r); await self.settle(r, 25)
        self.assertEqual(set(called), {"one", "two", "three"})
        self.assertEqual(r.snapshot("campaign")["summary"]["completed"], 2)
        self.assertEqual(r.snapshot("campaign")["summary"]["running"], 1)
        gate.set(); await self.settle(r)
        self.assertEqual(r.snapshot("campaign")["summary"]["completed"], 3)

    async def test_same_supplier_is_serialized_without_increasing_source_limit(self):
        active, peak = 0, 0
        async def execute(job, check):
            nonlocal active, peak
            active += 1; peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return await completed(job, check)
        self.rows = [association(str(i)) for i in range(5)]
        r = self.make(execute); await self.start(r); await self.settle(r, 50)
        self.assertEqual(peak, 1)
        self.assertEqual(r.snapshot("campaign")["summary"]["completed"], 5)

    async def test_completed_and_held_are_not_replayed_by_resume_or_replan(self):
        calls = []
        async def execute(job, check):
            calls.append(job["id"])
            return {"state": "held", "reason": "needs_profile"} if job["product_id"] == "two" else await completed(job, check)
        r = self.make(execute); await self.start(r); await self.settle(r, 40)
        await r.control("campaign", "pause"); await r.control("campaign", "resume")
        p = await r.plan("campaign")
        self.assertEqual(p["jobs"], [])
        self.assertTrue(all(b["reason"] == "existing_selection_preserved" for b in p["blockers"]))
        await self.settle(r)
        self.assertEqual(len(calls), 3)

    async def test_restart_running_unknown_effect_is_held_without_execute(self):
        r = self.make(); await self.start(r)
        await r.close()
        p = Path(self.temp.name) / "campaign-automation/campaign.json"
        record = json.loads(p.read_text()); first = next(iter(record["jobs"].values()))
        first["status"] = "running"; first["attempts"] = 1
        p.write_text(json.dumps(record)); os.chmod(p, 0o600)
        calls = []
        async def execute(job, check): calls.append(job["id"]); return await completed(job, check)
        again = self.make(execute); await self.settle(again, 30)
        found = again._records["campaign"]["jobs"][first["id"]]
        self.assertEqual(found["status"], "held")
        self.assertEqual(found["reason"], "worker_interrupted")
        self.assertNotIn(first["id"], calls)

    async def test_pending_uses_saved_resource_poll_not_execute(self):
        count = {"execute": 0, "poll": 0}
        async def execute(job, check):
            count["execute"] += 1
            return {"state": "pending", "pending_token": {"harvest_id": "a" * 32}}
        async def poll(job, check):
            count["poll"] += 1
            self.assertEqual(job["pending_token"], {"harvest_id": "a" * 32})
            return await completed(job, check)
        r = self.make(execute, poll=poll); await self.start(r, product_ids=["one"])
        await self.settle(r); self.now += 3; await self.settle(r)
        self.assertEqual(count, {"execute": 1, "poll": 1})
        self.assertEqual(r.snapshot("campaign")["summary"]["completed"], 1)

    async def test_pending_limit_is_finite_and_does_not_retry_source(self):
        calls = []
        async def pending(job, check):
            calls.append(job["pending_token"] is None)
            return {"state": "pending", "pending_token": {"harvest_id": "b" * 32}}
        r = self.make(pending, poll=pending, max_polls=2)
        await self.start(r, product_ids=["one"])
        for _ in range(5): await self.settle(r); self.now += 3
        self.assertEqual(calls, [True, False, False])
        self.assertEqual(r.snapshot("campaign")["jobs"][0]["reason"], "pending_limit_reached")

    async def test_pending_clock_rollback_holds(self):
        async def pending(job, check): return {"state": "pending", "pending_token": {"job_id": "b" * 32}}
        r = self.make(pending, poll=pending); await self.start(r, product_ids=["one"]); await self.settle(r)
        self.now -= 2; await self.settle(r)
        self.assertEqual(r.snapshot("campaign")["jobs"][0]["reason"], "pending_limit_reached")

    async def test_unknown_error_is_private_and_bad_product_does_not_block_others(self):
        async def execute(job, check):
            if job["product_id"] == "one": raise ValueError("PRIVATE_ACCOUNT_SECRET")
            return await completed(job, check)
        r = self.make(execute); await self.start(r); await self.settle(r, 40)
        self.assertEqual(r.snapshot("campaign")["summary"]["completed"], 2)
        text = (r.root / "campaign.json").read_text()
        self.assertNotIn("PRIVATE", text)
        self.assertEqual(r.snapshot("campaign")["summary"]["held"], 1)

    async def test_auth_blocks_just_supplier_and_explicit_resume_does_not_retry_failed_job(self):
        calls = []
        async def execute(job, check):
            calls.append(job["product_id"])
            return {"state": "access_required", "reason": "login_required"} if job["product_id"] == "one" else await completed(job, check)
        r = self.make(execute); await self.start(r); await self.settle(r, 30)
        self.assertEqual(calls, ["one", "three"])
        self.assertEqual(r.snapshot("campaign")["summary"]["queued"], 1)
        await r.control("campaign", "resume"); await self.settle(r)
        self.assertEqual(calls, ["one", "three", "two"])

    async def test_scope_change_before_execute_never_invokes(self):
        calls = []
        async def execute(job, check): calls.append(1); return await completed(job, check)
        r = self.make(execute); await self.start(r, product_ids=["one"])
        self.rows[0]["source_url"] += "-changed"
        await self.settle(r)
        self.assertEqual(calls, [])
        self.assertEqual(r.snapshot("campaign")["summary"]["held"], 1)

    async def test_scope_change_during_execute_cannot_commit_receipt(self):
        async def execute(job, check):
            self.rows[0]["source_url"] += "-changed"
            return {"state": "completed", "result_reference": "objects/new.json"}
        r = self.make(execute); await self.start(r, product_ids=["one"]); await self.settle(r)
        j = r.snapshot("campaign")["jobs"][0]
        self.assertEqual(j["status"], "held"); self.assertIsNone(j["result_reference"])

    async def test_pause_fences_inflight_without_replay(self):
        gate = asyncio.Event(); calls = []
        async def execute(job, check): calls.append(1); await gate.wait(); return await completed(job, check)
        r = self.make(execute); await self.start(r, product_ids=["one"]); await self.settle(r)
        await r.control("campaign", "pause"); await self.settle(r)
        await r.control("campaign", "resume"); gate.set(); await self.settle(r)
        self.assertEqual(calls, [1]); self.assertEqual(r.snapshot("campaign")["summary"]["held"], 1)

    async def test_timeout_unknown_effect_never_retries(self):
        calls = []
        async def execute(job, check): calls.append(1); await asyncio.Event().wait()
        r = self.make(execute, call_timeout=0.001); await self.start(r, product_ids=["one"])
        await r.tick(); await asyncio.sleep(0.02); await self.settle(r)
        self.assertEqual(calls, [1]); self.assertEqual(r.snapshot("campaign")["jobs"][0]["reason"], "handler_timeout_unknown_effect")

    async def test_owner_lock_private_modes_and_no_historical_paths(self):
        r = self.make(); await self.start(r)
        self.assertEqual(r.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((r.root / "campaign.json").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError): self.make()
        self.assertEqual({p.name for p in Path(self.temp.name).iterdir()}, {"campaign-automation"})

    async def test_unsupported_and_invalid_product_do_not_guess(self):
        self.rows.append(association("unsupported", "unknown"))
        r = self.make(); p = await r.plan("campaign", product_ids=["unsupported", "missing"])
        self.assertEqual(p["jobs"], [])
        self.assertEqual([b["reason"] for b in p["blockers"]], ["needs_profile", "product_outside_active_scope"])

    async def test_invalid_profile_discovery_isolated(self):
        def discover(a, bound):
            if a["product_id"] == "one": raise ValueError("private")
            return {"configurations": [{"scope": "supported"}], "blockers": []}
        profiles = [m.Profile("a", "a", lambda a: True, discover, lambda c:c, completed)]
        r = self.make(profiles=profiles); p = await r.plan("campaign")
        self.assertEqual(len(p["jobs"]), 1)
        self.assertEqual(p["blockers"][0]["reason"], "profile_plan_failed")

    async def test_plan_mapping_revalidation_is_atomic_before_start(self):
        r = self.make(); p = await r.plan("campaign")
        self.rows.pop()
        with self.assertRaises(ValueError): await r.start("campaign", p["plan_id"])
        self.assertEqual(r.snapshot("campaign")["jobs"], [])

    async def test_nonfinite_or_extra_unvalidated_configuration_held(self):
        profiles = [m.Profile("a", "a", lambda a:True,
            lambda a,n:{"configurations":[{"amount":float("nan")}], "blockers":[]},lambda c:c,completed)]
        r = self.make(profiles=profiles); p = await r.plan("campaign")
        self.assertEqual(p["jobs"], [])
        self.assertEqual(p["blockers"][0]["reason"], "invalid_profile_configuration")

    async def test_no_quote_cost_or_whole_product_completion_in_queue_snapshot(self):
        r = self.make(); await self.start(r); await self.settle(r, 40)
        s = r.snapshot("campaign")
        self.assertTrue(s["stage_scoped_only"])
        self.assertFalse(s["source_data_finalized"]); self.assertFalse(s["woocommerce_import_ready"])
        self.assertNotIn("price", json.dumps(s))

    async def test_continuation_requeues_only_after_durable_monotonic_progress(self):
        calls=[]
        async def execute(job, check):
            calls.append(job["progress_count"])
            if len(calls) < 3:
                return {"state":"continue","result_reference":"cursors/one.json",
                        "progress_count":len(calls)*10,"progress_token":str(len(calls))*64}
            return await completed(job,check)
        r=self.make(execute);await self.start(r,product_ids=["one"]);await self.settle(r,40)
        self.assertEqual(calls,[0,10,20]);self.assertEqual(r.snapshot("campaign")["summary"]["completed"],1)

    async def test_continuation_no_progress_or_reused_hash_cannot_loop(self):
        calls=[]
        async def execute(job,check):
            calls.append(1)
            return {"state":"continue","result_reference":"cursors/one.json",
                    "progress_count":10,"progress_token":"a"*64}
        r=self.make(execute);await self.start(r,product_ids=["one"]);await self.settle(r,40)
        self.assertEqual(calls,[1,1]);self.assertEqual(r.snapshot("campaign")["summary"]["held"],1)

    async def test_continuation_boolean_progress_is_rejected(self):
        async def execute(job,check):
            return {"state":"continue","result_reference":"cursors/one.json",
                    "progress_count":True,"progress_token":"a"*64}
        r=self.make(execute);await self.start(r,product_ids=["one"]);await self.settle(r)
        self.assertEqual(r.snapshot("campaign")["summary"]["held"],1)

    async def test_changed_source_after_plan_requires_new_plan_no_execution(self):
        r=self.make();p=await r.plan("campaign");self.rows[0]["source_url"]="https://changed.example/new"
        with self.assertRaises(ValueError):await r.start("campaign",p["plan_id"])
        self.assertFalse(r.snapshot("campaign")["enabled"])

    async def test_async_discovery_cannot_accidentally_start_source(self):
        calls=[]
        async def discover(*args):calls.append(1);return {"configurations":[],"blockers":[]}
        profiles=[m.Profile("a","a",lambda a:True,discover,lambda c:c,completed)]
        r=self.make(profiles=profiles)
        with self.assertRaises(ValueError):await r.plan("campaign")
        self.assertEqual(calls,[])

    async def test_result_reference_and_private_pending_payload_rejected(self):
        async def execute(job,check):
            return {"state":"pending","pending_token":{"password":"PRIVATE"}}
        r=self.make(execute,poll=execute);await self.start(r,product_ids=["one"]);await self.settle(r)
        self.assertEqual(r.snapshot("campaign")["summary"]["held"],1)
        self.assertNotIn("PRIVATE",(r.root/"campaign.json").read_text())

    async def test_private_directory_permission_failure_does_not_tighten_other_data(self):
        path=Path(self.temp.name)/"campaign-automation";path.mkdir(mode=0o755);os.chmod(path,0o755)
        with self.assertRaises(ValueError):self.make()
        self.assertEqual(path.stat().st_mode&0o777,0o755)

    async def test_plan_counts_products_and_rejects_multi_configuration_fanout(self):
        profiles=[m.Profile("a","a",lambda a:True,
            lambda a,n:{"configurations":[{"x":1},{"x":2}],"blockers":[]},lambda c:c,completed)]
        r=self.make(profiles=profiles)
        with self.assertRaises(ValueError):await r.plan("campaign",max_jobs=99)

    async def test_supplier_auth_hold_durable_after_restart(self):
        async def execute(job,check):return {"state":"access_required","reason":"login_required"}
        r=self.make(execute);await self.start(r);await self.settle(r,25);await r.close()
        calls=[]
        async def after(job,check):calls.append(1);return await completed(job,check)
        again=self.make(after);await self.settle(again,25)
        self.assertEqual(calls,[])

    async def test_escaped_final_save_failure_does_not_kill_tick(self):
        r=self.make();await self.start(r,product_ids=["one"])
        save=r._save
        def fault(record):
            if any(j["status"]=="completed" for j in record["jobs"].values()):raise OSError("PRIVATE_DISK_PATH")
            save(record)
        r._save=fault
        await self.settle(r,20)
        s=r.snapshot("campaign")
        self.assertFalse(s["enabled"])
        self.assertEqual(s["summary"]["held"],1)
        self.assertEqual(s["worker_faults"][0]["reason"],"automation_storage_or_worker_failure")
        self.assertNotIn("PRIVATE",json.dumps(s))

    async def test_dispatch_level_fault_is_visible_and_loop_remains_alive(self):
        r=self.make();await self.start(r)
        original=r.tick;calls=[]
        async def faulty_tick():
            calls.append(1)
            if len(calls)==1:raise OSError("PRIVATE")
            await original()
        r.tick=faulty_tick
        await r.open();await asyncio.sleep(.15)
        self.assertFalse(r._loop.done())
        s=r.snapshot("campaign")
        self.assertFalse(s["enabled"]);self.assertTrue(s["worker_faults"])

    async def test_specific_profile_wins_and_equal_priority_conflict_is_held(self):
        def profile(name,priority):return m.Profile(name,"a",lambda a:True,
            lambda a,n:{"configurations":[{"scope":"known"}],"blockers":[]},lambda c:c,completed,priority=priority)
        r=self.make(profiles=[profile("general",0),profile("specific",100)])
        p=await r.plan("campaign",product_ids=["one"])
        self.assertEqual(p["jobs"][0]["profile_id"],"specific")
        await r.close()
        other=self.make(profiles=[profile("first",100),profile("second",100)])
        p=await other.plan("campaign",product_ids=["one"])
        self.assertEqual(p["jobs"],[]);self.assertEqual(p["blockers"][0]["reason"],"ambiguous_profile")

    async def test_known_supplier_aliases_copy_only_and_profile_matching(self):
        aliases={"fourover":"4over","4over":"4over","Printograph":"printograph",
                 "B2Sign":"b2sign","Digital Print Solutions":"dps","digital_print_solutions":"dps"}
        self.rows=[association(str(i),raw) for i,raw in enumerate(aliases)]
        original=json.loads(json.dumps(self.rows))
        profiles=[m.Profile(s,s,lambda a:True,
            lambda a,n:{"configurations":[{"scope":"known"}],"blockers":[]},lambda c:c,completed)
            for s in set(aliases.values())]
        r=self.make(profiles=profiles);p=await r.plan("campaign")
        self.assertEqual([j["supplier"] for j in p["jobs"]],list(aliases.values()))
        self.assertEqual(self.rows,original)
        self.assertEqual([j["association"]["source_url"] for j in p["jobs"]],[a["source_url"] for a in original])

    async def test_100000_advancing_chunks_bound_checked_before_next_execute(self):
        calls=[]
        async def execute(job,check):
            calls.append(1)
            return {"state":"continue","result_reference":"cursors/one.json",
                    "progress_count":1,"progress_token":"a"*64}
        r=self.make(execute);await self.start(r,product_ids=["one"])
        job=next(iter(r._records["campaign"]["jobs"].values()));job["continuations"]=99999
        await self.settle(r,30)
        self.assertEqual(calls,[1]);self.assertEqual(job["continuations"],100000)
        self.assertEqual(job["reason"],"continuation_limit_reached")

    async def test_delay_applies_to_supplier_without_stalling_other_supplier(self):
        calls=[]
        async def execute(job,check):
            calls.append(job["product_id"])
            if job["product_id"]=="one" and job["progress_count"]==0:
                return {"state":"continue","result_reference":"cursors/one.json",
                        "progress_count":1,"progress_token":"a"*64,"delay_seconds":10}
            return await completed(job,check)
        r=self.make(execute);await self.start(r);await self.settle(r,30)
        self.assertEqual(calls,["one","three"])
        self.now+=9;await self.settle(r);self.assertEqual(calls,["one","three"])
        self.now+=1;await self.settle(r,30);self.assertEqual(calls,["one","three","two","one"])

    async def test_invalid_continuation_delays_are_held_without_nan_persistence(self):
        for index,delay in enumerate([-1,86401,float("nan"),float("inf"),True,"3"]):
            async def execute(job,check):
                return {"state":"continue","result_reference":"cursors/one.json",
                        "progress_count":1,"progress_token":"a"*64,"delay_seconds":delay}
            r=self.make(execute);await self.start(r,product_ids=["one"]);await self.settle(r)
            self.assertEqual(r.snapshot("campaign")["summary"]["held"],1)
            self.assertEqual(r.snapshot("campaign")["supplier_not_before"],{})
            await r.close()
            # Every case gets fresh isolated state; completed/held state is not
            # reset by the actual runner.
            for p in (Path(self.temp.name)/"campaign-automation").glob("*.json"):p.unlink()

    async def test_delay_upper_boundary_and_zero_are_accepted(self):
        async def execute(job,check):
            return {"state":"continue","result_reference":"cursors/one.json",
                    "progress_count":job["progress_count"]+1,"progress_token":str(job["progress_count"]+1)*64,
                    "delay_seconds":86400}
        r=self.make(execute);await self.start(r,product_ids=["one"]);await self.settle(r)
        self.assertEqual(r.snapshot("campaign")["supplier_not_before"]["a"],self.now+86400)
        self.assertEqual(next(iter(r._records["campaign"]["jobs"].values()))["continuations"],1)

    async def test_delay_and_supplier_hold_survive_restart(self):
        calls=[]
        async def execute(job,check):
            calls.append(1)
            return {"state":"continue","result_reference":"cursors/one.json",
                    "progress_count":1,"progress_token":"a"*64,"delay_seconds":10}
        r=self.make(execute);await self.start(r,product_ids=["one"]);await self.settle(r);await r.close()
        again=self.make(execute);await self.settle(again)
        self.assertEqual(calls,[1]);self.assertEqual(again.snapshot("campaign")["supplier_not_before"]["a"],1010)

    async def test_uninstalled_v1_record_format_cannot_silently_rekey_alias_jobs(self):
        r=self.make();await self.start(r,product_ids=["one"]);await r.close()
        path=Path(self.temp.name)/"campaign-automation/campaign.json"
        record=json.loads(path.read_text());record["schema"]=1;path.write_text(json.dumps(record));os.chmod(path,0o600)
        with self.assertRaises(ValueError):self.make()


class APITests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_plan_start_and_extra_fields(self):
        from fastapi import FastAPI, Header, HTTPException
        import httpx
        routes = load("campaign_automation_routes")
        calls = []
        class Fake:
            def snapshot(self, cid): return {"campaign_id":cid,"enabled":False}
            async def plan(self,cid,**kw): calls.append((cid,kw)); return {"plan_id":"a"*64,"jobs":[],"blockers":[]}
            async def start(self,cid,pid): calls.append(pid); return self.snapshot(cid)
            async def control(self,cid,action): calls.append(action); return self.snapshot(cid)
        async def auth(authorization: str = Header(default="")):
            if authorization != "Bearer test": raise HTTPException(401,"required")
        app = FastAPI(); app.include_router(routes.automation_router(lambda:Fake(),auth))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://offline.invalid") as c:
            self.assertEqual((await c.get("/api/v1/campaigns/c/automation")).status_code,401)
            headers={"Authorization":"Bearer test"}
            p=await c.post("/api/v1/campaigns/c/automation/plan",headers=headers,json={"max_jobs":2})
            self.assertEqual(p.status_code,200); self.assertIn("plan_id",p.json())
            for data in [{"max_jobs":True},{"max_jobs":100},{"unknown":1}]:
                self.assertEqual((await c.post("/api/v1/campaigns/c/automation/plan",headers=headers,json=data)).status_code,422)
            self.assertEqual((await c.post("/api/v1/campaigns/c/automation/start",headers=headers,json={"plan_id":"a"*64})).status_code,200)
            self.assertEqual((await c.post("/api/v1/campaigns/c/automation/pause",headers=headers,json={"force":True})).status_code,422)
            self.assertEqual((await c.post("/api/v1/campaigns/c/automation/pause",headers=headers,json={})).status_code,200)


class BindingsTests(unittest.IsolatedAsyncioTestCase):
    def bindings(self):
        package=types.ModuleType("offline_automation");package.__path__=[str(ROOT/"backend/crawler")]
        sys.modules[package.__name__]=package
        sys.modules[package.__name__+".campaign_automation"]=m
        path=ROOT/"backend/crawler/campaign_automation_bindings.py"
        spec=importlib.util.spec_from_file_location(package.__name__+".campaign_automation_bindings",path)
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
        return module

    async def test_family_adapter_forwards_scope_and_preserves_hold_flags(self):
        b=self.bindings();events=[]
        async def check():events.append("check")
        async def execute(association,configuration,*,check_scope):
            events.append("execute");await check_scope()
            return {"status":"matched_current_source_quote","source_cost_subtotal":"88.74",
                    "source_data_finalized":False,"result_reference":"receipts/source.json"}
        p=b.family_profile(profile_id="profile",supplier="supplier",supports=lambda a:True,
            discover=lambda a,n:None,normalize=lambda c:c,execute=execute)
        result=await p.execute({"association":association("one"),"configuration":{"quantity":"250"}},check)
        self.assertEqual(result,{"state":"completed","result_reference":"receipts/source.json"})
        self.assertEqual(events,["check","execute","check","check"])
        self.assertNotIn("source_cost",json.dumps(result))

    async def test_family_adapter_spent_and_unknown_are_held(self):
        b=self.bindings()
        for status in ("already_spent","needs_profile","unsupported",None):
            async def execute(a,c,*,check_scope):return {"status":status}
            async def check():pass
            p=b.family_profile(profile_id="p",supplier="s",supports=lambda a:True,
                discover=lambda a,n:None,normalize=lambda c:c,execute=execute)
            self.assertEqual((await p.execute({"association":{},"configuration":{}},check))["state"],"held")

    async def test_associations_do_not_resume_excluded_finalized_or_other_group(self):
        b=self.bindings();calls=[]
        class Campaigns:
            def get(self,cid):return {"active_group_product_ids":["one","excluded","finalized"],
                "products":[{"id":pid,"supplier":"a","supplier_url":"https://s.example/"+pid,"stage":stage}
                    for pid,stage in [("one","queued"),("excluded","excluded"),("finalized","finalized"),("later","queued")]]}
            def require_active_group(self,cid,pid):calls.append(pid)
        result=b.active_associations(Campaigns(),"campaign")
        self.assertEqual([a["product_id"] for a in result],["one"]);self.assertEqual(calls,["one"])
