"""Compact observed worker timing; never infer active time from creation dates."""
from copy import deepcopy
import math

STATES = {"active", "queued", "waiting", "paused", "attention", "finished", "unknown"}
COUNTERS = ("active", "queued", "waiting", "paused", "attention")


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def new_record(state, wall, mono, session, *, created_at=None, complete=False, waiting_until=None):
    return {"schema": 1, "state": state, "seconds": {key: 0.0 for key in COUNTERS},
        "created_at": created_at, "observed_since": wall, "last_wall": wall, "last_mono": mono,
        "session": session, "state_since": wall, "complete_history": complete, "clock_anomaly": False,
        "waiting_until": waiting_until, "finished_at": wall if state == "finished" else None}


def valid(record):
    return (isinstance(record, dict) and type(record.get("schema")) is int and record.get("schema") == 1
        and isinstance(record.get("state"), str) and record.get("state") in STATES
        and all(number(record.get(key)) for key in ("observed_since", "last_wall", "last_mono", "state_since"))
        and isinstance(record.get("seconds"), dict)
        and all(number(record["seconds"].get(key)) and record["seconds"][key] >= 0 for key in COUNTERS))


def advance(record, state, wall, mono, session, *, created_at=None, waiting_until=None):
    if state not in STATES or not number(wall) or not number(mono):
        raise ValueError("Invalid timing observation")
    if not valid(record):
        return new_record(state, wall, mono, session, created_at=created_at, waiting_until=waiting_until)
    result = deepcopy(record)
    wall_delta = wall - result["last_wall"]
    same_session = result.get("session") == session
    mono_delta = mono - result["last_mono"]
    if same_session and mono_delta >= 0:
        elapsed = mono_delta
        if abs(wall_delta - mono_delta) > 5:
            result["clock_anomaly"] = True
    elif result["state"] == "active":
        # A restarted worker cannot certify its predecessor's uncheckpointed
        # tail. In particular, downtime is never added to active collection.
        elapsed = 0
        result["complete_history"] = False
    else:
        elapsed = max(0, wall_delta)
    if wall_delta < 0 or (same_session and mono_delta < 0):
        result["clock_anomaly"] = True
        result["complete_history"] = False
        if same_session and mono_delta < 0: elapsed = 0
    previous = result["state"]
    if previous in COUNTERS:
        if previous == "waiting" and number(result.get("waiting_until")):
            waiting = min(elapsed, max(0, result["waiting_until"] - result["last_wall"]))
            result["seconds"]["waiting"] += waiting
            result["seconds"]["queued"] += elapsed - waiting
        else:
            result["seconds"][previous] += elapsed
    if state != previous or (state == "active" and not same_session):
        result["state_since"] = wall
    result.update(state=state, last_wall=wall, last_mono=mono, session=session,
        waiting_until=waiting_until,
        finished_at=(result.get("finished_at") or wall) if state == "finished" else None)
    if state == "unknown": result["complete_history"] = False
    return result


def product_state(product, run, wall):
    if product.status in {"completed", "cancelled"}: return "finished"
    if run is not None and run.status == "paused": return "paused"
    if product.status == "running": return "active"
    if product.status == "queued": return "waiting" if (product.lease_until or 0) > wall else "queued"
    if product.status == "paused": return "paused"
    if product.status in {"partial", "needs_attention", "access_required", "failed"}: return "attention"
    return "unknown"


def job_state(job, enabled, wall, *, owned=False):
    if owned: return "active"
    if job["status"] == "completed": return "finished"
    if not enabled: return "paused"
    if job["status"] in {"held", "access_required"}: return "attention"
    if job["status"] == "waiting": return "waiting"
    if job["status"] == "queued": return "waiting" if job.get("next_at", 0) > wall else "queued"
    return "unknown"


def snapshot(record, state, wall, mono, session, *, live=False, live_until=None, created_at=None, ended_at=None, scope="product_worker"):
    if not valid(record):
        ended = ended_at if state == "finished" else wall
        return {"schema_version": 1, "state": state, **{key + "_seconds": None for key in COUNTERS},
            "elapsed_seconds": max(0, ended - created_at) if number(created_at) and number(ended) else None,
            "observed_since": None, "server_now": wall, "active_since": None, "live_until": None,
            "active_now": False, "complete_history": False, "coverage": "unknown", "clock_anomaly": False,
            "scope": scope}
    current = deepcopy(record)
    terminal_unobserved = state == "finished" and current["state"] != "finished"
    active_now = bool(live and state == "active" and current["state"] == "active" and current.get("session") == session)
    known_wait_expiry = current["state"] == "waiting" and state == "queued" and number(current.get("waiting_until"))
    if (current["state"] == "active" and not active_now) or (current["state"] != state and not known_wait_expiry):
        # Out-of-band status changes have no observed transition timestamp.
        # Preserve measured totals; do not attribute the unknown gap to a state.
        current.update(last_wall=wall, last_mono=mono, complete_history=False)
    current = advance(current, state, wall, mono, session, created_at=created_at,
        waiting_until=current.get("waiting_until") if state == "waiting" else None)
    began = current.get("created_at") if number(current.get("created_at")) else created_at
    ended = None if terminal_unobserved else current.get("finished_at") if state == "finished" else wall
    complete = current.get("complete_history") is True
    return {"schema_version": 1, "state": state,
        **{key + "_seconds": round(current["seconds"][key], 3) for key in COUNTERS},
        "elapsed_seconds": max(0, ended - began) if number(began) and number(ended) else None,
        "observed_since": current["observed_since"], "server_now": wall,
        "active_since": current["state_since"] if active_now else None,
        "live_until": min(wall + 10, live_until) if active_now and number(live_until) else wall + 10 if active_now else None,
        "active_now": active_now, "complete_history": complete,
        "coverage": "complete" if complete else "partial", "clock_anomaly": bool(current.get("clock_anomaly")),
        "scope": scope}


def stable_summary(record):
    """Last durable measurement for an archive; no live/as-of fields or ledger."""
    if not valid(record): return None
    value = snapshot(record, record["state"], record["last_wall"], record["last_mono"], record.get("session"))
    keys = ("schema_version", "state", "active_seconds", "queued_seconds", "waiting_seconds", "paused_seconds",
        "attention_seconds", "elapsed_seconds", "observed_since", "complete_history", "coverage", "clock_anomaly", "scope")
    return {**{key: value[key] for key in keys}, "recorded_at": record["last_wall"]}
