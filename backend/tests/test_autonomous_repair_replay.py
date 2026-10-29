"""Model replay and interrupted writes reuse one durable repair identity."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from crawler.autonomous_product_pipeline import AutonomousProductPipeline
from crawler.companion_environments import digest


CID, PID, PROPOSAL, REPAIR = "a" * 32, "9532", "b" * 32, "c" * 32


class Baseline:
    def status(self):
        return {"ready": True}
    async def ensure_current(self):
        return None


class Repairs:
    def __init__(self, *, interrupt=None, held=False):
        self.previews, self.applies, self.interrupt, self.held = 0, 0, interrupt, held
        self.record = None
    async def preview(self, *args):
        self.previews += 1
        await asyncio.sleep(0)
        if self.interrupt == "preview":
            raise asyncio.CancelledError()
        if self.held:
            return {"status": "held", "reason": "calculator_family_repair_unsupported"}
        self.record = {"id": REPAIR, "status": "preview", "manifest": {"staging_revision": "r1"}}
        return deepcopy(self.record)
    def get(self, identifier):
        assert identifier == REPAIR
        return deepcopy(self.record)
    async def apply(self, identifier, expected):
        assert identifier == REPAIR and expected == "r1"
        self.applies += 1
        if self.interrupt == "apply":
            self.record["status"] = "staging_applying"
            self.interrupt = None
            raise asyncio.CancelledError()
        self.record["status"] = "staging_verified"
        return deepcopy(self.record)


def pipeline(tmp_path, repairs):
    value = AutonomousProductPipeline(SimpleNamespace(root=tmp_path), SimpleNamespace(), None, repairs, None,
                                      baseline=Baseline())
    proposal = {"id": PROPOSAL, "product_id": PID, "eligible": True,
                "changes": [{"kind": "set_cost", "cost": "1.00"}], "evidence": {"audit_id": "audit"}}
    value.proposals_for_cycle = lambda cid: [deepcopy(proposal)]
    return value, proposal


DECISIONS = [{"proposal_id": PROPOSAL, "action": "stage_repair"}]


@pytest.mark.asyncio
async def test_replayed_or_concurrent_director_choices_create_only_one_repair(tmp_path):
    repairs = Repairs()
    value, _ = pipeline(tmp_path, repairs)
    first, second = await asyncio.gather(value.apply_decisions(CID, DECISIONS), value.apply_decisions(CID, DECISIONS))
    reopened, _ = pipeline(tmp_path, repairs)
    assert first == second == await reopened.apply_decisions(CID, DECISIONS)
    assert repairs.previews == repairs.applies == 1


@pytest.mark.asyncio
async def test_interrupted_apply_recovers_same_saved_repair_after_restart(tmp_path):
    repairs = Repairs(interrupt="apply")
    value, _ = pipeline(tmp_path, repairs)
    with pytest.raises(asyncio.CancelledError):
        await value.apply_decisions(CID, DECISIONS)
    reopened, _ = pipeline(tmp_path, repairs)
    result = await reopened.apply_decisions(CID, DECISIONS)
    assert result[0]["repair_id"] == REPAIR and result[0]["status"] == "staging_verified"
    assert repairs.previews == 1 and repairs.applies == 2


@pytest.mark.asyncio
async def test_interrupted_preview_does_not_dispatch_another_preview(tmp_path):
    repairs = Repairs(interrupt="preview")
    value, _ = pipeline(tmp_path, repairs)
    with pytest.raises(asyncio.CancelledError):
        await value.apply_decisions(CID, DECISIONS)
    reopened, _ = pipeline(tmp_path, repairs)
    result = await reopened.apply_decisions(CID, DECISIONS)
    assert result[0]["reason"] == "repair_preview_outcome_uncertain"
    assert repairs.previews == 1 and repairs.applies == 0


@pytest.mark.asyncio
async def test_unsupported_preview_is_saved_instead_of_repeated(tmp_path):
    repairs = Repairs(held=True)
    value, _ = pipeline(tmp_path, repairs)
    result = await value.apply_decisions(CID, DECISIONS)
    assert result[0]["status"] == "held"
    assert result == await value.apply_decisions(CID, DECISIONS)
    assert repairs.previews == 1


@pytest.mark.asyncio
async def test_saved_proposal_identity_cannot_be_reused_for_changed_edits(tmp_path):
    repairs = Repairs()
    value, proposal = pipeline(tmp_path, repairs)
    await value.apply_decisions(CID, DECISIONS)
    proposal["changes"][0]["cost"] = "99.00"
    with pytest.raises(ValueError, match="monthly_repair_proposal_changed"):
        await value.apply_decisions(CID, DECISIONS)
    assert repairs.previews == repairs.applies == 1
