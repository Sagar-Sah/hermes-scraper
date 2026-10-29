"""Unattended notices use fixtures only; no real desktop or external service."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import stat

import pytest

from crawler.companion_notifications import CompanionNotifications, native_macos_notification


CYCLE = "a" * 32
OTHER = "b" * 32


def cycle(phase="running", **values):
    return {"id": CYCLE, "status": phase, "summary": {"verified": 0},
            "inventory": {"count": 99}, "supplier_blocks": {}, **values}


def state(record=None, **values):
    return {"cycle": record, "config": {"director_enabled": False}, **values}


@pytest.mark.asyncio
async def test_default_off_and_unchanged_healthy_work_stay_quiet(tmp_path):
    calls = []
    notices = CompanionNotifications(tmp_path, delivery=lambda *args: calls.append(args))
    assert await notices.observe({}) == []
    assert await notices.observe(state(cycle())) == []
    assert calls == notices.recent() == []
    await notices.close()


@pytest.mark.asyncio
async def test_completed_cycle_is_recorded_once_across_restart_and_count_changes(tmp_path):
    calls = []
    notices = CompanionNotifications(tmp_path, delivery=lambda *args: calls.append(args))
    observed = state(None, cycles=[cycle("completed", summary={"verified": 99})])
    records = await notices.observe(observed)
    assert len(records) == len(calls) == 1
    first = records[0]
    assert first["status"] == "delivered" and first["attempts"] == 1
    assert first["kind"] == "completion" and "99 of 99" in first["body"]
    for _ in range(5):
        await notices.observe(observed)
    await notices.close()
    notices = CompanionNotifications(tmp_path, delivery=lambda *args: calls.append(args))
    # A later summary update cannot resend the same completion.
    observed["cycles"][0]["summary"]["verified"] = 98
    await notices.observe(observed)
    assert len(calls) == len(notices.recent()) == 1
    assert notices.recent()[0]["body"] == first["body"]
    await notices.close()


@pytest.mark.asyncio
async def test_a_new_cycle_gets_a_fresh_notice_and_historical_cycles_are_supported(tmp_path):
    calls = []
    notices = CompanionNotifications(tmp_path, delivery=lambda *args: calls.append(args))
    first = cycle("completed_with_attention", summary={"verified": 80, "failed": 2})
    second = {**first, "id": OTHER}
    await notices.observe(state(first, cycles=[first, second]))
    assert len(calls) == 2
    assert all("unknown evidence remains held" in body for _, body in calls)
    assert {row["cycle_id"] for row in notices.recent()} == {CYCLE, OTHER}
    await notices.close()


@pytest.mark.asyncio
async def test_supplier_notices_are_deduplicated_without_remote_text_or_credentials(tmp_path):
    secret = 'sk-secret_value https://supplier.invalid/path?token=hidden $(touch /tmp/not-created)'
    calls = []
    notices = CompanionNotifications(tmp_path, delivery=lambda *args: calls.append(args))
    record = cycle(supplier_blocks={"4over": secret, "b2sign": secret, secret: secret}, title=secret)
    await notices.observe(state(record))
    await notices.observe(state(record))
    assert len(calls) == 3
    assert {row["supplier"] for row in notices.recent()} == {"4over", "B2Sign", "Supplier"}
    serialized = json.dumps(notices.recent())
    assert secret not in serialized and "token" not in serialized and "https:" not in serialized
    assert all("Other eligible suppliers continue" in body for _, body in calls)
    await notices.close()


@pytest.mark.asyncio
async def test_product_failure_and_hold_notify_once_not_once_per_product(tmp_path):
    calls = []
    notices = CompanionNotifications(tmp_path, delivery=lambda *args: calls.append(args))
    await notices.observe(state(cycle(summary={"failed": 1, "held": 1})))
    await notices.observe(state(cycle(summary={"failed": 42, "held": 33})))
    assert len(calls) == 2
    assert {row["reason"] for row in notices.recent()} == {"product_checks_failed", "held_work_requires_review"}
    await notices.close()


@pytest.mark.asyncio
async def test_director_and_baseline_actions_require_an_active_started_cycle(tmp_path):
    calls = []
    notices = CompanionNotifications(tmp_path, delivery=lambda *args: calls.append(args))
    settings = {"config": {"director_enabled": True},
                "director": {"ready": False, "reason": "director_provider_request_rejected_401"},
                "baseline": {"ready": False}}
    await notices.observe(state(cycle("created"), **settings))
    assert calls == []
    await notices.observe(state(cycle(), **settings))
    await notices.observe(state(cycle(), **settings))
    assert len(calls) == 2
    assert all(row["kind"] == "required_action" for row in notices.recent())
    # Unknown provider reasons never enter OS notifications or the outbox.
    settings["director"]["reason"] = "sk_this_would_be_a_secret"
    await notices.observe(state(cycle(), **settings))
    assert len(calls) == 2
    await notices.close()


@pytest.mark.asyncio
async def test_unavailable_delivery_is_held_without_poll_retries_and_explicit_retry_works(tmp_path):
    calls = []
    def unavailable(*args):
        calls.append(args)
        raise ValueError("desktop_notifications_unavailable")
    notices = CompanionNotifications(tmp_path, delivery=unavailable)
    await notices.observe(state(cycle("completed")))
    record = notices.recent()[0]
    assert record["status"] == "held" and record["error_code"] == "desktop_notifications_unavailable"
    for _ in range(4):
        await notices.observe(state(cycle("completed")))
    assert len(calls) == 1
    notices.delivery = lambda *args: calls.append(args)
    recovered = await notices.retry(record["id"])
    assert recovered["status"] == "delivered" and recovered["attempts"] == 2
    assert len(calls) == 2
    with pytest.raises(ValueError, match="notification_not_retryable"):
        await notices.retry(record["id"])
    await notices.close()


@pytest.mark.asyncio
async def test_unknown_delivery_errors_are_redacted_and_failed_notices_survive_restart(tmp_path):
    calls = []
    def failed(*args):
        calls.append(args)
        raise RuntimeError("credential=SECRET remote body")
    notices = CompanionNotifications(tmp_path, delivery=failed)
    await notices.observe(state(cycle("inventory_failed")))
    record = notices.recent()[0]
    assert record["error_code"] == "desktop_notification_delivery_failed"
    assert "SECRET" not in json.dumps(record)
    await notices.close()
    notices = CompanionNotifications(tmp_path, delivery=failed)
    await notices.observe(state(cycle("inventory_failed")))
    assert len(calls) == 1 and notices.recent()[0]["status"] == "held"
    await notices.close()


@pytest.mark.asyncio
async def test_cancellation_records_uncertain_delivery_and_never_repeats_automatically(tmp_path):
    entered = asyncio.Event()
    calls = []
    async def blocking(*args):
        calls.append(args)
        entered.set()
        await asyncio.Event().wait()
    notices = CompanionNotifications(tmp_path, delivery=blocking)
    task = asyncio.create_task(notices.observe(state(cycle("completed"))))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    record = notices.recent()[0]
    assert record["status"] == "held" and record["error_code"] == "notification_delivery_interrupted"
    await notices.observe(state(cycle("completed")))
    assert len(calls) == 1
    await notices.close()


@pytest.mark.asyncio
async def test_process_crash_recovery_holds_durable_delivering_intent(tmp_path):
    notices = CompanionNotifications(tmp_path, delivery=lambda *_: None)
    await notices.observe(state(cycle("completed")))
    record = notices.recent()[0]
    await notices.close()
    db = sqlite3.connect(tmp_path / "notifications/outbox.sqlite3")
    db.execute("UPDATE notifications SET status='delivering',delivered_at=NULL")
    db.execute("UPDATE delivery_attempts SET status='delivering',finished_at=NULL")
    db.commit()
    db.close()
    calls = []
    notices = CompanionNotifications(tmp_path, delivery=lambda *args: calls.append(args))
    assert notices.get(record["id"])["status"] == "held"
    assert notices.get(record["id"])["error_code"] == "notification_delivery_interrupted"
    await notices.observe(state(cycle("completed")))
    assert calls == []
    await notices.close()


@pytest.mark.asyncio
async def test_pending_before_dispatch_can_finish_after_restart(tmp_path):
    notices = CompanionNotifications(tmp_path, delivery=lambda *_: None)
    event = notices.project(state(cycle("completed")))[0]
    notices._record(event)  # Crash after durable creation, before dispatch claim.
    await notices.close()
    calls = []
    notices = CompanionNotifications(tmp_path, delivery=lambda *args: calls.append(args))
    await notices.observe({})
    assert len(calls) == 1 and notices.get(event["id"])["status"] == "delivered"
    await notices.close()


@pytest.mark.asyncio
async def test_singleton_ownership_private_files_and_bounded_safe_listing(tmp_path):
    notices = CompanionNotifications(tmp_path, delivery=lambda *_: None)
    assert stat.S_IMODE((tmp_path / "notifications").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "notifications/outbox.sqlite3").stat().st_mode) == 0o600
    with pytest.raises(ValueError, match="notifications_already_owned"):
        CompanionNotifications(tmp_path, delivery=lambda *_: None)
    for limit in (0, 201, True, "50"):
        with pytest.raises(ValueError, match="invalid_notification_limit"):
            notices.recent(limit)
    for identifier in ("../private", "wrong", 42):
        with pytest.raises(ValueError, match="notification_not_found"):
            await notices.retry(identifier)
    await notices.close()
    await notices.close()
    assert notices.recent() == []


def test_symlinked_storage_is_rejected_without_writing_target(tmp_path):
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    (tmp_path / "notifications").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="private_notification_directory_required"):
        CompanionNotifications(tmp_path)
    assert list(external.iterdir()) == []


@pytest.mark.asyncio
async def test_native_notification_uses_fixed_script_and_literal_argv_for_malicious_text(monkeypatch):
    import crawler.companion_notifications as module
    captured = []
    class Process:
        returncode = 0
        async def wait(self):
            return 0
    async def spawn(*args, **kwargs):
        captured.append((args, kwargs))
        return Process()
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    malicious = 'title" & do shell script "touch /tmp/never" $(whoami)'
    await native_macos_notification(malicious, malicious)
    args, kwargs = captured[0]
    assert args[:2] == ("/usr/bin/osascript", "-e")
    assert args[3:] == ("--", malicious, malicious)
    assert malicious not in args[2]
    assert "do shell script" not in args[2] and "display notification notificationBody" in args[2]
    assert "shell" not in kwargs
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == asyncio.subprocess.DEVNULL


@pytest.mark.asyncio
async def test_nonmac_keeps_notices_but_requires_explicit_retry(monkeypatch, tmp_path):
    import crawler.companion_notifications as module
    monkeypatch.setattr(module.sys, "platform", "linux")
    notices = CompanionNotifications(tmp_path)
    await notices.observe(state(cycle("completed")))
    record = notices.recent()[0]
    assert record["status"] == "held" and record["error_code"] == "desktop_notifications_unavailable"
    await notices.close()


@pytest.mark.asyncio
async def test_overlapping_observations_have_one_claim_and_one_delivery(tmp_path):
    calls = []
    entered, finish = asyncio.Event(), asyncio.Event()
    async def deliver(*args):
        calls.append(args)
        entered.set()
        await finish.wait()
    notices = CompanionNotifications(tmp_path, delivery=deliver)
    first = asyncio.create_task(notices.observe(state(cycle("completed"))))
    await entered.wait()
    second = asyncio.create_task(notices.observe(state(cycle("completed"))))
    await asyncio.sleep(0)
    assert len(calls) == 1
    finish.set()
    await asyncio.gather(first, second)
    assert len(calls) == 1 and notices.recent()[0]["attempts"] == 1
    await notices.close()


@pytest.mark.asyncio
async def test_native_failure_and_timeout_do_not_leak_process_output(monkeypatch):
    import crawler.companion_notifications as module
    class Process:
        returncode = None
        killed = False
        async def wait(self):
            return 1
        def kill(self):
            self.killed = True
    process = Process()
    async def spawn(*args, **kwargs):
        return process
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(ValueError, match="^desktop_notification_delivery_failed$"):
        await native_macos_notification("Hermes", "Fixed text")
    async def timed_out(coroutine, *, timeout):
        assert timeout == 15
        coroutine.close()
        raise asyncio.TimeoutError()
    monkeypatch.setattr(module.asyncio, "wait_for", timed_out)
    with pytest.raises(ValueError, match="^desktop_notification_delivery_timeout$"):
        await native_macos_notification("Hermes", "Fixed text")
    assert process.killed is True


def test_malformed_or_untrusted_state_never_becomes_notification_text():
    assert CompanionNotifications.project(None) == []
    assert CompanionNotifications.project(state({"id": "../../../private", "status": "completed"})) == []
    assert CompanionNotifications.project(state({"id": int("1" * 32), "status": "completed"})) == []
    assert CompanionNotifications.project(state(cycle(status={"invalid": "status"}))) == []
    assert CompanionNotifications.project(state(cycle(), config={"director_enabled": True}, director={"reason": {"not": "safe"}, "ready": False})) == []


@pytest.mark.asyncio
async def test_sync_status_readers_use_their_own_thread_local_read_connection(tmp_path):
    notices = CompanionNotifications(tmp_path, delivery=lambda *_: None)
    await notices.observe(state(cycle("completed")))
    expected = notices.recent()[0]
    # These mirror synchronous FastAPI routes running outside the loop thread.
    recent = await asyncio.to_thread(notices.recent)
    by_id = await asyncio.to_thread(notices.get, expected["id"])
    assert recent == [expected] and by_id == expected
    # A reader cannot acquire the writer connection or mutate its transaction.
    assert notices._db.in_transaction is False
    await notices.close()
