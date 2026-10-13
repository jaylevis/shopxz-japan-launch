"""Packaged asset loading/validation, Liquid variable analysis and recall copy rendering."""

import hashlib
import html
import json
import re
from html.parser import HTMLParser

from .profile import (build_profile, load_marketing_preset, validate_marketing_preset, validate_profile,
                      PRESET_FILES)
from . import store_settings
from .util import ASSETS_ROOT, LaunchError, read_json

SYSTEM_EMAIL_COUNT = 15
SYSTEM_EMAIL_FILE = re.compile(r"^(\d{2})-(\d+)-[A-Za-z0-9_.-]+\.json$")
RECALL_LAYOUTS = ("reminder.html", "offer.html", "final-offer.html")
MARKERS = ("%%TITLE%%", "%%INTRO%%", "%%TERMS%%")
LEFTOVER_MARKER = re.compile(r"%%[A-Z_]+%%")
TRACK_IMAGE = re.compile(r"\{\{-?\s*order\.track_image\s*-?\}\}")
TRACK_IMAGE_IN_ATTR = re.compile(r"=\s*[\"']\s*\{\{-?\s*order\.track_image")
VIEW_URL = re.compile(r"\{\{-?\s*order\.view_url\s*-?\}\}")
JAPANESE = re.compile(r"[぀-ヿ一-鿿]")
URL_HOST = re.compile(r"(?:https?:)?//([A-Za-z0-9.-]+\.[A-Za-z]{2,})", re.I)
ALLOWED_HOSTS = {"www.w3.org"}
SOURCE_ID_PATH = re.compile(r"/(?:common|product|system|theme)/\d+/")
SOURCE_ID_KEY = re.compile(r"\bshop_id\b")

# ------------------------------------------------------------------ name order

_NAME_VAR = r"\{{\{{-?\s*(?P<{g}obj>(?:[A-Za-z_]\w*\.)*){part}(?:\s*\|[^}}]*)?\s*-?\}}\}}"
_SEP = r"(?P<sep>\s|&nbsp;|　)?"


def _pair(first, second):
    """Adjacent {{ [obj.]first }} {{ [obj.]second }} outputs on the same object (bare or prefixed)."""
    return re.compile("(?P<a>" + _NAME_VAR.format(g="a", part=first) + ")" + _SEP
                      + "(?P<b>" + _NAME_VAR.format(g="b", part=second) + ")")


_FIRST_LAST = _pair("first_name", "last_name")
_LAST_FIRST = _pair("last_name", "first_name")


def _same_obj(m):
    return m.group("aobj") == m.group("bobj")


def name_pairs(text, order="first_last"):
    pattern = _FIRST_LAST if order == "first_last" else _LAST_FIRST
    return sum(1 for m in pattern.finditer(text or "") if _same_obj(m))


def apply_name_order(text, order):
    """Assets are authored first_last; swap supported variable pairs only for last_first."""
    if order != "last_first" or not text:
        return text
    return _FIRST_LAST.sub(
        lambda m: f"{m.group('b')}{m.group('sep') or ''}{m.group('a')}" if _same_obj(m) else m.group(0), text)


# ------------------------------------------------------------------ Liquid analysis

_OUTPUT = re.compile(r"\{\{-?(.*?)-?\}\}", re.S)
_TAG = re.compile(r"\{%-?\s*(\w+)(.*?)-?%\}", re.S)
_STRING = re.compile(r"'[^']*'|\"[^\"]*\"")
_IDENT = re.compile(r"(?<![\w.])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*|\[\d+\])*)")
_KEYWORDS = {"and", "or", "not", "contains", "blank", "empty", "nil", "null", "true", "false", "in",
             "with", "as", "limit", "offset", "reversed", "forloop", "cols", "else"}
_BLOCKS = {"if": "endif", "unless": "endunless", "for": "endfor", "case": "endcase", "capture": "endcapture",
           "raw": "endraw", "comment": "endcomment"}


def _expr_idents(expr):
    expr = _STRING.sub(" ", expr)
    parts = expr.split("|")
    found = _IDENT.findall(parts[0])
    for flt in parts[1:]:
        if ":" in flt:
            found += _IDENT.findall(flt.split(":", 1)[1])
    return [f for f in found if f.split(".")[0].split("[")[0] not in _KEYWORDS]


def liquid_variables(text):
    """Return (used variable paths, locally bound names) for a Liquid template."""
    used, local = set(), {"forloop"}
    text = text or ""
    for m in _OUTPUT.finditer(text):
        used.update(_expr_idents(m.group(1)))
    for m in _TAG.finditer(text):
        tag, rest = m.group(1), m.group(2)
        if tag == "for":
            fm = re.match(r"\s*(\w+)\s+in\s+(.*)", rest, re.S)
            if fm:
                local.add(fm.group(1))
                used.update(_expr_idents(fm.group(2)))
        elif tag == "assign":
            am = re.match(r"\s*(\w+)\s*=(.*)", rest, re.S)
            if am:
                local.add(am.group(1))
                used.update(_expr_idents(am.group(2)))
        elif tag == "capture":
            cm = re.match(r"\s*(\w+)", rest)
            if cm:
                local.add(cm.group(1))
        elif tag in ("if", "elsif", "unless", "case", "when"):
            used.update(_expr_idents(rest))
    return used, local


def allowed_from(texts, declared):
    allowed = set()
    for t in texts:
        u, _ = liquid_variables(t)
        allowed |= u
    for d in declared or []:
        u, _ = liquid_variables(d if "{{" in str(d) else "{{" + str(d) + "}}")
        allowed |= u
    return allowed


def unsupported_variables(text, allowed):
    used, local = liquid_variables(text)
    bad = []
    for var in sorted(used):
        root = var.split(".")[0].split("[")[0]
        if root in local:
            continue
        if any(var == a or var.startswith(a + ".") or var.startswith(a + "[") for a in allowed):
            continue
        bad.append(var)
    return bad


# ------------------------------------------------------------------ structure checks

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_OPTIONAL_CLOSE = {"p", "li", "td", "th", "tr", "tbody", "thead", "tfoot", "option", "html", "head", "body",
                   "colgroup", "dd", "dt"}


class _TagBalance(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if tag not in self.stack:
            self.errors.append(f"unexpected </{tag}>")
            return
        while self.stack:
            top = self.stack.pop()
            if top == tag:
                return
            if top not in _OPTIONAL_CLOSE:
                self.errors.append(f"<{top}> not closed before </{tag}>")


def html_errors(text):
    parser = _TagBalance()
    parser.feed(text or "")
    parser.close()
    unclosed = [t for t in parser.stack if t not in _OPTIONAL_CLOSE]
    return parser.errors + [f"<{t}> never closed" for t in unclosed]


def liquid_errors(text):
    errors = []
    text = text or ""
    if text.count("{{") != text.count("}}"):
        errors.append("unbalanced {{ }}")
    if len(re.findall(r"\{%", text)) != len(re.findall(r"%\}", text)):
        errors.append("unbalanced {% %}")
    stack = []
    for m in _TAG.finditer(text):
        tag = m.group(1)
        if tag in _BLOCKS:
            stack.append(tag)
        elif tag.startswith("end"):
            if not stack or _BLOCKS[stack[-1]] != tag:
                errors.append(f"unexpected {{% {tag} %}}")
            else:
                stack.pop()
    errors += [f"{{% {t} %}} never closed" for t in stack]
    return errors


def source_leaks(text):
    """Literal hosts, shop IDs or ID-bearing media paths copied from a source store."""
    leaks = []
    for host in URL_HOST.findall(text or ""):
        if host.lower() not in ALLOWED_HOSTS:
            leaks.append(f"literal host {host}")
    if SOURCE_ID_PATH.search(text or ""):
        leaks.append("shop-specific media path")
    if SOURCE_ID_KEY.search(text or ""):
        leaks.append("shop_id reference")
    return leaks


# ------------------------------------------------------------------ loaders

def load_system_emails(assets_root=ASSETS_ROOT):
    root = assets_root / "system-emails"
    if not root.is_dir():
        raise LaunchError("ASSET_MISSING", "assets/system-emails is missing")
    out = []
    for path in sorted(root.iterdir()):
        if path.suffix.lower() != ".json":
            continue
        m = SYSTEM_EMAIL_FILE.match(path.name)
        if not m:
            raise LaunchError("ASSET_INVALID", f"unexpected system email file name: {path.name}")
        data = read_json(path)
        body = data.get("email") if isinstance(data.get("email"), dict) else data
        out.append({
            "file": path.name,
            "type": body.get("type"),
            "category": body.get("category"),
            "subject": body.get("subject"),
            "body_html": body.get("body_html"),
            "lang_emails": data.get("lang_emails", body.get("lang_emails")),
            "name_stem": (int(m.group(1)), int(m.group(2))),
        })
    return out


def load_recall_layouts(assets_root=ASSETS_ROOT):
    root = assets_root / "recall-emails"
    layouts = {}
    for name in RECALL_LAYOUTS:
        path = root / name
        if not path.is_file():
            raise LaunchError("ASSET_MISSING", f"assets/recall-emails/{name} is missing")
        layouts[name] = path.read_text(encoding="utf-8")
    return layouts


def render_recall(layout, title, intro, terms):
    out = layout
    for marker, value in zip(MARKERS, (title, intro, terms)):
        if marker not in out:
            raise LaunchError("ASSET_INVALID", f"recall layout lacks {marker}")
        # Copy is plain text; Liquid braces survive escaping so {{ shop.name }} stays dynamic.
        out = out.replace(marker, html.escape(value, quote=False).replace("\n", "<br>"))
    if LEFTOVER_MARKER.search(out):
        raise LaunchError("ASSET_INVALID", "recall layout has unresolved %%MARKERS%%")
    return out



# ------------------------------------------------------------------ recall templates (v1.3)
# Four unpaid-order steps plus two payment-pending reminders. Coupon blocks render only when the
# message binds a coupon ({{ coupon_code }}), so a template never shows an empty code. Copy is
# factual: no stock scarcity, deadlines or delivery promises the tool cannot verify.
RECALL_TEMPLATE_KEYS = ("reminder", "help", "offer", "final")
PENDING_TEMPLATE_KEYS = ("pay1", "pay2")
_IF, _ELSE, _END = "{% if coupon_code %}", "{% else %}", "{% endif %}"
_ALREADY_PAID = "すでにお支払いのお手続きをお済みの場合は、行き違いとなりますので本メールはご放念ください。"
_PENDING_PAID = "すでにお支払い済みの場合、入金の確認までお時間をいただくことがございます。行き違いの際はご容赦ください。"
_COUPON_TERMS = "割引内容とご利用条件は、ご注文画面でご確認ください。"
_COUPON_BLOCK = re.compile(r'(\s*<table role="presentation"[^>]*border:1px solid #404040;.*?</table>\s*<p[^>]*>'
                           r'下のボタンからご注文画面を開き、クーポンの適用内容をご確認ください。</p>)', re.S)
_PAYMENT_BLOCK = """{% if order.txn_id %}
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 24px 0;border:1px solid #ffe0b2;background-color:#fff8e1;">
                <tr>
                  <td style="padding:16px;font-size:14px;line-height:1.7;color:#333333;">
                    <p style="margin:0 0 8px 0;font-weight:bold;">■ コンビニ・銀行振込のお支払い情報</p>
                    <p style="margin:0 0 12px 0;">以下のリンクより、お支払い先（振込先口座や払込番号）とお支払い期限をご確認ください。</p>
                    <p style="margin:0 0 12px 0;text-align:center;"><a href="{{ shop.url }}/instructions/ja/{{ order.txn_id }}" style="font-size:16px;font-weight:bold;color:#1567BB;text-decoration:underline;">お支払い詳細ページへ</a></p>
                    <p style="margin:0;padding-top:10px;border-top:1px dashed #ffe0b2;font-size:12px;color:#d32f2f;"><strong>【重要：銀行振込のお客様へ】</strong><br>振込先口座はご注文ごとに異なる場合がございます。必ず本ご注文のお支払い詳細ページに記載された口座へお振込みください。</p>
                  </td>
                </tr>
              </table>
              {% endif %}
              """
RECALL_COPY = {
    "reminder": {"layout": "reminder.html", "subject": "【{{ shop.name }}】ご注文のお手続きが完了していません",
                 "title": "お手続きが完了していないご注文があります",
                 "intro": "カートに追加された商品のご注文が、まだ完了していないようです。下のボタンからご注文内容をご確認ください。",
                 "terms": _ALREADY_PAID},
    "help": {"layout": "reminder.html",
             "subject": "{{ order.shippingAddress.first_name }} {{ order.shippingAddress.last_name }}様、ご注文についてご不明な点はございませんか？【{{ shop.name }}】",
             "title": "ご不明な点はございませんか？",
             "intro": "サイズやお支払い方法、お届けについてご不明な点がございましたら、このメールにご返信いただくか、"
                      "下記のお問い合わせ先までお気軽にご連絡ください。ご注文内容は下のボタンからご確認いただけます。",
             "terms": _ALREADY_PAID},
    "offer": {"layout": "offer.html", "coupon_conditional": True,
              "subject": "【{{ shop.name }}】カートの商品についてのご案内",
              "title": f"{_IF}クーポンをご用意しました{_ELSE}カートの商品はまだご注文いただけます{_END}",
              "intro": f"{_IF}お手続きの際にご利用いただけるクーポンをご用意しました。下のボタンからご注文画面に戻ると、クーポンが自動で適用されます。"
                       f"{_ELSE}カートに追加された商品は、下のボタンからそのままご注文手続きにお進みいただけます。{_END}" + _ALREADY_PAID,
              "terms": _COUPON_TERMS},
    "final": {"layout": "offer.html", "coupon_conditional": True,
              "subject": "【{{ shop.name }}】カートの商品について最後のご案内",
              "title": f"{_IF}最後のご案内：クーポンをご用意しました{_ELSE}カートの商品について最後のご案内{_END}",
              "intro": f"{_IF}お手続きが完了していないご注文について、最後のご案内です。下のボタンからご注文画面に戻ると、クーポンが自動で適用されます。"
                       f"{_ELSE}お手続きが完了していないご注文について、最後のご案内です。ご注文をご希望の場合は、下のボタンからお手続きにお進みください。{_END}"
                       "このご注文についてのご案内は、本メールが最後となります。" + _ALREADY_PAID,
              "terms": _COUPON_TERMS},
    "pay1": {"layout": "reminder.html", "payment_block": True,
             "subject": "【{{ shop.name }}】お支払いのご案内（ご注文番号：{{ order.order_number }}）",
             "title": "お支払いのご案内",
             "intro": "ご注文ありがとうございます。現在、ご注文のお支払いを確認できておりません。コンビニ払い・銀行振込をお選びの場合は、"
                      "下記のお支払い詳細ページから払込番号・振込先口座をご確認のうえ、お手続きをお願いいたします。",
             "terms": _PENDING_PAID},
    "pay2": {"layout": "reminder.html", "payment_block": True,
             "subject": "【{{ shop.name }}】お支払い期限が近づいています（ご注文番号：{{ order.order_number }}）",
             "title": "お支払い期限が近づいています",
             "intro": "ご注文のお支払いを、まだ確認できておりません。お手続きがお済みでない場合は、下記のお支払い詳細ページでお支払い期限を"
                      "ご確認のうえ、お早めにお手続きください。このご注文についてのご案内は、本メールが最後となります。",
             "terms": _PENDING_PAID},
}


def build_recall_template(key, layouts):
    """Return (subject, body_html) for a v1.3 recall template key; authored first_last."""
    copy_ = RECALL_COPY.get(key)
    if copy_ is None:
        raise LaunchError("ASSET_INVALID", f"unknown recall template key {key}")
    body = render_recall(layouts[copy_["layout"]], copy_["title"], copy_["intro"], copy_["terms"])
    if copy_.get("coupon_conditional"):
        m = _COUPON_BLOCK.search(body)
        if not m:
            raise LaunchError("ASSET_INVALID", f"{copy_['layout']}: coupon block not found for {key}")
        body = body.replace(m.group(1), f"\n              {_IF}{m.group(1)}\n              {_END}", 1)
    if copy_.get("payment_block"):
        if body.count("<!-- CTA -->") != 1:
            raise LaunchError("ASSET_INVALID", f"{copy_['layout']}: CTA marker not found for {key}")
        body = body.replace("<!-- CTA -->", _PAYMENT_BLOCK + "<!-- CTA -->", 1)
    return copy_["subject"], body

# ------------------------------------------------------------------ validate-assets

def validate_system_email(item):
    errs = []
    t, c = item.get("type"), item.get("category")
    if not isinstance(t, int) or not isinstance(c, int):
        errs.append("type/category must be integers")
    elif (t, c) != item["name_stem"]:
        errs.append(f"type/category {t}-{c} do not match file name")
    if not isinstance(item.get("subject"), str) or not item["subject"].strip():
        errs.append("subject missing")
    elif not JAPANESE.search(item["subject"]):
        errs.append("subject is not Japanese")
    if not isinstance(item.get("body_html"), str) or "<" not in (item.get("body_html") or ""):
        errs.append("body_html missing or not HTML")
    if item.get("lang_emails") != []:
        errs.append("lang_emails must be an empty list in packaged assets")
    for text in (item.get("subject") or "", item.get("body_html") or ""):
        errs += liquid_errors(text)
        errs += source_leaks(text)
        if LEFTOVER_MARKER.search(text):
            errs.append("unresolved %%MARKER%%")
    errs += html_errors(item.get("body_html") or "")
    return errs


def catalog_errors(assets_root=ASSETS_ROOT):
    """assets/catalog.json must list exactly the packaged email/layout files with matching sha256."""
    path = assets_root / "catalog.json"
    if not path.is_file():
        return ["assets/catalog.json is missing"]
    try:
        catalog = read_json(path)
    except ValueError as exc:
        return [f"catalog.json unreadable: {exc}"]
    errors = []
    listed = {}
    for section in ("system_emails", "recall_layouts"):
        for entry in catalog.get(section) or []:
            listed[entry.get("file")] = entry
    actual = {f"system-emails/{p.name}" for p in (assets_root / "system-emails").glob("*.json")}
    actual |= {f"recall-emails/{n}" for n in RECALL_LAYOUTS if (assets_root / "recall-emails" / n).is_file()}
    for missing in sorted(actual - set(listed)):
        errors.append(f"catalog.json does not list {missing}")
    for name, entry in sorted(listed.items()):
        file = assets_root / str(name)
        if not file.is_file():
            errors.append(f"catalog.json lists missing file {name}")
            continue
        if hashlib.sha256(file.read_bytes()).hexdigest() != entry.get("sha256"):
            errors.append(f"catalog.json sha256 mismatch for {name}")
        if "type" in entry:
            data = read_json(file)
            if (data.get("type"), data.get("category")) != (entry.get("type"), entry.get("category")):
                errors.append(f"catalog.json type/category mismatch for {name}")
    return errors


def validate_assets(assets_root=ASSETS_ROOT):
    errors, warnings, counts = [], [], {}
    try:
        emails = load_system_emails(assets_root)
        counts["system_emails"] = len(emails)
        if len(emails) != SYSTEM_EMAIL_COUNT:
            errors.append(f"expected {SYSTEM_EMAIL_COUNT} system email assets, found {len(emails)}")
        seen = set()
        for item in emails:
            key = (item.get("type"), item.get("category"))
            if key in seen:
                errors.append(f"{item['file']}: duplicate type/category {key}")
            seen.add(key)
            errors += [f"{item['file']}: {e}" for e in validate_system_email(item)]
            text = (item.get("subject") or "") + (item.get("body_html") or "")
            pairs = name_pairs(text)
            counts["name_pairs"] = counts.get("name_pairs", 0) + pairs
            swapped = apply_name_order(text, "last_first")
            if name_pairs(swapped, "last_first") != pairs or name_pairs(swapped) != 0:
                errors.append(f"{item['file']}: name-order swap is not a clean pair-for-pair swap")
            if name_pairs(text, "last_first"):
                errors.append(f"{item['file']}: assets must be authored first_last (found last_first pairs)")
    except LaunchError as exc:
        errors.append(exc.message)
    except (ValueError, json.JSONDecodeError) as exc:
        errors.append(f"system email asset unreadable: {exc}")

    try:
        layouts = load_recall_layouts(assets_root)
        counts["recall_layouts"] = len(layouts)
        for name, text in layouts.items():
            for marker in MARKERS:
                if marker not in text:
                    errors.append(f"{name}: missing {marker}")
            if not TRACK_IMAGE.search(text):
                errors.append(f"{name}: missing {{{{ order.track_image }}}}")
            if TRACK_IMAGE_IN_ATTR.search(text):
                errors.append(f"{name}: {{{{ order.track_image }}}} must be a standalone output, not an attribute")
            if not VIEW_URL.search(text):
                errors.append(f"{name}: missing {{{{ order.view_url }}}} checkout link")
            try:
                rendered = render_recall(text, "テスト", "{{ shop.name }} テスト", "条件")
                errors += [f"{name}: {e}" for e in html_errors(rendered) + liquid_errors(rendered)]
            except LaunchError as exc:
                errors.append(f"{name}: {exc.message}")
            errors += [f"{name}: {e}" for e in source_leaks(text)]
            if "{{" not in text.replace("{{ order.track_image }}", ""):
                warnings.append(f"{name}: no dynamic shop/order variables found")
    except LaunchError as exc:
        errors.append(exc.message)

    errors += catalog_errors(assets_root)
    try:
        errors += [f"store-settings.json: {e}" for e in store_settings.validate(store_settings.load_defaults(assets_root))]
    except (LaunchError, ValueError, OSError) as exc:
        errors.append(f"store-settings.json: {getattr(exc, 'message', exc)}")
    for preset in PRESET_FILES:
        try:
            report = validate_profile(build_profile(preset, assets_root), assets_root)
            errors += [f"profile {preset}: {e}" for e in report["errors"]]
        except (LaunchError, ValueError) as exc:
            errors.append(f"profile {preset}: {getattr(exc, 'message', exc)}")
    try:
        preset = load_marketing_preset("jp-apparel", assets_root)
        errors += [f"marketing preset: {e}" for e in validate_marketing_preset(preset)]
        for msg in preset.get("messages") or []:
            if msg.get("template") not in RECALL_TEMPLATE_KEYS:
                errors.append(f"marketing message {msg.get('key')}: unknown template {msg.get('template')}")
        layouts = load_recall_layouts(assets_root)
        for key in RECALL_TEMPLATE_KEYS + PENDING_TEMPLATE_KEYS:
            subject, body = build_recall_template(key, layouts)
            errs = html_errors(body) + liquid_errors(body) + liquid_errors(subject)
            if not TRACK_IMAGE.search(body):
                errs.append("missing {{ order.track_image }}")
            if name_pairs(subject + body, "last_first"):
                errs.append("must be authored first_last")
            errors += [f"recall template {key}: {e}" for e in errs]
        counts["recall_templates"] = len(RECALL_TEMPLATE_KEYS + PENDING_TEMPLATE_KEYS)
    except (LaunchError, ValueError) as exc:
        errors.append(f"marketing preset: {getattr(exc, 'message', exc)}")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "counts": counts}
