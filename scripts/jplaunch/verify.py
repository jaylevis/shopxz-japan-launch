"""Read-only verification of a plan against the live store; JSON + escaped HTML reports."""

import html

from .adapter import ApiFailure, ReadOnlyAdapter
from .backend import SETTLED, area_report
from .engine import STATE_SCHEMA, check_plan
from .handlers import CONFLICT, SATISFIED, Context, UnresolvedRef, evaluate
from .live import require_identity
from .util import contained_file, iso, now_jst, read_json, redact, write_json_immutable

LAUNCH_GATES = [
    {"gate": "browser_qa", "detail": "desktop/mobile storefront, JP address checkout, fixedcart, viewed", "ref": "AT-browser-qa"},
    {"gate": "payment_transaction", "detail": "real payment and refund path", "ref": "AT-payment"},
    {"gate": "smtp_delivery", "detail": "actual email delivery and rendering", "ref": "AT-smtp"},
    {"gate": "shipping_tax_totals", "detail": "shipping fees, free threshold and tax totals at checkout", "ref": "AT-shipping"},
    {"gate": "policies_pages", "detail": "特定商取引法表記, privacy, returns pages", "ref": "AT-legal-pages"},
    {"gate": "domain_publish", "detail": "domain/HTTPS and publish decision", "ref": "AT-publish"},
]


def _status(ev):
    return {SATISFIED: "verified", CONFLICT: "conflict"}.get(ev.status, "not_configured")


def verify(plan, adapter, workdir, now=None):
    now = now or now_jst()
    check_plan(plan, need_runnable=False)
    ro = ReadOnlyAdapter(adapter)
    require_identity(ro, plan["target"])
    journal = {}
    state_path = workdir / "state.json"
    if state_path.exists():
        state = read_json(state_path)
        if state.get("schema") == STATE_SCHEMA and state.get("plan_fingerprint") == plan["fingerprint"]:
            journal = state.get("ops", {})
    resolved = {}
    ctx = Context(ro, plan, resolved.get)
    rows = []
    for op in plan["operations"]:
        row = {"op_id": op["op_id"], "module": op["module"], "kind": op["kind"],
               "journal": (journal.get(op["op_id"]) or {}).get("status")}
        try:
            ev = evaluate(ctx, op)
            row["status"] = _status(ev)
            row["evidence"] = ev.evidence
            if ev.status == SATISFIED and ev.result_id is not None:
                resolved[op["op_id"]] = ev.result_id
                row["id"] = ev.result_id
            elif ev.status == SATISFIED:
                resolved[op["op_id"]] = (journal.get(op["op_id"]) or {}).get("result_id")
        except (UnresolvedRef, ApiFailure) as exc:
            row["status"], row["evidence"] = "unknown", {"error": exc.message}
        rows.append(row)
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    store_settings = area_report(plan, ro, rows)
    areas = store_settings.get("areas") or {}
    # Shipping/tax use the fresh re-read; other agent workflows (e.g. currency rebase) stay pending.
    pending_workflows = sorted(
        {k for k, w in (plan.get("backend_workflows") or {}).items()
         if k not in ("shipping", "tax") and w.get("status") not in ("preserve", "config_matches")}
        | {k for k in ("shipping", "tax") if k in areas and areas[k]["status"] not in SETTLED})
    unsettled_areas = sorted(k for k, a in areas.items() if a["status"] not in SETTLED)
    config_ok = (bool(plan.get("runnable")) and not plan.get("blockers") and not pending_workflows
                 and not unsettled_areas and all(r["status"] == "verified" for r in rows))
    report = {
        "schema": "SHOPXZ_JP_VERIFY_v1",
        "verified_at": iso(now),
        "target": plan["target"],
        "plan_fingerprint": plan["fingerprint"],
        "configuration_status": "CONFIGURATION_VERIFIED" if config_ok else "CONFIGURATION_INCOMPLETE",
        "ready_to_launch": False,
        "launch_status": "NOT_READY_TO_LAUNCH",
        "launch_gates": [dict(g, status="unverified_by_tool") for g in LAUNCH_GATES],
        "counts": counts,
        "pending_agent_workflows": pending_workflows,
        "unsettled_store_settings_areas": unsettled_areas,
        "operations": rows,
        "blockers": plan.get("blockers", []),
        "agent_tasks": [{k: t[k] for k in ("id", "title", "status", "depends_on")} for t in plan.get("agent_tasks", [])],
        "store_settings": store_settings,
        "backend_workflows": {k: {"status": w.get("status"), "executed": False, "execution": w.get("execution")}
                              for k, w in (plan.get("backend_workflows") or {}).items()},
    }
    json_path = write_json_immutable(contained_file(workdir, "report.json"), report, now.strftime("%Y%m%dT%H%M%S"))
    html_path = contained_file(workdir, json_path.stem + ".html")
    html_path.write_text(render_html(report), encoding="utf-8")
    return {"ok": True, "configuration_status": report["configuration_status"], "ready_to_launch": False,
            "counts": counts, "pending_agent_workflows": pending_workflows, "report_json": str(json_path), "report_html": str(html_path)}


def _e(value):
    return html.escape("" if value is None else str(value), quote=True)


def render_html(report):
    report = redact(report)
    row_html = "".join(
        f"<tr class=\"{_e(r['status'])}\"><td>{_e(r['op_id'])}</td><td>{_e(r['module'])}</td><td>{_e(r['kind'])}</td>"
        f"<td>{_e(r['status'])}</td><td>{_e(r.get('journal'))}</td><td><code>{_e(r.get('evidence'))}</code></td></tr>"
        for r in report["operations"])
    gates = "".join(f"<li>{_e(g['gate'])}: {_e(g['detail'])} ({_e(g['ref'])}) — {_e(g['status'])}</li>"
                    for g in report["launch_gates"])
    blockers = "".join(f"<li>[{_e(b['scope'])}] {_e(b['code'])}: {_e(b['message'])}</li>" for b in report["blockers"])
    tasks = "".join(f"<li>{_e(t['id'])} {_e(t['title'])} — {_e(t['status'])}</li>" for t in report["agent_tasks"])
    areas = (report.get("store_settings") or {}).get("areas") or {}
    area_html = "".join(
        f"<tr class=\"{_e(a.get('status'))}\"><td>{_e(name)}</td><td>{_e(a.get('status'))}</td>"
        f"<td><code>{_e(a.get('requested'))}</code></td><td><code>{_e(a.get('observed'))}</code></td>"
        f"<td>{_e(a.get('evidence'))}</td><td>{_e(a.get('blockers'))}</td></tr>" for name, a in areas.items())
    area_section = ("<h2>Store settings menu</h2><table><tr><th>area</th><th>status</th><th>requested</th>"
                    f"<th>observed</th><th>evidence</th><th>blockers</th></tr>{area_html}</table>") if areas else ""
    return (
        "<!DOCTYPE html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        "<title>Shopxz Japan launch verification</title><style>body{font-family:sans-serif;margin:24px}"
        "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px;font-size:13px}"
        ".verified{background:#eef9ee}.conflict{background:#fdecea}.unknown,.not_configured{background:#fff8e1}</style>"
        "</head><body>"
        f"<h1>{_e(report['configuration_status'])}</h1>"
        f"<p>Store: {_e(report['target'].get('shop_name'))} ({_e(report['target'].get('public_url'))}, "
        f"shop_id {_e(report['target'].get('shop_id'))}) — verified at {_e(report['verified_at'])}</p>"
        f"<p><strong>{_e(report['launch_status'])}</strong>: this report covers configuration only.</p>"
        f"<h2>Launch gates (agent workflow)</h2><ul>{gates}</ul>"
        "<h2>Operations</h2><table><tr><th>op</th><th>module</th><th>kind</th><th>status</th><th>journal</th>"
        f"<th>evidence</th></tr>{row_html}</table>{area_section}"
        f"<h2>Blockers</h2><ul>{blockers}</ul><h2>Agent tasks</h2><ul>{tasks}</ul></body></html>"
    )
