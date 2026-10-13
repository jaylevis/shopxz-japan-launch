"""Journaled write engine for apply / resume.

state.json (atomic) records per-op fingerprint, status, ids and evidence. Unknown write outcomes
mark the op 'uncertain' and stop; resume reconciles by exact semantic match and never re-creates.
"""

import uuid

from .adapter import Adapter, ApiFailure, FatalApiError, UncertainWrite, is_write
from .handlers import (CONFLICT, PENDING, SATISFIED, Context, GateFailed, ReadbackMismatch, UnresolvedRef, evaluate,
                       execute, live_gate, undo)
from .live import require_identity
from .planner import PLAN_SCHEMA, plan_fingerprint
from .util import LaunchError, iso, now_jst, read_json, sha256_obj, write_json_atomic, write_json_immutable

STATE_SCHEMA = "SHOPXZ_JP_STATE_v1"
DONE = ("completed", "satisfied")
UNSETTLED = ("started", "uncertain")
MAX_ATTEMPTS = 2


def check_plan(plan, need_runnable=True):
    if plan.get("schema") != PLAN_SCHEMA:
        raise LaunchError("PLAN_INVALID", f"not a {PLAN_SCHEMA} plan")
    if plan.get("fingerprint") != plan_fingerprint(plan):
        raise LaunchError("PLAN_MODIFIED", "plan fingerprint does not match its content; re-plan instead of editing")
    if need_runnable and not plan.get("runnable"):
        raise LaunchError("PLAN_NOT_RUNNABLE", "plan has global blockers", {"blockers": plan.get("blockers")})


def op_fingerprint(op):
    return sha256_obj(op)


class IdentityGuard(Adapter):
    """Re-checks the bound store (via the unwrapped adapter) immediately before every write."""

    def __init__(self, inner, target):
        self.inner, self.target = inner, target

    def call(self, api_id, query=None, data=None):
        if is_write(api_id):
            require_identity(self.inner, self.target)
        return self.inner.call(api_id, query=query, data=data)


class Engine:
    def __init__(self, plan, adapter, workdir, now_fn=now_jst):
        self.plan, self.adapter, self.workdir, self.now_fn = plan, adapter, workdir, now_fn
        self.state_path = workdir / "state.json"
        self.run_id = uuid.uuid4().hex[:12]
        self.writes = 0
        self.ops = {o["op_id"]: o for o in plan["operations"]}
        self.ctx = Context(IdentityGuard(adapter, plan["target"]), plan, self._resolve, now_fn)

    # -------------------------------------------------------------- state
    def _load_state(self):
        if self.state_path.exists():
            state = read_json(self.state_path)
            if state.get("schema") != STATE_SCHEMA:
                raise LaunchError("STATE_INVALID", "state.json schema mismatch")
            if state.get("plan_fingerprint") != self.plan["fingerprint"]:
                raise LaunchError("PLAN_CHANGED", "workdir state belongs to a different plan; use a new workdir",
                                  {"state_plan": state.get("plan_fingerprint"), "plan": self.plan["fingerprint"]})
            return state
        frozen = write_json_immutable(self.workdir / "plan-frozen.json", self.plan)
        return {"schema": STATE_SCHEMA, "plan_fingerprint": self.plan["fingerprint"], "frozen_plan": frozen.name,
                "target": self.plan["target"], "created_at": iso(self.now_fn()), "ops": {}, "runs": []}

    def _save(self):
        self.state["updated_at"] = iso(self.now_fn())
        write_json_atomic(self.state_path, self.state)

    def _entry(self, op):
        entry = self.state["ops"].setdefault(op["op_id"], {"status": "new", "attempts": 0})
        fp = op_fingerprint(op)
        if entry.get("fingerprint") not in (None, fp):
            raise LaunchError("PLAN_CHANGED", f"operation {op['op_id']} changed since journaled")
        entry["fingerprint"] = fp
        return entry

    def _mark(self, op, status, **fields):
        entry = self._entry(op)
        entry.update(fields)
        entry["status"] = status
        entry["updated_at"] = iso(self.now_fn())
        self._save()
        return entry

    def _resolve(self, op_id):
        entry = self.state["ops"].get(op_id) or {}
        return entry.get("result_id") if entry.get("status") in DONE else None

    # -------------------------------------------------------------- run
    def run(self, mode):
        check_plan(self.plan)
        self.state = self._load_state()
        self.state["runs"].append({"run_id": self.run_id, "mode": mode, "started_at": iso(self.now_fn())})
        self._save()
        outcome, reason = "completed", None
        try:
            require_identity(self.adapter, self.plan["target"])  # fresh live check before any write
            unsettled = [o for o in self.plan["operations"] if self._entry(o)["status"] in UNSETTLED]
            if unsettled and mode != "resume":
                raise LaunchError("RESUME_REQUIRED", "uncertain operations exist; run resume to reconcile",
                                  {"ops": [o["op_id"] for o in unsettled]})
            for op in unsettled:
                self._reconcile(op)
            self._loop()
        except (FatalApiError, LaunchError) as exc:
            outcome, reason = "stopped", exc.to_dict()
        except _Stop as stop:
            outcome, reason = "stopped", stop.info
        if outcome == "completed" and any(self.state["ops"].get(o["op_id"], {}).get("status") not in DONE
                                          for o in self.plan["operations"]):
            outcome = "partial"
        self.state["runs"][-1].update({"ended_at": iso(self.now_fn()), "outcome": outcome, "writes": self.writes})
        self._save()
        counts = {}
        for o in self.plan["operations"]:
            s = self.state["ops"].get(o["op_id"], {}).get("status", "new")
            counts[s] = counts.get(s, 0) + 1
        return {"ok": outcome == "completed", "outcome": outcome, "writes": self.writes, "op_status": counts,
                "stopped": reason, "state": str(self.state_path),
                "attention": {k: v for k, v in self.state["ops"].items()
                              if v.get("status") not in DONE + ("new",)}}

    def _reconcile(self, op):
        try:
            ev = evaluate(self.ctx, op)
        except (ApiFailure, UnresolvedRef) as exc:
            raise _Stop({"op": op["op_id"], "reason": "reconcile read failed", "error": exc.message})
        if ev.status == SATISFIED:
            self._mark(op, "completed", result_id=ev.result_id, reconciled=True)
            if op.get("activation"):
                drift = self._dependency_drift(op, transitive=True)
                if drift:
                    raise _Stop({"op": op["op_id"], "reason": "reconciled cut-over step but the recall graph or "
                                 "activation no longer holds; stopping without further switches",
                                 "dependency_drift": drift})
            return
        self._mark(op, "uncertain", evidence=ev.evidence,
                   error="no exact match" if ev.status == PENDING else "ambiguous or conflicting match")
        raise _Stop({"op": op["op_id"], "reason": "uncertain write could not be reconciled; manual review required",
                     "live_status": ev.status, "evidence": ev.evidence,
                     "next_step": "inspect the store, then re-inspect and re-plan in a new workdir"})

    def _loop(self):
        for op in self.plan["operations"]:
            entry = self._entry(op)
            if entry["status"] in DONE:
                continue
            if entry["status"] == "failed" and entry.get("attempts", 0) >= MAX_ATTEMPTS:
                continue
            blocked = [d for d in op.get("depends_on", []) if self.state["ops"].get(d, {}).get("status") not in DONE]
            if op.get("activation") and self._activation_exhausted():
                self._mark(op, "blocked", blocked_by=["activation failed repeatedly; agent review required"])
                continue
            if blocked:
                self._mark(op, "blocked", blocked_by=blocked)
                continue
            try:
                ev = evaluate(self.ctx, op)
            except (ApiFailure, UnresolvedRef) as exc:
                self._mark(op, "unknown", error=exc.message)
                raise _Stop({"op": op["op_id"], "reason": "live read failed", "error": exc.message})
            if ev.status == SATISFIED:
                self._mark(op, "satisfied", result_id=ev.result_id, evidence={})
                continue
            if ev.status == CONFLICT:
                self._mark(op, "conflict", evidence=ev.evidence)
                if op.get("activation"):
                    self._compensate(op)
                continue
            drift = self._dependency_drift(op, transitive=bool(op.get("activation")))
            if drift:
                # Merchant/other drift since our earlier steps: preserve it and stop this branch.
                self._mark(op, "blocked", blocked_by=["dependency drift"], evidence={"dependency_drift": drift})
                continue
            try:
                live_gate(self.ctx, op)
            except GateFailed as exc:
                self._mark(op, "blocked", blocked_by=["live precondition"], evidence=exc.details)
                if op.get("activation"):
                    self._compensate(op)
                continue
            self._write(op, ev)

    def _closure(self, op, transitive):
        seen, todo = [], list(op.get("depends_on", []))
        while todo:
            dep = todo.pop(0)
            if dep in seen or dep not in self.ops:
                continue
            seen.append(dep)
            if transitive:
                todo += self.ops[dep].get("depends_on", [])
        return seen

    def _dependency_drift(self, op, transitive):
        """Re-evaluate completed dependencies live; journaled success is not proof the state still holds."""
        drift = []
        for dep in self._closure(op, transitive):
            try:
                ev = evaluate(self.ctx, self.ops[dep])
            except (ApiFailure, UnresolvedRef) as exc:
                drift.append({"op": dep, "status": "unknown", "error": exc.message})
                continue
            expected = self._resolve(dep)
            if ev.status != SATISFIED or (expected is not None and ev.result_id not in (None, expected)):
                drift.append({"op": dep, "status": ev.status, "evidence": ev.evidence})
        return drift

    def _activation_exhausted(self):
        return any(o.get("activation") and self.state["ops"].get(o["op_id"], {}).get("status") == "failed"
                   and self.state["ops"][o["op_id"]].get("attempts", 0) >= MAX_ATTEMPTS
                   for o in self.plan["operations"])

    def _write(self, op, ev):
        entry = self._entry(op)
        self._mark(op, "started", attempts=entry.get("attempts", 0) + 1, run_id=self.run_id)
        try:
            result_id = execute(self.ctx, op, ev)
        except UncertainWrite as exc:
            self._mark(op, "uncertain", error=exc.message)
            raise _Stop({"op": op["op_id"], "reason": "write outcome unknown; run resume to reconcile",
                         "error": exc.message})
        except ReadbackMismatch as exc:
            # The write was sent but its effect is unconfirmed: never retry blindly; resume must reconcile.
            self.writes += 1
            self._mark(op, "uncertain", error=exc.message, evidence=exc.details, wrote=True)
            if op.get("activation"):
                self._compensate(op)
            raise _Stop({"op": op["op_id"], "reason": "read-back did not confirm the write; run resume to reconcile",
                         "error": exc.message})
        except ApiFailure as exc:
            self._mark(op, "failed", error=exc.message)
            if op.get("activation"):
                self._compensate(op)
            return
        except FatalApiError as exc:
            self._mark(op, "failed", error=exc.message)
            raise
        self.writes += 1
        self._mark(op, "completed", result_id=result_id, wrote=True, run_id=self.run_id, error=None)

    def _compensate(self, failed_op):
        """Undo activation toggles written by this run only, newest first; never others' edits."""
        done = [o for o in self.plan["operations"] if o.get("activation") and o["op_id"] != failed_op["op_id"]
                and self.state["ops"].get(o["op_id"], {}).get("wrote")
                and self.state["ops"][o["op_id"]].get("run_id") == self.run_id
                and self.state["ops"][o["op_id"]].get("status") == "completed"]
        results = []
        for op in reversed(done):
            try:
                res = undo(self.ctx, op)
            except (ApiFailure, UncertainWrite, ReadbackMismatch) as exc:
                res = {"restored": False, "error": exc.message}
            self.state["ops"][op["op_id"]]["compensation"] = res
            if res.get("restored"):
                self.state["ops"][op["op_id"]]["status"] = "rolled_back"
                self.writes += 1
            results.append({"op": op["op_id"], **res})
            self._save()
        self.state["ops"][failed_op["op_id"]]["compensations"] = results
        self._save()


class _Stop(Exception):
    def __init__(self, info):
        super().__init__(info.get("reason"))
        self.info = info
