"""Private, deduplicated local notifications for unattended Companion work.

Only compact coordinator state is projected. Product names, source URLs, remote
errors, customer records and provider credentials never enter the outbox. Native
notifications are an optional delivery channel; the durable outbox is the source
of truth. An interrupted or failed delivery requires an explicit retry.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
import uuid


_ID = re.compile(r"^[a-f0-9]{32}$")
_NOTIFICATION_ID = re.compile(r"^[a-f0-9]{64}$")
_SCHEMA = "hermes-companion-notification/v1"
_APPLESCRIPT = """on run argv
    set notificationTitle to item 1 of argv
    set notificationBody to item 2 of argv
    display notification notificationBody with title notificationTitle
end run"""
_SUPPLIERS = {
    "4over": "4over", "fourover": "4over", "b2sign": "B2Sign",
    "printograph": "Printograph", "gotprint": "Printograph", "wsdisplay": "WSDisplay",
    "dps": "DPS", "digitalprintsolutions": "DPS", "soardist": "Soardist",
}
_DIRECTOR_REASONS = {
    "director_preflight_required", "director_provider_request_rejected_401",
    "director_provider_credentials_unavailable", "director_isolated_monthly_key_limit_required",
    "director_provider_budget_exhausted", "director_budget_exhausted", "director_model_not_available",
    "director_provider_transport_failed", "director_provider_metadata_invalid",
    "director_pricing_metadata_required", "director_provider_unavailable",
}
_DELIVERY_FAILURES = {
    "desktop_notifications_unavailable", "desktop_notification_delivery_failed",
    "desktop_notification_delivery_timeout", "notification_delivery_interrupted",
}


async def native_macos_notification(title: str, body: str):
    """Fixed AppleScript, with data passed only as argv; no shell or interpolation."""
    if sys.platform != "darwin":
        raise ValueError("desktop_notifications_unavailable")
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/osascript", "-e", _APPLESCRIPT, "--", title, body,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            code = await asyncio.wait_for(process.wait(), timeout=15)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise ValueError("desktop_notification_delivery_timeout") from None
        if code != 0:
            raise ValueError("desktop_notification_delivery_failed")
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        raise
    except OSError:
        raise ValueError("desktop_notification_delivery_failed") from None


def _safe_count(value):
    return min(value, 100000) if type(value) is int and value >= 0 else 0


def _private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("private_notification_directory_required")


class CompanionNotifications:
    """Durable one-shot notices, owned by the already singleton coordinator.

    ``observe`` accepts its safe status plus optional ``cycles`` summaries, since
    finished cycles no longer occupy ``status.cycle``. Delivery is awaited and
    serialized. Rendering/recent() never delivers. Failed, interrupted and
    unavailable delivery stays held until retry(notification_id) is called.
    """

    def __init__(self, root, *, delivery=None, clock=None):
        self.root = Path(root) / "notifications"
        _private_directory(self.root)
        self.clock = clock or time.time
        self.delivery = delivery or native_macos_notification
        self._lock = asyncio.Lock()
        self._closed = False
        owner_fd = os.open(self.root / ".owner", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self._owner = os.fdopen(owner_fd, "a+b")
        try:
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self._owner.close()
            raise ValueError("notifications_already_owned") from None
        db_path = self.root / "outbox.sqlite3"
        self._db_path = db_path
        try:
            db_fd = os.open(db_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(db_fd)
            os.close(db_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("private_notification_database_required")
            self._db = sqlite3.connect(db_path, isolation_level=None)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS notifications (
                  id TEXT PRIMARY KEY, kind TEXT NOT NULL, cycle_id TEXT NOT NULL,
                  supplier TEXT NOT NULL, reason TEXT NOT NULL, title TEXT NOT NULL,
                  body TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                  error_code TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                  delivered_at REAL
                );
                CREATE TABLE IF NOT EXISTS delivery_attempts (
                  id TEXT PRIMARY KEY, notification_id TEXT NOT NULL REFERENCES notifications(id),
                  status TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL, error_code TEXT
                );
            """)
            with self._transaction():
                # Only possible after the previous singleton process released
                # its lock. A prior external operation may already have shown.
                self._db.execute("UPDATE notifications SET status='held',error_code='notification_delivery_interrupted',updated_at=? WHERE status='delivering'", (self.clock(),))
                self._db.execute("UPDATE delivery_attempts SET status='held',error_code='notification_delivery_interrupted',finished_at=? WHERE status='delivering'", (self.clock(),))
        except BaseException:
            if hasattr(self, "_db"):
                self._db.close()
            self._owner.close()
            raise

    @contextmanager
    def _transaction(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")

    @staticmethod
    def _event(kind, cycle_id, reason, body, supplier=""):
        key = json.dumps([_SCHEMA, kind, cycle_id, supplier, reason], separators=(",", ":"))
        return {"id": hashlib.sha256(key.encode()).hexdigest(), "kind": kind,
                "cycle_id": cycle_id, "supplier": supplier, "reason": reason,
                "title": "Hermes Product Companion", "body": body}

    @classmethod
    def project(cls, status):
        """A fixed, allowlisted vocabulary prevents remote text leaking to OS UI."""
        if not isinstance(status, dict):
            return []
        cycles = status.get("cycles") if isinstance(status.get("cycles"), list) else []
        active = status.get("cycle")
        if isinstance(active, dict):
            cycles = [active, *cycles]
        seen, events = set(), []
        for cycle in cycles[:100]:
            if (not isinstance(cycle, dict) or not isinstance(cycle.get("id"), str)
                    or not _ID.fullmatch(cycle["id"])):
                continue
            cycle_id = cycle["id"]
            if cycle_id in seen:
                continue
            seen.add(cycle_id)
            phase = cycle.get("status")
            if not isinstance(phase, str):
                continue
            summary = cycle.get("summary") if isinstance(cycle.get("summary"), dict) else {}
            inventory = cycle.get("inventory") if isinstance(cycle.get("inventory"), dict) else {}
            verified = _safe_count(summary.get("verified"))
            total = _safe_count(inventory.get("count"))
            if phase in {"completed", "completed_with_attention"}:
                body = f"Audit cycle completed: {verified} of {total} products verified."
                if phase == "completed_with_attention":
                    body += " Some products require review; unknown evidence remains held."
                body += " Open Hermes for results and pending release approvals."
                events.append(cls._event("completion", cycle_id, "audit_cycle_completed", body))
            elif phase == "inventory_failed":
                events.append(cls._event("failure", cycle_id, "catalog_inventory_failed",
                    "Catalog inventory could not finish. Saved evidence is retained. Open Hermes to review the failure before retrying."))
            elif cycle.get("fault") and phase == "paused":
                events.append(cls._event("failure", cycle_id, "audit_cycle_paused_after_failure",
                    "An audit cycle paused after a failure. Open Hermes to review saved progress. Uncertain requests will not be repeated automatically."))
            if _safe_count(summary.get("failed")) > 0 and phase not in {"completed", "completed_with_attention"}:
                events.append(cls._event("failure", cycle_id, "product_checks_failed",
                    "One or more product checks failed. Other eligible suppliers continue. Open Hermes to review the exceptions."))
            if _safe_count(summary.get("held")) > 0 and phase not in {"completed", "completed_with_attention"}:
                events.append(cls._event("required_action", cycle_id, "held_work_requires_review",
                    "Some product work is held and needs review. Saved evidence is retained; uncertain operations are not retried automatically."))
            blocks = cycle.get("supplier_blocks")
            if isinstance(blocks, dict):
                suppliers = {_SUPPLIERS.get(str(key).lower(), "Supplier") for key in blocks}
                for supplier in sorted(suppliers):
                    events.append(cls._event("required_action", cycle_id, "supplier_sign_in_required",
                        f"{supplier} needs attention in Hermes. Sign in through its saved supplier browser if requested. Other eligible suppliers continue.", supplier))
        # Director or baseline holds are actionable only after an audit has
        # actually begun. An untouched default-off installation stays quiet.
        if (isinstance(active, dict) and isinstance(active.get("id"), str) and _ID.fullmatch(active["id"])
                and isinstance(active.get("status"), str) and active["status"] in {"running", "paused"}):
            cycle_id = active["id"]
            config = status.get("config") if isinstance(status.get("config"), dict) else {}
            director = status.get("director") if isinstance(status.get("director"), dict) else {}
            reason = director.get("reason")
            if (config.get("director_enabled") is True and director.get("ready") is not True
                    and isinstance(reason, str) and reason in _DIRECTOR_REASONS):
                events.append(cls._event("required_action", cycle_id, reason,
                    "The Muse director needs attention. Collection and deterministic checks continue; AI decisions remain held. Open Hermes for the provider or monthly budget requirement."))
            baseline = status.get("baseline")
            if isinstance(baseline, dict) and baseline.get("ready") is False:
                events.append(cls._event("required_action", cycle_id, "staging_baseline_requires_review",
                    "Staging repairs are held until the environment baseline, backups and test evidence are verified. Read-only audits can continue. Open Hermes for setup requirements."))
        return events

    def _record(self, event):
        now = self.clock()
        self._db.execute("""INSERT OR IGNORE INTO notifications
            (id,kind,cycle_id,supplier,reason,title,body,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,'pending',?,?)""",
            tuple(event[key] for key in ("id", "kind", "cycle_id", "supplier", "reason", "title", "body")) + (now, now))

    async def observe(self, status):
        async with self._lock:
            if self._closed:
                return []
            events = self.project(status)
            with self._transaction():
                for event in events:
                    self._record(event)
            # Pending means durable intent only: no external operation has
            # started. Delivering is claimed separately before calling the OS.
            pending = [row[0] for row in self._db.execute("SELECT id FROM notifications WHERE status='pending' ORDER BY created_at,id LIMIT 100")]
            for identifier in pending:
                await self._deliver(identifier)
            return [self.get(event["id"]) for event in events]

    async def _deliver(self, identifier):
        with self._transaction():
            record = self._db.execute("SELECT * FROM notifications WHERE id=?", (identifier,)).fetchone()
            if record is None or record["status"] != "pending":
                return
            attempt = uuid.uuid4().hex
            self._db.execute("UPDATE notifications SET status='delivering',attempts=attempts+1,updated_at=? WHERE id=?", (self.clock(), identifier))
            self._db.execute("INSERT INTO delivery_attempts(id,notification_id,status,started_at) VALUES (?,?,'delivering',?)", (attempt, identifier, self.clock()))
        error, cancelled = None, False
        try:
            outcome = self.delivery(record["title"], record["body"])
            if inspect.isawaitable(outcome):
                outcome = await outcome
            if outcome is not None and outcome is not True:
                raise ValueError("desktop_notification_delivery_failed")
        except asyncio.CancelledError:
            error, cancelled = "notification_delivery_interrupted", True
        except Exception as exc:
            code = str(exc)
            error = code if code in _DELIVERY_FAILURES else "desktop_notification_delivery_failed"
        with self._transaction():
            now = self.clock()
            state = "held" if error else "delivered"
            self._db.execute("UPDATE notifications SET status=?,error_code=?,updated_at=?,delivered_at=? WHERE id=?", (state, error, now, None if error else now, identifier))
            self._db.execute("UPDATE delivery_attempts SET status=?,finished_at=?,error_code=? WHERE id=?", (state, now, error, attempt))
        if cancelled:
            raise asyncio.CancelledError

    async def retry(self, identifier):
        if not isinstance(identifier, str) or not _NOTIFICATION_ID.fullmatch(identifier):
            raise ValueError("notification_not_found")
        async with self._lock:
            if self._closed:
                raise ValueError("notifications_closed")
            record = self.get(identifier)
            if record["status"] not in {"held", "pending"}:
                raise ValueError("notification_not_retryable")
            self._db.execute("UPDATE notifications SET status='pending',error_code=NULL,updated_at=? WHERE id=?", (self.clock(), identifier))
            await self._deliver(identifier)
            return self.get(identifier)

    def get(self, identifier):
        if self._closed or not isinstance(identifier, str) or not _NOTIFICATION_ID.fullmatch(identifier):
            raise ValueError("notification_not_found")
        records = self._read_rows("SELECT * FROM notifications WHERE id=?", (identifier,))
        if not records:
            raise ValueError("notification_not_found")
        return records[0]

    def _read_rows(self, statement, parameters=()):
        # FastAPI runs synchronous status routes in worker threads. The writer
        # remains owned by its event-loop thread; readers get their own short,
        # read-only connection and never share a sqlite Connection object.
        if self._db_path.is_symlink():
            raise ValueError("private_notification_database_required")
        info = self._db_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("private_notification_database_required")
        reader = sqlite3.connect(self._db_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            reader.row_factory = sqlite3.Row
            return [dict(row) for row in reader.execute(statement, parameters)]
        finally:
            reader.close()

    def recent(self, limit=50):
        if self._closed:
            return []
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("invalid_notification_limit")
        return self._read_rows("SELECT * FROM notifications ORDER BY created_at DESC,id LIMIT ?", (limit,))

    async def close(self):
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._db.close()
            self._owner.close()
