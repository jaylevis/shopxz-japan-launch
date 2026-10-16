"""Per-operation live evaluation, write and read-back. Shared by apply/resume and verify.

evaluate() is read-only and returns satisfied / pending / conflict. execute() performs exactly
one documented write and validates business fields from a fresh GET before reporting success.
"""

import copy

from . import marketing as mkt
from .adapter import ApiFailure, UncertainWrite
from .configs import RESETS_OMITTED, submit_view, unknown_keys
from .gates import coupon_problems, locale_problems, mail_problems, merge_coupon, notice_readiness
from .live import list_items, paginate
from .util import LaunchError, as_int, get_path, leaf_diff, now_jst, sem_equal, set_path, sha256_text

SATISFIED, PENDING, CONFLICT = "satisfied", "pending", "conflict"


class ReadbackMismatch(LaunchError):
    pass


class UnresolvedRef(LaunchError):
    pass


class Eval:
    def __init__(self, status, result_id=None, evidence=None, live=None):
        self.status, self.result_id, self.evidence, self.live = status, result_id, evidence or {}, live


class Context:
    """adapter + plan ops + resolve(op_id) -> semantically verified resource id."""

    def __init__(self, adapter, plan, resolve, now_fn=now_jst):
        self.adapter = adapter
        self.ops = {o["op_id"]: o for o in plan["operations"]}
        self.resolve = resolve
        self.now_fn = now_fn
        self.wrote = False

    def write(self, api_id, data):
        """The single mutation entry point. A definite rejection (ApiFailure) means nothing was written;
        once the call returns, any later read failure leaves the outcome unknown."""
        result = self.adapter.call(api_id, data=data)
        self.wrote = True
        return result

    def ref(self, op_id):
        rid = self.resolve(op_id)
        if rid is None:
            raise UnresolvedRef("UNRESOLVED_REF", f"{op_id} has no verified id yet")
        return rid


def _paths(op):
    return [tuple(p) for p in op.get("changes", [])]


def _within(path, paths):
    return any(path[:len(p)] == p for p in paths)


# ------------------------------------------------------------------ config_set

def _eval_config(ctx, op):
    """Satisfied only when every contract field matches desired; pending only when every field this
    plan changes still holds its planned 'before' value and every other contract field is unchanged."""
    live = ctx.adapter.call(op["api"]["read"]) or {}
    paths = _paths(op)
    contract = [tuple(p) for p in op.get("verify", [])] or paths
    write_api = op["api"]["write"]
    extra = set(unknown_keys(live, write_api)) - set(op.get("preserved_unknown_fields", []))
    if all(sem_equal(get_path(live, p), get_path(op["desired"], p)) for p in set(contract) | set(paths)):
        return Eval(SATISFIED, live=live)
    if write_api in RESETS_OMITTED and extra:
        return Eval(CONFLICT, evidence={"undocumented_fields": sorted(extra)}, live=live)
    drift = [{"path": list(p), "live": get_path(live, p), "planned_before": get_path(op["before"], p)}
             for p in sorted(set(paths) | set(contract))
             if not sem_equal(get_path(live, p), get_path(op["before"], p))]
    if drift:
        return Eval(CONFLICT, evidence={"changed_since_plan": drift}, live=live)
    return Eval(PENDING, live=live)


def _exec_config(ctx, op, ev):
    write_api = op["api"]["write"]
    paths = _paths(op)
    base = submit_view(ev.live, write_api)
    body = copy.deepcopy(base)
    for p in paths:
        set_path(body, p, get_path(op["desired"], p))
    # fresh stores read unset array/object fields back as "", which v6 strict validation rejects
    for k, v in op["desired"].items():
        if isinstance(v, (list, dict)) and not isinstance(body.get(k), type(v)):
            body[k] = copy.deepcopy(v)
    if write_api == "fixedcart/set" and not isinstance(body.get("diy_view"), dict):
        body["diy_view"] = {}
    ctx.write(write_api, body)
    after = ctx.adapter.call(op["api"]["read"]) or {}
    wrong = [list(p) for p in paths if not sem_equal(get_path(after, p), get_path(op["desired"], p))]
    collateral = [list(p) for p in leaf_diff(base, submit_view(after, write_api)) if not _within(p, paths)]
    if wrong or collateral:
        raise ReadbackMismatch("READBACK_MISMATCH", f"{op['op_id']} read-back differs",
                               {"not_applied": wrong, "unexpected_changes": collateral})
    return None


def _undo_config(ctx, op):
    """Restore planned 'before' values on changed paths, only where live still equals what we wrote."""
    live = ctx.adapter.call(op["api"]["read"]) or {}
    paths = _paths(op)
    if not all(sem_equal(get_path(live, p), get_path(op["desired"], p)) for p in paths):
        return {"restored": False, "reason": "live value changed by someone else; left as is"}
    body = submit_view(live, op["api"]["write"])
    for p in paths:
        set_path(body, p, get_path(op["before"], p))
    ctx.write(op["api"]["write"], body)
    after = ctx.adapter.call(op["api"]["read"]) or {}
    ok = all(sem_equal(get_path(after, p), get_path(op["before"], p)) for p in paths)
    return {"restored": ok}


# ------------------------------------------------------------------ system emails

def _hashes(rec):
    return sha256_text(rec.get("subject") or ""), sha256_text(rec.get("body_html") or "")


def _email_live(ctx, op):
    t = op["target"]
    if op["kind"] == "email_lang_update":
        rows = list_items(ctx.adapter.call("notice/theme-get-languages", query={"type": t["type"], "category": t["category"]}))
        rows = [r for r in rows if as_int(r.get("id")) == as_int(t["id"])]
        return rows[0] if len(rows) == 1 else None
    data = ctx.adapter.call("notice/theme-info", query={"id": t["id"]}) or {}
    return data.get("email") if isinstance(data.get("email"), dict) else None


def _identity_ok(rec, t):
    return (rec is not None and as_int(rec.get("id")) == as_int(t["id"]) and as_int(rec.get("type")) == t["type"]
            and as_int(rec.get("category")) == t["category"] and rec.get("lang_code", "default") == t["lang_code"])


def _eval_email(ctx, op):
    rec = _email_live(ctx, op)
    if not _identity_ok(rec, op["target"]):
        return Eval(CONFLICT, evidence={"reason": "email record missing or changed identity", "live": rec}, live=rec)
    live_h = _hashes(rec)
    if live_h == (op["desired"]["subject_sha256"], op["desired"]["body_sha256"]):
        return Eval(SATISFIED, result_id=rec.get("id"), live=rec)
    if live_h == (op["before"]["subject_sha256"], op["before"]["body_sha256"]):
        return Eval(PENDING, live=rec)
    return Eval(CONFLICT, evidence={"reason": "template edited since plan", "live_subject": rec.get("subject")}, live=rec)


def _exec_email(ctx, op, ev):
    t, d = op["target"], op["desired"]
    if op["kind"] == "email_lang_update":
        ctx.write("notice/theme-save-languages", {"id": t["id"], "lang_code": "ja", "type": t["type"],
                                                               "category": t["category"], "subject": d["subject"],
                                                               "body_html": d["body_html"]})
    else:
        ctx.write("notice/theme-update", {"id": t["id"], "subject": d["subject"], "body_html": d["body_html"]})
    after = _email_live(ctx, op)
    if not _identity_ok(after, t) or _hashes(after) != (d["subject_sha256"], d["body_sha256"]) \
            or not sem_equal(after.get("status"), ev.live.get("status")):
        raise ReadbackMismatch("READBACK_MISMATCH", f"{op['op_id']} subject/body hash or status differs after save")
    return t["id"]


def _eval_email_status(ctx, op):
    rec = _email_live(ctx, dict(op, kind="email_default_update", target=dict(op["target"], lang_code="default")))
    if rec is None or as_int(rec.get("id")) != as_int(op["target"]["id"]):
        return Eval(CONFLICT, evidence={"reason": "legacy recall email missing"})
    status = as_int(rec.get("status"))
    if status == op["desired"]["status"]:
        return Eval(SATISFIED, result_id=rec.get("id"), live=rec)
    if status == op["before"]["status"]:
        return Eval(PENDING, live=rec)
    return Eval(CONFLICT, evidence={"live_status": status}, live=rec)


def _set_email_status(ctx, op, status):
    ctx.write("notice/theme-change-status", {"id": op["target"]["id"], "status": status})
    rec = _email_live(ctx, dict(op, kind="email_default_update", target=dict(op["target"], lang_code="default")))
    if rec is None or as_int(rec.get("status")) != status:
        raise ReadbackMismatch("READBACK_MISMATCH", f"{op['op_id']} status not {status} after save")
    return op["target"]["id"]


# ------------------------------------------------------------------ marketing resources

def _coupon_candidates(ctx, code):
    rows = [r for r in paginate(ctx.adapter, "coupon/list", {"code": code})
            if str(r.get("code", "")).upper() == code.upper()]
    return [merge_coupon(r, ctx.adapter.call("coupon/info", query={"id": r.get("id")})) for r in rows]


def _template_candidates(ctx, name):
    rows = [r for r in paginate(ctx.adapter, "emailrecall/message-template-list") if r.get("name") == name]
    return [ctx.adapter.call("emailrecall/message-template-info", query={"template_id": r.get("id")}) or {} for r in rows]


def _activity_candidates(ctx, name):
    rows = [r for r in paginate(ctx.adapter, "emailrecall/activities-list") if r.get("name") == name]
    return [ctx.adapter.call("emailrecall/activities-info", query={"id": r.get("id")}) or {} for r in rows]


def _message_rows(ctx, activity_id):
    return list_items(ctx.adapter.call("emailrecall/activities-message-list", query={"activities_id": activity_id}))


def _resolved_message(ctx, op):
    body = dict(op["desired"])
    body["activities_id"] = ctx.ref(body["activities_id"]["$ref"])
    body["template_id"] = ctx.ref(body["template_id"]["$ref"])
    return body


RESOURCES = {
    "coupon": {"diff": mkt.coupon_differences, "key": lambda op: op["desired"]["code"],
               "candidates": _coupon_candidates,
               "info": lambda ctx, rid: ctx.adapter.call("coupon/info", query={"id": rid}) or {}},
    "template": {"diff": mkt.template_differences, "key": lambda op: op["desired"]["name"],
                 "candidates": _template_candidates,
                 "info": lambda ctx, rid: ctx.adapter.call("emailrecall/message-template-info", query={"template_id": rid}) or {}},
    "activity": {"diff": mkt.activity_differences, "key": lambda op: op["desired"]["name"],
                 "candidates": _activity_candidates,
                 "info": lambda ctx, rid: ctx.adapter.call("emailrecall/activities-info", query={"id": rid}) or {}},
}


def _desired_for(ctx, op):
    return _resolved_message(ctx, op) if op["kind"] == "message" else op["desired"]


def _candidates(ctx, op, desired):
    if op["kind"] == "message":
        return [r for r in _message_rows(ctx, desired["activities_id"]) if r.get("name") == desired["name"]]
    spec = RESOURCES[op["kind"]]
    return spec["candidates"](ctx, spec["key"](op))


def _diff(kind, live, desired):
    return mkt.message_differences(live, desired) if kind == "message" else RESOURCES[kind]["diff"](live, desired)


def _eval_resource(ctx, op):
    desired = _desired_for(ctx, op)
    found = _candidates(ctx, op, desired)
    if op.get("mode") == "adopt":
        found = [f for f in found if as_int(f.get("id")) == as_int(op["target"]["id"])]
        if len(found) != 1:
            return Eval(CONFLICT, evidence={"reason": "adopted resource no longer present", "id": op["target"]["id"]})
    exact = [f for f in found if not _diff(op["kind"], f, desired)]
    if not found:
        return Eval(PENDING)
    if len(found) == 1 and exact:
        return Eval(SATISFIED, result_id=as_int(exact[0].get("id")), live=exact[0])
    return Eval(CONFLICT, evidence={"reason": "ambiguous or non-matching existing resource",
                                    "candidates": [f.get("id") for f in found],
                                    "differences": [_diff(op["kind"], f, desired) for f in found]})


_WRITE_BODY = {
    "coupon": lambda d: dict(d),
    "template": lambda d: dict(d, id=0),
    "activity": lambda d: dict(d),
    "message": lambda d: dict(d),
}


def _exec_resource(ctx, op, ev):
    desired = _desired_for(ctx, op)
    resp = ctx.write(op["api"]["write"], _WRITE_BODY[op["kind"]](desired))
    new_id = as_int((resp or {}).get("id")) if isinstance(resp, dict) else None
    if new_id is None:
        again = _eval_resource(ctx, op)
        if again.status != SATISFIED:
            raise ReadbackMismatch("READBACK_MISMATCH", f"{op['op_id']}: created id unknown and no exact match")
        return again.result_id
    if op["kind"] == "message":
        live = ctx.adapter.call("emailrecall/activities-message-info", query={"message_id": new_id}) or {}
    else:
        live = RESOURCES[op["kind"]]["info"](ctx, new_id)
    diffs = _diff(op["kind"], live, desired)
    if op["kind"] == "activity" and as_int(live.get("status")) != 2:
        diffs.append({"field": "status", "live": live.get("status"), "desired": 2})
    if diffs:
        raise ReadbackMismatch("READBACK_MISMATCH", f"{op['op_id']} created resource differs", {"id": new_id, "differences": diffs})
    return new_id


# ------------------------------------------------------------------ v1.3 in-place migration

def _eval_coupon_ref(ctx, op):
    """An existing store coupon used by a recall message: must still exist exactly once and be usable."""
    rows = [c for c in _coupon_candidates(ctx, op["desired"]["code"]) if as_int(c.get("id")) == as_int(op["target"]["id"])]
    if len(rows) != 1:
        return Eval(CONFLICT, evidence={"reason": "mapped coupon no longer present", "code": op["desired"]["code"]})
    unusable = coupon_problems(rows[0], ctx.now_fn())
    if unusable:
        return Eval(CONFLICT, evidence={"reason": "mapped coupon not usable now", "problems": unusable})
    return Eval(SATISFIED, result_id=as_int(rows[0].get("id")), live=rows[0])


def _activity_info(ctx, aid):
    return ctx.adapter.call("emailrecall/activities-info", query={"id": aid}) or {}


def _eval_activity_update(ctx, op):
    live = _activity_info(ctx, op["target"]["id"])
    if as_int(live.get("id")) != as_int(op["target"]["id"]):
        return Eval(CONFLICT, evidence={"reason": "adopted activity no longer present"}, live=live)
    fields = list(op["desired"])
    if all(sem_equal(live.get(f), op["desired"][f]) for f in fields):
        return Eval(SATISFIED, result_id=as_int(live.get("id")), live=live)
    drift = [{"field": f, "live": live.get(f), "planned_before": op["before"].get(f)} for f in fields
             if not sem_equal(live.get(f), op["before"].get(f)) and not sem_equal(live.get(f), op["desired"][f])]
    if drift:
        return Eval(CONFLICT, evidence={"changed_since_plan": drift}, live=live)
    return Eval(PENDING, live=live)


def _exec_activity_update(ctx, op, ev):
    info = ev.live
    body = _activity_body(info, as_int(info.get("status")))
    body.update(op["desired"])
    ctx.write("emailrecall/activities-save", body)
    after = _activity_info(ctx, info.get("id"))
    wrong = [f for f in op["desired"] if not sem_equal(after.get(f), op["desired"][f])]
    expected = dict(info, **op["desired"])
    collateral = [d for d in mkt.activity_differences(after, expected, include_status=True) if d["field"] not in op["desired"]]
    if wrong or collateral:
        raise ReadbackMismatch("READBACK_MISMATCH", f"{op['op_id']} read-back differs",
                               {"not_applied": wrong, "unexpected_changes": collateral})
    return as_int(info.get("id"))


def _undo_activity_update(ctx, op):
    live = _activity_info(ctx, op["target"]["id"])
    if not all(sem_equal(live.get(f), op["desired"][f]) for f in op["desired"]):
        return {"restored": False, "reason": "live value changed by someone else; left as is"}
    body = _activity_body(live, as_int(live.get("status")))
    body.update(op["before"])
    ctx.write("emailrecall/activities-save", body)
    return {"restored": True}


def _message_info(ctx, mid):
    return ctx.adapter.call("emailrecall/activities-message-info", query={"message_id": mid}) or {}


def _desired_update_message(ctx, op):
    body = dict(op["desired"])
    body["template_id"] = ctx.ref(body["template_id"]["$ref"])
    return body


def _eval_message_update(ctx, op):
    live = _message_info(ctx, op["target"]["id"])
    if as_int(live.get("id")) != as_int(op["target"]["id"]) or \
            as_int(live.get("activities_id")) != as_int(op["desired"]["activities_id"]):
        return Eval(CONFLICT, evidence={"reason": "message no longer present in the adopted activity"}, live=live)
    desired = _desired_update_message(ctx, op)
    if not mkt.message_differences(live, desired):
        return Eval(SATISFIED, result_id=as_int(live.get("id")), live=live)
    if mkt.message_differences(live, op["before"]):
        return Eval(CONFLICT, evidence={"changed_since_plan": mkt.message_differences(live, op["before"])}, live=live)
    return Eval(PENDING, live=live)


def _exec_message_update(ctx, op, ev):
    desired = _desired_update_message(ctx, op)
    ctx.write("emailrecall/activities-message-save", dict(desired, id=as_int(op["target"]["id"])))
    after = _message_info(ctx, op["target"]["id"])
    diffs = mkt.message_differences(after, desired)
    if diffs:
        raise ReadbackMismatch("READBACK_MISMATCH", f"{op['op_id']} read-back differs", {"differences": diffs})
    return as_int(op["target"]["id"])


def _undo_message_update(ctx, op):
    live = _message_info(ctx, op["target"]["id"])
    if mkt.message_differences(live, _desired_update_message(ctx, op)):
        return {"restored": False, "reason": "message changed by someone else; left as is"}
    ctx.write("emailrecall/activities-message-save", dict(op["before"], id=as_int(op["target"]["id"])))
    return {"restored": True}


# ------------------------------------------------------------------ graph and activation

def _eval_graph(ctx, op):
    problems = []
    for member in op["members"]:
        mop = ctx.ops[member]
        ev = evaluate(ctx, mop)
        expected = ctx.resolve(member)
        if ev.status != SATISFIED or (expected is not None and ev.result_id != expected):
            problems.append({"op": member, "status": ev.status, "evidence": ev.evidence})
    if not problems:
        activity_ref = op.get("activity_ref", "marketing.activity")
        activity_id = ctx.ref(activity_ref)
        msg_ids = {as_int(ctx.resolve(m)) for m in op["members"] if ctx.ops[m]["kind"] in ("message", "message_update")}
        live_ids = {as_int(r.get("id")) for r in _message_rows(ctx, activity_id)}
        if live_ids != msg_ids:
            problems.append({"op": activity_ref, "reason": "activity messages differ from plan",
                             "live": sorted(live_ids), "expected": sorted(msg_ids)})
        for member in op["members"]:
            mop = ctx.ops[member]
            if mop["kind"] not in ("coupon", "coupon_ref"):
                continue
            rows = [c for c in _coupon_candidates(ctx, mop["desired"]["code"])
                    if as_int(c.get("id")) == as_int(ctx.resolve(member))]
            unusable = coupon_problems(rows[0], ctx.now_fn()) if len(rows) == 1 else ["coupon not found"]
            if unusable:
                problems.append({"coupon": mop["desired"]["code"], "reason": "coupon not usable now",
                                 "problems": unusable})
    return Eval(SATISFIED) if not problems else Eval(CONFLICT, evidence={"graph": problems})


def _activity_body(info, status):
    body = {k: copy.deepcopy(info.get(k)) for k in mkt.ACTIVITY_SUBMIT_FIELDS if k in info}
    for f in mkt.ACTIVITY_FIELDS:
        if f in mkt._ZERO_EMPTY:
            v = mkt._zero_empty(info.get(f))
            body[f] = mkt.unlimited_value(f) if v == "" else v
    if not isinstance(body.get("c_order_product_ids"), list):
        body["c_order_product_ids"] = []
    body["status"] = status
    return body


def _eval_enable(ctx, op):
    info = ctx.adapter.call("emailrecall/activities-info", query={"id": ctx.ref(op["target"]["ref"])}) or {}
    status = as_int(info.get("status"))
    if status == 1:
        return Eval(SATISFIED, result_id=as_int(info.get("id")), live=info)
    if status == 2:
        return Eval(PENDING, live=info)
    return Eval(CONFLICT, evidence={"status": info.get("status")}, live=info)


def _set_activity_status(ctx, info, status):
    ctx.write("emailrecall/activities-save", _activity_body(info, status))
    after = ctx.adapter.call("emailrecall/activities-info", query={"id": info.get("id")}) or {}
    if as_int(after.get("status")) != status or mkt.activity_differences(after, info):
        raise ReadbackMismatch("READBACK_MISMATCH", f"activity {info.get('id')} status {status} not confirmed")
    return as_int(info.get("id"))


def _exec_enable(ctx, op, ev):
    graph = _eval_graph(ctx, ctx.ops[op.get("graph", "marketing.graph")])  # re-verify immediately before enabling
    if graph.status != SATISFIED:
        raise ReadbackMismatch("GRAPH_INCOMPLETE", "recall graph no longer verified; not enabling", graph.evidence)
    return _set_activity_status(ctx, ev.live, 1)


def _undo_enable(ctx, op):
    info = ctx.adapter.call("emailrecall/activities-info", query={"id": ctx.ref(op["target"]["ref"])}) or {}
    if as_int(info.get("status")) != 1:
        return {"restored": False, "reason": "activity no longer enabled"}
    _set_activity_status(ctx, info, 2)
    return {"restored": True}


def _undo_email_status(ctx, op):
    ev = _eval_email_status(ctx, op)
    if ev.status != SATISFIED:
        return {"restored": False, "reason": "status changed by someone else"}
    _set_email_status(ctx, op, op["before"]["status"])
    return {"restored": True}


# ------------------------------------------------------------------ payment (activation/order only)

def _payment_rows(ctx):
    return {r.get("code"): r for r in list_items(ctx.adapter.call("payment/list")) if isinstance(r, dict)}


def _statuses(rows):
    return {code: as_int(r.get("status")) for code, r in rows.items()}


def _eval_payment_status(ctx, op):
    rows = _payment_rows(ctx)
    row = rows.get(op["target"]["code"])
    if row is None:
        return Eval(CONFLICT, evidence={"reason": "payment method no longer listed"}, live=rows)
    status = as_int(row.get("status"))
    if status == op["desired"]["status"]:
        return Eval(SATISFIED, live=rows)
    if status == op["before"]["status"]:
        return Eval(PENDING, live=rows)
    return Eval(CONFLICT, evidence={"live_status": status}, live=rows)


def _exec_payment_status(ctx, op, ev):
    code = op["target"]["code"]
    ctx.write("payment/update-status", {"payment_method": code, "status": 1})
    after = _payment_rows(ctx)
    expected = dict(_statuses(ev.live), **{code: 1})
    if _statuses(after) != expected:
        raise ReadbackMismatch("READBACK_MISMATCH", f"{op['op_id']}: payment statuses differ after activation",
                               {"expected": expected, "live": _statuses(after)})
    return None


def _eval_payment_top(ctx, op):
    rows = _payment_rows(ctx)
    row = rows.get(op["target"]["code"])
    if row is None or as_int(row.get("id")) != as_int(op["target"]["payment_id"]):
        return Eval(CONFLICT, evidence={"reason": "preferred payment method missing or id changed"}, live=rows)
    if as_int(row.get("status")) != 1:
        return Eval(CONFLICT, evidence={"reason": "preferred payment method is not active"}, live=rows)
    if as_int(row.get("position")) == 1:
        return Eval(SATISFIED, live=rows)
    if not sem_equal(row.get("position"), op["before"].get("position")):
        # merchant reordered since the plan: preserve their ordering decision
        return Eval(CONFLICT, evidence={"changed_since_plan": {"position": row.get("position"),
                                                               "planned_before": op["before"].get("position")}},
                    live=rows)
    return Eval(PENDING, live=rows)


def _exec_payment_top(ctx, op, ev):
    ctx.write("payment/set-top", {"payment_id": op["target"]["payment_id"]})
    after = _payment_rows(ctx)
    row = after.get(op["target"]["code"]) or {}
    if as_int(row.get("position")) != 1 or _statuses(after) != _statuses(ev.live):
        raise ReadbackMismatch("READBACK_MISMATCH", f"{op['op_id']}: preferred payment not first or statuses changed")
    return None


HANDLERS = {
    "payment_status": (_eval_payment_status, _exec_payment_status, None),
    "payment_top": (_eval_payment_top, _exec_payment_top, None),
    "config_set": (_eval_config, _exec_config, _undo_config),
    "email_default_update": (_eval_email, _exec_email, None),
    "email_lang_update": (_eval_email, _exec_email, None),
    "email_status": (_eval_email_status, lambda ctx, op, ev: _set_email_status(ctx, op, op["desired"]["status"]),
                     _undo_email_status),
    "coupon": (_eval_resource, _exec_resource, None),
    "template": (_eval_resource, _exec_resource, None),
    "activity": (_eval_resource, _exec_resource, None),
    "message": (_eval_resource, _exec_resource, None),
    "coupon_ref": (_eval_coupon_ref, None, None),
    "activity_update": (_eval_activity_update, _exec_activity_update, _undo_activity_update),
    "message_update": (_eval_message_update, _exec_message_update, _undo_message_update),
    "graph_verify": (_eval_graph, None, None),
    "activity_enable": (_eval_enable, _exec_enable, _undo_enable),
}


def evaluate(ctx, op):
    return HANDLERS[op["kind"]][0](ctx, op)


def execute(ctx, op, ev):
    """Run one write. A read failure after the write was sent is an unknown outcome, never a plain failure."""
    fn = HANDLERS[op["kind"]][1]
    if fn is None:
        raise LaunchError("NOT_WRITABLE", f"{op['kind']} has no write step")
    ctx.wrote = False
    try:
        return fn(ctx, op, ev)
    except ApiFailure as exc:
        if ctx.wrote:
            raise UncertainWrite("READBACK_FAILED", f"{op['op_id']}: write sent but read-back failed: {exc.message}")
        raise


class GateFailed(LaunchError):
    pass


def live_gate(ctx, op):
    """Fresh business preconditions before a marketing write (locale) or recall cut-over (also email)."""
    if op.get("module") != "marketing":
        return
    problems = locale_problems(ctx.adapter.call("system-settings/basic_get"))
    if op.get("pending_timeout_min_seconds"):
        checkout = ctx.adapter.call("system-settings/checkout_get") or {}
        seconds = as_int(checkout.get("pending_order_cancel_datetime"))
        if seconds is None or 0 < seconds < op["pending_timeout_min_seconds"]:
            problems.append(f"unpaid orders are cancelled after {seconds}s, before the last recall message "
                            f"({op['pending_timeout_min_seconds']}s)")
    if op.get("kind") == "message":
        # Preparation must stay inactive. A merchant may have enabled the partially
        # prepared activity while the previous run was interrupted; preserve that
        # decision and stop instead of appending messages to a live queue.
        desired = _resolved_message(ctx, op)
        activity = ctx.adapter.call("emailrecall/activities-info", query={"id": desired["activities_id"]}) or {}
        if as_int(activity.get("status")) != 2:
            problems.append("recall activity is not disabled; cannot append messages to an enabled or unknown queue")
    if op.get("activation"):
        problems += mail_problems(notice_readiness(ctx.adapter.call("notice/email-config-get")))
    if problems:
        raise GateFailed("MARKETING_GATE", f"{op['op_id']}: live preconditions not met", {"problems": problems})


def undo(ctx, op):
    fn = HANDLERS[op["kind"]][2]
    return fn(ctx, op) if fn else {"restored": False, "reason": "no undo for this kind"}
