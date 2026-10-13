"""`store_settings` profile section (v1.1): packaged defaults loader and strict validation.

Defaults live in assets/store-settings.json; this module only knows the documented field
ranges so that unknown keys or out-of-range values are rejected instead of reaching an API.
"""

import re

from .util import ASSETS_ROOT, LaunchError, read_json

ASSET_SCHEMA = "SHOPXZ_JP_STORE_SETTINGS_v1"
HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
TOGGLE = (1, 2)

CHECKOUT_ENUMS = {
    "login_account": TOGGLE, "deduct_stock": TOGGLE, "product_status_check": TOGGLE,
    "checkout_page_type": TOGGLE, "coupon_show_type": (1, 2, 3, 4), "address_company": (1, 2, 3),
    "contact_type": (1, 2, 3, 4, 5, 6), "name_type": TOGGLE, "name_require_type": TOGGLE,
    "address_zip": (1, 2, 3), "customer_can_cancel_order": TOGGLE, "address_street_2": (1, 2, 3),
}
CHECKOUT_INTS = {"pending_order_cancel_datetime"}  # seconds, 0 = unlimited
VUE_ONLY_CHECKOUT = ("checkout_page_type", "coupon_show_type")
ADVANCED_ENUMS = {"product_search_type": TOGGLE, "product_search_like_type": TOGGLE, "download_file_type": TOGGLE,
                  "referrerpolicy": TOGGLE, "liquid_debug": TOGGLE}
TASK_ENUMS = {"reflush_cache_type": (1, 2, 3)}
CAPTCHA_TOGGLES = ("manager_login", "front_register_account", "front_contacts", "product_review",
                   "b2b_inquire", "b2b_inquire_sheet", "b2c_inquire")
CAPTCHA_FRONT = tuple(k for k in CAPTCHA_TOGGLES if k != "manager_login")
CAPTCHA_ENUMS = dict({k: TOGGLE for k in CAPTCHA_TOGGLES}, length=(4, 5, 6), str_type=(1, 2, 3),
                     text_position=("random", "center"), text_angle=("rotate", "center"))
CAPTCHA_COLORS = ("bg_color", "font_color", "vert_color")
SECTIONS = ("basic", "checkout", "country", "advanced", "tasks", "captcha", "payment", "shipping", "tax")
PAYMENT_CODE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def load_defaults(assets_root=ASSETS_ROOT):
    data = read_json(assets_root / "store-settings.json")
    if data.get("schema") != ASSET_SCHEMA:
        raise LaunchError("ASSET_INVALID", f"store-settings.json must be {ASSET_SCHEMA}")
    return {k: v for k, v in data.items() if k != "schema"}


def _keys(section, value, allowed, errors):
    prefix = f"store_settings.{section}" if section else "store_settings"
    if not isinstance(value, dict):
        errors.append(f"{prefix} must be an object")
        return False
    for key in value:
        if key not in allowed:
            errors.append(f"{prefix}.{key} is not a supported field")
    return True


def _bool(section, value, key, errors, required=True):
    if key not in value and not required:
        return
    if not isinstance(value.get(key), bool):
        errors.append(f"store_settings.{section}.{key} must be boolean")


def _int_value(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _enum_values(section, values, enums, ints, errors, checked_elsewhere=()):
    if not isinstance(values, dict):
        errors.append(f"store_settings.{section}.values must be an object")
        return
    for key, v in values.items():
        if key in checked_elsewhere:
            continue
        if key in enums:
            ok = (v in enums[key]) and (isinstance(v, str) or _int_value(v) is not None)
            if not ok:
                errors.append(f"store_settings.{section}.values.{key} must be one of {list(enums[key])}")
        elif key in ints:
            if _int_value(v) is None or v < 0:
                errors.append(f"store_settings.{section}.values.{key} must be a non-negative integer")
        else:
            errors.append(f"store_settings.{section}.values.{key} is not a supported field")


def _applied_section(ss, name, enums, ints, errors, checked_elsewhere=()):
    sec = ss.get(name)
    if sec is None:
        return
    if _keys(name, sec, ("apply", "values"), errors):
        _bool(name, sec, "apply", errors)
        _enum_values(name, sec.get("values", {}), enums, ints, errors, checked_elsewhere)


def validate(ss):
    """Return a list of errors for a store_settings object (None means section absent: legacy profile)."""
    errors = []
    if ss is None:
        return errors
    if not _keys("", ss, SECTIONS, errors):
        return errors
    basic = ss.get("basic")
    if basic is not None and _keys("basic", basic, ("apply", "allow_currency_change"), errors):
        _bool("basic", basic, "apply", errors)
        _bool("basic", basic, "allow_currency_change", errors)
    country = ss.get("country")
    if country is not None and _keys("country", country, ("apply",), errors):
        _bool("country", country, "apply", errors)
    _applied_section(ss, "checkout", CHECKOUT_ENUMS, CHECKOUT_INTS, errors)
    _applied_section(ss, "advanced", ADVANCED_ENUMS, set(), errors)
    _applied_section(ss, "tasks", TASK_ENUMS, set(), errors)
    _applied_section(ss, "captcha", CAPTCHA_ENUMS, {"expire"}, errors, CAPTCHA_COLORS)
    captcha = ss.get("captcha")
    captcha_values = captcha.get("values") if isinstance(captcha, dict) else None
    if isinstance(captcha_values, dict):
        for key in CAPTCHA_COLORS:
            if key in captcha_values:
                v = captcha_values[key]
                if not (v == "random" or (isinstance(v, str) and HEX.match(v))):
                    errors.append(f"store_settings.captcha.values.{key} must be 'random' or a hex colour")
        if "expire" in captcha_values and (_int_value(captcha_values["expire"]) or 0) <= 0:
            errors.append("store_settings.captcha.values.expire must be a positive number of seconds "
                          "(image CAPTCHA expiry; not the guest email code expiry)")
    payment = ss.get("payment")
    if payment is not None and _keys("payment", payment, ("enable_codes", "credentials_verified_codes",
                                                          "preferred_code"), errors):
        for key in ("enable_codes", "credentials_verified_codes"):
            codes = payment.get(key, [])
            if not isinstance(codes, list) or not all(isinstance(c, str) and PAYMENT_CODE.match(c) for c in codes):
                errors.append(f"store_settings.payment.{key} must be a list of payment codes")
        pref = payment.get("preferred_code")
        if pref is not None and not (isinstance(pref, str) and PAYMENT_CODE.match(pref)):
            errors.append("store_settings.payment.preferred_code must be a payment code or null")
    shipping = ss.get("shipping")
    if shipping is not None and _keys("shipping", shipping, ("mode", "shipping_id", "zone_id", "name", "zone_name",
                                                             "max_order_amount"), errors):
        if shipping.get("mode") not in ("prepare", "preserve"):
            errors.append("store_settings.shipping.mode must be 'prepare' or 'preserve'")
        for key in ("shipping_id", "zone_id"):
            v = shipping.get(key)
            if v is not None and (_int_value(v) is None or v <= 0):
                errors.append(f"store_settings.shipping.{key} must be a positive integer or null")
        if shipping.get("zone_id") is not None and shipping.get("shipping_id") is None:
            errors.append("store_settings.shipping.zone_id requires shipping_id")
        for key in ("name", "zone_name"):
            if not isinstance(shipping.get(key), str) or not shipping.get(key).strip():
                errors.append(f"store_settings.shipping.{key} must be a non-empty string")
        if _int_value(shipping.get("max_order_amount")) is None or shipping["max_order_amount"] <= 0:
            errors.append("store_settings.shipping.max_order_amount must be a positive integer (exclusive end)")
    tax = ss.get("tax")
    if tax is not None and _keys("tax", tax, ("mode", "prices_include_tax", "country_rate", "province_rates",
                                              "replace_province_rates"), errors):
        if tax.get("mode") not in ("preserve", "prepare"):
            errors.append("store_settings.tax.mode must be 'preserve' or 'prepare'")
        # identity checks: JSON 0/1 must not pass as false/true through Python equality
        if not any(tax.get("prices_include_tax") is v for v in (None, True, False)):
            errors.append("store_settings.tax.prices_include_tax must be true, false or null")
        rate = tax.get("country_rate")
        if rate is not None and (isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 100):
            errors.append("store_settings.tax.country_rate must be a number between 0 and 100 or null")
        rows = tax.get("province_rates", [])
        if not isinstance(rows, list) or not all(
                isinstance(r, dict) and set(r) == {"state_code", "tax"} and isinstance(r["state_code"], str)
                and not isinstance(r["tax"], bool) and isinstance(r["tax"], (int, float)) and 0 <= r["tax"] <= 100
                for r in rows):
            errors.append("store_settings.tax.province_rates must be [{state_code, tax}] with 0..100 rates")
        else:
            seen = {}
            for r in rows:
                if r["state_code"] in seen and seen[r["state_code"]] != r["tax"]:
                    errors.append(f"store_settings.tax.province_rates has conflicting duplicate rows for "
                                  f"{r['state_code']!r} ({seen[r['state_code']]} vs {r['tax']})")
                seen.setdefault(r["state_code"], r["tax"])
        if not any(tax.get("replace_province_rates") is v for v in (None, True, False)):
            errors.append("store_settings.tax.replace_province_rates must be boolean")
        if tax.get("mode") == "preserve" and (rate is not None or rows):
            errors.append("store_settings.tax explicit rates require mode 'prepare' (no rate is assumed otherwise)")
    return errors


def backend_selected(profile):
    """The v1.1 backend group runs only for profiles that carry store_settings and select modules.basic."""
    return bool((profile.get("modules") or {}).get("basic")) and isinstance(profile.get("store_settings"), dict)
