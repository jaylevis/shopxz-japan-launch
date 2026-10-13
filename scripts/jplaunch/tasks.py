"""Agent-work tasks and readiness gaps: work this tool deliberately does not automate."""

from .gates import locale_problems, mail_problems

MANAGER_REFS = {
    "basic": ["fecify-site-manager-v6: system-settings/basic_get|basic_set, country_get|country_set, shop-info/languages|currencies"],
    "shipping": ["fecify-site-manager-v6: shipping module (zones/fees) and tax module"],
    "payment": ["fecify-site-manager-v6: payment/list|info (merchant credentials entered by merchant)"],
    "smtp": ["fecify-site-manager-v6: notice/email-config-get|set (secrets supplied by merchant, never stored here)"],
    "pages": ["fecify-site-manager-v6: page module; docs/workflows/site-builder/custom-pages.md"],
    "catalog": ["fecify-site-manager-v6: products/collections workflows"],
    "theme": ["fecify-site-manager-v6: docs/theme.md (page-cache gate, preview validation)"],
    "analytics": ["fecify-site-manager-v6: google/facebook/scripttag modules"],
    "qa": ["fecify-site-manager-v6: docs/workflows/site-builder/acceptance.md (host browser evidence)"],
}


def _task(tid, title, status, depends_on=(), needs=(), reason="", refs=()):
    return {"id": tid, "title": title, "status": status, "depends_on": list(depends_on),
            "inputs_needed": list(needs), "reason": reason, "refs": list(refs), "automated": False}


def readiness_gaps(validation, state):
    gaps = [{"field": g, "source": "profile"} for g in validation.get("readiness_gaps", [])]
    payments = (state.get("payments") or {})
    if payments.get("status") == "read" and not payments.get("data"):
        gaps.append({"field": "payment.active_methods", "source": "store", "detail": "no active payment method"})
    notice_block = state.get("notice_readiness") or {}
    facts = notice_block.get("data") if notice_block.get("status") == "read" else None
    for problem in mail_problems(facts):
        field = "notice.smtp" if "SMTP" in problem else "notice.email"
        gaps.append({"field": field, "source": "store", "detail": problem})
    basic = state.get("basic") or {}
    for problem in locale_problems(basic.get("data") if basic.get("status") == "read" else None):
        gaps.append({"field": "basic.locale", "source": "store", "detail": problem})
    return gaps


def build_agent_tasks(profile, snapshot, selection, validation, identity_ok, backend=None, workflows=None):
    modules = profile.get("modules") or {}
    state = (snapshot or {}).get("state") or {}
    gaps = set(validation.get("readiness_gaps", []))
    integrations = profile.get("integrations") or {}
    storefront = profile.get("storefront") or {}
    tasks = []
    if not identity_ok:
        tasks.append(_task("AT-identity", "确认目标店铺 shop_id / public_url 后重新 inspect", "required",
                           reason="identity unresolved or mismatched; no live writes allowed"))
    backend = backend or {}
    workflows = workflows or {}
    if backend.get("selected"):
        planned = sorted(v for v in backend.get("ops", {}).values() if v)
        tasks.append(_task("AT-basic-locale", "后台基础/国家/结账/任务设置：按计划自动保存并回读；被阻断项需补资料",
                           "automated_in_plan" if planned else "pending",
                           reason=f"planned operations: {planned}; blocked areas listed in plan blockers",
                           refs=MANAGER_REFS["basic"]))
    else:
        tasks.append(_task("AT-basic-locale", "基础设置：站点名称、联系邮箱、时区 Asia/Tokyo、默认语言 ja、货币 JPY、展示国家 JP",
                           "pending" if modules.get("basic") else "not_requested",
                           reason="basic settings are agent-reviewed; this tool only reads basic_get/country_get",
                           refs=MANAGER_REFS["basic"]))
    if backend.get("currency_review_required"):
        tasks.append(_task("AT-currency-review", "店铺基础货币不是 JPY：先审查现有商品价格含义，再决定是否设置 "
                           "store_settings.basic.allow_currency_change=true（工具不换算/不改商品价格）", "needs_input",
                           ["AT-basic-locale"], ["store_settings.basic.allow_currency_change"],
                           reason="base currency change is a business decision recorded in the plan"))
    rebase = workflows.get("currency_rebase")
    if rebase:
        tasks.append(_task("AT-currency-rebase", "基础货币改为 JPY 后：按新基础货币重设多货币汇率（exchange rate rebase），"
                           "并在前台核对各币种价格显示", "required" if rebase.get("status") == "required" else "needs_input",
                           ["AT-basic-locale"],
                           reason=("multi-currency enabled; rates derive from the base currency" if rebase.get(
                               "multi_currency_enabled") else "multi-currency state unknown; verification needed"),
                           refs=["fecify-site-manager-v6: currency/get, currency/get-rate, currency/set "
                                 "(agent workflow after authorization; no invented rates)"]))
    if (profile.get("email") or {}).get("name_order") is None and (modules.get("emails") or modules.get("marketing")):
        tasks.append(_task("AT-name-order", "在前台注册/结账表单核实姓名字段顺序，设置 email.name_order", "required",
                           needs=["email.name_order"], reason="email content changes are blocked until verified"))
    ship_needs = sorted(g for g in gaps if g.startswith("shipping."))
    ship_wf = workflows.get("shipping") or {}
    tasks.append(_task("AT-shipping", "配送方案与运费（含免运费门槛、偏远地区规则）及税费合计核对",
                       "needs_input" if ship_needs or ship_wf.get("status") == "needs_data" else "pending",
                       ["AT-basic-locale"], ship_needs,
                       reason=(f"plan.backend_workflows.shipping status={ship_wf.get('status')}; danger-marked "
                               "requests run only via the manager confirmation workflow" if ship_wf else ""),
                       refs=MANAGER_REFS["shipping"]))
    tax_wf = workflows.get("tax") or {}
    if tax_wf:
        tasks.append(_task("AT-tax", "税费方案核对（本工具不假定税率，不提供税务/法律意见）",
                           "needs_input" if tax_wf.get("status") == "needs_data" else "pending", ["AT-basic-locale"],
                           reason=f"plan.backend_workflows.tax status={tax_wf.get('status')}",
                           refs=MANAGER_REFS["shipping"]))
    has_payment = bool((state.get("payments") or {}).get("data"))
    tasks.append(_task("AT-payment", "支付方式开通与真实交易验证", "pending" if has_payment else "needs_input",
                       ["AT-basic-locale"], [] if has_payment else ["merchant payment account"],
                       reason="payment credentials are merchant-supplied; transactions are not verified by this tool",
                       refs=MANAGER_REFS["payment"]))
    tasks.append(_task("AT-smtp", "SMTP/发件配置与真实投递验证（本工具不发送测试邮件）", "pending",
                       ["AT-basic-locale"], refs=MANAGER_REFS["smtp"]))
    page_needs = sorted(g for g in gaps if g.startswith(("business.", "returns.")))
    tasks.append(_task("AT-legal-pages", "特定商取引法表記、隐私政策、退换货政策、配送说明页面",
                       "needs_input" if page_needs else "pending", ["AT-shipping"], page_needs,
                       reason="legal copy must come from merchant facts, never fabricated", refs=MANAGER_REFS["pages"]))
    tasks.append(_task("AT-catalog", "商品与专辑（日语标题/描述、价格 JPY、迁移句柄保留）", "pending",
                       ["AT-basic-locale"], refs=MANAGER_REFS["catalog"]))
    tasks.append(_task("AT-theme", "主题装修（品牌、移动端优先）", "pending", ["AT-basic-locale", "AT-catalog"],
                       reason=storefront.get("style_brief") or "", refs=MANAGER_REFS["theme"]))
    has_analytics = any(integrations.get(k) for k in ("ga4", "google_ads", "line", "clarity"))
    tasks.append(_task("AT-analytics", "GA4 / Google Ads / LINE / Clarity 接入", "pending" if has_analytics else "optional",
                       ["AT-theme"], refs=MANAGER_REFS["analytics"]))
    for module, status in sorted(selection.items()):
        if status == "unavailable":
            tasks.append(_task(f"AT-addon-{module}", f"插件 {module} 未开通：由商家决定是否购买/启用（工具不会自动购买）",
                               "needs_input", reason="addon absent from shop-info/init"))
    if selection.get("marketing") == "blocked":
        tasks.append(_task("AT-marketing-review", "审查与召回营销冲突的现有优惠券/模板/活动", "required",
                           reason="marketing branch blocked; see plan blockers for evidence"))
    tasks.append(_task("AT-browser-qa", "浏览器验收：桌面/手机、日本地址结账、悬浮购物车、浏览记录、邮件渲染",
                       "pending", ["AT-theme", "AT-payment", "AT-shipping"], refs=MANAGER_REFS["qa"]))
    tasks.append(_task("AT-publish", "上线发布（需全部前置验收）",
                       "blocked" if storefront.get("publish_requested") else "not_requested",
                       ["AT-browser-qa", "AT-legal-pages", "AT-payment", "AT-smtp"],
                       reason="publishing is never performed by this tool"))
    return tasks
