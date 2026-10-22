"""Read-only inspection of the currently bound store into an immutable snapshot."""

import copy

from .adapter import ApiFailure
from .backend import read_backend
from .store_settings import backend_selected
from .live import list_items, paginate, read_identity, check_identity
from .gates import merge_coupon, notice_readiness
from .marketing import coupon_codes
from .profile import profile_hash, validate_profile
from .util import LaunchError, iso, now_jst, sha256_obj, write_json_immutable

SNAPSHOT_SCHEMA = "SHOPXZ_JP_SNAPSHOT_v1"
# profile module -> fecify addon identity reported by shop-info/init
MODULE_ADDONS = {"japancheckout": "japancheckout", "viewed": "prodviewed", "fixedcart": "fixedcart",
                 "cfsaas": "cfsaas", "emails": "notice", "marketing": "emailrecall"}
CONFIG_READS = {"japancheckout": "japancheckout/get", "viewed": "prodviewed/get", "fixedcart": "fixedcart/get",
                "cfsaas": "cfsaas/get"}
PAYMENT_KEYS = ("id", "name", "title", "identity", "code", "type", "status", "sort", "is_top")


class _Recorder:
    def __init__(self, adapter):
        self.adapter = adapter
        self.reads = []
        self.unknowns = []

    def get(self, api_id, query=None, required=True):
        try:
            data = self.adapter.call(api_id, query=query)
        except ApiFailure as exc:
            self.reads.append({"api": api_id, "query": query or {}, "ok": False, "error": exc.message})
            self.unknowns.append({"api": api_id, "query": query or {}, "reason": exc.message})
            if required:
                raise
            return None
        self.reads.append({"api": api_id, "query": query or {}, "ok": True, "code": 200, "sha256": sha256_obj(data)})
        return data

    def call(self, api_id, query=None, data=None):
        return self.get(api_id, query)

    def pages(self, api_id, query=None):
        before = len(self.reads)
        try:
            items = paginate(self.adapter, api_id, query)
        except ApiFailure as exc:
            self.unknowns.append({"api": api_id, "query": query or {}, "reason": exc.message})
            self.reads.append({"api": api_id, "query": query or {}, "ok": False, "error": exc.message})
            raise
        self.reads.insert(before, {"api": api_id, "query": query or {}, "ok": True, "code": 200,
                                   "items": len(items), "sha256": sha256_obj(items)})
        return items


def _module_block(rec, fn):
    """Run a module's reads; a non-fatal read failure leaves the module 'unknown'."""
    try:
        return {"status": "read", **fn()}
    except ApiFailure as exc:
        return {"status": "unknown", "error": exc.message}


def _read_emails(rec, with_details):
    templates = []
    for row in list_items(rec.get("notice/theme-list")):
        entry = {"list": copy.deepcopy(row)}
        if with_details and row.get("lang_code", "default") == "default":
            info = rec.get("notice/theme-info", {"id": row.get("id")}) or {}
            entry["record"] = info.get("email") or {}
            entry["liquid_variables"] = info.get("liquid_variables") or []
            langs = rec.get("notice/theme-get-languages", {"type": row.get("type"), "category": row.get("category")})
            entry["lang_emails"] = list_items(langs)
        templates.append(entry)
    return {"templates": templates}


def _read_marketing(rec, profile):
    candidates = {c.upper() for c in coupon_codes(profile)}
    coupons = rec.pages("coupon/list")
    details = []
    for row in coupons:
        if str(row.get("code", "")).upper() in candidates:
            details.append(merge_coupon(row, rec.get("coupon/info", {"id": row.get("id")})))
    templates = [rec.get("emailrecall/message-template-info", {"template_id": t.get("id")})
                 for t in rec.pages("emailrecall/message-template-list")]
    activities = []
    for act in rec.pages("emailrecall/activities-list"):
        info = rec.get("emailrecall/activities-info", {"id": act.get("id")})
        msgs = list_items(rec.get("emailrecall/activities-message-list", {"activities_id": act.get("id")}))
        activities.append({"activity": info, "messages": msgs})
    email_vars = [v.get("var") if isinstance(v, dict) else v
                  for v in list_items(rec.get("emailrecall/message-email-vars"))]
    return {"config": rec.get("emailrecall/get"), "coupons": coupons, "coupon_details": details,
            "templates": templates, "activities": activities, "email_vars": email_vars}


def inspect(profile, adapter, workdir, now=None):
    now = now or now_jst()
    report = validate_profile(profile, now=now)
    if report["errors"]:
        raise LaunchError("PROFILE_INVALID", "profile has validation errors", {"errors": report["errors"]})
    target = profile["target"]
    if not target.get("public_url"):
        raise LaunchError("IDENTITY_UNRESOLVED", "profile target.public_url is required for inspect")

    rec = _Recorder(adapter)
    identity = read_identity(rec)  # init + site_base_url first
    check_identity(identity, target.get("shop_id"), target.get("public_url"))

    modules = profile["modules"]
    addons = set(identity["addons"])
    availability = {m: ("available" if addon in addons else "unavailable") for m, addon in MODULE_ADDONS.items()}
    state = {
        "basic": _module_block(rec, lambda: {"data": rec.get("system-settings/basic_get")}),
        "country": _module_block(rec, lambda: {"data": rec.get("system-settings/country_get")}),
        "payments": _module_block(rec, lambda: {"data": [
            {k: p.get(k) for k in PAYMENT_KEYS if k in p}
            for p in list_items(rec.get("payment/active-list")) if isinstance(p, dict)]}),
    }
    if availability["emails"] == "available":
        state["notice_readiness"] = _module_block(
            rec, lambda: {"data": notice_readiness(rec.get("notice/email-config-get"))})
    for module, api_id in CONFIG_READS.items():
        if modules.get(module) and availability[module] == "available":
            state[module] = _module_block(rec, lambda a=api_id: {"config": rec.get(a)})
    if (modules.get("emails") or modules.get("marketing")) and availability["emails"] == "available":
        state["emails"] = _module_block(rec, lambda: _read_emails(rec, with_details=bool(modules.get("emails"))))
    if modules.get("marketing") and availability["marketing"] == "available":
        state["marketing"] = _module_block(rec, lambda: _read_marketing(rec, profile))
    if backend_selected(profile):  # v1.1 store settings menu; legacy profiles keep the narrow read set
        state["backend"] = read_backend(rec, profile, _module_block)

    resolved = target.get("shop_id") is not None
    snapshot = {
        "schema": SNAPSHOT_SCHEMA,
        "captured_at": iso(now),
        "profile_hash": profile_hash(profile),
        "expected": {"shop_id": target.get("shop_id"), "public_url": target.get("public_url")},
        "target": {k: identity[k] for k in ("shop_id", "shop_type", "shop_name", "public_url",
                                            "manager_base_url", "theme_identity")},
        "identity_status": "verified" if resolved else "discovered_unconfirmed",
        "addons": identity["addons"],
        "availability": availability,
        "selected_modules": {k: bool(v) for k, v in modules.items()},
        "state": state,
        "reads": rec.reads,
        "unknowns": rec.unknowns,
    }
    path = write_json_immutable(workdir / "snapshot.json", snapshot, now.strftime("%Y%m%dT%H%M%S"))
    out = {"ok": True, "snapshot": str(path), "identity_status": snapshot["identity_status"],
           "target": snapshot["target"], "availability": availability,
           "unknowns": len(rec.unknowns), "reads": len(rec.reads)}
    if not resolved:
        resolved_profile = copy.deepcopy(profile)
        resolved_profile["target"]["shop_id"] = identity["shop_id"]
        rp = write_json_immutable(workdir / "resolved-profile.json", resolved_profile, now.strftime("%Y%m%dT%H%M%S"))
        out["resolved_profile"] = str(rp)
        out["next_step"] = ("cross-check the discovered shop name, public URL and shop_id against the store the "
                            "user named; if they agree, re-run inspect with resolved-profile.json (no extra "
                            "approval of the numeric ID needed). This snapshot itself cannot produce a runnable plan.")
    return out
