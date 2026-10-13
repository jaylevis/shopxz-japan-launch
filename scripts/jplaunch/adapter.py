"""Bridge to the installed fecify-site-manager-v6 skill (api-doc.js -> api-call.js).

Only the allowlisted documented APIs below can be called. The adapter never saves
credentials, never switches stores, never passes --no-validate and never confirms
NEED_CONFIRM challenges itself.
"""

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from .util import LaunchError, redact

MANAGER_NAME = "fecify-site-manager-v6"
FATAL_CODES = {10000014, 10000025}

READ_APIS = frozenset({
    "shop-info/init", "shop-info/site_base_url",
    "system-settings/basic_get", "system-settings/country_get",
    "payment/active-list",
    "notice/email-config-get", "notice/theme-list", "notice/theme-info", "notice/theme-get-languages",
    "japancheckout/get", "prodviewed/get", "fixedcart/get",
    "coupon/list", "coupon/info",
    "emailrecall/get", "emailrecall/message-template-list", "emailrecall/message-template-info",
    "emailrecall/activities-list", "emailrecall/activities-info",
    "emailrecall/activities-message-list", "emailrecall/activities-message-info",
    "emailrecall/message-email-vars",
    # v1.1 store settings menu (documented GETs)
    "system-settings/checkout_get", "system-settings/advance_get", "system-settings/task_get",
    "system-settings/captcha_get", "system-settings/cache_get", "payment/list", "shop-info/country_list",
    "shipping/shipping-list", "shipping/shipping-info", "shipping/zone-info", "shipping/zone-countries",
    "tax/base-list", "tax/base-info", "tax/custom-list", "tax/custom-info", "tax/custom-country-tax-list",
})
# Documented read-only POST endpoints (option pools). Only these POSTs are reads; they are never
# treated as mutations (no identity guard / uncertain-write semantics) and carry an empty JSON body.
READ_POST_APIS = frozenset({"shop-info/languages", "shop-info/currencies", "shop-info/timezone_list"})
WRITE_APIS = frozenset({
    "japancheckout/set", "prodviewed/set", "fixedcart/set",
    "notice/theme-update", "notice/theme-save-languages", "notice/theme-change-status",
    "coupon/create",
    "emailrecall/message-template-save", "emailrecall/activities-save",
    "emailrecall/activities-message-save", "emailrecall/set",
    # v1.1 store settings menu (non-danger documented writes only; shipping/tax saves are danger and
    # are prepared as gated manager workflows instead)
    "system-settings/basic_set", "system-settings/basic_set_currency", "system-settings/country_set",
    "system-settings/checkout_set", "system-settings/task_set", "system-settings/advance_set",
    "system-settings/captcha_set", "payment/update-status", "payment/set-top",
})
FORBIDDEN_MODULES = frozenset({"order", "customer", "customervip", "webhook", "task"})

# fixedcart/set schema repeats nested fields as required top-level aliases.
FIXEDCART_COLOR_ALIASES = ("background", "button_background", "price", "original_price",
                           "border_color", "attribute_color", "other_text")


class AdapterError(LaunchError):
    pass


class FatalApiError(AdapterError):
    """Permission/type errors, refused docs and confirmation challenges: stop everything."""


class ConfirmationRequired(FatalApiError):
    def __init__(self, payload):
        super().__init__("NEED_CONFIRM", "manager returned a confirmation challenge; not self-confirming",
                         {"challenge": payload})


class UncertainWrite(AdapterError):
    """A write whose outcome is unknown (timeout, transport error, unparseable output)."""


class ApiFailure(AdapterError):
    """A definite non-success response."""


def is_write(api_id):
    return api_id in WRITE_APIS


def check_allowed(api_id):
    module = api_id.split("/", 1)[0]
    if module in FORBIDDEN_MODULES or api_id not in READ_APIS | READ_POST_APIS | WRITE_APIS:
        raise FatalApiError("API_NOT_ALLOWED", f"{api_id} is outside the launch allowlist")


def classify_response(api_id, resp):
    """Return response data for code 200; raise the right error class otherwise."""
    if not isinstance(resp, dict):
        if is_write(api_id):
            raise UncertainWrite("UNPARSEABLE", f"{api_id}: no JSON result")
        raise ApiFailure("UNPARSEABLE", f"{api_id}: no JSON result")
    code = resp.get("code")
    if code == "NEED_CONFIRM":
        raise ConfirmationRequired(resp)
    try:
        num = int(code)
    except (TypeError, ValueError):
        num = None
    if num in FATAL_CODES:
        raise FatalApiError(f"API_{num}", f"{api_id}: {resp.get('message')}", {"code": num})
    if num == 200:
        return resp.get("data")
    message = str(resp.get("message", ""))
    if num == -1 and is_write(api_id) and "参数校验未通过" not in message:
        # api-call.js reports transport failures as -1; the server may have applied the write.
        raise UncertainWrite("WRITE_TRANSPORT", f"{api_id}: {message}", {"code": code})
    raise ApiFailure("API_ERROR", f"{api_id}: {message}", {"code": code})


class Adapter:
    """Interface: call(api_id, query=None, data=None) -> response data (code 200 only)."""

    def call(self, api_id, query=None, data=None):  # pragma: no cover - interface
        raise NotImplementedError


class ReadOnlyAdapter(Adapter):
    def __init__(self, inner):
        self.inner = inner

    def call(self, api_id, query=None, data=None):
        if is_write(api_id):
            raise FatalApiError("READ_ONLY", f"read-only mode refused write {api_id}")
        return self.inner.call(api_id, query=query, data=data)


def _parse_json_output(*streams):
    for stream in streams:
        text = (stream or "").strip()
        if not text:
            continue
        try:
            return json.loads(text)
        except ValueError:
            pass
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    continue
    return None


def _assert_manager_package(skill_dir):
    """Refuse a different manager package (e.g. v5) when its package.json declares a name."""
    pkg = Path(skill_dir) / "package.json"
    if not pkg.is_file():
        return
    try:
        name = json.loads(pkg.read_text(encoding="utf-8")).get("name")
    except (OSError, ValueError):
        return
    if name and name != MANAGER_NAME:
        raise LaunchError("MANAGER_VERSION_MISMATCH", f"{skill_dir} is {name}; this package requires {MANAGER_NAME}")


def discover_manager_skill(explicit=None, env=None, skill_root=None):
    env = os.environ if env is None else env
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if env.get("FECIFY_SKILL_DIR"):
        candidates.append(Path(env["FECIFY_SKILL_DIR"]))
    if skill_root:
        candidates.append(Path(skill_root).parent / MANAGER_NAME)
    if env.get("CODEX_HOME"):
        candidates.append(Path(env["CODEX_HOME"]) / "skills" / MANAGER_NAME)
    candidates.append(Path.home() / ".codex" / "skills" / MANAGER_NAME)
    candidates.append(Path.home() / ".claude" / "skills" / MANAGER_NAME)
    for cand in candidates:
        if (cand / "scripts" / "proxy" / "api-doc.js").is_file() and (cand / "scripts" / "proxy" / "api-call.js").is_file():
            _assert_manager_package(cand)
            return cand.resolve()
        if explicit and cand == Path(explicit):
            raise LaunchError("MANAGER_NOT_FOUND", f"--manager-skill has no scripts/proxy/api-doc.js: {explicit}")
    raise LaunchError("MANAGER_NOT_FOUND", f"{MANAGER_NAME} not found (use --manager-skill or FECIFY_SKILL_DIR)")


def resolve_node(explicit=None, env=None):
    env = os.environ if env is None else env
    for cand in (explicit, env.get("NODE_BINARY")):
        if cand:
            path = shutil.which(cand) or (cand if Path(cand).is_file() else None)
            if not path:
                raise LaunchError("NODE_NOT_FOUND", f"node executable not found: {cand}")
            return path
    path = shutil.which("node")
    if not path:
        raise LaunchError("NODE_NOT_FOUND", "node not found (use --node or NODE_BINARY)")
    return path


class ManagerAdapter(Adapter):
    def __init__(self, skill_dir, node, io_dir, timeout=90):
        self.skill_dir = Path(skill_dir)
        self.node = node
        self.io_dir = Path(io_dir)
        self.timeout = timeout
        self._docs = {}

    def _run(self, args, timeout):
        return subprocess.run(
            [self.node] + args, cwd=str(self.skill_dir), env=dict(os.environ),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            shell=False, timeout=timeout, encoding="utf-8", errors="replace",
        )

    def doc(self, api_id):
        if api_id in self._docs:
            return self._docs[api_id]
        try:
            proc = self._run([str(self.skill_dir / "scripts/proxy/api-doc.js"), f"--api={api_id}"], 30)
        except subprocess.TimeoutExpired:
            raise FatalApiError("DOC_TIMEOUT", f"api-doc.js timed out for {api_id}")
        doc = _parse_json_output(proc.stdout, proc.stderr)
        if not isinstance(doc, dict) or doc.get("error") or not doc.get("path") or not doc.get("method"):
            raise FatalApiError("DOC_MISSING", f"no documentation for {api_id}", {"doc": doc})
        if f"{doc.get('module')}/{doc.get('api')}" != api_id:
            raise FatalApiError("DOC_MISMATCH", f"api-doc returned a different API for {api_id}")
        if doc.get("danger") or doc.get("need_permit"):
            raise FatalApiError("DOC_DANGER", f"{api_id} is marked danger/need_permit; refused")
        expected = "POST" if is_write(api_id) or api_id in READ_POST_APIS else "GET"
        if str(doc["method"]).upper() != expected or not str(doc["path"]).startswith("/api/skill/"):
            raise FatalApiError("DOC_UNEXPECTED", f"{api_id} documented as {doc['method']} {doc['path']}")
        self._docs[api_id] = doc
        return doc

    def _with_documented_aliases(self, api_id, data, doc):
        if api_id != "fixedcart/set" or not isinstance(data, dict):
            return data
        params = doc.get("params") or {}
        body = dict(data)
        aliases = {"name": (data.get("button_text") or {}).get("name"),
                   "language": (data.get("button_text") or {}).get("language")}
        for key in FIXEDCART_COLOR_ALIASES:
            aliases[key] = (data.get("color_info") or {}).get(key)
        for key, value in aliases.items():
            if key not in params or value is None:
                continue
            if key in body and body[key] != value:
                raise FatalApiError("ALIAS_CONFLICT", f"fixedcart alias {key} differs from nested value")
            body[key] = value
        return body

    def _write_io(self, payload):
        self.io_dir.mkdir(parents=True, exist_ok=True)
        path = self.io_dir / f"io-{uuid.uuid4().hex}.json"
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        return path

    def call(self, api_id, query=None, data=None):
        check_allowed(api_id)
        doc = self.doc(api_id)
        args = [str(self.skill_dir / "scripts/proxy/api-call.js"), doc["method"].upper(), doc["path"]]
        files = []
        try:
            if query:
                files.append(self._write_io(query))
                args += ["--query", str(files[-1])]
            if is_write(api_id):
                files.append(self._write_io(self._with_documented_aliases(api_id, data or {}, doc)))
                args += ["--data", str(files[-1])]
            elif api_id in READ_POST_APIS:
                files.append(self._write_io({}))  # explicit empty body; never caller-supplied
                args += ["--data", str(files[-1])]
            try:
                proc = self._run(args, self.timeout)
            except subprocess.TimeoutExpired:
                if is_write(api_id):
                    raise UncertainWrite("WRITE_TIMEOUT", f"{api_id} timed out after {self.timeout}s")
                raise ApiFailure("READ_TIMEOUT", f"{api_id} timed out after {self.timeout}s")
            except OSError as exc:
                raise FatalApiError("NODE_FAILED", f"cannot run node: {exc}")
            resp = _parse_json_output(proc.stdout, proc.stderr)
            return classify_response(api_id, redact(resp) if resp is not None else None)
        finally:
            for f in files:
                try:
                    f.unlink()
                except OSError:
                    pass
