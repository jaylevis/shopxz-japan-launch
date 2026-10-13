"""Shared helpers: JSON IO, hashing, paths, time, redaction and semantic comparison."""

import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

SKILL_ROOT = Path(__file__).resolve().parents[2]
ASSETS_ROOT = SKILL_ROOT / "assets"
# Japan has no DST; a fixed offset avoids depending on tzdata being installed.
JST = timezone(timedelta(hours=9), "Asia/Tokyo")


class LaunchError(Exception):
    """Expected, user-facing failure; carries a stable code for JSON output."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self):
        out = {"ok": False, "error": self.code, "message": self.message}
        if self.details:
            out["details"] = redact(self.details)
        return out


# ---------------------------------------------------------------- time / hashing

def now_jst():
    return datetime.now(JST).replace(microsecond=0)


def jst_text(dt):
    """Fecify datetime format (YYYY-MM-DD HH:MM:SS), Asia/Tokyo wall clock."""
    return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")


def iso(dt):
    return dt.astimezone(JST).isoformat()


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_obj(obj):
    return sha256_text(canonical(obj))


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------- JSON IO

def read_json(path):
    # utf-8-sig accepts files written with a BOM (e.g. PowerShell UTF8 writers).
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def dumps(obj, indent=2):
    return json.dumps(obj, ensure_ascii=False, indent=indent)


def write_json_atomic(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(dumps(redact(obj)))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def write_json_immutable(path, obj, stamp=None):
    """Create a new file; never overwrite. Falls back to a timestamped sibling name."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    candidates = [path]
    stamp = stamp or now_jst().strftime("%Y%m%dT%H%M%S")
    candidates.append(path.with_name(f"{path.stem}-{stamp}{path.suffix}"))
    candidates += [path.with_name(f"{path.stem}-{stamp}-{i}{path.suffix}") for i in range(2, 100)]
    text = dumps(redact(obj))
    for cand in candidates:
        try:
            with open(cand, "x", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            return cand
        except FileExistsError:
            continue
    raise LaunchError("FILE_EXISTS", f"cannot create a new file near {path}")


# ---------------------------------------------------------------- paths

def _inside(child, parent):
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _check_outside_skill(path, what):
    raw = Path(os.path.abspath(path))
    real = Path(os.path.realpath(path))
    skill_raw = Path(os.path.abspath(SKILL_ROOT))
    skill_real = Path(os.path.realpath(SKILL_ROOT))
    for cand in (raw, real):
        for root in (skill_raw, skill_real):
            if _inside(cand, root):
                raise LaunchError("UNSAFE_PATH", f"{what} must be outside the skill package: {path}")
    return real


def safe_workdir(path, create=True):
    if not path:
        raise LaunchError("UNSAFE_PATH", "workdir is required")
    p = Path(path)
    if p.exists() and p.is_symlink():
        raise LaunchError("UNSAFE_PATH", f"workdir must not be a symlink: {path}")
    real = _check_outside_skill(p, "workdir")
    if real.exists() and not real.is_dir():
        raise LaunchError("UNSAFE_PATH", f"workdir is not a directory: {path}")
    if create:
        real.mkdir(parents=True, exist_ok=True)
    return real


def safe_output_file(path):
    if not path:
        raise LaunchError("UNSAFE_PATH", "output path is required")
    p = Path(path)
    if p.exists() and (p.is_symlink() or p.is_dir()):
        raise LaunchError("UNSAFE_PATH", f"output must be a regular file path: {path}")
    _check_outside_skill(p.parent if str(p.parent) else Path("."), "output")
    _check_outside_skill(p, "output")
    return Path(os.path.abspath(p))


def contained_file(root, name):
    """Join a workdir-relative artifact name and refuse escapes via '..' or symlinks."""
    root = Path(os.path.realpath(root))
    target = Path(os.path.realpath(root / name))
    if not _inside(target, root):
        raise LaunchError("UNSAFE_PATH", f"artifact escapes workdir: {name}")
    return target


# ---------------------------------------------------------------- URLs

def normalize_url(url):
    """Return scheme://host[:port] lowercased, without trailing path '/'; None if unusable."""
    if not isinstance(url, str) or not url.strip():
        return None
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.hostname:
        return None
    host = parts.hostname.lower()
    port = parts.port
    default = {"https": 443, "http": 80}.get(parts.scheme.lower())
    netloc = host if port in (None, default) else f"{host}:{port}"
    path = parts.path.rstrip("/")
    return f"{parts.scheme.lower()}://{netloc}{path}"


# ---------------------------------------------------------------- redaction

_SECRET_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|key[_-]?(id|secret)|credential|authorization|cookie)",
    re.I,
)


# Public, non-secret lists whose key name happens to contain a secret-looking word. Only a list of
# short payment codes is exempt; any other value under these keys is still masked.
_PUBLIC_CODE_LISTS = {"credentials_verified_codes"}
_PUBLIC_CODE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def _public_code_list(key, value):
    return key in _PUBLIC_CODE_LISTS and isinstance(value, list) and all(
        isinstance(v, str) and _PUBLIC_CODE.match(v) for v in value)


def redact(obj):
    """Deep copy with secret-looking keys masked. Applied to every persisted artifact."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _public_code_list(k, v):
                out[k] = list(v)
                continue
            # Booleans are readiness facts ("is it set?"), never secret values themselves.
            if isinstance(k, str) and _SECRET_KEY.search(k) and not isinstance(v, bool) and v not in (None, "", [], {}):
                out[k] = "[REDACTED]"
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


# ---------------------------------------------------------------- semantic comparison

_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")
_HEX = re.compile(r"^#[0-9a-fA-F]{3,8}$")
_EMPTY = ("empty",)


def _norm(value):
    if value is None or value == "" or value == [] or value == {}:
        return _EMPTY
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("num", Decimal(str(value)))
    if isinstance(value, str):
        if _NUMERIC.match(value):
            try:
                return ("num", Decimal(value))
            except InvalidOperation:
                pass
        if _HEX.match(value):
            return ("str", value.lower())
        return ("str", value)
    if isinstance(value, list):
        return ("list", tuple(_norm(v) for v in value))
    if isinstance(value, dict):
        items = [(str(k), _norm(v)) for k, v in value.items()]
        return ("dict", tuple(sorted((k, v) for k, v in items if v != _EMPTY)))
    return ("other", repr(value))


def sem_equal(a, b):
    """Equality tolerant of API serialization noise ('' vs [] vs {}, '10.00' vs 10, hex case)."""
    return _norm(a) == _norm(b)


def leaf_diff(before, desired, prefix=()):
    """Paths (tuples) where desired differs from before; dicts recurse, other values compare whole."""
    if isinstance(before, dict) and isinstance(desired, dict):
        paths = []
        for key in sorted(set(before) | set(desired), key=str):
            paths += leaf_diff(before.get(key), desired.get(key), prefix + (key,))
        return paths
    if isinstance(desired, dict) and desired and not isinstance(before, dict) and _norm(before) == _EMPTY:
        return leaf_diff({}, desired, prefix)
    return [] if sem_equal(before, desired) else [prefix]


def get_path(obj, path, default=None):
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def set_path(obj, path, value):
    cur = obj
    for key in path[:-1]:
        if not isinstance(cur.get(key), dict):
            cur[key] = {}
        cur = cur[key]
    cur[path[-1]] = copy.deepcopy(value)


def path_text(path):
    return ".".join(str(p) for p in path)


def as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None
