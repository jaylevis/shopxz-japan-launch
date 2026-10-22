"""Offline planner: profile + immutable snapshot -> SHOPXZ_JP_PLAN_v1 (no Node, no network)."""

from . import marketing as mkt
from .assets import (JAPANESE, allowed_from, apply_name_order, load_recall_layouts, load_system_emails,
                     name_pairs, unsupported_variables)
from .configs import (CONFIG_APIS, CONTRACT_PATHS, RESETS_OMITTED, cfsaas_desired, fixedcart_desired, japancheckout_desired, recall_desired,
                      submit_view, unknown_keys, viewed_desired)
from .profile import load_marketing_preset, profile_hash, valid_accent, validate_profile
from .backend import plan_backend
from .gates import coupon_problems, locale_problems, mail_problems
from .tasks import build_agent_tasks, readiness_gaps
from .util import ASSETS_ROOT, sem_equal, LaunchError, as_int, iso, jst_text, leaf_diff, normalize_url, now_jst, \
    sha256_obj, sha256_text

PLAN_SCHEMA = "SHOPXZ_JP_PLAN_v1"
MODULES = ("japancheckout", "viewed", "fixedcart", "cfsaas", "emails", "marketing")
LEGACY_RECALL = (9, 3)  # notice type 9 (unfinished-order recall), category 3 (marketing)
# Platform-provided variables that liquid_variables omits and older store templates may never have used.
# order.txn_id drives the konbini/bank-transfer payment-instructions link in the payment-processing email.
PLATFORM_EMAIL_VARIABLES = {(15, 1): frozenset({"order.txn_id"})}


def plan_fingerprint(plan):
    return sha256_obj({k: v for k, v in plan.items() if k != "fingerprint"})


class _Builder:
    def __init__(self):
        self.ops, self.blockers, self.notes = [], [], []

    def block(self, scope, code, message, evidence=None):
        self.blockers.append({"scope": scope, "code": code, "message": message, "evidence": evidence or {}})

    def add(self, op):
        op.setdefault("depends_on", [])
        op.setdefault("notes", [])
        self.ops.append(op)
        return op["op_id"]


def _config_op(b, op_id, module, apis, live, desired, notes, contract=None, scope=None):
    write_api = apis["write"]
    before = submit_view(live or {}, write_api)
    changes = leaf_diff(before, desired)
    unknown = unknown_keys(live or {}, write_api)
    if unknown and write_api in RESETS_OMITTED and changes:
        b.block(scope or module, "UNDOCUMENTED_FIELDS",
                f"{write_api} resets omitted fields; live config has undocumented fields that would be lost",
                {"fields": unknown})
        return None
    contract = sorted(set(CONTRACT_PATHS.get(write_api, []) if contract is None else contract) | set(changes))
    return b.add({"op_id": op_id, "module": module, "kind": "config_set", "api": dict(apis),
                  "before": before, "desired": desired, "changes": [list(p) for p in changes],
                  "verify": [list(p) for p in contract],
                  "write_expected": bool(changes), "preserved_unknown_fields": unknown, "notes": notes})


# ------------------------------------------------------------------ emails

def _email_ops(b, profile, emails_state, assets_root):
    order = (profile.get("email") or {}).get("name_order")
    if order is None:
        b.block("emails", "EMAIL_NAME_ORDER_UNRESOLVED",
                "email.name_order must be verified against storefront fields before email changes")
        return []
    try:
        assets = load_system_emails(assets_root)
    except LaunchError as exc:
        b.block("emails", exc.code, exc.message)
        return []
    defaults = {}
    for entry in emails_state.get("templates", []):
        rec = entry.get("record")
        if rec and rec.get("lang_code", "default") == "default":
            defaults.setdefault((as_int(rec.get("type")), as_int(rec.get("category"))), []).append(entry)
    unsupported = []
    for asset in assets:
        key = (asset["type"], asset["category"])
        scope = f"emails:{key[0]}-{key[1]}"
        matches = defaults.get(key, [])
        if not matches:
            unsupported.append({"type": key[0], "category": key[1], "asset": asset["file"],
                                "reason": "email type not present in this store; not manufactured"})
            continue
        if len(matches) > 1:
            b.block(scope, "EMAIL_AMBIGUOUS", "several default records share type+category",
                    {"ids": [m["record"].get("id") for m in matches]})
            continue
        entry = matches[0]
        rec = entry["record"]
        langs = entry.get("lang_emails") or []
        subject = apply_name_order(asset["subject"], order)
        body = apply_name_order(asset["body_html"], order)
        allowed = allowed_from([rec.get("subject", ""), rec.get("body_html", "")]
                               + [l.get("subject", "") + l.get("body_html", "") for l in langs],
                               entry.get("liquid_variables"))
        allowed |= PLATFORM_EMAIL_VARIABLES.get(key, frozenset())
        bad = unsupported_variables(subject + "\n" + body, allowed)
        if bad:
            b.block(scope, "EMAIL_VARIABLES_UNSUPPORTED",
                    "asset uses variables not present in the current template or returned liquid_variables",
                    {"variables": bad, "asset": asset["file"], "note": "liquid_variables list is known to be incomplete"})
            continue
        desired = {"subject": subject, "body_html": body,
                   "subject_sha256": sha256_text(subject), "body_sha256": sha256_text(body)}
        records = [("default", rec, "email_default_update", "notice/theme-info", "notice/theme-update")]
        ja = [l for l in langs if l.get("lang_code") == "ja"]
        if len(ja) > 1:
            b.block(scope, "EMAIL_AMBIGUOUS", "several ja overrides exist", {"ids": [l.get("id") for l in ja]})
            continue
        if ja:
            records.append(("ja", ja[0], "email_lang_update", "notice/theme-get-languages", "notice/theme-save-languages"))
        for lang, r, kind, read_api, write_api in records:
            cur_subject, cur_body = r.get("subject") or "", r.get("body_html") or ""
            same = cur_subject == subject and cur_body == body
            b.add({"op_id": f"emails.{key[0]}-{key[1]}.{lang}", "module": "emails", "kind": kind,
                   "api": {"read": read_api, "write": write_api},
                   "target": {"id": r.get("id"), "type": key[0], "category": key[1], "lang_code": lang},
                   "before": {"subject": cur_subject, "subject_sha256": sha256_text(cur_subject),
                              "body_sha256": sha256_text(cur_body), "status": r.get("status")},
                   "desired": desired, "write_expected": not same, "asset": asset["file"],
                   "localized_custom": bool(not same and JAPANESE.search(cur_subject + cur_body)),
                   "changes": [] if same else [["subject"], ["body_html"]]})
    return unsupported


# ------------------------------------------------------------------ marketing

def _create(op):
    return {**op, "mode": "create", "before": None, "write_expected": True}


def _adopt(op, rid):
    """Reuse an existing resource only because every business field matched exactly."""
    return {**op, "mode": "adopt", "target": {"id": rid}, "before": {"id": rid, "exact_match": True},
            "write_expected": False}


def _find_by(items, field, value):
    return [i for i in items if isinstance(i, dict) and str(i.get(field, "")) == str(value)]


def _marketing_ops(b, profile, snapshot, now, assets_root, backend=None):
    backend = backend or {}
    state = snapshot["state"]
    mstate = state.get("marketing") or {}
    emails_state = state.get("emails") or {}
    order = (profile.get("email") or {}).get("name_order")
    preset = load_marketing_preset(profile["marketing"]["preset"], assets_root)
    try:
        layouts = load_recall_layouts(assets_root)
    except LaunchError as exc:
        b.block("marketing", exc.code, exc.message)
        return False
    if order is None and any(name_pairs(t) for t in layouts.values()):
        b.block("marketing", "EMAIL_NAME_ORDER_UNRESOLVED", "recall layouts contain name variables")
        return False
    if snapshot["availability"].get("emails") != "available" or emails_state.get("status") != "read":
        b.block("marketing", "NOTICE_UNAVAILABLE", "notice addon/state required to manage legacy recall email")
        return False
    basic = state.get("basic") or {}
    locale = locale_problems(basic.get("data") if basic.get("status") == "read" else None)
    if locale:
        evidence = {"problems": locale}
        if (backend.get("ops") or {}).get("basic") or (backend.get("ops") or {}).get("currency"):
            evidence["next_step"] = ("apply the planned backend basic/currency operations, then re-inspect and "
                                     "re-plan; the profile's ja/JPY is not treated as a live fact")
        b.block("marketing", "STORE_LOCALE_NOT_JA_JPY",
                "monetary coupons/recall need the live store default ja/JPY; change basic settings via the agent "
                "task first (never changed automatically here)", evidence)
        return False
    pending = backend.get("pending_timeout") or {}
    schedule = preset_schedule_minutes(profile, assets_root)
    if pending.get("seconds") and schedule and pending["seconds"] < schedule[-1] * 60:
        b.block("marketing", "PENDING_TIMEOUT_SHORTER_THAN_RECALL",
                "unpaid orders are cancelled before the last recall message could be sent; recall is not activated "
                "(the configured timeout is not overridden)",
                {"pending_order_cancel_datetime": pending["seconds"], "source": pending.get("source"),
                 "last_recall_delay_minutes": schedule[-1]})
        return False
    notice = state.get("notice_readiness") or {}
    mail = mail_problems(notice.get("data") if notice.get("status") == "read" else None)
    if mail:
        b.block("marketing", "EMAIL_NOT_READY",
                "recall emails cannot be relied on; marketing branch stopped (system email content can still be prepared)",
                {"problems": mail})
        return False

    conflicts, plan_ops = [], []
    mk = profile["marketing"]
    mapped = {k: v for k, v in (mk.get("coupon_codes") or {}).items() if k in mkt.referenced_coupon_keys(preset)}
    deadline = as_int(mk.get("payment_deadline_days"))
    coupons = mkt.desired_coupons(profile, preset, jst_text(now))
    details = [d for d in mstate.get("coupon_details", []) if isinstance(d, dict)]
    for c in coupons:
        same_code = [d for d in details if str(d.get("code", "")).upper() == c["code"].upper()]
        listed = [r for r in mstate.get("coupons", []) if str(r.get("code", "")).upper() == c["code"].upper()]
        op = {"op_id": f"marketing.coupon.{c['key']}", "module": "marketing", "kind": "coupon",
              "api": {"read": "coupon/info", "search": "coupon/list", "write": "coupon/create"},
              "desired": c["body"]}
        if len(listed) != len(same_code):
            conflicts.append({"resource": "coupon", "code": c["code"], "reason": "listed coupon without details"})
        elif not same_code:
            plan_ops.append(_create(op))
        elif len(same_code) == 1 and not mkt.coupon_differences(same_code[0], c["body"]):
            unusable = coupon_problems(same_code[0], now)
            if unusable:
                conflicts.append({"resource": "coupon", "code": c["code"], "existing": [same_code[0].get("id")],
                                  "reason": "matching coupon is not usable now", "problems": unusable})
            else:
                plan_ops.append(_adopt(op, same_code[0].get("id")))
                live_style = same_code[0].get("copywriting")
                if isinstance(live_style, dict) and live_style != c["body"]["copywriting"]:
                    cid = as_int(same_code[0].get("id"))
                    plan_ops.append({"op_id": f"marketing.coupon_style.{c['key']}", "module": "marketing",
                                     "kind": "coupon_style", "mode": "adopt",
                                     "api": {"read": "coupon/info", "write": "coupon/update"},
                                     "target": {"id": cid}, "before": {"id": cid, "copywriting": live_style},
                                     "desired": {"copywriting": c["body"]["copywriting"]}, "write_expected": True,
                                     "notes": ["card palette only; business terms are re-submitted unchanged"]})
        else:
            conflicts.append({"resource": "coupon", "code": c["code"], "existing": [d.get("id") for d in same_code],
                              "differences": [mkt.coupon_differences(d, c["body"]) for d in same_code],
                              "reason": "same code with different business terms; existing coupon is not altered"})
    # v1.3: messages may use existing store coupons; their business terms are kept, never edited.
    live_coupons = {}
    for key, code in mapped.items():
        same_code = [d for d in details if str(d.get("code", "")).upper() == str(code).upper()]
        if len(same_code) != 1:
            conflicts.append({"resource": "coupon", "code": code, "key": key,
                              "reason": "mapped coupon code not found exactly once",
                              "existing": [d.get("id") for d in same_code]})
            continue
        unusable = coupon_problems(same_code[0], now)
        if unusable:
            conflicts.append({"resource": "coupon", "code": code, "key": key,
                              "reason": "mapped coupon is not usable now", "problems": unusable})
            continue
        live_coupons[key] = same_code[0]
        cid = as_int(same_code[0].get("id"))
        plan_ops.append({"op_id": f"marketing.coupon.{key}", "module": "marketing", "kind": "coupon_ref",
                         "api": {"read": "coupon/info", "search": "coupon/list"}, "mode": "adopt",
                         "desired": {"code": same_code[0].get("code")}, "target": {"id": cid},
                         "before": {"id": cid, "existing_coupon": True}, "write_expected": False})

    templates = mstate.get("templates", [])
    template_ids = {}
    wanted_templates = mkt.desired_templates(profile, preset, layouts, order, pending=bool(deadline))
    allowed = allowed_from([], mstate.get("email_vars"))
    bad_vars = {t["key"]: unsupported_variables(t["body"]["email_subject"] + t["body"]["email_body_html"], allowed)
                for t in wanted_templates} if allowed else {}
    if any(bad_vars.values()):
        b.block("marketing", "RECALL_VARIABLES_UNSUPPORTED",
                "recall templates use variables not returned by emailrecall/message-email-vars",
                {k: v for k, v in bad_vars.items() if v})
        return False
    for t in wanted_templates:
        found = _find_by(templates, "name", t["body"]["name"])
        op = {"op_id": f"marketing.template.{t['key']}", "module": "marketing", "kind": "template",
              "api": {"read": "emailrecall/message-template-info", "search": "emailrecall/message-template-list",
                      "write": "emailrecall/message-template-save"}, "desired": t["body"]}
        if not found:
            plan_ops.append(_create(op))
        elif len(found) == 1 and not mkt.template_differences(found[0], t["body"]):
            template_ids[t["key"]] = found[0].get("id")
            plan_ops.append(_adopt(op, found[0].get("id")))
        else:
            conflicts.append({"resource": "template", "name": t["body"]["name"], "existing": [f.get("id") for f in found],
                              "differences": [mkt.template_differences(f, t["body"]) for f in found]})

    activities = mstate.get("activities", [])
    messages = mkt.desired_messages(profile, preset, mkt.coupon_discount_values(preset, live_coupons))
    coupon_ops = {code: f"marketing.coupon.{k}" for k, code in mkt.coupon_code_map(profile, preset).items()}
    adopt_id = as_int(mk.get("adopt_activity_id"))
    if adopt_id:
        act_status = _adopt_unpaid_activity(b, preset, activities, messages, adopt_id, coupon_ops, conflicts, plan_ops)
    else:
        act_status = _create_unpaid_activity(profile, preset, activities, messages, template_ids, coupon_ops,
                                             conflicts, plan_ops)
    own_name = mkt.desired_activity(profile, preset)["name"]
    for other in activities:
        info = other.get("activity") or {}
        if as_int(info.get("id")) == adopt_id or info.get("name") == own_name:
            continue
        if mkt.activity_overlaps_jp(info):
            conflicts.append({"resource": "activity", "id": info.get("id"), "name": info.get("name"),
                              "reason": "another enabled unpaid-order campaign can match JP/ja/JPY orders; agent "
                                        "review needed (set marketing.adopt_activity_id to migrate it in place)"})

    pending_status = None
    if deadline:
        pending_status = _pending_activity(profile, preset, activities, template_ids, conflicts, plan_ops)

    if conflicts:
        b.block("marketing", "MARKETING_CONFLICT",
                "marketing branch blocked; no coupon/template/activity/recall changes are planned", {"conflicts": conflicts})
        return False

    pending_ops = {o["op_id"] for o in plan_ops if o["op_id"] == "marketing.pending_activity"
                   or o["op_id"].startswith("marketing.pending_message.")
                   or o["op_id"] in ("marketing.template.pay1", "marketing.template.pay2")}
    unpaid_pool = {o["op_id"] for o in plan_ops} - pending_ops
    member_ids, pending_ids = [], []
    for op in plan_ops:
        if op["kind"] in ("activity", "activity_update"):
            pool = pending_ops if op["op_id"] in pending_ops else unpaid_pool
            op["depends_on"] = [o["op_id"] for o in plan_ops if o["kind"] in ("coupon", "coupon_ref", "template")
                                and o["op_id"] in pool]
        (pending_ids if op["op_id"] in pending_ops else member_ids).append(b.add(op))
    graph = b.add({"op_id": "marketing.graph", "module": "marketing", "kind": "graph_verify",
                   "activity_ref": "marketing.activity", "depends_on": member_ids, "members": member_ids,
                   "write_expected": False})
    enable = b.add({"op_id": "marketing.activity_enable", "module": "marketing", "kind": "activity_enable",
                    "api": {"read": "emailrecall/activities-info", "write": "emailrecall/activities-save"},
                    "target": {"ref": "marketing.activity"}, "graph": "marketing.graph", "depends_on": [graph],
                    "before": {"status": act_status}, "desired": {"status": 1}, "write_expected": act_status != 1,
                    "activation": True})
    if deadline:
        pgraph = b.add({"op_id": "marketing.pending_graph", "module": "marketing", "kind": "graph_verify",
                        "activity_ref": "marketing.pending_activity", "depends_on": pending_ids,
                        "members": pending_ids, "write_expected": False})
        b.add({"op_id": "marketing.pending_enable", "module": "marketing", "kind": "activity_enable",
               "api": {"read": "emailrecall/activities-info", "write": "emailrecall/activities-save"},
               "target": {"ref": "marketing.pending_activity"}, "graph": "marketing.pending_graph",
               "depends_on": [pgraph, enable], "before": {"status": pending_status}, "desired": {"status": 1},
               "write_expected": pending_status != 1, "activation": True})
    else:
        b.notes.append("marketing.payment_deadline_days not set: no payment-pending (konbini/bank transfer) "
                       "reminder activity planned")
    rc_live = (mstate.get("config") or {})
    rc = _config_op(b, "marketing.recall_config", "marketing", CONFIG_APIS["recall"], rc_live,
                    recall_desired(rc_live), ["enhanced recall on, legacy system recall off (after graph verified)"])
    b.ops[-1]["depends_on"] = [enable]
    b.ops[-1]["activation"] = True
    legacy = [e.get("record") or e.get("list") for e in emails_state.get("templates", [])]
    legacy = [r for r in legacy if r and (as_int(r.get("type")), as_int(r.get("category"))) == LEGACY_RECALL
              and r.get("lang_code", "default") == "default"]
    if len(legacy) == 1:
        rec = legacy[0]
        b.add({"op_id": "marketing.legacy_recall_off", "module": "marketing", "kind": "email_status",
               "api": {"read": "notice/theme-info", "write": "notice/theme-change-status"},
               "target": {"id": rec.get("id"), "type": 9, "category": 3}, "depends_on": [rc],
               "before": {"status": as_int(rec.get("status"))}, "desired": {"status": 2},
               "write_expected": as_int(rec.get("status")) != 2, "activation": True})
    else:
        b.notes.append(f"legacy recall email type 9 default records found: {len(legacy)}; status left unchanged")
    return True


def _message_op(op_id, body, depends_on):
    return {"op_id": op_id, "module": "marketing", "kind": "message",
            "api": {"read": "emailrecall/activities-message-info", "search": "emailrecall/activities-message-list",
                    "write": "emailrecall/activities-message-save"},
            "desired": body, "depends_on": depends_on}


def _message_deps(body, activity_ref, coupon_ops):
    deps = [activity_ref, body["template_id"]["$ref"]]
    if body.get("coupon_code") in coupon_ops:
        deps.append(coupon_ops[body["coupon_code"]])
    return deps


def _create_unpaid_activity(profile, preset, activities, messages, template_ids, coupon_ops, conflicts, plan_ops):
    """Tool-named unpaid activity: create it, or adopt it only on an exact match."""
    activity = mkt.desired_activity(profile, preset)
    ours = [a for a in activities if str((a.get("activity") or {}).get("name", "")) == activity["name"]]
    act_op = {"op_id": "marketing.activity", "module": "marketing", "kind": "activity",
              "api": {"read": "emailrecall/activities-info", "search": "emailrecall/activities-list",
                      "write": "emailrecall/activities-save"}, "desired": activity}
    existing_msgs, act_status = [], 2
    if not ours:
        plan_ops.append(_create(act_op))
    elif len(ours) == 1 and not mkt.activity_differences(ours[0]["activity"], activity):
        info = ours[0]["activity"]
        act_status = as_int(info.get("status"))
        existing_msgs = ours[0].get("messages") or []
        plan_ops.append(_adopt(act_op, info.get("id")))
    else:
        conflicts.append({"resource": "activity", "name": activity["name"],
                          "existing": [(a.get("activity") or {}).get("id") for a in ours],
                          "differences": [mkt.activity_differences(a.get("activity") or {}, activity) for a in ours]})
    msg_names = {m["body"]["name"] for m in messages}
    for extra in existing_msgs:
        if extra.get("name") not in msg_names:
            conflicts.append({"resource": "message", "id": extra.get("id"), "name": extra.get("name"),
                              "reason": "unexpected message inside the tool-named activity"})
    for m in messages:
        op = _message_op(f"marketing.message.{m['key']}", m["body"],
                         _message_deps(m["body"], "marketing.activity", coupon_ops))
        found = _find_by(existing_msgs, "name", m["body"]["name"])
        tkey = m["body"]["template_id"]["$ref"].rsplit(".", 1)[-1]
        resolved = dict(m["body"], activities_id=(ours[0]["activity"].get("id") if ours else None),
                        template_id=template_ids.get(tkey))
        if not found:
            plan_ops.append(_create(op))
        elif len(found) == 1 and resolved["template_id"] and not mkt.message_differences(found[0], resolved):
            plan_ops.append(_adopt(op, found[0].get("id")))
        else:
            conflicts.append({"resource": "message", "name": m["body"]["name"], "existing": [f.get("id") for f in found]})
    if ours and act_status == 1 and any(o["kind"] == "message" and o.get("mode") == "create"
                                        and o["op_id"].startswith("marketing.message.") for o in plan_ops):
        conflicts.append({"resource": "activity", "name": activity["name"],
                          "reason": "activity already enabled with an incomplete message graph"})
    return act_status


def _adopt_unpaid_activity(b, preset, activities, messages, adopt_id, coupon_ops, conflicts, plan_ops):
    """v1.3 migration: update an existing unpaid activity in place. Its messages, ordered by send time, map
    one-to-one onto the preset steps. Name, order filters and status are kept; old templates stay for rollback."""
    target = [a for a in activities if as_int((a.get("activity") or {}).get("id")) == adopt_id]
    if len(target) != 1:
        conflicts.append({"resource": "activity", "id": adopt_id, "reason": "marketing.adopt_activity_id not found"})
        return 2
    info = target[0]["activity"]
    live_msgs = sorted(target[0].get("messages") or [],
                       key=lambda m: (as_int(m.get("c_date_time")) or 0, as_int(m.get("id")) or 0))
    if as_int(info.get("payment_status")) != 1:
        conflicts.append({"resource": "activity", "id": adopt_id,
                          "reason": "adopted activity is not an unpaid-order activity"})
        return 2
    if len(live_msgs) != len(messages):
        conflicts.append({"resource": "activity", "id": adopt_id, "messages": len(live_msgs), "expected": len(messages),
                          "reason": "adopted activity must have exactly one message per preset step; "
                                    "messages are never added or deleted"})
        return 2
    if any(as_int(m.get("type")) != 1 for m in live_msgs):
        conflicts.append({"resource": "activity", "id": adopt_id, "reason": "adopted activity contains non-email messages"})
        return 2
    desired = {"max_time": as_int(preset["max_time_days"]), "is_end_pending_order": 1}
    before = {k: info.get(k) for k in desired}
    plan_ops.append({"op_id": "marketing.activity", "module": "marketing", "kind": "activity_update",
                     "api": {"read": "emailrecall/activities-info", "write": "emailrecall/activities-save"},
                     "mode": "update", "target": {"id": adopt_id}, "before": before, "desired": desired,
                     "write_expected": any(not sem_equal(before[k], desired[k]) for k in desired)})
    ids = []
    for live, m in zip(live_msgs, messages):
        body = dict(m["body"], activities_id=adopt_id)
        op = _message_op(f"marketing.message.{m['key']}", body, _message_deps(body, "marketing.activity", coupon_ops))
        op.update({"kind": "message_update", "mode": "update", "target": {"id": as_int(live.get("id"))},
                   "before": {f: live.get(f) for f in mkt.MESSAGE_FIELDS}, "write_expected": True})
        plan_ops.append(op)
        ids.append(as_int(live.get("id")))
    b.notes.append(f"adopting unpaid activity {adopt_id}: messages {ids} are updated in place in send-time order; "
                   "previous recall templates are left untouched for rollback")
    return as_int(info.get("status"))


def _pending_activity(profile, preset, activities, template_ids, conflicts, plan_ops):
    """Payment-pending (konbini/bank transfer) reminder activity; ends the order's matched unpaid activity."""
    activity = mkt.desired_pending_activity(profile, preset)
    ours = [a for a in activities if str((a.get("activity") or {}).get("name", "")) == activity["name"]]
    act_op = {"op_id": "marketing.pending_activity", "module": "marketing", "kind": "activity",
              "api": {"read": "emailrecall/activities-info", "search": "emailrecall/activities-list",
                      "write": "emailrecall/activities-save"}, "desired": activity}
    existing_msgs, status = [], 2
    if not ours:
        plan_ops.append(_create(act_op))
    elif len(ours) == 1 and not mkt.activity_differences(ours[0]["activity"], activity):
        status = as_int(ours[0]["activity"].get("status"))
        existing_msgs = ours[0].get("messages") or []
        plan_ops.append(_adopt(act_op, ours[0]["activity"].get("id")))
    else:
        conflicts.append({"resource": "pending_activity", "name": activity["name"],
                          "differences": [mkt.activity_differences(a.get("activity") or {}, activity) for a in ours]})
    for other in activities:
        info = other.get("activity") or {}
        if (info.get("name") != activity["name"] and as_int(info.get("status")) == 1
                and as_int(info.get("payment_status")) == 2):
            conflicts.append({"resource": "pending_activity", "id": info.get("id"), "name": info.get("name"),
                              "reason": "another enabled payment-pending activity exists; agent review needed"})
    for m in mkt.desired_pending_messages(profile, preset):
        op = _message_op(f"marketing.pending_message.{m['key']}", m["body"],
                         _message_deps(m["body"], "marketing.pending_activity", {}))
        found = _find_by(existing_msgs, "name", m["body"]["name"])
        resolved = dict(m["body"], activities_id=(ours[0]["activity"].get("id") if ours else None),
                        template_id=template_ids.get(m["key"]))
        if not found:
            plan_ops.append(_create(op))
        elif len(found) == 1 and resolved["template_id"] and not mkt.message_differences(found[0], resolved):
            plan_ops.append(_adopt(op, found[0].get("id")))
        else:
            conflicts.append({"resource": "pending_message", "name": m["body"]["name"]})
    if ours and status == 1 and any(o["op_id"].startswith("marketing.pending_message.") and o.get("mode") == "create"
                                    for o in plan_ops):
        conflicts.append({"resource": "pending_activity", "reason": "enabled with an incomplete message graph"})
    return status


def preset_schedule_minutes(profile, assets_root):
    mk = profile.get("marketing") or {}
    try:
        preset = load_marketing_preset(mk.get("preset"), assets_root)
    except LaunchError:
        return []
    minutes = [as_int(x) for x in (preset.get("schedules") or {}).get(mk.get("schedule"), [])]
    if minutes and as_int(mk.get("payment_deadline_days")) and preset.get("payment_pending"):
        # payment-pending reminders also need the order to stay open until their last send
        minutes = sorted(set(minutes + mkt.pending_schedule(preset, mk["payment_deadline_days"])))
    return minutes


def _backend_dependencies(ops, backend):
    """App ops that rely on backend facts must not run when the backend step did not complete."""
    # Only backend steps that actually change something gate later app ops (sequencing + failure isolation);
    # verify-only backend ops stay independent so unrelated drift does not block app configuration.
    writing = {o["op_id"] for o in ops if o.get("module") == "backend" and o.get("write_expected")}
    bops = {k: v for k, v in (backend.get("ops") or {}).items() if v in writing}
    # needed-but-blocked prerequisites have no op; depending on their id keeps dependents blocked
    bops.update(backend.get("blocked_prerequisites") or {})
    locale = [bops[k] for k in ("basic", "currency") if bops.get(k)]
    commerce = locale + ([bops["checkout"]] if bops.get("checkout") else [])
    # Japan checkout triggers on the Japanese storefront language only (condition 1), so it waits for the
    # basic (language) step but not for the base-currency decision; monetary marketing still needs both.
    language = [bops["basic"]] if bops.get("basic") else []
    last_delay = backend.get("recall_last_delay_seconds") if backend.get("selected") else None
    for op in ops:
        extra = []
        if op["op_id"] == "japancheckout.config":
            extra = language
        elif op["module"] == "marketing" and op["kind"] in ("coupon", "coupon_ref", "template", "activity",
                                                             "activity_update", "message_update"):
            extra = commerce
        op["depends_on"] = op.get("depends_on", []) + [d for d in extra if d not in op.get("depends_on", [])]
        if op.get("activation") and last_delay:
            op["pending_timeout_min_seconds"] = last_delay  # re-checked live before each cut-over step


# ------------------------------------------------------------------ plan

def build_plan(profile, snapshot, snapshot_sha, assets_root=ASSETS_ROOT, now=None):
    now = now or now_jst()
    b = _Builder()
    validation = validate_profile(profile, assets_root, now)
    if validation["errors"]:
        b.block("global", "PROFILE_INVALID", "profile has validation errors", {"errors": validation["errors"]})
    if snapshot.get("schema") != "SHOPXZ_JP_SNAPSHOT_v1":
        raise LaunchError("SNAPSHOT_INVALID", "not a SHOPXZ_JP_SNAPSHOT_v1 snapshot")

    target = profile.get("target") or {}
    snap_target = snapshot.get("target") or {}
    identity_ok = True
    expected_id = (snapshot.get("expected") or {}).get("shop_id")
    if target.get("shop_id") is None or expected_id is None:
        identity_ok = False
        b.block("global", "IDENTITY_UNRESOLVED",
                "snapshot was not taken with a confirmed shop_id; cross-check the discovered identity, then "
                "re-run inspect with resolved-profile.json and plan from that snapshot",
                {"discovered_shop_id": snap_target.get("shop_id")})
    elif target.get("shop_id") != snap_target.get("shop_id") or expected_id != target.get("shop_id"):
        identity_ok = False
        b.block("global", "IDENTITY_MISMATCH", "profile shop_id differs from the snapshot",
                {"profile": target.get("shop_id"), "snapshot": snap_target.get("shop_id")})
    if normalize_url(target.get("public_url")) != normalize_url(snap_target.get("public_url")):
        identity_ok = False
        b.block("global", "IDENTITY_MISMATCH", "profile public_url differs from the snapshot",
                {"profile": target.get("public_url"), "snapshot": snap_target.get("public_url")})
    if snap_target.get("shop_type") != 10:
        b.block("global", "SHOP_TYPE_UNSUPPORTED", "only B2C shop_type=10 is supported")
    if snapshot.get("profile_hash") != profile_hash(profile):
        b.notes.append("profile changed since snapshot; modules not read at inspect time are blocked")

    modules = profile.get("modules") or {}
    state = snapshot.get("state") or {}
    selection, unsupported = {}, []
    backend, workflows = {"selected": False, "reason": "profile invalid"}, {}
    if not validation["errors"]:
        def backend_op(op_id, area, live, desired, contract, notes):
            oid = _config_op(b, op_id, "backend", CONFIG_APIS[area], live, desired, notes,
                             contract=contract, scope=f"backend.{area}")
            if oid:
                b.ops[-1]["area"] = "basic" if area == "currency" else area
            return oid
        backend, workflows = plan_backend(b, profile, snapshot, backend_op)
        if backend.get("selected") and modules.get("marketing"):
            schedule = preset_schedule_minutes(profile, assets_root)
            backend["recall_last_delay_seconds"] = schedule[-1] * 60 if schedule else None
    if not validation["errors"]:
        accent = valid_accent(profile)
        for module in MODULES:
            if not modules.get(module):
                selection[module] = "disabled"
                continue
            if snapshot.get("availability", {}).get(module) != "available":
                selection[module] = "unavailable"
                b.block(module, "ADDON_UNAVAILABLE", "addon not present for this store; not purchased or enabled automatically")
                continue
            mstate = state.get(module)
            if not mstate or mstate.get("status") != "read":
                selection[module] = "unknown"
                b.block(module, "STATE_UNKNOWN", "module state was not read successfully at inspect time",
                        {"error": (mstate or {}).get("error")})
                continue
            selection[module] = "selected"
            if module == "japancheckout":
                desired, notes = japancheckout_desired(mstate["config"])
                _config_op(b, "japancheckout.config", module, CONFIG_APIS[module], mstate["config"], desired, notes)
            elif module == "viewed":
                desired, notes = viewed_desired(mstate["config"], accent)
                _config_op(b, "viewed.config", module, CONFIG_APIS[module], mstate["config"], desired, notes)
            elif module == "fixedcart":
                desired, notes = fixedcart_desired(mstate["config"])
                _config_op(b, "fixedcart.config", module, CONFIG_APIS[module], mstate["config"], desired, notes)
            elif module == "cfsaas":
                desired, notes = cfsaas_desired(mstate["config"])
                _config_op(b, "cfsaas.config", module, CONFIG_APIS[module], mstate["config"], desired, notes)
            elif module == "emails":
                unsupported = _email_ops(b, profile, mstate, assets_root)
            elif module == "marketing":
                if not _marketing_ops(b, profile, snapshot, now, assets_root, backend):
                    selection[module] = "blocked"
        _backend_dependencies(b.ops, backend)

    runnable = not any(x["scope"] == "global" for x in b.blockers)
    plan = {
        "schema": PLAN_SCHEMA,
        "created_at": iso(now),
        "target": {"shop_id": target.get("shop_id"), "public_url": target.get("public_url"),
                   "shop_name": snap_target.get("shop_name")},
        "profile_hash": profile_hash(profile),
        "snapshot_sha256": snapshot_sha,
        "snapshot_captured_at": snapshot.get("captured_at"),
        "runnable": runnable,
        "selection": selection,
        "summary": {"operations": len(b.ops), "writes_expected": sum(1 for o in b.ops if o.get("write_expected")),
                    "blockers": len(b.blockers)},
        "operations": b.ops,
        "blockers": b.blockers,
        "unsupported_emails": unsupported,
        "backend": backend,
        "backend_workflows": workflows,
        "readiness_gaps": readiness_gaps(validation, state),
        "agent_tasks": build_agent_tasks(profile, snapshot, selection, validation, identity_ok, backend, workflows),
        "warnings": validation["warnings"] + b.notes,
        "launch_status": "NOT_READY_TO_LAUNCH",
    }
    plan["fingerprint"] = plan_fingerprint(plan)
    return plan
