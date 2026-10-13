"""Read helpers shared by inspect, apply and verify: envelopes, pagination, identity."""

from .adapter import ApiFailure, FatalApiError
from .util import LaunchError, as_int, normalize_url

PAGE_SIZE = 50
MAX_PAGES = 500


def list_items(data):
    """Accept the list envelopes seen in the docs: [..], {list}, {message}, {lang_emails}, {vars}."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("list", "message", "lang_emails", "vars", "tax_list", "data"):
            if isinstance(data.get(key), list):
                return data[key]
    if data in (None, "", {}):
        return []
    raise ApiFailure("ENVELOPE", "unrecognized list envelope")


def paginate(adapter, api_id, query=None, page_size=PAGE_SIZE):
    """Fetch every page using total/totalPage; refuse to return a truncated listing."""
    items, page, total, total_pages = [], 1, None, None
    while True:
        q = dict(query or {})
        q.update({"pageNum": page, "pageSize": page_size})
        data = adapter.call(api_id, query=q)
        batch = list_items(data)
        items += batch
        if isinstance(data, dict):
            total = as_int(data.get("total")) if data.get("total") is not None else total
            if data.get("totalPage") is not None:
                total_pages = as_int(data.get("totalPage"))
            elif total is not None:
                size = as_int(data.get("pageSize")) or page_size
                total_pages = -(-total // size) if size else 1
        if total_pages is None:
            if isinstance(data, list) or len(batch) < page_size:
                break
        elif page >= total_pages:
            break
        if not batch:
            raise ApiFailure("PAGINATION", f"{api_id}: empty page {page} before totalPage {total_pages}")
        page += 1
        if page > MAX_PAGES:
            raise ApiFailure("PAGINATION", f"{api_id}: more than {MAX_PAGES} pages")
    if total is not None and len(items) != total:
        raise ApiFailure("PAGINATION", f"{api_id}: collected {len(items)} of {total} items")
    return items


def read_identity(adapter):
    """shop-info/init then site_base_url; always the first calls of any live session."""
    init = adapter.call("shop-info/init") or {}
    base = adapter.call("shop-info/site_base_url") or {}
    return {
        "shop_id": as_int(init.get("shop_id")),
        "shop_type": as_int(init.get("shop_type")),
        "shop_name": init.get("shop_name"),
        "theme_identity": init.get("shop_theme_identity"),
        "addons": sorted(str(a) for a in (init.get("addons") or [])),
        "public_url": base.get("public_url"),
        "manager_base_url": base.get("base_url"),
    }


def check_identity(identity, expected_shop_id, expected_public_url):
    """Raise unless the bound store is exactly the expected B2C store."""
    actual_url = normalize_url(identity.get("public_url"))
    if actual_url is None or actual_url != normalize_url(expected_public_url):
        raise FatalApiError("IDENTITY_MISMATCH", "bound store public URL does not match the profile",
                            {"expected_public_url": expected_public_url, "actual_public_url": identity.get("public_url")})
    if expected_shop_id is not None and identity.get("shop_id") != expected_shop_id:
        raise FatalApiError("IDENTITY_MISMATCH", "bound store shop_id does not match",
                            {"expected_shop_id": expected_shop_id, "actual_shop_id": identity.get("shop_id")})
    if identity.get("shop_id") is None or identity["shop_id"] <= 0:
        raise FatalApiError("IDENTITY_MISMATCH", "bound store returned no shop_id")
    if identity.get("shop_type") != 10:
        raise FatalApiError("SHOP_TYPE_UNSUPPORTED", "only B2C stores (shop_type=10) are supported",
                            {"shop_type": identity.get("shop_type")})


def require_identity(adapter, target):
    identity = read_identity(adapter)
    check_identity(identity, target.get("shop_id"), target.get("public_url"))
    if target.get("shop_id") is None:
        raise LaunchError("IDENTITY_UNRESOLVED", "plan target has no confirmed shop_id")
    return identity
