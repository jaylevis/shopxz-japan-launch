"""v1.1 store settings menu: basic, advanced, task, captcha, payment, checkout, tax, country, shipping.

Safe documented settings become ordinary journaled operations (config_set / payment_*). Shipping and
tax saves are danger-marked in the manager skill, so they are emitted as reviewed, gated manager
workflows (executed=false) with exact request bodies and read-back checks; this tool never runs them.
"""

import copy
import re

from .adapter import ApiFailure
from .configs import CONFIG_APIS, WRITE_KEYS, submit_view
from .live import list_items, paginate
from .store_settings import (CAPTCHA_FRONT, VUE_ONLY_CHECKOUT, backend_selected)
from .util import as_int, sem_equal

AREAS = ("basic", "advanced", "tasks", "captcha", "payment", "checkout", "tax", "country", "shipping")
PAYMENT_KEYS = ("id", "code", "label", "cn_label", "status", "position", "theme_liquid", "theme_vue",
                "refund_online", "need_billing_address")
FOREIGN_MEDIA = re.compile(r"/(?:common|product|system|theme)/(\d+)/")
QTY_RATES_OFF = {"is_free_shipping": 2, "fist_item": 0, "each_additional_item": 0,
                 "is_over_qty_free_shipping": 2, "over_qty": 0}
NUMERIC_AMOUNT = re.compile(r"^\s*-?\d+(\.\d+)?\s*$")


# ------------------------------------------------------------------ inspect

def _codes(rows):
    return sorted({str(r.get("code")) for r in rows if isinstance(r, dict) and r.get("code")})


def _zone_summary(zone):
    keep = ("id", "shipping_id", "name", "countries", "type", "weight_based_shipping_rates",
            "price_based_shipping_rates", "qty_based_shipping_rates")
    return {k: copy.deepcopy(zone.get(k)) for k in keep if k in zone}


def _read_shipping(rec, ss_ship):
    schemes = []
    for row in paginate(rec, "shipping/shipping-list"):
        info = rec.get("shipping/shipping-info", {"id": row.get("id")}) or {}
        zones = []
        for z in info.get("zones") or row.get("zones") or []:
            detail = rec.get("shipping/zone-info", {"shipping_id": row.get("id"), "shipping_zone_id": z.get("id")}) or {}
            zones.append(_zone_summary(detail.get("shipping_zone") or z))
        schemes.append({"id": as_int(row.get("id")), "name": (info.get("name") or row.get("name")),
                        "priority": info.get("priority", row.get("priority")),
                        "status": as_int(info.get("status", row.get("status"))), "zones": zones})
    out = {"schemes": schemes}
    sid, zid = ss_ship.get("shipping_id"), ss_ship.get("zone_id")
    if sid is not None:
        q = {"shipping_id": sid}
        if zid is not None:
            q["shipping_zone_id"] = zid
        occ = rec.get("shipping/zone-countries", q) or {}
        out["occupancy"] = {"query": q, "selected": occ.get("selectedCountryAndProvinces") or []}
    return out


def _tax_rows(rows):
    return [{"type": as_int(r.get("type")), "country_code": r.get("country_code"),
             "state_code": r.get("state_code") or "", "tax": r.get("tax")} for r in rows if isinstance(r, dict)]


def _read_tax(rec):
    bases = list_items(rec.get("tax/base-list"))
    jp = []
    for b in bases:
        if b.get("country_code") == "JP":
            info = rec.get("tax/base-info", {"tax_base_id": b.get("id")})
            jp.append({"id": as_int(b.get("id")), "status": as_int(b.get("status")), "rows": _tax_rows(list_items(info))})
    customs = []
    for c in list_items(rec.get("tax/custom-list")):
        info = (rec.get("tax/custom-info", {"id": c.get("id")}) or {}).get("info") or c
        rows = _tax_rows(paginate(rec, "tax/custom-country-tax-list", {"tax_custom_id": c.get("id")}))
        customs.append({"id": as_int(c.get("id")), "title": info.get("title"),
                        "product_count": as_int(info.get("product_count")),
                        "jp_rows": [r for r in rows if r["country_code"] == "JP"],
                        "other_countries": sorted({r["country_code"] for r in rows if r["country_code"] != "JP"})})
    return {"base_countries": [{"country_code": b.get("country_code"), "status": as_int(b.get("status")),
                                "min_tax": b.get("min_tax"), "max_tax": b.get("max_tax")} for b in bases],
            "base_jp": jp, "custom": customs}


def _country_pool(data):
    rows = [c for c in list_items(data) if isinstance(c, dict)]
    jp = next((c for c in rows if c.get("code") == "JP"), None)
    return {"count": len(rows), "has_jp": jp is not None,
            "jp_provinces": sorted(str(p.get("code")) for p in (jp or {}).get("provinces") or [])}


def read_backend(rec, profile, block):
    """Extended reads for the backend group; each area is its own block so failures stay local."""
    ss = profile["store_settings"]
    out = {"selected": True}
    for area, api_id in (("checkout", "system-settings/checkout_get"), ("advanced", "system-settings/advance_get"),
                         ("tasks", "system-settings/task_get"), ("captcha", "system-settings/captcha_get"),
                         ("cache", "system-settings/cache_get")):
        out[area] = block(rec, lambda a=api_id: {"config": rec.get(a)})
    out["payment"] = block(rec, lambda: {"methods": [{k: p.get(k) for k in PAYMENT_KEYS if k in p}
                                                     for p in list_items(rec.get("payment/list"))
                                                     if isinstance(p, dict)]})

    def languages():
        d = rec.get("shop-info/languages") or {}
        return {"base": d.get("shop_base_language_code"), "codes": _codes(d.get("shop_language_list") or [])}

    def currencies():
        d = rec.get("shop-info/currencies") or {}
        return {"base": d.get("shop_base_currency_code"), "codes": _codes(d.get("shop_currency_list") or [])}

    def timezones():
        keys = {str(t.get("key")) for t in list_items(rec.get("shop-info/timezone_list")) if isinstance(t, dict)}
        return {"count": len(keys), "has_asia_tokyo": "Asia/Tokyo" in keys}

    def countries():
        return _country_pool(rec.get("shop-info/country_list"))

    for area, fn in (("languages", languages), ("currencies", currencies), ("timezones", timezones),
                     ("countries", countries)):
        out[area] = block(rec, lambda f=fn: {"data": f()})
    out["shipping"] = block(rec, lambda: {"data": _read_shipping(rec, ss.get("shipping") or {})})
    out["tax"] = block(rec, lambda: {"data": _read_tax(rec)})
    return out


# ------------------------------------------------------------------ plan helpers

class _Area:
    """Snapshot accessor that reports unknown areas as scoped blockers instead of fabricating facts."""

    def __init__(self, b, state):
        self.b, self.state = b, state
        self.backend = state.get("backend") or {}

    def get(self, area, key, scope):
        blk = self.backend.get(area) if area not in ("basic", "country") else self.state.get(area)
        if not blk or blk.get("status") != "read":
            self.b.block(scope, "STATE_UNKNOWN", f"{area} was not read at inspect time; re-inspect",
                         {"error": (blk or {}).get("error")})
            return None
        return blk.get(key)


def _pool(acc, area, scope):
    return acc.get(area, "data", scope)


def _missing_required(live, write_api, optional=()):
    return [k for k in WRITE_KEYS[write_api] if k not in (live or {}) and k not in optional]


def _apply_values(b, live, values, write_api, scope):
    missing = _missing_required(live, write_api, optional=("address_street_2",))
    if missing:
        b.block(scope, "SCHEMA_INCOMPLETE", "live object lacks documented required fields; not saving a partial object",
                {"missing": missing})
        return None
    desired = submit_view(live, write_api)
    desired.update(copy.deepcopy(values))
    return desired


def _basic_ops(b, cfg_op, acc, profile, ss, target):
    ops = {}
    if not (ss.get("basic") or {}).get("apply"):
        return ops
    scope = "backend.basic"
    live = acc.get("basic", "data", scope)
    if live is None:
        return ops
    write_api = CONFIG_APIS["basic"]["write"]
    missing = _missing_required(live, write_api)
    brand = profile.get("brand") or {}
    problems = []
    desired = submit_view(live, write_api)
    for field, key in (("name", "site_name"), ("support_email", "site_email"), ("logo_path", "site_logo"),
                       ("favicon_path", "site_ico")):
        value = brand.get(field)
        if value not in (None, ""):
            m = FOREIGN_MEDIA.search(str(value))
            if key in ("site_logo", "site_ico") and m and as_int(m.group(1)) != target.get("shop_id"):
                problems.append(f"brand.{field} points at another shop's media ({value}); upload to this store first")
            desired[key] = value
    if live.get("language") != "ja":
        pool = _pool(acc, "languages", scope)
        if pool is None or "ja" not in pool.get("codes", []):
            problems.append("language 'ja' is not in the store's enabled language pool")
        desired["language"] = "ja"
    if live.get("timezone") != "Asia/Tokyo":
        pool = _pool(acc, "timezones", scope)
        if pool is None or not pool.get("has_asia_tokyo"):
            problems.append("timezone 'Asia/Tokyo' is not in the returned timezone pool")
        desired["timezone"] = "Asia/Tokyo"
    if missing:
        problems.append(f"basic_get lacks required basic_set fields: {missing}")
    if problems:
        b.block(scope, "BASIC_NOT_SAVABLE", "basic settings cannot be saved safely", {"problems": problems})
    else:
        oid = cfg_op("backend.basic", "basic", desired, [("language",), ("timezone",)],
                     ["site_status, order number prefix/suffix and handle_type are preserved"])
        if oid:  # None when a schema-drift blocker was recorded instead
            ops["basic"] = oid
    # base currency: isolated documented write; never a product repricing
    current = live.get("currency")
    allow = bool((ss.get("basic") or {}).get("allow_currency_change"))
    if current == "JPY":
        oid = cfg_op("backend.currency", "currency", {"currency": "JPY"}, [("currency",)], [])
        if oid:
            ops["currency"] = oid
    elif not allow:
        b.block("backend.currency", "CURRENCY_CHANGE_NOT_ALLOWED",
                f"store base currency is {current!r}; changing it to JPY is a business decision about existing "
                "product prices. Set store_settings.basic.allow_currency_change=true after the price review "
                "(prices are not converted by this tool)", {"current": current})
    else:
        pool = _pool(acc, "currencies", "backend.currency")
        if pool is not None and "JPY" in pool.get("codes", []):
            oid = cfg_op("backend.currency", "currency", {"currency": "JPY"}, [("currency",)],
                         ["base currency only: product prices are NOT converted or edited"])
            if oid:
                ops["currency"] = oid
        elif pool is not None:
            b.block("backend.currency", "JPY_NOT_ENABLED", "JPY is not in the store's enabled currency list",
                    {"codes": pool.get("codes")})
    return ops


def _country_op(b, cfg_op, acc, ss):
    if not (ss.get("country") or {}).get("apply"):
        return None
    scope = "backend.country"
    live = acc.get("country", "data", scope)
    pool = _pool(acc, "countries", scope)
    if live is None or pool is None:
        return None
    if not pool.get("has_jp"):
        b.block(scope, "JP_NOT_IN_COUNTRY_POOL", "country list does not contain JP")
        return None
    missing = _missing_required(live, CONFIG_APIS["country"]["write"])
    if missing:
        b.block(scope, "SCHEMA_INCOMPLETE", "country_get lacks required fields", {"missing": missing})
        return None
    return cfg_op("backend.country", "country", {"status": 2, "countrys": ["JP"], "sorts": ["JP"]},
                  [("status",), ("countrys",), ("sorts",)], [])


def _values_op(b, cfg_op, acc, ss, area, theme):
    sec = ss.get(area) or {}
    values = sec.get("values") or {}
    if not sec.get("apply") or not values:
        return None
    scope = f"backend.{area}"
    live = acc.get(area, "config", scope)
    if live is None:
        return None
    desired = _apply_values(b, live, values, CONFIG_APIS[area]["write"], scope)
    if desired is None:
        return None
    notes = []
    if area == "checkout":
        for key in VUE_ONLY_CHECKOUT:
            if key in values:
                notes.append(f"{key} only affects Vue themes (current theme: {theme}); Liquid themes ignore it")
    if area == "captcha":
        disabled = [k for k in ("manager_login",) + CAPTCHA_FRONT
                    if as_int(live.get(k)) == 1 and as_int(desired.get(k)) == 2]
        if "manager_login" in disabled or (all(as_int(desired.get(k)) == 2 for k in CAPTCHA_FRONT)
                                           and any(as_int(live.get(k)) == 1 for k in CAPTCHA_FRONT)):
            b.block(scope, "CAPTCHA_PROTECTION_DISABLE", "refusing to disable admin login or all storefront "
                    "CAPTCHA protection", {"would_disable": disabled})
            return None
    if area == "advanced" and as_int(values.get("liquid_debug")) == 2:
        notes.append("liquid_debug=2 is meant for the production/publish stage, not during theme development")
    return cfg_op(f"backend.{area}", area, desired, [(k,) for k in values], notes)


def _payment_ops(b, add, acc, ss):
    pay = ss.get("payment") or {}
    codes, verified = list(pay.get("enable_codes") or []), set(pay.get("credentials_verified_codes") or [])
    preferred = pay.get("preferred_code")
    if not codes and not preferred:
        return []
    scope = "backend.payment"
    methods = acc.get("payment", "methods", scope)
    if methods is None:
        return []
    by_code = {m.get("code"): m for m in methods}
    out = []
    for code in codes:
        row = by_code.get(code)
        if row is None:
            b.block(scope, "PAYMENT_CODE_UNKNOWN", f"payment code {code!r} is not in payment/list", {"code": code})
            continue
        if code not in verified:
            b.block(scope, "PAYMENT_CREDENTIALS_UNVERIFIED",
                    f"{code}: credentials must be saved and verified by the merchant/agent in the provider settings "
                    "first (add it to credentials_verified_codes); not activated", {"code": code})
            continue
        status = as_int(row.get("status"))
        out.append(add({"op_id": f"backend.payment.enable.{code}", "module": "backend", "area": "payment",
                        "kind": "payment_status", "api": {"read": "payment/list", "write": "payment/update-status"},
                        "target": {"code": code}, "before": {"status": status}, "desired": {"status": 1},
                        "write_expected": status != 1,
                        "notes": ["activation only; credentials and a real transaction remain unverified"]}))
    if preferred:
        row = by_code.get(preferred)
        active_or_planned = row is not None and (as_int(row.get("status")) == 1 or
                                                f"backend.payment.enable.{preferred}" in out)
        if not active_or_planned:
            b.block(scope, "PAYMENT_PREFERRED_NOT_ACTIVE", f"preferred payment {preferred!r} is neither active nor "
                    "planned for activation", {"code": preferred})
        else:
            deps = [f"backend.payment.enable.{preferred}"] if f"backend.payment.enable.{preferred}" in out else []
            out.append(add({"op_id": "backend.payment.top", "module": "backend", "area": "payment",
                            "kind": "payment_top", "api": {"read": "payment/list", "write": "payment/set-top"},
                            "target": {"code": preferred, "payment_id": as_int(row.get("id"))},
                            "before": {"position": row.get("position")}, "desired": {"position": 1},
                            "depends_on": deps, "write_expected": as_int(row.get("position")) != 1}))
    return out


# ------------------------------------------------------------------ shipping / tax workflows

def _bands(fee, threshold, max_amount):
    if threshold == 0 or fee == 0:
        return [{"start": 0, "end": max_amount, "amount": "0"}]
    return [{"start": 0, "end": threshold, "amount": str(fee)},
            {"start": threshold, "end": max_amount, "amount": "0"}]


def quote(bands, subtotal):
    for band in bands:
        if as_int(band["start"]) <= subtotal < as_int(band["end"]):
            return as_int(band["amount"])
    return None


def _jp_only(zone):
    countries = zone.get("countries") or []
    return len(countries) == 1 and countries[0].get("country") == "JP"


def _covers_jp(zone):
    return any(c.get("country") in ("JP", "*") for c in zone.get("countries") or [])


def _rates_equal(a, b):
    if len(a or []) != len(b or []):
        return False
    return all(sem_equal(x.get(k), y.get(k)) for x, y in zip(a, b) for k in ("start", "end", "amount"))


def _zone_matches(zone, bands, jp_provinces):
    if as_int(zone.get("type")) != 2 or not _jp_only(zone):
        return False
    prov = sorted(zone["countries"][0].get("province") or [])
    return prov in ([], sorted(jp_provinces)) and _rates_equal(zone.get("price_based_shipping_rates"), bands)


def _full_jp(zone, jp_provinces):
    if not _jp_only(zone):
        return False
    prov = sorted(zone["countries"][0].get("province") or [])
    return prov in ([], sorted(jp_provinces))


def _candidate(scheme, zone):
    return {"shipping_id": scheme.get("id"), "scheme_status": scheme.get("status"), "zone_id": zone.get("id"),
            "zone_name": zone.get("name"), "type": zone.get("type"), "countries": zone.get("countries"),
            "price_based_shipping_rates": zone.get("price_based_shipping_rates"),
            "weight_based_shipping_rates": zone.get("weight_based_shipping_rates")}


def _complexity(zone):
    reasons = []
    if as_int(zone.get("type")) != 2:
        reasons.append(f"zone uses type {zone.get('type')} (weight/quantity) pricing")
    if not _jp_only(zone):
        reasons.append("zone covers countries other than JP (mixed-country zone)")
    for r in (zone.get("price_based_shipping_rates") or []) + (zone.get("weight_based_shipping_rates") or []):
        if not NUMERIC_AMOUNT.match(str(r.get("amount", ""))):
            reasons.append(f"formula amount {r.get('amount')!r}")
    if len(zone.get("price_based_shipping_rates") or []) > 2:
        reasons.append("multiple price bands")
    return reasons


def _occupied_jp(occupancy):
    return [o for o in occupancy if o.get("country") in ("JP", "*")]


def shipping_workflow(profile, ss, acc):
    ship = ss.get("shipping") or {}
    wf = {"area": "shipping", "executed": False, "execution": "gated-manager-workflow", "status": None,
          "mode": ship.get("mode"), "current": None, "target_rules": None, "blockers": [], "requests": [],
          "quote_checks": [],
          "notes": ["shipping writes are danger-marked: run them only through the manager skill's documented "
                    "confirmation workflow after authorization; this CLI never executes or confirms them",
                    "handling/transit business days are policy/Feed facts, not shipping rate API fields",
                    "country occupancy is checked within one shipping scheme; other schemes serving JP are "
                    "reported, not assumed to be prohibited"]}
    data = acc.get("shipping", "data", "backend.shipping")
    if data is None:
        wf["status"] = "unknown"
        return wf
    schemes = data.get("schemes") or []
    wf["current"] = [{"id": s["id"], "name": (s.get("name") or {}).get("text") if isinstance(s.get("name"), dict)
                      else s.get("name"), "status": s.get("status"),
                      "zones": [{"id": z.get("id"), "name": z.get("name"), "type": z.get("type"),
                                 "countries": z.get("countries"),
                                 "price_based_shipping_rates": z.get("price_based_shipping_rates")}
                                for z in s.get("zones") or []]} for s in schemes]
    if ship.get("mode") != "prepare":
        wf["status"] = "preserve"
        return wf
    fee, threshold = (profile.get("shipping") or {}).get("base_fee"), (profile.get("shipping") or {}).get("free_threshold")
    if fee is None or threshold is None:
        wf["status"] = "needs_data"
        wf["blockers"].append({"code": "SHIPPING_FACTS_MISSING", "fields": [f for f, v in
                               (("shipping.base_fee", fee), ("shipping.free_threshold", threshold)) if v is None],
                               "message": "no fee/threshold is invented"})
        return wf
    max_amount = ship["max_order_amount"]
    if threshold >= max_amount:
        wf["status"] = "needs_review"
        wf["blockers"].append({"code": "THRESHOLD_ABOVE_MAX", "message": "free_threshold must be below max_order_amount"})
        return wf
    bands = _bands(fee, threshold, max_amount)
    pool = acc.get("countries", "data", "backend.shipping")
    jp_provinces = (pool or {}).get("jp_provinces") or []
    if pool is None or not pool.get("has_jp"):
        wf["status"] = "unknown" if pool is None else "needs_review"
        wf["blockers"].append({"code": "JP_NOT_IN_COUNTRY_POOL", "message": "JP regions must come from the country pool"})
        return wf
    countries = [{"country": "JP", "province": []}]  # [] = all JP provinces per API docs
    wf["target_rules"] = {"country": "JP", "province": "all ([] per API)", "type": 2,
                          "price_based_shipping_rates": bands, "range_rule": "start <= subtotal < end",
                          "currency": "store base currency (must be JPY before relying on these amounts)"}
    probes = [threshold - 1, threshold, threshold + 1] if threshold > 0 else [0, 1]
    wf["quote_checks"] = [{"subtotal": s, "expected_fee": quote(bands, s), "kind": "math_check_only",
                           "note": "arithmetic on the band table; not a checkout test"} for s in probes if s >= 0]
    qty = dict(QTY_RATES_OFF)
    sid, zid = ship.get("shipping_id"), ship.get("zone_id")
    zone_body = {"name": ship["zone_name"], "type": 2, "countries": countries, "weight_based_shipping_rates": [],
                 "price_based_shipping_rates": bands, "qty_based_shipping_rates": qty}
    if sid is None:
        candidates = [(s, z) for s in schemes for z in s.get("zones") or [] if _covers_jp(z)]
        if len(candidates) == 1:
            s, z = candidates[0]
            if as_int(s.get("status")) == 1 and _zone_matches(z, bands, jp_provinces):
                wf["status"] = "config_matches"
                wf["notes"].append("the only JP shipping zone is active and has exactly these full-Japan bands; "
                                   "real checkout still unverified")
                return wf
        if candidates:
            # Never add another live JP option next to existing ones, and never pick one implicitly.
            wf["status"] = "needs_review"
            wf["blockers"].append({"code": "SHIPPING_SELECTION_REQUIRED",
                                   "message": "existing JP/wildcard shipping zones found; choose shipping_id/zone_id "
                                              "explicitly or retire alternatives through the manager workflow",
                                   "candidates": [_candidate(s, z) for s, z in candidates]})
            return wf
        wf["status"] = "prepared"
        wf["requests"] = [
            {"step": 1, "api": "shipping/shipping-create", "danger": True, "executable": True,
             "body": {"name": {"text": ship["name"], "lang_params": {}}, "priority": 1, "status": 2},
             "binds": {"shipping_id": "response data.id"},
             "readback": {"api": "shipping/shipping-info", "query": {"id": "<shipping_id>"},
                          "expect": {"status": 2, "name.text": ship["name"]}}},
            {"step": 2, "api": "shipping/zone-create", "danger": True, "executable": False,
             "executable_after": "replace <shipping_id> with the id returned by step 1",
             "body": dict(zone_body, shipping_id="<shipping_id>"),
             "precheck": {"api": "shipping/zone-countries", "query": {"shipping_id": "<shipping_id>"},
                          "expect": "JP not occupied within this scheme"},
             "readback": {"api": "shipping/shipping-info", "query": {"id": "<shipping_id>"},
                          "expect": {"zones": "exactly one zone with these countries and price bands"}}},
            {"step": 3, "api": "shipping/shipping-change-status", "danger": True, "executable": False,
             "executable_after": "step 2 read-back verified",
             "body": {"id": "<shipping_id>", "status": 1},
             "readback": {"api": "shipping/shipping-info", "query": {"id": "<shipping_id>"}, "expect": {"status": 1}}},
        ]
        return wf
    scheme = next((s for s in schemes if s["id"] == sid), None)
    if scheme is None:
        wf["status"] = "needs_review"
        wf["blockers"].append({"code": "SHIPPING_ID_NOT_FOUND", "shipping_id": sid})
        return wf
    occupancy = (data.get("occupancy") or {}).get("selected") or []
    if zid is None:
        jp_zones = [z.get("id") for z in scheme.get("zones") or [] if _covers_jp(z)]
        if jp_zones or _occupied_jp(occupancy):
            wf["status"] = "needs_review"
            wf["blockers"].append({"code": "AMBIGUOUS_JP_ZONES", "zones": jp_zones,
                                   "message": "JP/wildcard zones already exist in this scheme; set zone_id explicitly "
                                              "(the tool does not pick one)"})
            return wf
        wf["status"] = "prepared"
        wf["requests"] = [{"step": 1, "api": "shipping/zone-create", "danger": True, "executable": True,
                           "body": dict(zone_body, shipping_id=sid),
                           "precheck": {"api": "shipping/zone-countries", "query": {"shipping_id": sid},
                                        "expect": "JP not occupied within this scheme"},
                           "readback": {"api": "shipping/shipping-info", "query": {"id": sid},
                                        "expect": {"zones": "new JP zone with these price bands"}}}]
    else:
        zone = next((z for z in scheme.get("zones") or [] if as_int(z.get("id")) == zid), None)
        if zone is None:
            wf["status"] = "needs_review"
            wf["blockers"].append({"code": "ZONE_NOT_FOUND", "zone_id": zid})
            return wf
        if _occupied_jp(occupancy):
            wf["status"] = "needs_review"
            wf["blockers"].append({"code": "JP_OCCUPIED_BY_OTHER_ZONE", "occupied": _occupied_jp(occupancy)})
            return wf
        if _jp_only(zone) and not _full_jp(zone, jp_provinces):
            # A regional zone is a scope/fee exception; widening it to all of Japan is never automatic.
            wf["status"] = "needs_review"
            wf["blockers"].append({"code": "REGIONAL_ZONE_SELECTED", "zone": _candidate(scheme, zone),
                                   "message": "selected zone covers only part of Japan; the all-Japan preset is not "
                                              "applied to it (keep it or select/create a full-Japan zone)"})
            return wf
        if _zone_matches(zone, bands, jp_provinces) and as_int(scheme.get("status")) == 1:
            wf["status"] = "config_matches"
            wf["notes"].append("selected zone already matches; real checkout still unverified")
            return wf
        complexity = _complexity(zone)
        if complexity:
            wf["status"] = "needs_review"
            wf["blockers"].append({"code": "COMPLEX_EXISTING_ZONE", "reasons": complexity,
                                   "message": "existing zone is not replaced automatically"})
            return wf
        body = {"id": zid, "shipping_id": sid, "name": zone.get("name"), "type": 2, "countries": countries,
                "weight_based_shipping_rates": [], "price_based_shipping_rates": bands,
                "qty_based_shipping_rates": zone.get("qty_based_shipping_rates") or qty}
        wf["status"] = "prepared"
        wf["requests"] = [{"step": 1, "api": "shipping/zone-update", "danger": True, "executable": True,
                           "body": body,
                           "precheck": {"api": "shipping/zone-countries",
                                        "query": {"shipping_id": sid, "shipping_zone_id": zid},
                                        "expect": "JP not occupied by another zone of this scheme"},
                           "readback": {"api": "shipping/zone-info",
                                        "query": {"shipping_id": sid, "shipping_zone_id": zid},
                                        "expect": {"price_based_shipping_rates": bands, "countries": countries}}}]
    if as_int(scheme.get("status")) != 1:
        wf["requests"].append({"step": len(wf["requests"]) + 1, "api": "shipping/shipping-change-status",
                               "danger": True, "executable": False, "executable_after": "previous read-back verified",
                               "body": {"id": sid, "status": 1},
                               "readback": {"api": "shipping/shipping-info", "query": {"id": sid},
                                            "expect": {"status": 1}}})
    return wf


def tax_workflow(ss, acc):
    tax = ss.get("tax") or {}
    wf = {"area": "tax", "executed": False, "execution": "gated-manager-workflow", "status": None,
          "mode": tax.get("mode"), "current": None, "blockers": [], "requests": [],
          "prices_include_tax": tax.get("prices_include_tax"),
          "notes": ["no tax rate is assumed; this is not tax or legal advice",
                    "prices_include_tax is a business fact to cross-check displayed, Feed and checkout totals; it "
                    "does not mean the tax engine is disabled and no tax is added automatically",
                    "custom tax schemes take priority over the base JP rate for their linked products"]}
    data = acc.get("tax", "data", "backend.tax")
    if data is None:
        wf["status"] = "unknown"
        return wf
    wf["current"] = data
    if tax.get("mode") != "prepare":
        wf["status"] = "preserve"
        return wf
    rate = tax.get("country_rate")
    if rate is None:
        wf["status"] = "needs_data"
        wf["blockers"].append({"code": "TAX_RATE_MISSING", "message": "country_rate must be provided explicitly"})
        return wf
    pool = acc.get("countries", "data", "backend.tax")
    if pool is None:
        wf["status"] = "unknown"
        return wf
    explicit = {r["state_code"]: r["tax"] for r in tax.get("province_rates") or []}
    bad = sorted(set(explicit) - set(pool.get("jp_provinces") or []))
    if bad:
        wf["status"] = "needs_review"
        wf["blockers"].append({"code": "UNKNOWN_PROVINCE", "codes": bad})
        return wf
    if len(data.get("base_jp") or []) > 1:
        wf["status"] = "needs_review"
        wf["blockers"].append({"code": "AMBIGUOUS_JP_BASE", "ids": [b["id"] for b in data["base_jp"]]})
        return wf
    current_rows = (data["base_jp"][0]["rows"] if data.get("base_jp") else [])
    current_prov = {r["state_code"]: r["tax"] for r in current_rows if r["type"] == 2 and r["state_code"]}
    current_country = next((r["tax"] for r in current_rows if r["type"] == 1), None)
    provinces = {} if tax.get("replace_province_rates") else dict(current_prov)
    provinces.update(explicit)
    body = {"country_code": "JP", "country_tax": rate,
            "country_taxs": [{"state_code": k, "tax": provinces[k]} for k in sorted(provinces)]}
    same = (current_country is not None and sem_equal(current_country, rate)
            and set(current_prov) == set(provinces) and all(sem_equal(current_prov[k], provinces[k]) for k in provinces))
    wf["target"] = body
    if same:
        wf["status"] = "config_matches"
        return wf
    wf["status"] = "prepared"
    wf["requests"] = [{"step": 1, "api": "tax/base-save", "danger": True, "executable": True, "body": body,
                       "preserved_province_rates": sorted(set(current_prov) - set(explicit))
                       if not tax.get("replace_province_rates") else [],
                       "readback": {"api": "tax/base-info", "query": {"tax_base_id": "JP id from tax/base-list"},
                                    "expect": {"country_tax": rate, "province_rates": body["country_taxs"]}}}]
    if any(c["jp_rows"] for c in data.get("custom") or []):
        wf["notes"].append("custom tax schemes with JP rows exist and are left unchanged")
    return wf


def currency_rebase_workflow(b, snapshot, acc, ops):
    """Changing the base currency with multi-currency enabled needs every rate rebased on the new base."""
    if "currency" not in ops:
        return None
    op = next((o for o in b.ops if o["op_id"] == ops["currency"]), None)
    if not op or not op.get("write_expected"):
        return None
    pool = (acc.backend.get("currencies") or {})
    addons = set(snapshot.get("addons") or [])
    if pool.get("status") != "read":
        multi, status = None, "verification_needed"
    else:
        codes = (pool.get("data") or {}).get("codes") or []
        multi = "currency" in addons or len(codes) > 1
        status = "required" if multi else None
    if status is None:
        return None
    return {"area": "currency_rebase", "executed": False, "execution": "agent-workflow", "status": status,
            "multi_currency_enabled": multi,
            "current": {"base": op["before"].get("currency"), "enabled_codes": (pool.get("data") or {}).get("codes"),
                        "currency_addon": "currency" in addons},
            "steps": [
                "after backend.currency is applied, read the multi-currency config (manager api-doc currency/get) "
                "and confirm every enabled currency's exchange rate is now expressed against JPY",
                "refresh/rebase the exchange rates through the manager workflow (e.g. currency/get-rate / "
                "currency/set) after authorization; no rate is invented by this tool",
                "verify storefront price display in each enabled currency (browser), then re-inspect"],
            "notes": ["shop-info/currencies: all rates derive from shop_base_currency_code; changing the base "
                      "without rebasing leaves converted prices wrong",
                      "product prices are not repriced automatically"]}


# ------------------------------------------------------------------ plan entry

def plan_backend(b, profile, snapshot, cfg_op_factory):
    """Add backend ops to builder b and return (summary, workflows). No-op for legacy profiles."""
    if not backend_selected(profile):
        reason = ("modules.basic is false" if "store_settings" in profile else
                  "profile has no store_settings (pre-1.1 profile); backend settings stay agent tasks")
        return {"selected": False, "reason": reason}, {}
    ss = profile["store_settings"]
    state = snapshot.get("state") or {}
    if not (state.get("backend") or {}).get("selected"):
        b.block("backend", "STATE_UNKNOWN", "snapshot lacks backend settings; re-run inspect with this profile")
        return {"selected": True, "areas": {}, "requested": ss}, {}
    acc = _Area(b, state)
    target = profile.get("target") or {}
    theme = (snapshot.get("target") or {}).get("theme_identity")
    areas = {a: [] for a in AREAS}

    def cfg_op(op_id, area_key, desired, contract, notes):
        live = acc.backend.get(area_key, {}).get("config") if area_key in ("checkout", "advanced", "tasks", "captcha") \
            else (state.get("country") if area_key == "country" else state.get("basic")).get("data")
        oid = cfg_op_factory(op_id, area_key, live, desired, contract, notes)
        if oid:
            areas["basic" if area_key == "currency" else area_key].append(oid)
        return oid

    ops = _basic_ops(b, cfg_op, acc, profile, ss, target)
    if _country_op(b, cfg_op, acc, ss):
        ops["country"] = "backend.country"
    for area in ("checkout", "tasks", "advanced", "captcha"):
        if _values_op(b, cfg_op, acc, ss, area, theme):
            ops[area] = f"backend.{area}"
    areas["payment"] = _payment_ops(b, b.add, acc, ss)
    workflows = {"shipping": shipping_workflow(profile, ss, acc), "tax": tax_workflow(ss, acc)}
    rebase = currency_rebase_workflow(b, snapshot, acc, ops)
    if rebase:
        workflows["currency_rebase"] = rebase
    # Prerequisite steps that were needed but blocked at plan time: dependents must not run.
    blocked = {}
    for key in ("basic", "currency", "checkout"):
        if key not in ops and any(x["scope"] == f"backend.{key}" for x in b.blockers):
            blocked[key] = f"backend.{key}"
    checkout = (acc.backend.get("checkout") or {})
    pending = None
    if checkout.get("status") == "read":
        values = ((ss.get("checkout") or {}).get("values") or {}) if (ss.get("checkout") or {}).get("apply") else {}
        pending = {"seconds": as_int(values.get("pending_order_cancel_datetime",
                                                (checkout.get("config") or {}).get("pending_order_cancel_datetime"))),
                   "source": "profile" if "pending_order_cancel_datetime" in values else "live"}
    summary = {"selected": True, "requested": ss, "areas": areas, "ops": ops, "pending_timeout": pending,
               "blocked_prerequisites": blocked,
               "shipping_facts": {k: (profile.get("shipping") or {}).get(k) for k in ("base_fee", "free_threshold")},
               "currency_review_required": any(x["code"] == "CURRENCY_CHANGE_NOT_ALLOWED" for x in b.blockers)}
    return summary, workflows


# ------------------------------------------------------------------ verify

OBSERVE = {
    "basic": ("system-settings/basic_get", ("site_status", "site_name", "language", "timezone", "currency")),
    "advanced": ("system-settings/advance_get", None),
    "tasks": ("system-settings/task_get", None),
    "captcha": ("system-settings/captcha_get", ("manager_login",) + CAPTCHA_FRONT + ("length", "str_type", "expire")),
    "checkout": ("system-settings/checkout_get", None),
    "country": ("system-settings/country_get", None),
}


def _observe(ro, area):
    try:
        if area in OBSERVE:
            api_id, keys = OBSERVE[area]
            data = ro.call(api_id) or {}
            return {k: data.get(k) for k in (keys or data.keys())}
        if area == "payment":
            return [{k: p.get(k) for k in ("code", "status", "position")} for p in list_items(ro.call("payment/list"))]
        if area == "shipping":
            return [{"id": s.get("id"), "status": s.get("status")} for s in paginate(ro, "shipping/shipping-list")]
        if area == "tax":
            return [b for b in list_items(ro.call("tax/base-list")) if b.get("country_code") == "JP"]
    except ApiFailure as exc:
        return {"unknown": exc.message}
    return None


class _Reader:
    """Read-only adapter facade with the inspector's .get(api, query) shape."""

    def __init__(self, ro):
        self.ro = ro

    def get(self, api_id, query=None):
        return self.ro.call(api_id, query=query)

    call = get


class _FreshAcc:
    """Fresh verify-time facts for the shipping/tax workflow evaluators (None = unknown)."""

    def __init__(self, data):
        self.data = data

    def get(self, area, key, scope):
        return self.data.get(area)


def fresh_workflows(plan, ro):
    """Re-evaluate shipping/tax against complete fresh reads; a historical plan match is never reused."""
    backend = plan.get("backend") or {}
    ss = backend.get("requested") or {}
    reader = _Reader(ro)
    facts = {}
    for area, fn in (("countries", lambda: _country_pool(ro.call("shop-info/country_list"))),
                     ("shipping", lambda: _read_shipping(reader, ss.get("shipping") or {})),
                     ("tax", lambda: _read_tax(reader))):
        try:
            facts[area] = fn()
        except ApiFailure:
            facts[area] = None
    acc = _FreshAcc(facts)
    profile_like = {"shipping": backend.get("shipping_facts") or {}}
    out = {}
    for name, wf in (("shipping", shipping_workflow(profile_like, ss, acc)), ("tax", tax_workflow(ss, acc))):
        planned = ((plan.get("backend_workflows") or {}).get(name) or {}).get("status")
        out[name] = {"fresh_status": wf.get("status"), "planned_status": planned,
                     "drifted_since_plan": planned == "config_matches" and wf.get("status") != "config_matches",
                     "blockers": wf.get("blockers"), "current": wf.get("current")}
    return out


SETTLED = ("configured", "preserved", "preserve", "config_matches")


def area_report(plan, ro, rows):
    """Per menu area: observed / requested / configured / agent-required / unknown, with fresh evidence.

    Scope blockers always prevent a full 'configured'; partial successes stay visible as evidence.
    Shipping/tax statuses come from a fresh complete re-read, never from the plan's historical match.
    """
    backend = plan.get("backend") or {}
    if not backend.get("selected"):
        return {"selected": False, "reason": backend.get("reason")}
    by_op = {r["op_id"]: r for r in rows}
    requested = backend.get("requested") or {}
    blockers = plan.get("blockers") or []
    fresh = fresh_workflows(plan, ro)
    rebase = (plan.get("backend_workflows") or {}).get("currency_rebase")
    out = {}
    for area in AREAS:
        op_ids = (backend.get("areas") or {}).get(area) or []
        statuses = [by_op[o]["status"] for o in op_ids if o in by_op]
        area_blockers = [b["code"] for b in blockers if b["scope"] == f"backend.{area}"
                         or (area == "basic" and b["scope"] == "backend.currency")]
        entry = {"requested": requested.get(area), "observed": _observe(ro, area), "operations": op_ids,
                 "operation_status": statuses, "blockers": area_blockers}
        if area in ("shipping", "tax"):
            f = fresh[area]
            status = f["fresh_status"]
            entry.update(workflow_status=status, planned_workflow_status=f["planned_status"], executed=False,
                         drifted_since_plan=f["drifted_since_plan"], fresh_blockers=f["blockers"],
                         fresh_current=f["current"])
            if status in ("prepared", "needs_data", "needs_review"):
                entry["status"] = "agent_required"
                entry["evidence"] = ("fresh read differs from the plan's earlier match" if f["drifted_since_plan"]
                                     else "danger-marked manager workflow prepared, not executed by this tool")
            elif status == "config_matches":
                entry["status"] = "config_matches"
                entry["evidence"] = "fresh complete re-read matches; real checkout totals unverified"
            elif status == "preserve":
                entry["status"] = "preserve"
                entry["evidence"] = "preserved by request; fresh current state reported"
            else:
                entry["status"] = "unknown"
                entry["evidence"] = "fresh read failed; not verified"
        elif statuses:
            if "unknown" in statuses:
                entry["status"] = "unknown"
            elif "conflict" in statuses:
                entry["status"] = "conflict"
            elif not all(s == "verified" for s in statuses):
                entry["status"] = "requested_not_configured"
            elif area_blockers:
                entry["status"] = "partially_configured"
                entry["evidence"] = "some requested settings verified by read-back; others blocked (see blockers)"
            else:
                entry["status"] = "configured"
                entry["evidence"] = "read-back of the saved configuration (not a transaction)"
        elif area_blockers:
            entry["status"] = "agent_required"
        elif isinstance(entry["observed"], dict) and "unknown" in entry["observed"]:
            entry["status"] = "unknown"
        else:
            entry["status"] = "preserved"
            if area in ("advanced", "captcha"):
                entry["evidence"] = "intentionally preserved (no explicit values requested)"
        if area == "basic" and rebase and entry["status"] in SETTLED:
            entry["status"] = "partially_configured"
            entry["pending"] = ["currency_rebase"]
            entry["evidence"] = "base currency saved; multi-currency rate rebase/display verification is agent work"
        if area == "payment":
            entry["credentials_proven"] = False
            entry["transaction_verified"] = False
        out[area] = entry
    return {"selected": True, "areas": out}
