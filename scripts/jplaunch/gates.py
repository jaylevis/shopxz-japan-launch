"""Business preconditions for monetary marketing and recall cut-over.

Every check is strict: only explicit positive evidence passes; missing/unknown facts fail.
The same functions gate planning (snapshot facts) and live writes/activation (fresh reads).
"""

from datetime import datetime

from .util import JST, as_int

REQUIRED_LANGUAGE, REQUIRED_CURRENCY = "ja", "JPY"


def notice_readiness(cfg):
    """Keep only non-secret readiness booleans from notice/email-config-get."""
    cfg = cfg or {}
    mode = {"1": "hosted", "2": "store"}.get(str(cfg.get("smtp_force_use_admin")), "unknown")
    store = mode == "store"
    return {
        "email_status": as_int(cfg.get("email_status")),
        "smtp_mode": mode,
        "smtp_host_set": bool(cfg.get("smtp_host")) if store else None,
        "smtp_account_set": bool(cfg.get("smtp_account")) if store else None,
        "smtp_auth_set": bool(cfg.get("smtp_password")) if store else None,
        "reply_to_set": bool(cfg.get("email_address")),
        "store_name_set": bool(cfg.get("store_name")),
        "merchant_order_email_set": bool(cfg.get("merchant_receive_order_email")),
    }


def mail_problems(facts):
    """Reasons recall emails cannot be relied on; [] only with explicit positive evidence."""
    if not isinstance(facts, dict):
        return ["email configuration unknown"]
    problems = []
    if facts.get("email_status") != 1:
        problems.append("email sending is not enabled (email_status != 1)")
    if facts.get("reply_to_set") is not True or facts.get("store_name_set") is not True:
        problems.append("sender name / reply-to address not configured")
    mode = facts.get("smtp_mode")
    if mode == "store":
        missing = [k for k in ("smtp_host_set", "smtp_account_set", "smtp_auth_set") if facts.get(k) is not True]
        if missing:
            problems.append("store SMTP incomplete: " + ", ".join(missing))
    elif mode != "hosted":
        problems.append("SMTP mode unknown")
    return problems


def locale_problems(basic):
    """Monetary marketing requires the live store default to be ja / JPY (not just the profile)."""
    if not isinstance(basic, dict):
        return ["store basic settings unknown"]
    problems = []
    if basic.get("language") != REQUIRED_LANGUAGE:
        problems.append(f"store default language is {basic.get('language')!r}, not 'ja'")
    if basic.get("currency") != REQUIRED_CURRENCY:
        problems.append(f"store default currency is {basic.get('currency')!r}, not 'JPY'")
    return problems


def _parse_time(value):
    if value in (None, ""):
        return None
    iv = as_int(value)
    if iv is not None:
        return datetime.fromtimestamp(iv, JST) if iv > 0 else None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
    except ValueError:
        return "invalid"


def coupon_problems(coupon, now):
    """Is the coupon usable right now? Uses list metadata merged with info (info may omit activity_status)."""
    problems = []
    if as_int(coupon.get("status")) != 1:
        problems.append("coupon disabled")
    if "activity_status" in coupon and as_int(coupon.get("activity_status")) != 2:
        problems.append(f"activity_status={coupon.get('activity_status')} (not running)")
    start = _parse_time(coupon.get("starts_at"))
    if start is None or start == "invalid":
        problems.append("starts_at unknown")
    elif start > now:
        problems.append(f"starts in the future ({coupon.get('starts_at')})")
    end = _parse_time(coupon.get("ends_at"))
    if end == "invalid":
        problems.append("ends_at unparseable")
    elif end is not None and end <= now:
        problems.append(f"expired ({coupon.get('ends_at')})")
    limit = as_int(coupon.get("usage_limit"))
    if limit is None:
        problems.append("usage_limit unknown")
    elif limit > 0:
        used = as_int(coupon.get("usage_count"))
        if used is None:
            problems.append("usage_count unknown for a limited coupon")
        elif used >= limit:
            problems.append(f"usage exhausted ({used}/{limit})")
    return problems


def merge_coupon(list_row, info):
    """Detail is authoritative for fields it returns; list supplies activity_status/usage_count."""
    merged = dict(list_row or {})
    merged.update(info or {})
    return merged
