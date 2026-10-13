"""Desired marketing resources (coupons, recall templates, activity, messages) and exact-match rules.

All financial terms come from the profile + marketing preset; nothing here invents discounts.
"""

from datetime import datetime

from .assets import PENDING_TEMPLATE_KEYS, apply_name_order, build_recall_template
from .profile import PREFIX, load_marketing_preset
from .util import JST, as_int, jst_text, sem_equal

TOOL_NOTE = "shopxz-japan-launch"
# Documented coupon/create example palette; copywriting is mandatory for the API.
COUPON_COPYWRITING = {
    "color": "blue", "timeType": 1,
    "backgroundLeft": "rgba(255,72,72,100)", "backgroundRight": "rgba(255,13,125,100)",
    "fontTip": "rgba(255,255,255,100)", "timeBg": "rgba(255,243,203,100)", "timeFont": "rgba(0,0,0,100)",
    "couponBg": "rgba(255,248,227,100)", "couponFont": "rgba(233,5,73,100)",
    "couponBtLeft": "rgba(255,139,2,100)", "couponBtRight": "rgba(255,109,4,100)",
    "couponBtFont": "rgba(255,255,255,100)", "copyColor": "rgba(255,109,4,100)",
}
COUPON_CONFLICT_FIELDS = ("value_type", "value", "minimum_requirements_type", "prerequisite_subtotal",
                          "product_selection_type", "entitled_product_ids", "entitled_collection_ids",
                          "usage_limit", "usage_count_per_customer", "ends_at", "is_campaign",
                          "status", "is_product_page_show")
ACTIVITY_FIELDS = ("name", "position", "c_country_code", "c_language_code", "c_currency_code",
                   "c_order_amount_min", "c_order_amount_max", "c_order_history_amount_min",
                   "c_order_history_amount_max", "c_order_history_count_min", "c_order_history_count_max",
                   "c_order_history_first_create_begin", "c_order_history_first_create_end",
                   "c_order_history_last_create_begin", "c_order_history_last_create_end",
                   "c_throw_order_time_interval", "payment_status", "is_end_pending_order",
                   "c_order_product_type", "c_order_product_ids", "max_time")
ACTIVITY_SUBMIT_FIELDS = ("id", "status") + ACTIVITY_FIELDS
MESSAGE_FIELDS = ("name", "activities_id", "c_date_time_type", "c_date_time", "c_activities_email_opened",
                  "type", "template_id", "is_send_end_activities", "coupon_code", "discount")
TEMPLATE_FIELDS = ("type", "name", "email_subject", "email_body_html")
_ZERO_EMPTY = {f for f in ACTIVITY_FIELDS if f.startswith("c_order_") and f != "c_order_product_type"
               and f != "c_order_product_ids"} | {"discount"}
# fecify-site-manager-v6 validates these as number/int: "no limit" is submitted as 0, never "".
NUMERIC_ZERO_FIELDS = {"c_order_amount_min", "c_order_amount_max", "c_order_history_amount_min",
                       "c_order_history_amount_max", "c_order_history_count_min", "c_order_history_count_max"}


def unlimited_value(field):
    return 0 if field in NUMERIC_ZERO_FIELDS else ""


def _zero_empty(value):
    return "" if value in (None, "", 0, "0", "0.00", "0.0") else value


def _ends_at(value):
    """Coupon ends_at: '' on write, 0 on read when unlimited; epoch seconds are normalised to JST text."""
    if value in (None, "", 0, "0"):
        return ""
    iv = as_int(value)
    if iv is not None and iv > 100000:
        return jst_text(datetime.fromtimestamp(iv, JST))
    return str(value)


def preset_for(profile):
    return load_marketing_preset((profile.get("marketing") or {}).get("preset"))


def referenced_coupon_keys(preset):
    return [m["coupon"] for m in preset.get("messages", []) if m.get("coupon")]


def coupon_code_map(profile, preset=None):
    """key -> code for every coupon a message uses: an existing mapped code, else prefix+suffix."""
    mk = profile.get("marketing") or {}
    preset = preset or preset_for(profile)
    prefix = mk.get("prefix") or ""
    mapped = mk.get("coupon_codes") or {}
    by_key = {c["key"]: c for c in preset.get("coupons", [])}
    out = {}
    for key in referenced_coupon_keys(preset):
        if key in mapped:
            out[key] = str(mapped[key])
        elif PREFIX.match(prefix) and key in by_key:
            out[key] = prefix + str(by_key[key]["suffix"])
    return out


def coupon_codes(profile):
    try:
        return list(coupon_code_map(profile).values())
    except Exception:  # profile not yet valid: inspect still reads the coupon list
        return []


def yen(amount):
    return f"{int(amount):,}円"


def discount_text(c):
    return f"{int(c['value']) if float(c['value']).is_integer() else c['value']}%OFF" if c["value_type"] == 2 \
        else f"{yen(c['value'])}OFF"


def coupon_terms(c, code, mk):
    lines = [f"クーポンコード「{code}」：商品小計から{discount_text(c)}（全商品対象）。"]
    if c["minimum_requirements_type"] == 2:
        lines.append(f"商品小計{yen(c['prerequisite_subtotal'])}以上のご注文でご利用いただけます。")
    else:
        lines.append("最低購入金額の条件はありません。")
    per = as_int(mk.get("per_customer_limit")) or 0
    lines.append(f"お一人様{per}回までご利用いただけます。" if per else "お一人様あたりの利用回数に制限はありません。")
    total = as_int(mk.get("total_usage_limit")) or 0
    if total:
        lines.append(f"先着{total}回のご利用で終了します。")
    if mk.get("ends_at"):
        end = datetime.strptime(mk["ends_at"], "%Y-%m-%d %H:%M:%S")
        lines.append(f"有効期限：{end.year}年{end.month}月{end.day}日 {end:%H:%M}（日本時間）まで。")
    else:
        lines.append("有効期限の設定はありません。")
    # is_campaign governs stacking with campaign discounts only, not with other coupons.
    lines.append("他のキャンペーン割引との併用が可能です。" if mk.get("allow_campaign_stacking")
                 else "他のキャンペーン割引との併用はできません。")
    return "\n".join(lines)


def desired_coupons(profile, preset, starts_at):
    mk = profile["marketing"]
    prefix = mk["prefix"]
    out = []
    wanted = set(referenced_coupon_keys(preset)) - set(mk.get("coupon_codes") or {})
    for c in preset["coupons"]:
        if c["key"] not in wanted:
            continue
        code = prefix + str(c["suffix"])
        display = bool(c.get("public")) and (c["value_type"] != 2 or bool(mk.get("show_percentage_coupons")))
        out.append({"key": c["key"], "code": code, "body": {
            "title": f"{code} カート復帰 {discount_text(c)}",
            "code": code,
            "status": 1,
            "is_campaign": 1 if mk.get("allow_campaign_stacking") else 2,
            "starts_at": starts_at,
            "ends_at": mk.get("ends_at") or "",
            "value_type": c["value_type"],
            "value": c["value"],
            "usage_limit": as_int(mk.get("total_usage_limit")) or 0,
            "usage_count_per_customer": as_int(mk.get("per_customer_limit")) or 0,
            "product_selection_type": 1,
            "entitled_product_ids": [],
            "entitled_collection_ids": [],
            "minimum_requirements_type": c["minimum_requirements_type"],
            "prerequisite_subtotal": c["prerequisite_subtotal"] if c["minimum_requirements_type"] == 2 else "",
            "prerequisite_quantity": "",
            "product_sort": 1,
            "banner_image": "",
            "banner_show_type": 1,
            "is_product_page_show": 1 if display else 2,
            "product_image_height_type": "auto",
            "copywriting": dict(COUPON_COPYWRITING),
        }})
    return out


def coupon_differences(live, desired):
    diffs = []
    for field in COUPON_CONFLICT_FIELDS:
        a, b = live.get(field), desired.get(field)
        if field == "ends_at":
            a, b = _ends_at(a), _ends_at(b)
        if field == "prerequisite_subtotal" and desired.get("minimum_requirements_type") != 2:
            continue
        if not sem_equal(a, b):
            diffs.append({"field": field, "live": live.get(field), "desired": desired.get(field)})
    if str(live.get("code", "")) != desired["code"]:
        diffs.append({"field": "code", "live": live.get("code"), "desired": desired["code"]})
    if as_int(live.get("activity_status")) == 3:
        diffs.append({"field": "activity_status", "live": 3, "desired": "active"})
    return diffs


def template_name(profile, key):
    prefix = profile["marketing"]["prefix"]
    return f"{prefix}-JP-PAY-{key[-1]}" if key in PENDING_TEMPLATE_KEYS else f"{prefix}-JP-RECALL-{key.upper()}"


def desired_templates(profile, preset, layouts, name_order, pending=False):
    keys = [m["template"] for m in preset["messages"]] + (list(PENDING_TEMPLATE_KEYS) if pending else [])
    out = []
    for key in dict.fromkeys(keys):
        subject, body = build_recall_template(key, layouts)
        out.append({"key": key, "body": {
            "id": 0, "type": 1, "name": template_name(profile, key), "note": TOOL_NOTE,
            "email_subject": apply_name_order(subject, name_order),
            "email_body_html": apply_name_order(body, name_order),
        }})
    return out


def template_differences(live, desired):
    return [f for f in TEMPLATE_FIELDS if str(live.get(f, "")) != str(desired.get(f, ""))
            and not (f == "type" and sem_equal(live.get(f), desired.get(f)))]


def desired_activity(profile, preset):
    mk = profile["marketing"]
    body = {"status": 2, "position": 1, "name": f"{mk['prefix']}-JP-RECALL",
            "c_country_code": preset["country"], "c_language_code": preset["language"],
            "c_currency_code": preset["currency"],
            "c_throw_order_time_interval": as_int(preset["abandoned_order_interval_minutes"]),
            "payment_status": 1, "is_end_pending_order": 1,
            "c_order_product_type": 3, "c_order_product_ids": [],
            "max_time": as_int(preset["max_time_days"])}
    for field in _ZERO_EMPTY - {"discount"}:
        body[field] = unlimited_value(field)
    return body


def activity_differences(live, desired, include_status=False):
    fields = ACTIVITY_FIELDS + (("status",) if include_status else ())
    diffs = []
    for f in fields:
        a, b = live.get(f), desired.get(f)
        if f in _ZERO_EMPTY:
            a, b = _zero_empty(a), _zero_empty(b)
        if not sem_equal(a, b):
            diffs.append({"field": f, "live": live.get(f), "desired": desired.get(f)})
    return diffs


def activity_overlaps_jp(act):
    """An enabled unpaid-order campaign that could also match JP/ja/JPY orders."""
    return (as_int(act.get("status")) == 1 and as_int(act.get("payment_status")) == 1
            and act.get("c_country_code") in ("", None, "JP")
            and act.get("c_language_code") in ("", None, "ja")
            and act.get("c_currency_code") in ("", None, "JPY"))


def _num(value):
    f = float(value)
    return int(f) if f.is_integer() else f


def coupon_discount_values(preset, live_details=None):
    """Message `discount` (a number under the v6 schema): the coupon value - live terms for mapped coupons."""
    values = {c["key"]: _num(c["value"]) for c in preset.get("coupons", [])}
    for key, d in (live_details or {}).items():
        try:
            values[key] = _num(d.get("value"))
        except (TypeError, ValueError):
            pass
    return values


def desired_messages(profile, preset, discounts=None):
    mk = profile["marketing"]
    schedule = preset["schedules"][mk["schedule"]]
    codes = coupon_code_map(profile, preset)
    discounts = discounts or coupon_discount_values(preset)
    out = []
    for idx, msg in enumerate(preset["messages"], start=1):
        key = msg.get("coupon")
        out.append({"key": msg["key"], "body": {
            "name": f"{mk['prefix']}-JP-RECALL-{idx}-{msg['key'].upper()}",
            "activities_id": {"$ref": "marketing.activity"},
            "c_date_time_type": 1,
            "c_date_time": as_int(schedule[idx - 1]),
            "c_activities_email_opened": 3,
            "type": 1,
            "template_id": {"$ref": f"marketing.template.{msg['template']}"},
            "is_send_end_activities": 1 if msg.get("end_activity") else 2,
            "coupon_code": codes.get(key, "") if key else "",
            "discount": discounts.get(key, 0) if key else 0,
        }})
    return out


def pending_schedule(preset, deadline_days):
    pp = preset.get("payment_pending") or {}
    first = as_int(pp.get("first_minutes"))
    final = (as_int(deadline_days) - as_int(pp.get("final_lead_days"))) * 1440
    return [first, final]


def desired_pending_activity(profile, preset):
    mk = profile["marketing"]
    body = desired_activity(profile, preset)
    body.update({"name": f"{mk['prefix']}-JP-PAYMENT", "position": 2, "payment_status": 2, "is_end_pending_order": 1,
                 "max_time": as_int(mk["payment_deadline_days"]) + 1})
    return body


def desired_pending_messages(profile, preset):
    mk = profile["marketing"]
    out = []
    for idx, (key, minutes) in enumerate(zip(PENDING_TEMPLATE_KEYS, pending_schedule(preset, mk["payment_deadline_days"])), start=1):
        out.append({"key": key, "body": {
            "name": f"{mk['prefix']}-JP-PAY-{idx}",
            "activities_id": {"$ref": "marketing.pending_activity"},
            "c_date_time_type": 1, "c_date_time": minutes, "c_activities_email_opened": 3, "type": 1,
            "template_id": {"$ref": f"marketing.template.{key}"},
            "is_send_end_activities": 1 if idx == len(PENDING_TEMPLATE_KEYS) else 2,
            "coupon_code": "", "discount": 0,
        }})
    return out


def message_differences(live, desired):
    diffs = []
    for f in MESSAGE_FIELDS:
        a, b = live.get(f), desired.get(f)
        if f in _ZERO_EMPTY:
            a, b = _zero_empty(a), _zero_empty(b)
        if not sem_equal(a, b):
            diffs.append({"field": f, "live": live.get(f), "desired": desired.get(f)})
    return diffs
