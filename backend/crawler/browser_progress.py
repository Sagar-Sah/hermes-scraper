"""Durable completed-branch journal; never an inferred cursor over old rows."""

import ast
import asyncio
import gzip
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .store import canonical, digest
from .transport import ExtractionError
from .service_context import service_context_is_bound

JOURNAL_VERSION = 2
VERIFICATION_NOTES = {"quantity_candidates_pending_verification"}


def semantic_code_hash(source):
    """Formatting, comments and file timestamps do not invalidate progress."""
    return digest(ast.dump(ast.parse(source), include_attributes=False))


@lru_cache(maxsize=1)
def adapter_semantics():
    root = Path(__file__).parent
    return {
        name: semantic_code_hash((root / name).read_text())
        for name in (
            "adapters/browser.py",
            "adapters/browser_tables.py",
            "commerce.py",
            "browser_progress.py",
            "service_context.py",
            "record_scope.py",
        )
    }


def extraction_scope(spec):
    return digest(
        {
            "journal_version": JOURNAL_VERSION,
            "spec": spec,
            "code": adapter_semantics(),
        }
    )


def normalized_prefix(prefix):
    return {str(k): str(v) for k, v in prefix.items()}


class CoverageNotes(set):
    """Count every observed enumeration gap, including a repeated sibling gap."""

    def __init__(self):
        super().__init__()
        self.generation = 0
        self.retained_notes = set()

    def add(self, reason):
        if reason not in VERIFICATION_NOTES:
            self.generation += 1
        else:
            self.retained_notes.add(reason)
        super().add(reason)

    def update(self, reasons):
        for reason in reasons:
            self.add(reason)

    def replay(self, reasons):
        # These are known outside-domain gaps retained by a completed finite
        # branch, not a declaration that the whole product was enumerated.
        self.retained_notes.update(reasons)
        super().update(reasons)

    def observe_snapshot(self, snapshot):
        known_source = str(snapshot.get("product_type")) in {"1", "27", "36"}
        for reason in [
            *snapshot.get("unsupported", []),
            *snapshot.get("coverage_gaps", []),
        ]:
            outside_domain = known_source and reason in {
                "collapsed_service_control:proofType",
                "collapsed_service_control:fileDeliveryForIop",
            }
            if known_source and reason == "custom_values:size":
                size = next(
                    (c for c in snapshot["controls"] if c["key"] == "size"), None
                )
                outside_domain = bool(
                    size
                    and size["kind"] == "select"
                    and any(
                        str(choice["value"]) == str(size.get("value"))
                        and not choice.get("disabled")
                        and not choice.get("placeholder")
                        for choice in size.get("choices", [])
                    )
                )
            if outside_domain:
                self.replay([reason])
            else:
                self.add(reason)


@dataclass
class BranchFrame:
    prefix: dict
    row_ids: set = field(default_factory=set)
    control_keys: set = field(default_factory=set)
    service_context: dict | None = None
    context_changed: bool = False
    abandoned: bool = False


def branch_key(prefix, service_context=None):
    return canonical(
        {"prefix": normalized_prefix(prefix), "service_context": service_context}
    )


class BrowserProgress:
    def __init__(self, store, run_id, product_id, epoch, scope, known_ids):
        self.store, self.run_id, self.product_id = store, run_id, product_id
        self.epoch, self.scope = epoch, scope
        self.durable_ids = set(known_ids)
        self.completed = {}
        self.frames = []
        self.skipped_ids = set()

    async def load(self):
        records = await asyncio.to_thread(
            self.store.browser_branches, self.run_id, self.product_id, self.scope
        )
        for record in records:
            ids = set(
                json.loads(
                    gzip.decompress(
                        await asyncio.to_thread(
                            self.store.get_object, record["rows_object"]
                        )
                    )
                )
            )
            if len(ids) != record["row_count"] or not ids <= self.durable_ids:
                raise ExtractionError(
                    "Browser checkpoint refers to missing durable rows; resume was stopped",
                    "checkpoint_inconsistent",
                )
            self.completed[
                branch_key(record["prefix"], record["details"].get("service_context"))
            ] = dict(record, row_ids=ids)
        return self

    def contains(self, prefix, service_context=None):
        return branch_key(prefix, service_context) in self.completed

    def begin(self, prefix, service_context=None):
        frame = BranchFrame(normalized_prefix(prefix), service_context=service_context)
        self.frames.append(frame)
        return frame

    def end(self, frame):
        if frame.abandoned:
            return
        if not self.frames or self.frames[-1] is not frame:
            raise RuntimeError("Browser checkpoint frames must close in order")
        self.frames.pop()

    def abandon(self, frame):
        """Unwind an interrupted subtree even if parent finalizes first.

        Async-generator finalizers need not run in nesting order. Descendants
        cannot complete once their ancestor is abandoned, and their later
        finalizers may safely call end without weakening normal LIFO checks.
        """
        if frame.abandoned:
            return
        index = next((i for i, active in enumerate(self.frames) if active is frame), None)
        if index is None:
            raise RuntimeError("Cannot abandon an inactive browser checkpoint frame")
        for active in self.frames[index:]:
            active.abandoned = True
        del self.frames[index:]

    def acknowledge(self, row_id, row):
        """Engine calls only after row commit, or a matching durable duplicate."""
        self.durable_ids.add(row_id)
        keys = set(row.get("configuration") or {}) | {"quantity"}
        if any(k in (row.get("production") or {}) for k in ("value", "mode", "id")):
            keys.add("productionTime")
        for frame in self.frames:
            frame.row_ids.add(row_id)
            frame.control_keys.update(keys)
            if frame.service_context is not None and (
                (row.get("pricing_context") or {}).get("service_context")
                != frame.service_context
            ):
                frame.context_changed = True

    def skip(self, prefix, service_context=None):
        if service_context is not None and not service_context_is_bound(
            service_context
        ):
            return None
        record = self.completed.get(branch_key(prefix, service_context))
        if record is None:
            return None
        self.skipped_ids.update(record["row_ids"])
        for frame in self.frames:
            frame.row_ids.update(record["row_ids"])
            frame.control_keys.update(record["details"].get("control_keys", []))
            if (
                frame.service_context is not None
                and service_context != frame.service_context
            ):
                frame.context_changed = True
        return record

    async def complete(self, frame, retained_notes):
        if frame.abandoned:
            return
        if frame.context_changed or (
            frame.service_context is not None
            and not service_context_is_bound(frame.service_context)
        ):
            return
        descendants = [
            item
            for item in self.completed.values()
            if item["details"].get("service_context") == frame.service_context
            and all(item["prefix"].get(k) == v for k, v in frame.prefix.items())
        ]
        if any(not item["row_ids"] <= frame.row_ids for item in descendants):
            raise ExtractionError(
                "Previously completed source branches disappeared during resume. Start a new refresh run.",
                "snapshot_changed",
            )
        if not frame.row_ids or not frame.prefix:
            # Root is checked but never persisted: each resume must rediscover
            # the current top-level domain before skipping its completed children.
            return
        if not frame.row_ids <= self.durable_ids:
            raise RuntimeError("Cannot checkpoint uncommitted browser rows")
        details = {
            "control_keys": sorted(frame.control_keys),
            "coverage_reasons": sorted(retained_notes),
            "source_revalidated_on_resume": False,
            "coverage_basis": "previously_exhausted_supported_visible_finite_branch",
            "service_context": frame.service_context,
        }
        await asyncio.to_thread(
            self.store.complete_browser_branch,
            self.run_id,
            self.product_id,
            self.epoch,
            self.scope,
            frame.prefix,
            frame.row_ids,
            details,
        )
        for key, item in list(self.completed.items()):
            if item["details"].get("service_context") == frame.service_context and all(
                item["prefix"].get(k) == v for k, v in frame.prefix.items()
            ):
                del self.completed[key]
        self.completed[branch_key(frame.prefix, frame.service_context)] = {
            "prefix": frame.prefix,
            "row_count": len(frame.row_ids),
            "row_ids": set(frame.row_ids),
            "details": details,
        }
