"""Profile generation (preset merge) and validation."""

import copy
import re
from datetime import datetime
from urllib.parse import urlsplit

from . import store_settings
from .util import ASSETS_ROOT, JST, LaunchError, as_int, now_jst, read_json, sha256_obj

PROFILE_SCHEMA = "SHOPXZ_JP_PROFILE_v1"
PATCH_SCHEMA = "SHOPXZ_JP_PROFILE_PATCH_v1"
PRESET_FILES = {"jp-core": "profiles/jp-core.json", "jp-apparel": "profiles/jp-apparel.json"}
MODULE_KEYS = ("basic", "japancheckout", "viewed", "fixedcart", "cfsaas", "emails", "marketing")
NAME_ORDERS = ("first_last", "last_first")
HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
PREFIX = re.compile(r"^[A-Za-z0-9]{2,16}$")


def deep_merge(base, patch):
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def build_profile(preset, assets_root=ASSETS_ROOT):
    if preset not in PRESET_FILES:
        raise LaunchError("UNKNOWN_PRESET", f"preset must be one of {sorted(PRESET_FILES)}")
    core = read_json(assets_root / PRESET_FILES["jp-core"])
    if core.get("schema") != PROFILE_SCHEMA:
        raise LaunchError("ASSET_INVALID", "jp-core profile schema mismatch")
    # v1.1: new profiles carry the packaged backend defaults; existing profiles are never upgraded silently.
    core["store_settings"] = deep_merge(store_settings.load_defaults(assets_root), core.get("store_settings") or {})
    if preset == "jp-core":
        return core
    patch = read_json(assets_root / PRESET_FILES[preset])
    if patch.get("schema") != PATCH_SCHEMA or patch.get("extends") != "jp-core":
        raise LaunchError("ASSET_INVALID", f"{preset} must be a {PATCH_SCHEMA} extending jp-core")
    body = {k: v for k, v in patch.items() if k not in ("schema", "extends", "label")}
    merged = deep_merge(core, body)
    merged["schema"] = PROFILE_SCHEMA
    return merged


def profile_hash(profile):
    return sha256_obj(profile)


def load_marketing_preset(name, assets_root=ASSETS_ROOT):
    data = read_json(assets_root / "marketing-presets.json")
    if data.get("schema") != "SHOPXZ_JP_MARKETING_v1":
        raise LaunchError("ASSET_INVALID", "marketing-presets.json schema mismatch")
    preset = (data.get("presets") or {}).get(name)
    if not isinstance(preset, dict):
        raise LaunchError("ASSET_INVALID", f"marketing preset not found: {name}")
    return preset


def validate_marketing_preset(preset):
    errors = []
    coupons = preset.get("coupons") or []
    keys = set()
    for c in coupons:
        key = c.get("key")
        keys.add(key)
        vt, val = c.get("value_type"), c.get("value")
        if vt not in (1, 2) or not isinstance(val, (int, float)) or isinstance(val, bool) or val <= 0:
            errors.append(f"coupon {key}: value_type/value invalid")
        elif vt == 2 and not (0 < val < 100):
            errors.append(f"coupon {key}: percentage must be between 0 and 100")
        mrt, sub = c.get("minimum_requirements_type"), c.get("prerequisite_subtotal")
        if mrt not in (1, 2):
            errors.append(f"coupon {key}: minimum_requirements_type must be 1 or 2")
        elif mrt == 2 and (as_int(sub) is None or as_int(sub) <= 0):
            errors.append(f"coupon {key}: prerequisite_subtotal must be a positive JPY integer")
        elif mrt == 2 and vt == 1 and val >= as_int(sub):
            errors.append(f"coupon {key}: fixed discount must be below its minimum subtotal")
        if not re.fullmatch(r"[A-Za-z0-9]{1,8}", str(c.get("suffix", ""))):
            errors.append(f"coupon {key}: suffix must be ASCII alphanumeric")
        if not isinstance(c.get("public"), bool):
            errors.append(f"coupon {key}: public must be boolean")
    messages = preset.get("messages") or []
    for name, sched in (preset.get("schedules") or {}).items():
        ints = [as_int(x) for x in sched]
        if len(sched) != len(messages) or any(x is None or x <= 0 for x in ints) or ints != sorted(set(ints)):
            errors.append(f"schedule {name}: must be {len(messages)} increasing positive minute values")
    for m in messages:
        if m.get("coupon") is not None and m.get("coupon") not in keys:
            errors.append(f"message {m.get('key')}: unknown coupon {m.get('coupon')}")
    if messages and (messages[0].get("coupon") is not None or not messages[-1].get("end_activity")):
        errors.append("first message must not bind a coupon and last message must end the activity")
    if any(m.get("end_activity") for m in messages[:-1]):
        errors.append("only the last message may end the activity")
    if as_int(preset.get("max_time_days")) is None or as_int(preset.get("max_time_days")) <= 0:
        errors.append("max_time_days must be positive")
    if as_int(preset.get("abandoned_order_interval_minutes")) is None:
        errors.append("abandoned_order_interval_minutes must be an integer")
    if (preset.get("country"), preset.get("language"), preset.get("currency")) != ("JP", "ja", "JPY"):
        errors.append("marketing preset must target JP/ja/JPY")
    pp = preset.get("payment_pending")
    if pp is not None:
        first, lead, min_days = (as_int((pp or {}).get(k)) for k in ("first_minutes", "final_lead_days", "min_deadline_days"))
        if first is None or first <= 0 or lead is None or lead < 0 or min_days is None or min_days <= lead:
            errors.append("payment_pending needs positive first_minutes, final_lead_days >= 0 and min_deadline_days > final_lead_days")
        elif (min_days - lead) * 1440 <= first:
            errors.append("payment_pending: final reminder must come after the first reminder at min_deadline_days")
    return errors


def _https_url(value, field, errors, required=False):
    if value in (None, ""):
        return False
    if not isinstance(value, str):
        errors.append(f"{field} must be a string")
        return False
    parts = urlsplit(value.strip())
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        errors.append(f"{field} must be an https URL with a host and no credentials")
        return False
    if parts.query or parts.fragment:
        errors.append(f"{field} must not contain query or fragment")
        return False
    return True


def _non_negative_int(value, field, errors, allow_null=True):
    if value is None and allow_null:
        return
    iv = as_int(value)
    if iv is None or iv < 0 or isinstance(value, str):
        errors.append(f"{field} must be a non-negative integer (JPY/day units)")


def _day_range(value, field, errors):
    if value is None:
        return
    if isinstance(value, list):
        if len(value) != 2 or any(as_int(v) is None or as_int(v) < 0 or isinstance(v, str) for v in value) \
                or value[0] > value[1]:
            errors.append(f"{field} must be [min, max] non-negative business days")
        return
    _non_negative_int(value, field, errors)


def validate_profile(profile, assets_root=ASSETS_ROOT, now=None):
    """Return {errors, warnings, unresolved, readiness_gaps}. Errors make the profile unusable."""
    now = now or now_jst()
    errors, warnings, unresolved, gaps = [], [], [], []
    if not isinstance(profile, dict) or profile.get("schema") != PROFILE_SCHEMA:
        return {"errors": [f"schema must be {PROFILE_SCHEMA}"], "warnings": [], "unresolved": [], "readiness_gaps": []}

    target = profile.get("target") or {}
    shop_id = target.get("shop_id")
    if shop_id is None:
        unresolved.append("target.shop_id")
    elif isinstance(shop_id, bool) or not isinstance(shop_id, int) or shop_id <= 0:
        errors.append("target.shop_id must be a positive integer or null")
    if not _https_url(target.get("public_url"), "target.public_url", errors):
        if not target.get("public_url"):
            unresolved.append("target.public_url")
    _https_url(target.get("manager_url"), "target.manager_url", errors)

    locale = profile.get("locale") or {}
    if locale.get("language") != "ja" or locale.get("currency") != "JPY":
        errors.append("locale must be single-language ja / JPY (other locales and B2B are unsupported)")
    if locale.get("countries") != ["JP"]:
        errors.append("locale.countries must be exactly [\"JP\"]")
    if locale.get("timezone") != "Asia/Tokyo":
        errors.append("locale.timezone must be Asia/Tokyo")

    modules = profile.get("modules") or {}
    for key in MODULE_KEYS:
        if not isinstance(modules.get(key), bool):
            errors.append(f"modules.{key} must be boolean")
    for key in modules:
        if key not in MODULE_KEYS:
            errors.append(f"modules.{key} is not a supported module")

    brand = profile.get("brand") or {}
    accent = brand.get("accent_color")
    if accent not in (None, "") and not (isinstance(accent, str) and HEX_COLOR.match(accent)):
        warnings.append("brand.accent_color is not a valid hex color; current store colors will be kept")
    for field in ("name", "support_email"):
        if not brand.get(field):
            gaps.append(f"brand.{field}")
    if brand.get("support_email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", str(brand["support_email"])):
        errors.append("brand.support_email is not a valid email address")

    email = profile.get("email") or {}
    order = email.get("name_order")
    if order is None:
        unresolved.append("email.name_order")
    elif order not in NAME_ORDERS:
        errors.append("email.name_order must be first_last, last_first or null")
    if email.get("activation", "preserve") != "preserve":
        errors.append("email.activation only supports 'preserve'")
    # v1 preserves template statuses and the platform's own code expiry; requested changes are
    # rejected explicitly rather than silently ignored.
    for field in ("code_expiry_minutes", "notice_enabled_types"):
        if email.get(field) is not None:
            errors.append(f"email.{field} is not supported by this tool (must be null); "
                          "statuses and code expiry are preserved, change them via the manager workflow")

    mk = profile.get("marketing") or {}
    if modules.get("marketing") is True:
        preset = {}
        try:
            preset = load_marketing_preset(mk.get("preset"), assets_root)
            errors += [f"marketing preset: {e}" for e in validate_marketing_preset(preset)]
            if mk.get("schedule") not in (preset.get("schedules") or {}):
                errors.append(f"marketing.schedule must be one of {sorted(preset.get('schedules') or {})}")
        except LaunchError as exc:
            errors.append(exc.message)
        if not isinstance(mk.get("prefix"), str) or not PREFIX.match(mk.get("prefix") or ""):
            errors.append("marketing.prefix must be 2-16 ASCII letters/digits (e.g. BRAND)")
        for field in ("per_customer_limit", "total_usage_limit"):
            _non_negative_int(mk.get(field), f"marketing.{field}", errors, allow_null=False)
        if as_int(mk.get("per_customer_limit")) == 0:
            warnings.append("marketing.per_customer_limit=0 means unlimited use per customer")
        ends = mk.get("ends_at")
        if ends not in (None, ""):
            try:
                end_dt = datetime.strptime(ends, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
                if end_dt <= now:
                    errors.append("marketing.ends_at must be in the future (Asia/Tokyo)")
            except (TypeError, ValueError):
                errors.append("marketing.ends_at must be '' or 'YYYY-MM-DD HH:MM:SS' (Asia/Tokyo)")
        for field in ("show_percentage_coupons", "allow_campaign_stacking"):
            if not isinstance(mk.get(field), bool):
                errors.append(f"marketing.{field} must be boolean")
        # v1.3: payment-pending reminders, adopting an existing unpaid activity, mapping to existing coupons
        deadline = mk.get("payment_deadline_days")
        if deadline is not None:
            pp = preset.get("payment_pending") or {}
            min_days = as_int(pp.get("min_deadline_days")) or 3
            if not isinstance(deadline, int) or isinstance(deadline, bool) or not min_days <= deadline <= 60:
                errors.append(f"marketing.payment_deadline_days must be null or an integer between {min_days} and 60")
        adopt = mk.get("adopt_activity_id")
        if adopt is not None and (not isinstance(adopt, int) or isinstance(adopt, bool) or adopt <= 0):
            errors.append("marketing.adopt_activity_id must be null or a positive activity id")
        codes = mk.get("coupon_codes", {})
        if not isinstance(codes, dict):
            errors.append("marketing.coupon_codes must be an object {coupon key: existing code}")
        else:
            known = {c.get("key") for c in (preset.get("coupons") or [])}
            for key, code in codes.items():
                if key not in known:
                    errors.append(f"marketing.coupon_codes: unknown coupon key {key} (preset keys: {sorted(known)})")
                elif not isinstance(code, str) or not code.strip() or len(code) > 64:
                    errors.append(f"marketing.coupon_codes.{key} must be an existing coupon code string")

    if "store_settings" in profile:
        errors += store_settings.validate(profile.get("store_settings"))
    for field in ("logo_path", "favicon_path"):
        if brand.get(field) not in (None, "") and not isinstance(brand.get(field), str):
            errors.append(f"brand.{field} must be a string path")

    shipping = profile.get("shipping") or {}
    _non_negative_int(shipping.get("base_fee"), "shipping.base_fee", errors)
    _non_negative_int(shipping.get("free_threshold"), "shipping.free_threshold", errors)
    _day_range(shipping.get("handling_business_days"), "shipping.handling_business_days", errors)
    _day_range(shipping.get("transit_business_days"), "shipping.transit_business_days", errors)
    for field in ("base_fee", "free_threshold", "handling_business_days", "transit_business_days"):
        if shipping.get(field) is None:
            gaps.append(f"shipping.{field}")

    returns = profile.get("returns") or {}
    if returns.get("window_days") is not None:
        _non_negative_int(returns.get("window_days"), "returns.window_days", errors)
    for field in ("window_days", "eligibility", "procedure", "return_address", "return_shipping_payer", "refund_timing"):
        if returns.get(field) in (None, ""):
            gaps.append(f"returns.{field}")
    business = profile.get("business") or {}
    for field in ("legal_name", "representative", "address", "customer_service_hours"):
        if not business.get(field):
            gaps.append(f"business.{field}")
    if brand.get("phone") in (None, ""):
        gaps.append("brand.phone")
    return {"errors": errors, "warnings": warnings, "unresolved": unresolved, "readiness_gaps": gaps}


def valid_accent(profile):
    accent = (profile.get("brand") or {}).get("accent_color")
    return accent if isinstance(accent, str) and HEX_COLOR.match(accent) else None
