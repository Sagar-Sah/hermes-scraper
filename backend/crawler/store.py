"""Small durable state machine. Large response/row bodies live in immutable objects."""

import gzip
import hashlib
import json
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import (
    Column,
    String,
    Integer,
    Float,
    Text,
    JSON,
    create_engine,
    select,
    update,
    delete,
    event,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Session

from .config import Settings
from . import timing


def uid():
    return uuid.uuid4().hex


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"
    id = Column(String, primary_key=True, default=uid)
    name = Column(String, nullable=False)
    status = Column(String, default="queued", index=True)
    created_at = Column(Float, default=time.time)
    updated_at = Column(Float, default=time.time)
    spec = Column(JSON, default=dict)
    error = Column(Text, nullable=True)


class Product(Base):
    __tablename__ = "products"
    id = Column(String, primary_key=True, default=uid)
    run_id = Column(String, index=True, nullable=False)
    url = Column(Text, nullable=False)
    title = Column(String, default="")
    status = Column(String, default="queued", index=True)
    row_count = Column(Integer, default=0)
    request_count = Column(Integer, default=0)
    cache_hits = Column(Integer, default=0)
    coverage_status = Column(String, default="unknown")
    error = Column(Text, nullable=True)
    lease_until = Column(Float, default=0)
    epoch = Column(Integer, default=0)
    owner = Column(String, default="")
    export_manifest = Column(JSON, default=dict)


class ProductTiming(Base):
    __tablename__ = "product_timing"
    product_id = Column(String, primary_key=True)
    run_id = Column(String, index=True, nullable=False)
    data = Column(JSON, nullable=False)


@event.listens_for(Product, "after_insert")
def initialize_product_timing(mapper, connection, product):
    # New products have an observed birth. Existing rows are not backfilled
    # from a Run creation date that may predate their own lifecycle.
    wall, mono = time.time(), time.monotonic()
    state = "queued" if product.status == "queued" else "paused" if product.status == "paused" else "unknown"
    timing_session = connection.info.get("hermes_timing_session", "created")
    connection.execute(ProductTiming.__table__.insert().values(product_id=product.id, run_id=product.run_id,
        data=timing.new_record(state, wall, mono, timing_session, created_at=wall,
            complete=timing_session != "created" and state in {"queued", "paused"} and not product.row_count and not product.request_count and not product.epoch)))


class Response(Base):
    __tablename__ = "responses"
    id = Column(String, primary_key=True)
    request_key = Column(String, index=True)
    created_at = Column(Float, default=time.time)
    status = Column(Integer)
    url = Column(Text)
    object_path = Column(Text)
    raw_hash = Column(String)
    semantic_hash = Column(String)
    etag = Column(Text, nullable=True)
    modified = Column(Text, nullable=True)
    content_type = Column(String)


class Batch(Base):
    __tablename__ = "batches"
    id = Column(String, primary_key=True, default=uid)
    product_id = Column(String, index=True)
    object_path = Column(Text)
    row_count = Column(Integer)
    keys = Column(JSON)
    sequence = Column(Integer)


class BrowserBranch(Base):
    __tablename__ = "browser_branches"
    id = Column(String, primary_key=True)
    run_id = Column(String, index=True, nullable=False)
    product_id = Column(String, index=True, nullable=False)
    scope = Column(String, index=True, nullable=False)
    prefix = Column(JSON, nullable=False)
    rows_object = Column(Text, nullable=False)
    row_count = Column(Integer, nullable=False)
    committed_row_count = Column(Integer, nullable=False)
    details = Column(JSON, default=dict)
    completed_at = Column(Float, default=time.time)


class ProductRecordScope(Base):
    __tablename__ = "product_record_scopes"
    product_id = Column(String, primary_key=True)
    schema_version = Column(Integer, nullable=False, default=2)
    legacy_row_count = Column(Integer, nullable=False, default=0)
    initialized_at = Column(Float, default=time.time)


class TrafficWindow(Base):
    __tablename__ = "traffic_windows"
    product_id = Column(String, primary_key=True)
    run_id = Column(String, index=True)
    started_at = Column(Float, default=time.time)
    requests_start = Column(Integer, nullable=False)
    cache_hits_start = Column(Integer, nullable=False)
    rows_start = Column(Integer, nullable=False)


class ResponseObservation(Base):
    __tablename__ = "response_observations"
    id = Column(String, primary_key=True)
    run_id = Column(String, index=True)
    product_id = Column(String, index=True)
    response_id = Column(String, index=True)
    created_at = Column(Float, default=time.time)
    kind = Column(String, index=True)
    request_signature = Column(String, index=True)
    context = Column(JSON, default=dict)


class ProbeJob(Base):
    __tablename__ = "probe_jobs"
    id = Column(String, primary_key=True)
    run_id = Column(String, index=True)
    status = Column(String, default="queued")
    budget = Column(Integer, nullable=False)
    used = Column(Integer, default=0)
    owner = Column(String, default="")
    created_at = Column(Float, default=time.time)
    updated_at = Column(Float, default=time.time)
    manifest = Column(JSON, default=dict)
    results = Column(JSON, default=list)
    error = Column(String, nullable=True)


class ProbeAttempt(Base):
    __tablename__ = "probe_attempts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String, index=True)
    sequence = Column(Integer, nullable=False)
    created_at = Column(Float, default=time.time)
    phase = Column(String)
    method = Column(String)
    public_url = Column(Text)
    status = Column(Integer, nullable=True)
    elapsed = Column(Float, nullable=True)


def safe_public_context(value):
    """Keep pricing selections readable without persisting credential material."""
    import re
    if isinstance(value, dict):
        return {str(k): safe_public_context(v) for k, v in value.items()
                if not re.search(r"secret|token|cookie|password|authorization|headers|credential|session_state|api.?key|private.?key|access.?key", str(k), re.I)}
    if isinstance(value, (list, tuple)):
        return [safe_public_context(v) for v in value]
    return value


class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String, index=True)
    created_at = Column(Float, default=time.time)
    type = Column(String)
    payload = Column(JSON)


class Profile(Base):
    __tablename__ = "profiles"
    id = Column(String, primary_key=True, default=uid)
    kind = Column(String, index=True)
    data = Column(JSON, default=dict)
    secret = Column(Text, default="")


class Permit(Base):
    __tablename__ = "permits"
    id = Column(String, primary_key=True, default=uid)
    origin = Column(String, index=True)
    expires = Column(Float, index=True)


class Origin(Base):
    __tablename__ = "origins"
    id = Column(String, primary_key=True)
    next_at = Column(Float, default=0)
    backoff_until = Column(Float, default=0)
    latency = Column(Float, default=0)
    rps = Column(Float, default=2)
    healthy = Column(Integer, default=0)


class RequestLease(Base):
    __tablename__ = "request_leases"
    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False)
    expires = Column(Float, nullable=False)


class Store:
    def __init__(self, config: Settings):
        self.config = config
        self.root = config.data_dir
        self.timing_session = uid()
        self.engine = create_engine(
            config.database_url,
            pool_pre_ping=True,
            connect_args=(
                {"check_same_thread": False, "timeout": 30}
                if config.database_url.startswith("sqlite")
                else {}
            ),
        )
        self.sqlite = self.engine.dialect.name == "sqlite"
        @event.listens_for(self.engine, "connect")
        def timing_connection(dbapi, connection_record):
            connection_record.info["hermes_timing_session"] = self.timing_session
        if self.sqlite:

            @event.listens_for(self.engine, "connect")
            def configure(dbapi, _):
                dbapi.execute("PRAGMA journal_mode=WAL")
                dbapi.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        key = os.getenv("ATLAS_SECRET_KEY")
        if not key:
            key_path = self.root / "encryption.key"
            try:
                fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(Fernet.generate_key())
            except FileExistsError:
                pass
            key = key_path.read_text().strip()
        self.cipher = Fernet(key.encode())
        self.s3 = None
        if config.s3_bucket:
            import boto3

            self.s3 = boto3.client("s3", endpoint_url=config.s3_endpoint)

    @contextmanager
    def session(self, immediate=False):
        with Session(self.engine, expire_on_commit=False) as s:
            if immediate and self.sqlite:
                s.connection().exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield s
                s.commit()
            except BaseException:
                s.rollback()
                raise

    def put_object(self, data: bytes, suffix="bin"):
        sha = hashlib.sha256(data).hexdigest()
        key = f"objects/{sha[:2]}/{sha}.{suffix}"
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_name(target.name + "." + uid() + ".tmp")
            with temporary.open("wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, target)
        if self.s3:
            self.s3.put_object(Bucket=self.config.s3_bucket, Key=key, Body=data)
        return key

    def get_object(self, key):
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Invalid object path")
        if path.exists():
            return path.read_bytes()
        if self.s3:
            data = self.s3.get_object(Bucket=self.config.s3_bucket, Key=key)[
                "Body"
            ].read()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return data
        raise FileNotFoundError(key)

    def event(self, run_id, kind, payload):
        with self.session() as s:
            s.add(Event(run_id=run_id, type=kind, payload=payload))
            s.execute(
                update(Run).where(Run.id == run_id).values(updated_at=time.time())
            )

    def status(self, run_id):
        with self.session() as s:
            return s.scalar(select(Run.status).where(Run.id == run_id))

    def control_product(self, run_id, product_id, action):
        if action not in {"pause", "resume"}:
            raise ValueError("Unknown product action")
        with self.session(immediate=True) as s:
            p = s.get(Product, product_id, with_for_update=not self.sqlite)
            r = s.get(Run, run_id)
            if p is None or r is None or p.run_id != run_id:
                raise LookupError("Product not found in this run")
            if action == "pause":
                if p.status in {"queued", "running"}:
                    p.status, p.owner, p.lease_until = "paused", "", 0
                    p.epoch += 1
            elif p.status == "paused":
                p.status, p.error, p.lease_until = "queued", None, 0
                if r.status in {"paused", "needs_attention", "completed_with_gaps"}:
                    r.status = "queued"
            r.updated_at = self.now(s)
            self.sync_product_timing(s, p, r, wall=r.updated_at)
            s.add(Event(run_id=run_id, type="product_" + action, payload={
                "product_id": product_id, "status": p.status, "preserved_rows": p.row_count,
            }))
            return p.status

    def delete_run(self, run_id):
        """Permanently remove a run and its own rows/events; shared content-addressed
        objects under objects/ are left in place since other runs may reference them."""
        with self.session(immediate=True) as s:
            r = s.get(Run, run_id)
            if r is None:
                raise LookupError("Run not found")
            product_ids = list(s.scalars(select(Product.id).where(Product.run_id == run_id)))
            job_ids = list(s.scalars(select(ProbeJob.id).where(ProbeJob.run_id == run_id)))
            if product_ids:
                s.execute(delete(Batch).where(Batch.product_id.in_(product_ids)))
                s.execute(delete(ProductRecordScope).where(ProductRecordScope.product_id.in_(product_ids)))
                s.execute(delete(ProductTiming).where(ProductTiming.product_id.in_(product_ids)))
            if job_ids:
                s.execute(delete(ProbeAttempt).where(ProbeAttempt.job_id.in_(job_ids)))
            s.execute(delete(ProbeJob).where(ProbeJob.run_id == run_id))
            s.execute(delete(BrowserBranch).where(BrowserBranch.run_id == run_id))
            s.execute(delete(TrafficWindow).where(TrafficWindow.run_id == run_id))
            s.execute(delete(ResponseObservation).where(ResponseObservation.run_id == run_id))
            s.execute(delete(Event).where(Event.run_id == run_id))
            s.execute(delete(Product).where(Product.run_id == run_id))
            s.delete(r)
        export_dir = (self.root / "exports" / run_id).resolve()
        exports_root = (self.root / "exports").resolve()
        if export_dir.is_relative_to(exports_root) and export_dir.exists():
            shutil.rmtree(export_dir, ignore_errors=True)

    def link_response(self, run_id, product_id, response_id, kind, request_signature, context):
        if not product_id:
            return
        with self.session(immediate=True) as s:
            key = digest({"product": product_id, "response": response_id, "kind": kind})
            if s.get(ResponseObservation, key) is None:
                s.add(ResponseObservation(id=key, run_id=run_id, product_id=product_id,
                    response_id=response_id, kind=kind, request_signature=request_signature,
                    context=safe_public_context(context or {})))

    def now(self, session):
        return (
            time.time()
            if self.sqlite
            else float(
                session.scalar(select(func.extract("epoch", func.clock_timestamp())))
            )
        )

    def claim(self, owner):
        with self.session(immediate=True) as s:
            now = self.now(s)
            q = (
                select(Product)
                .join(Run, Run.id == Product.run_id)
                .where(
                    Run.status.in_(["queued", "running"]),
                    (
                        ((Product.status == "queued") & (Product.lease_until <= now))
                        | ((Product.status == "running") & (Product.lease_until < now))
                    ),
                )
                .order_by(Run.created_at, Product.id)
                .limit(1)
            )
            if not self.sqlite:
                q = q.with_for_update(skip_locked=True, of=Product)
            p = s.scalar(q)
            if not p:
                return None
            p.status, p.owner, p.lease_until, p.epoch = (
                "running",
                owner,
                now + 60,
                p.epoch + 1,
            )
            p.error = None  # The previous attempt remains in durable events.
            r = s.get(Run, p.run_id)
            r.status, r.updated_at = "running", now
            self.sync_product_timing(s, p, r, wall=now, claimed=True)
            return {
                "id": p.id,
                "run_id": p.run_id,
                "url": p.url,
                "epoch": p.epoch,
                "spec": r.spec,
            }

    def heartbeat(self, product_id, epoch):
        with self.session() as s:
            updated = (
                s.execute(
                    update(Product)
                    .where(
                        Product.id == product_id,
                        Product.epoch == epoch,
                        Product.status == "running",
                    )
                    .values(lease_until=self.now(s) + 60)
                ).rowcount
                == 1
            )
            if updated:
                p = s.get(Product, product_id)
                self.sync_product_timing(s, p, s.get(Run, p.run_id))
            return updated

    def retry_product(self, run_id, product_id, epoch, code, message):
        """Durably delay a transient failure; never retry integrity/auth failures.

        Three recoveries are allowed without new durable rows. The transaction
        fences the failed worker and records the attempt before requeueing.
        Queued lease_until is a not-before time, not an active worker lease.
        """
        if code not in {
            "browser_network_error",
            "network_error",
            "retry_exhausted",
            "rate_limited",
            "source_unavailable",
        }:
            return None
        with self.session(immediate=True) as s:
            p = s.get(Product, product_id, with_for_update=not self.sqlite)
            r = s.get(Run, run_id)
            if (
                not p
                or p.run_id != run_id
                or p.epoch != epoch
                or p.status != "running"
                or not r
                or r.status not in {"queued", "running"}
            ):
                return None
            attempts = s.scalar(
                select(func.count())
                .select_from(Event)
                .where(
                    Event.run_id == run_id,
                    Event.type == "product_retry_scheduled",
                    Event.payload["product_id"].as_string() == product_id,
                    Event.payload["row_count"].as_integer() == p.row_count,
                )
            )
            if attempts >= 3:
                return None
            now = self.now(s)
            delay = 5 * (2**attempts)
            payload = {
                "product_id": product_id,
                "code": code,
                "message": message[:1200],
                "attempt": attempts + 1,
                "max_attempts_without_progress": 3,
                "retry_at": now + delay,
                "delay_seconds": delay,
                "row_count": p.row_count,
            }
            p.status, p.lease_until, p.owner = "queued", now + delay, ""
            p.epoch += 1
            p.error = message[:1200]
            r.updated_at = now
            self.sync_product_timing(s, p, r, wall=now)
            s.add(Event(run_id=run_id, type="product_retry_scheduled", payload=payload))
            return payload

    def sync_product_timing(self, session, product, run, *, wall=None, claimed=False, state=None):
        wall = self.now(session) if wall is None else wall
        state = state or timing.product_state(product, run, wall)
        saved = session.get(ProductTiming, product.id)
        data = saved.data if saved else None
        if isinstance(data, dict) and data.get("state") == "active" and claimed and data.get("epoch") != product.epoch:
            data = dict(data, session="unconfirmed_prior_owner")
        record = timing.advance(data, state, wall, time.monotonic(), self.timing_session,
            waiting_until=product.lease_until if state == "waiting" else None)
        record["epoch"] = product.epoch
        if saved: saved.data = record
        else: session.add(ProductTiming(product_id=product.id, run_id=product.run_id, data=record))

    def close_product_timing(self, product_id, epoch):
        with self.session(immediate=True) as session:
            product = session.get(Product, product_id)
            if product is not None and product.epoch == epoch:
                self.sync_product_timing(session, product, session.get(Run, product.run_id),
                    state="unknown" if product.status == "running" else None)

    def product_timings(self, session, products, runs=None):
        if not products: return {}
        wall, mono = self.now(session), time.monotonic()
        ids = [p.id for p in products]
        saved = {}
        for offset in range(0, len(ids), 500):
            saved.update({row.product_id: row.data for row in session.scalars(select(ProductTiming).where(
                ProductTiming.product_id.in_(ids[offset:offset + 500])))})
        if runs is None:
            run_ids, runs = list({p.run_id for p in products}), {}
            for offset in range(0, len(run_ids), 500):
                runs.update({r.id: r for r in session.scalars(select(Run).where(Run.id.in_(run_ids[offset:offset + 500])))})
        result = {}
        for product in products:
            state = timing.product_state(product, runs.get(product.run_id), wall)
            record = saved.get(product.id)
            live = (state == "active" and product.lease_until > wall and timing.valid(record)
                and type(record.get("epoch")) is int and record.get("epoch") == product.epoch
                and record.get("session") == self.timing_session)
            if state == "active" and not live: state = "unknown"
            result[product.id] = timing.snapshot(record, state, wall, mono, self.timing_session,
                live=live, live_until=product.lease_until)
        return result

    def existing_keys(self, product_id):
        with self.session() as s:
            return {
                k
                for keys in s.scalars(
                    select(Batch.keys).where(Batch.product_id == product_id)
                )
                for k in keys
            }

    def append_rows(self, product_id, epoch, rows):
        if not rows:
            return
        payload = gzip.compress(
            ("\n".join(canonical(r) for r in rows) + "\n").encode(), mtime=0
        )
        key = self.put_object(payload, "jsonl.gz")
        with self.session(immediate=True) as s:
            p = s.get(Product, product_id, with_for_update=not self.sqlite)
            if not p or p.epoch != epoch or p.status != "running":
                raise RuntimeError("Worker lease superseded")
            sequence = s.scalar(
                select(func.count())
                .select_from(Batch)
                .where(Batch.product_id == product_id)
            )
            s.add(
                Batch(
                    product_id=product_id,
                    object_path=key,
                    row_count=len(rows),
                    keys=[r["id"] for r in rows],
                    sequence=sequence,
                )
            )
            p.row_count += len(rows)
            p.lease_until = self.now(s) + 60
            if rows[0].get("title"):
                p.title = rows[0]["title"]

    def iter_rows(self, run_id=None, product_id=None):
        with self.session() as s:
            q = select(Batch.object_path).join(Product, Product.id == Batch.product_id)
            if run_id:
                q = q.where(Product.run_id == run_id)
            if product_id:
                q = q.where(Product.id == product_id)
            paths = list(s.scalars(q.order_by(Batch.product_id, Batch.sequence)))
        for path in paths:
            for line in gzip.decompress(self.get_object(path)).splitlines():
                yield json.loads(line)

    def set_record_scope(self, product_id, epoch, legacy_row_count):
        with self.session(immediate=True) as s:
            p = s.get(Product, product_id, with_for_update=not self.sqlite)
            if not p or p.epoch != epoch or p.status != "running":
                from .transport import RunInterrupted

                raise RunInterrupted("Worker superseded while classifying legacy rows")
            scope = s.get(ProductRecordScope, product_id)
            changed = scope is None or scope.legacy_row_count != legacy_row_count
            if scope is None:
                scope = ProductRecordScope(product_id=product_id)
                s.add(scope)
            scope.schema_version = 2
            scope.legacy_row_count = legacy_row_count
            if changed and legacy_row_count:
                s.add(
                    Event(
                        run_id=p.run_id,
                        type="legacy_rows_quarantined",
                        payload={
                            "product_id": product_id,
                            "rows": legacy_row_count,
                            "reason": "Historical quotes lack service-scoped identities; raw records retained without inferred selections",
                        },
                    )
                )

    def current_rows(self, run_id=None, product_id=None):
        from .record_scope import current_rows

        yield from current_rows(self.iter_rows(run_id=run_id, product_id=product_id))

    def record_counts(self, product_id):
        with self.session() as s:
            p = s.get(Product, product_id)
            scope = s.get(ProductRecordScope, product_id)
            total = p.row_count if p else 0
            legacy = scope.legacy_row_count if scope else 0
            return {
                "source_row_count": total,
                "legacy_row_count": legacy,
                "scoped_row_count": max(0, total - legacy),
                "record_scope": "service_context_v2" if scope else "not_assessed",
            }

    def browser_branches(self, run_id, product_id, scope):
        with self.session() as s:
            return [
                {
                    "prefix": item.prefix,
                    "rows_object": item.rows_object,
                    "row_count": item.row_count,
                    "committed_row_count": item.committed_row_count,
                    "details": item.details,
                }
                for item in s.scalars(
                    select(BrowserBranch).where(
                        BrowserBranch.run_id == run_id,
                        BrowserBranch.product_id == product_id,
                        BrowserBranch.scope == scope,
                    )
                )
            ]

    def complete_browser_branch(
        self, run_id, product_id, epoch, scope, prefix, row_ids, details
    ):
        # Rows commit before this marker is requested. A crash between those two
        # commits can replay work, but can never skip an uncommitted row.
        ids = sorted(set(row_ids))
        if not ids:
            return
        object_path = self.put_object(
            gzip.compress(canonical(ids).encode(), mtime=0), "branch-rows.json.gz"
        )
        key = digest(
            {
                "product_id": product_id,
                "scope": scope,
                "prefix": prefix,
                "service_context": details.get("service_context"),
            }
        )
        with self.session(immediate=True) as s:
            product = s.get(Product, product_id, with_for_update=not self.sqlite)
            run = s.get(Run, run_id)
            if (
                not product
                or product.run_id != run_id
                or product.epoch != epoch
                or product.status != "running"
                or not run
                or run.status not in {"queued", "running"}
            ):
                from .transport import RunInterrupted

                raise RunInterrupted(
                    "Worker lease superseded before browser branch checkpoint"
                )
            if product.row_count < len(ids):
                raise RuntimeError("Browser branch cannot precede its durable rows")
            # Replace smaller completed subtrees with their completed ancestor.
            # The DB keeps small metadata; row identities are immutable objects.
            for item in s.scalars(
                select(BrowserBranch).where(
                    BrowserBranch.product_id == product_id, BrowserBranch.scope == scope
                )
            ):
                if item.details.get("service_context") == details.get(
                    "service_context"
                ) and all(item.prefix.get(k) == v for k, v in prefix.items()):
                    s.delete(item)
            s.flush()
            s.add(
                BrowserBranch(
                    id=key,
                    run_id=run_id,
                    product_id=product_id,
                    scope=scope,
                    prefix=prefix,
                    rows_object=object_path,
                    row_count=len(ids),
                    committed_row_count=product.row_count,
                    details=details,
                )
            )

    def save_profile(self, kind, data, secret):
        profile_id = data.pop("id", None) or uid()
        with self.session() as s:
            p = s.get(Profile, profile_id)
            if p and p.kind != kind:
                raise ValueError("Profile kind mismatch")
            if p is None:
                p = Profile(id=profile_id, kind=kind)
                s.add(p)
            p.data = data
            if secret is not None:
                p.secret = self.cipher.encrypt(canonical(secret).encode()).decode()
        return profile_id

    def profiles(self, kind, secrets=False):
        with self.session() as s:
            result = []
            for p in s.scalars(select(Profile).where(Profile.kind == kind)):
                item = dict(p.data, id=p.id, has_credentials=bool(p.secret))
                if secrets:
                    item["credentials"] = (
                        json.loads(self.cipher.decrypt(p.secret.encode()))
                        if p.secret
                        else {}
                    )
                result.append(item)
            return result
