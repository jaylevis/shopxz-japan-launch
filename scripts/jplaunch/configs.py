"""Desired complete-object configs for japancheckout / prodviewed / fixedcart / emailrecall."""

import copy
import re

from .util import as_int

HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

CONFIG_APIS = {
    "japancheckout": {"read": "japancheckout/get", "write": "japancheckout/set"},
    "viewed": {"read": "prodviewed/get", "write": "prodviewed/set"},
    "fixedcart": {"read": "fixedcart/get", "write": "fixedcart/set"},
    "recall": {"read": "emailrecall/get", "write": "emailrecall/set"},
    # v1.1 store settings menu
    "basic": {"read": "system-settings/basic_get", "write": "system-settings/basic_set"},
    "currency": {"read": "system-settings/basic_get", "write": "system-settings/basic_set_currency"},
    "country": {"read": "system-settings/country_get", "write": "system-settings/country_set"},
    "checkout": {"read": "system-settings/checkout_get", "write": "system-settings/checkout_set"},
    "tasks": {"read": "system-settings/task_get", "write": "system-settings/task_set"},
    "advanced": {"read": "system-settings/advance_get", "write": "system-settings/advance_set"},
    "captcha": {"read": "system-settings/captcha_get", "write": "system-settings/captcha_set"},
}
# Documented request keys; anything else returned by GET is reported, never silently dropped.
WRITE_KEYS = {
    "japancheckout/set": ("status", "condition"),
    "prodviewed/set": ("status", "view_type", "auto_play", "button_type", "image_type", "clear_all",
                       "clear_all_text", "clear_all_popup_text", "show_type", "show_pages", "show_position",
                       "diy_config"),
    "fixedcart/set": ("status", "client_type", "button_text", "click_type", "color_type", "color_info",
                      "pc_type", "h5_type", "diy_view"),
    "emailrecall/set": ("status", "system_recall", "sms_product_title_length"),
    "system-settings/basic_set": ("site_status", "site_name", "site_logo", "site_ico", "site_email", "language",
                                  "timezone", "order_guest_query", "order_number_prefix", "order_number_suffix",
                                  "handle_type"),
    "system-settings/basic_set_currency": ("currency",),
    "system-settings/country_set": ("status", "countrys", "sorts"),
    "system-settings/checkout_set": ("login_account", "deduct_stock", "product_status_check", "checkout_page_type",
                                     "coupon_show_type", "pending_order_cancel_datetime", "address_company",
                                     "contact_type", "name_type", "name_require_type", "address_zip",
                                     "customer_can_cancel_order", "address_street_2"),
    "system-settings/task_set": ("reflush_cache_type",),
    "system-settings/advance_set": ("product_search_type", "product_search_like_type", "download_file_type",
                                    "referrerpolicy", "liquid_debug"),
    "system-settings/captcha_set": ("manager_login", "front_register_account", "front_contacts", "product_review",
                                    "b2b_inquire", "b2b_inquire_sheet", "b2c_inquire", "length", "str_type", "expire",
                                    "bg_color", "font_color", "vert_color", "text_position", "text_angle"),
}
# Fields a GET returns that the paired set must never receive (documented elsewhere-managed values).
READ_ONLY_FIELDS = {"system-settings/basic_set": {"currency"},
                    "system-settings/basic_set_currency": None}  # None: single-field write, ignore others
# Fields each config op asserts (verification contract), independent of whether this run changes them.
CONTRACT_PATHS = {
    "japancheckout/set": [("status",), ("condition",)],
    "prodviewed/set": [("status",), ("clear_all_text", "name"), ("clear_all_popup_text", "name"),
                       ("diy_config", "buttonName", "name")],
    "fixedcart/set": [("status",), ("client_type",), ("click_type",), ("pc_type",), ("h5_type",),
                      ("button_text", "name")],
    "emailrecall/set": [("status",), ("system_recall",)],
}
# Complete-object saves: omitted keys are reset, so unknown live keys would be lost.
RESETS_OMITTED = {"fixedcart/set", "system-settings/basic_set", "system-settings/country_set",
                  "system-settings/checkout_set", "system-settings/task_set", "system-settings/advance_set",
                  "system-settings/captcha_set"}

VIEWED_DEFAULTS = {"view_type": 2, "auto_play": 1, "button_type": 2, "image_type": 2, "clear_all": 2,
                   "show_type": 1, "show_pages": [], "show_position": 1}
VIEWED_RANGES = {"view_type": (1, 2), "auto_play": (1, 2), "button_type": (1, 2, 3), "image_type": (1, 2, 3, 4, 5),
                 "clear_all": (1, 2), "show_type": (1, 2), "show_position": (1, 2, 3, 4, 5, 6)}
VIEWED_CAPTIONS = {"buttonName": "最近チェックした商品", "clear_all_text": "履歴をすべて削除",
                   "clear_all_popup_text": "閲覧履歴をすべて削除しますか？"}
FIXEDCART_BUTTON = "カートに入れる"
# Documented fixedcart example colours; only used where the live value is missing/invalid.
FIXEDCART_COLOR_DEFAULTS = {"background": "#ffffff", "button_background": "#FF5A00", "button_text": "#ffffff",
                            "price": "#f5222d", "original_price": "#9ca0b0", "border_color": "#d9d9d9",
                            "attribute_color": "#939393", "other_text": "#212b36"}


def submit_view(live, write_api):
    return {k: copy.deepcopy(live.get(k)) for k in WRITE_KEYS[write_api] if k in live}


def unknown_keys(live, write_api):
    ignored = READ_ONLY_FIELDS.get(write_api, set())
    if ignored is None:
        return []
    return sorted(k for k in (live or {}) if k not in WRITE_KEYS[write_api] and k not in ignored)


def _localized(current, text):
    """Set the default caption; update an existing 'ja' translation so it cannot override with stale text."""
    cur = current if isinstance(current, dict) else {}
    language = cur.get("language") if isinstance(cur.get("language"), dict) else {}
    language = dict(language)
    if "ja" in language:
        language["ja"] = text
    return {"name": text, "language": language}


def _contrast(hex_color):
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return "#000000" if (0.299 * r + 0.587 * g + 0.114 * b) > 160 else "#FFFFFF"


def japancheckout_desired(live):
    live = live or {}
    # Default trigger: condition 1 = Japanese storefront language only (currency is not part of this
    # app's trigger). 4 = forced is an existing merchant choice that already covers it and is preserved;
    # 2 (JPY only) and 3 (JPY and ja) are moved to 1 by a new plan.
    if as_int(live.get("status")) == 1 and as_int(live.get("condition")) in (1, 4):
        return {"status": live.get("status"), "condition": live.get("condition")}, ["already enabled for Japanese language"]
    return {"status": 1, "condition": 1}, []


def viewed_desired(live, accent):
    live = live or {}
    out = submit_view(live, "prodviewed/set")
    notes = []
    out["status"] = 1
    for key, default in VIEWED_DEFAULTS.items():
        if key == "show_pages":
            continue
        if as_int(live.get(key)) not in VIEWED_RANGES[key]:
            out[key] = default
            notes.append(f"{key}: invalid/missing, documented default {default}")
    pages = live.get("show_pages")
    if as_int(out.get("show_type")) == 2 and not (isinstance(pages, list) and pages):
        out["show_type"], out["show_pages"] = 1, []
        notes.append("show_type=2 without pages; using all pages")
    else:
        out["show_pages"] = pages if isinstance(pages, list) else []
    out["clear_all_text"] = _localized(live.get("clear_all_text"), VIEWED_CAPTIONS["clear_all_text"])
    out["clear_all_popup_text"] = _localized(live.get("clear_all_popup_text"), VIEWED_CAPTIONS["clear_all_popup_text"])
    diy = dict(live.get("diy_config")) if isinstance(live.get("diy_config"), dict) else {}
    diy["buttonName"] = _localized(diy.get("buttonName"), VIEWED_CAPTIONS["buttonName"])
    if accent:
        diy["buttonBg"], diy["clearColor"], diy["buttonColor"] = accent, accent, _contrast(accent)
    for key in ("buttonBg", "buttonColor", "clearColor"):
        if not (isinstance(diy.get(key), str) and HEX.match(diy[key])):
            diy[key] = {"buttonBg": "#202020", "buttonColor": "#FFFFFF", "clearColor": "#202020"}[key]
            notes.append(f"diy_config.{key}: invalid/missing, documented example colour")
    out["diy_config"] = diy
    return out, notes


def fixedcart_desired(live):
    live = live or {}
    out = submit_view(live, "fixedcart/set")
    notes = []
    out.update({"status": 1, "client_type": 1, "click_type": 1, "pc_type": 1, "h5_type": 1})
    out["button_text"] = _localized(live.get("button_text"), FIXEDCART_BUTTON)
    if as_int(live.get("color_type")) not in (1, 2):
        out["color_type"] = 1
        notes.append("color_type invalid/missing; default colours")
    colors = dict(live.get("color_info")) if isinstance(live.get("color_info"), dict) else {}
    for key, default in FIXEDCART_COLOR_DEFAULTS.items():
        if not (isinstance(colors.get(key), str) and HEX.match(colors[key])):
            colors[key] = default
            notes.append(f"color_info.{key}: invalid/missing, documented example colour")
    out["color_info"] = colors
    diy = live.get("diy_view")
    out["diy_view"] = diy if isinstance(diy, dict) else {}
    return out, notes


def recall_desired(live):
    live = live or {}
    length = as_int(live.get("sms_product_title_length"))
    return {"status": 1, "system_recall": 2,
            "sms_product_title_length": length if length is not None and 0 < length <= 40 else 40}
